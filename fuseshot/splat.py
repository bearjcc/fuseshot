"""Gaussian splats from object scans and walk-arounds.

Each set goes through three steps, each safe to rerun and each picking up where
the last run stopped:

  prepare  downscaled, upright copies of the frames (junk frames left out)
  sfm      COLMAP feature extraction, exhaustive matching and incremental
           mapping, then a confidence gate on the largest model
  train    Brush training from the undistorted model, exporting a .ply every
           so often so a stopped run resumes from the last export, then a
           compact .splat for the review page

Everything for a set lives in work/splats/<set>/. Nothing here writes to Immich;
prepare only downloads originals when the set has not been fetched before.
"""

import json
import math
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import formats, limits, log, rig, tools
from .moving import moving_sets
from .files import write_atomic
from .group import is_group

LOG = log.get(__name__)

LONG_SIDE = 1600
MIN_FRAMES = 8
GATE_RATIO = 0.6
GATE_MIN_IMAGES = 8
GATE_MAX_ERROR_PX = 1.5
DEFAULT_STEPS = 7000
EXPORT_EVERY = 1000
HOLDOUT_EVERY = 10
GOOD_PSNR = 20.0
ROUGH_PSNR = 16.0
MAX_SPLATS = 2_000_000
WEB_SPLATS = 1_500_000
LOCK_STALE_S = 4 * 3600
SH_C0 = 0.28209479177387814
EXPORT_NAME = re.compile(r"^export_(\d+)\.ply$")


class SplatError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def splat_dir(work_dir, set_id):
    return Path(work_dir) / "splats" / set_id


def read_json(path, default=None):
    path = Path(path)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8-sig"))
    return default


def write_json(path, data):
    write_atomic(path, json.dumps(data, indent=2))



def thumb_sharpness(cache_dir, asset_ids):
    """Median variance of the Laplacian over the cached Immich thumbnails, or None."""
    import cv2
    import numpy as np

    values = []
    for asset_id in asset_ids:
        path = Path(cache_dir) / "thumbs" / asset_id[:2] / f"{asset_id}.jpg"
        if not path.exists():
            continue
        grey = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if grey is None:
            continue
        values.append(float(cv2.Laplacian(grey, cv2.CV_64F).var()))
    return round(float(np.median(values)), 1) if values else None


def set_kind(candidate):
    verified = (candidate.get("verified_type") or "").split(" (+")[0]
    if verified.startswith("object scan"):
        return "object scan"
    if verified == "mixed motion":
        return "mixed motion"
    if verified.startswith("linear walk"):
        return "walk"
    return None


def make_plan(candidates_file, cache_dir, min_parallax=4, types_file=None, work_dir=None):
    """Object scans first, then sets from stereo videos, then walks and mixed-motion sets with
    enough parallax, best first.
    Group photo sets are left out; they become best-shot merges."""
    from .fetch import asset_ids

    candidates = json.loads(Path(candidates_file).read_text(encoding="utf-8"))["candidates"]
    verified = read_json(Path(candidates_file).with_name("verified.json"), {})
    groups_file = Path(candidates_file).with_name("groups.json")
    moving = moving_sets(Path(candidates_file).with_name("moving.json"), work_dir)
    rows = []
    for c in candidates:
        kind = set_kind(c)
        if not kind or is_group(c["set_id"], groups_file, types_file):
            continue
        v = verified.get(c["set_id"], {})
        kinds = v.get("pair_kinds") or {}
        junk = set(c.get("junk_frames") or v.get("junk_frames") or [])
        ids = asset_ids(c)
        usable = len(ids) - len(junk)
        parallax = kinds.get("parallax", 0)
        turning = sum(kinds.get(k, 0) for k in ("parallax", "planar", "rotation", "zoom")) or 1
        joined, _, steps = (v.get("sequential_overlap") or "0/1").partition("/")
        seq = int(joined) / max(1, int(steps or 1))
        reasons = []
        if usable < MIN_FRAMES:
            reasons.append(f"only {usable} usable frames")
        if kind != "object scan" and parallax < min_parallax:
            reasons.append(f"{parallax} parallax pairs")
        if c["set_id"] in moving:
            reasons.append("the subject moves (made into a loop instead)")
        sharp = thumb_sharpness(cache_dir, [a for i, a in enumerate(ids, 1) if i not in junk])
        sharp_factor = 1.0 if sharp is None else min(1.0, sharp / 150.0) ** 0.5
        score = min(usable, 60) * seq * (0.3 + parallax / turning) * sharp_factor
        rows.append({
            "set_id": c["set_id"], "kind": kind, "frames": len(ids), "usable": usable,
            "junk_frames": sorted(junk), "parallax_pairs": parallax, "sequential_overlap": round(seq, 2),
            "sharpness": sharp, "camera": c.get("camera", ""), "taken": (c.get("start_local") or "")[:16],
            "places": c.get("places") or "", "score": round(score, 2),
            "skip": "; ".join(reasons),
        })
    if work_dir:
        rows += video_rows(work_dir)
    order = {"object scan": 0, "stereo video": 1, "mixed motion": 2, "walk": 3}
    rows.sort(key=lambda r: (bool(r["skip"]), order[r["kind"]], -r["score"]))
    return {"made": now(), "gate": gate_settings(), "sets": rows}


def gate_settings():
    return {"min_registered_ratio": GATE_RATIO, "min_registered_images": GATE_MIN_IMAGES,
            "max_mean_reprojection_error_px": GATE_MAX_ERROR_PX}



def manifest_for(immich_factory, candidates_file, work_dir, set_id):
    """The fetch manifest for a set, downloading the originals only if they are missing."""
    from .fetch import fetch_set, load_candidate

    manifest_file = Path(work_dir) / set_id / "manifest.json"
    manifest = read_json(manifest_file)
    if manifest and all(resolve(work_dir, m["image"]).exists() for m in manifest):
        return manifest
    _, manifest = fetch_set(immich_factory(), load_candidate(candidates_file, set_id), work_dir)
    return manifest


def resolve(work_dir, path):
    path = Path(path)
    return path if path.is_absolute() else Path(work_dir).resolve().parent / path


def prepare(work_dir, set_id, candidates_file, immich_factory):
    from PIL import Image, ImageOps

    formats.register()
    from .fetch import load_candidate

    out = splat_dir(work_dir, set_id)
    images = out / "images"
    images.mkdir(parents=True, exist_ok=True)
    candidate = load_candidate(candidates_file, set_id)
    junk = set(candidate.get("junk_frames") or [])
    manifest = manifest_for(immich_factory, candidates_file, work_dir, set_id)
    manifest = [m for m in manifest if m["index"] not in junk]
    pairs = {m["index"]: rig.views(resolve(work_dir, m["image"]), m.get("make")) for m in manifest}
    paired = bool(manifest) and all(v is not None for v in pairs.values())
    frames = []
    for m in manifest:
        f35 = m.get("f35") or m.get("f35_hint")
        name = f"{m['index']:03d}.jpg"
        with Image.open(resolve(work_dir, m["image"])) as im:
            exif = im.getexif()
            exif[0x0112] = 1
            if f35:
                exif.get_ifd(0x8769)[0xA405] = int(round(float(f35)))
            if paired:
                (file, size), (pair, _) = rig.write_pair(*pairs[m["index"]], images, name, LONG_SIDE, exif.tobytes())
                frames.append({"index": m["index"], "file": file, "pair": pair, "asset_id": m["asset_id"],
                               "size": size, "f35": f35})
                continue
            target = images / name
            if not target.exists():
                if pairs[m["index"]] is not None:
                    im = Image.fromarray(pairs[m["index"]][0][:, :, ::-1])
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((LONG_SIDE, LONG_SIDE), Image.LANCZOS)
                tmp = target.with_suffix(".tmp")
                im.save(tmp, "JPEG", quality=93, exif=exif.tobytes())
                os.replace(tmp, target)
        with Image.open(target) as im:
            size = im.size
        frames.append({"index": m["index"], "file": target.name, "asset_id": m["asset_id"], "size": size,
                       "f35": f35})
    info = {"set_id": set_id, "frames": frames, "junk_frames": sorted(junk), "long_side": LONG_SIDE,
            "prepared": now()}
    if paired:
        info["rig"] = {"baseline_m": rig.BASELINE_M}
    write_json(out / "frames.json", info)
    LOG.info(f"{set_id}: {len(frames)} frames ready in {images}")
    return info



def from_stereo_video(work_dir, video, set_id=None):
    """A set made from a side-by-side stereo video, ready for SfM; its frames.json."""
    video = Path(video)
    set_id = set_id or "SV-" + re.sub(r"[^A-Za-z0-9_-]+", "-", video.stem).strip("-")
    out = splat_dir(work_dir, set_id)
    if (out / "images").exists():
        shutil.rmtree(out / "images")
    try:
        frames = rig.video_pairs(video, out / "images", LONG_SIDE)
    except ValueError as err:
        raise SplatError(str(err)) from err
    if len(frames) < MIN_FRAMES:
        raise SplatError(f"{set_id}: only {len(frames)} frames in {video.name}")
    info = {"set_id": set_id, "frames": frames, "junk_frames": [], "long_side": LONG_SIDE,
            "rig": {"baseline_m": rig.BASELINE_M}, "video": video.name, "prepared": now()}
    write_json(out / "frames.json", info)
    LOG.info(f"{set_id}: {len(frames)} stereo pairs from {video.name}")
    return info


def video_rows(work_dir):
    """Plan rows for the sets made from stereo videos."""
    rows = []
    for path in sorted((Path(work_dir) / "splats").glob("*/frames.json")):
        info = read_json(path, {})
        if info.get("video"):
            count = len(info.get("frames") or [])
            rows.append({"set_id": info["set_id"], "kind": "stereo video", "frames": count, "usable": count,
                         "junk_frames": [], "parallax_pairs": count, "sequential_overlap": 1.0,
                         "sharpness": None, "camera": "", "taken": "", "places": info["video"],
                         "score": float(count), "skip": ""})
    return rows


class SetLock:
    """A lock file so a queued SfM run and a queued training run never work on one set at once."""

    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > LOCK_STALE_S:
                    self.path.unlink(missing_ok=True)
                    continue
                time.sleep(20)

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def colmap(args, log):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    started = time.time()
    with open(log, "a", encoding="utf-8", errors="replace") as f:
        f.write(f"\n$ colmap {' '.join(str(a) for a in args)}\n")
        f.flush()
        proc = subprocess.run([str(tools.require("colmap"))] + [str(a) for a in args], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, errors="replace", env=env)
        f.write(proc.stdout)
    if proc.returncode:
        tail = "\n".join(proc.stdout.strip().splitlines()[-8:])
        raise SplatError(f"colmap {args[0]} failed ({proc.returncode}):\n{tail}")
    LOG.info(f"  colmap {args[0]}: {time.time() - started:.0f} s")
    return proc.stdout


def analyse(model_dir, log):
    text = colmap(["model_analyzer", "--path", model_dir], log)
    found = {}
    for key, pattern in (("cameras", r"Cameras:\s*(\d+)"), ("registered", r"Registered images:\s*(\d+)"),
                         ("points", r"Points:\s*(\d+)"), ("observations", r"Observations:\s*(\d+)"),
                         ("mean_track_length", r"Mean track length:\s*([\d.]+)"),
                         ("mean_reprojection_error_px", r"Mean reprojection error:\s*([\d.]+)")):
        match = re.search(pattern, text)
        if match:
            found[key] = float(match.group(1)) if "." in match.group(1) else int(match.group(1))
    return found


def read_text_model(model_dir):
    import numpy as np

    cameras = {}
    for line in (Path(model_dir) / "cameras.txt").read_text().splitlines():
        if line and not line.startswith("#"):
            p = line.split()
            cameras[int(p[0])] = {"model": p[1], "width": int(p[2]), "height": int(p[3]),
                                  "params": [float(x) for x in p[4:]]}
    images = []
    lines = [l for l in (Path(model_dir) / "images.txt").read_text().splitlines() if not l.startswith("#")]
    for line in lines[0::2]:
        p = line.split()
        if len(p) < 10:
            continue
        qw, qx, qy, qz, tx, ty, tz = map(float, p[1:8])
        images.append({"q": (qw, qx, qy, qz), "t": (tx, ty, tz), "camera": int(p[8]), "name": p[9]})
    points = []
    for line in (Path(model_dir) / "points3D.txt").read_text().splitlines():
        if line and not line.startswith("#"):
            p = line.split()
            points.append((float(p[1]), float(p[2]), float(p[3])))
    return cameras, images, np.array(points, dtype=np.float64).reshape(-1, 3)


def rotation(q):
    import numpy as np

    w, x, y, z = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def starting_view(model_dir):
    """Camera position, target, up and field of view for the viewer, from the middle registered frame."""
    import numpy as np

    cameras, images, points = read_text_model(model_dir)
    if not images:
        return None
    images.sort(key=lambda i: i["name"])
    centres, ups = [], []
    for image in images:
        r = rotation(image["q"])
        centres.append(-r.T @ np.array(image["t"]))
        ups.append(-r.T @ np.array([0.0, 1.0, 0.0]))
    middle = images[len(images) // 2]
    r = rotation(middle["q"])
    centre = centres[len(images) // 2]
    forward = r.T @ np.array([0.0, 0.0, 1.0])
    depth = 1.0
    if len(points):
        ahead = (points - centre) @ forward
        ahead = ahead[ahead > 0]
        if len(ahead):
            depth = float(np.median(ahead))
    up = np.mean(ups, axis=0)
    up /= np.linalg.norm(up) or 1.0
    cam = cameras[middle["camera"]]
    fy = cam["params"][1] if cam["model"] == "PINHOLE" else cam["params"][0]
    fov = math.degrees(2 * math.atan(cam["height"] / 2 / fy))
    spread = float(np.linalg.norm(np.ptp(np.array(centres), axis=0)))
    return {"position": [round(float(v), 5) for v in centre],
            "target": [round(float(v), 5) for v in centre + forward * depth],
            "up": [round(float(v), 5) for v in up], "fov": round(fov, 1),
            "camera_spread": round(spread, 4), "depth": round(depth, 4)}


def sfm(work_dir, set_id, gpu=None, threads=4, force=False):
    out = splat_dir(work_dir, set_id)
    result_file = out / "sfm.json"
    frames = read_json(out / "frames.json")
    if not frames:
        raise SplatError(f"{set_id}: run prepare first")
    with SetLock(out / "set.lock"):
        done = read_json(result_file)
        if done and not force:
            LOG.info(f"{set_id}: SfM already done ({done.get('status')})")
            return done
        started = time.time()
        if gpu is None:
            # The GPU when there is an NVIDIA card and this COLMAP can use it; else the CPU.
            gpu = limits.gpu() is not None and limits.colmap_cuda(tools.require("colmap"))
        work = out / "colmap"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        log = work / "colmap.log"
        db = work / "database.db"
        sizes = {tuple(f["size"]) for f in frames["frames"]}
        f35s = {f["f35"] for f in frames["frames"]}
        extract = ["feature_extractor", "--database_path", db, "--image_path", out / "images",
                   "--ImageReader.camera_model", "SIMPLE_RADIAL",
                   "--FeatureExtraction.use_gpu", int(gpu), "--FeatureExtraction.num_threads", threads,
                   "--FeatureExtraction.max_image_size", LONG_SIDE,
                   "--SiftExtraction.max_num_features", 8192 if gpu else 4096]
        paired = bool(frames.get("rig"))
        if paired:
            extract += ["--ImageReader.single_camera_per_folder", 1]
        elif len(sizes) == 1:
            extract += ["--ImageReader.single_camera", 1]
        if len(sizes) == 1:
            (w, h), = sizes
            f35, = f35s if len(f35s) == 1 else (None,)
            if f35:
                focal = float(f35) / 36.0 * max(w, h)
                extract += ["--ImageReader.camera_params", f"{focal:.2f},{w / 2:.1f},{h / 2:.1f},0"]
        configure = ["rig_configurator", "--database_path", db, "--rig_config_path",
                     rig.config_file(work / "rig_config.json", frames["rig"]["baseline_m"])] if paired else None
        try:
            colmap(extract, log)
            if configure:
                colmap(configure, log)
            colmap(["exhaustive_matcher", "--database_path", db, "--FeatureMatching.use_gpu", int(gpu),
                    "--FeatureMatching.num_threads", threads, "--FeatureMatching.guided_matching", 1], log)
        except SplatError as err:
            if not gpu:
                raise
            LOG.info(f"{set_id}: the GPU did not work for COLMAP ({str(err).splitlines()[0]}); using the CPU")
            gpu = False
            db.unlink(missing_ok=True)
            extract = [str(a) for a in extract]
            extract[extract.index("--FeatureExtraction.use_gpu") + 1] = "0"
            extract[extract.index("--SiftExtraction.max_num_features") + 1] = "4096"
            colmap(extract, log)
            if configure:
                colmap(configure, log)
            colmap(["exhaustive_matcher", "--database_path", db, "--FeatureMatching.use_gpu", 0,
                    "--FeatureMatching.num_threads", threads, "--FeatureMatching.guided_matching", 1], log)
        sparse = work / "sparse"
        sparse.mkdir()
        mapper_error = None
        try:
            colmap(["mapper", "--database_path", db, "--image_path", out / "images", "--output_path", sparse,
                    "--Mapper.num_threads", threads, "--Mapper.min_model_size", 5]
                   + (["--Mapper.ba_refine_sensor_from_rig", 0] if paired else []), log)
        except SplatError as err:
            mapper_error = str(err).splitlines()[-1].strip()
        models = []
        for model in sorted(p for p in sparse.iterdir() if p.is_dir()):
            stats = analyse(model, log)
            stats["model"] = model.name
            models.append(stats)
        total = len(frames["frames"]) * (2 if paired else 1)
        result = {"set_id": set_id, "images": total, "device": "gpu" if gpu else "cpu", "threads": threads,
                  "models": models, "gate": gate_settings(), "at": now()}
        best = max(models, key=lambda m: (m.get("registered", 0), -m.get("mean_reprojection_error_px", 99)),
                   default=None)
        if not best or not best.get("registered"):
            result.update(status="failed", reason="COLMAP could not build a model from these frames"
                          + (f" ({mapper_error})" if mapper_error else ""))
        else:
            registered = best["registered"]
            error = best.get("mean_reprojection_error_px", 99.0)
            ratio = registered / total
            result.update(registered=registered, registered_ratio=round(ratio, 3), model=best["model"],
                          mean_reprojection_error_px=error, points=best.get("points"))
            reasons = []
            if ratio < GATE_RATIO:
                reasons.append(f"only {registered} of {total} frames registered")
            elif registered < GATE_MIN_IMAGES:
                reasons.append(f"only {registered} frames registered")
            if error > GATE_MAX_ERROR_PX:
                reasons.append(f"mean reprojection error {error:.2f} px")
            if reasons:
                result.update(status="failed", reason="; ".join(reasons))
            else:
                dataset = out / "dataset"
                if dataset.exists():
                    shutil.rmtree(dataset)
                colmap(["image_undistorter", "--image_path", out / "images", "--input_path", sparse / best["model"],
                        "--output_path", dataset, "--output_type", "COLMAP", "--max_image_size", LONG_SIDE], log)
                model0 = dataset / "sparse" / "0"
                model0.mkdir(parents=True)
                for p in list((dataset / "sparse").iterdir()):
                    if p.is_file():
                        os.replace(p, model0 / p.name)
                for extra in ("stereo", "run-colmap-geometric.sh", "run-colmap-photometric.sh"):
                    target = dataset / extra
                    if target.is_dir():
                        shutil.rmtree(target)
                    elif target.exists():
                        target.unlink()
                text = work / "text"
                text.mkdir()
                colmap(["model_converter", "--input_path", model0, "--output_path", text, "--output_type", "TXT"], log)
                result.update(status="passed", reason=f"{registered} of {total} frames registered, "
                                                      f"mean error {error:.2f} px",
                              view=starting_view(text))
        result["seconds"] = round(time.time() - started)
        write_json(result_file, result)
        LOG.info(f"{set_id}: SfM {result['status']}: {result['reason']} ({result['seconds']} s)")
        return result



def exports(train_dir):
    found = []
    for p in Path(train_dir).glob("export_*.ply"):
        match = EXPORT_NAME.match(p.name)
        if match:
            found.append((int(match.group(1)), p))
    return sorted(found)


def ply_complete(path):
    """True once Brush has finished writing a ply, judged from its header and size."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        end = head.find(b"end_header\n")
        if end < 0:
            return False
        count, size = 0, 0
        in_vertex = False
        for line in head[:end].decode("ascii", "replace").splitlines():
            parts = line.split()
            if parts[:1] == ["element"]:
                in_vertex = parts[1] == "vertex"
                if in_vertex:
                    count = int(parts[2])
            elif parts[:1] == ["property"] and in_vertex:
                size += int(PLY_TYPES[parts[1]][1])
        return Path(path).stat().st_size >= end + len(b"end_header\n") + count * size
    except (OSError, KeyError, ValueError, IndexError):
        return False


def run_brush(command, cwd, stop_file=None):
    """Run Brush; when stop_file is given, end it once that export is fully written."""
    proc = subprocess.Popen(command, cwd=cwd)
    if stop_file is None:
        return proc.wait(), False
    while proc.poll() is None:
        if Path(stop_file).exists() and ply_complete(stop_file):
            time.sleep(2)
            proc.terminate()
            proc.wait()
            return 0, True
        time.sleep(2)
    return proc.returncode, False


def holdout_quality(eval_dir, images_dir):
    """PSNR and mean absolute error of Brush's renders of the held-out frames against the photos."""
    import numpy as np
    from PIL import Image

    views = []
    for render_path in sorted(Path(eval_dir).rglob("*.png")):
        relative = render_path.relative_to(eval_dir).with_suffix("")
        photos = sorted(Path(images_dir).glob(f"{relative.as_posix()}.*"))
        if not photos:
            continue
        render = Image.open(render_path).convert("RGB")
        photo = Image.open(photos[0]).convert("RGB").resize(render.size, Image.LANCZOS)
        diff = np.asarray(render, dtype=np.float64) - np.asarray(photo, dtype=np.float64)
        mse = float(np.mean(diff ** 2))
        psnr = 99.0 if mse == 0 else 10 * math.log10(255.0 ** 2 / mse)
        views.append({"frame": photos[0].name, "psnr": round(psnr, 2),
                      "l1": round(float(np.mean(np.abs(diff))) / 255, 4)})
    if not views:
        return {}
    psnr = round(sum(v["psnr"] for v in views) / len(views), 2)
    quality = "good" if psnr >= GOOD_PSNR else "rough" if psnr >= ROUGH_PSNR else "poor"
    return {"holdout_psnr": psnr, "holdout_l1": round(sum(v["l1"] for v in views) / len(views), 4),
            "holdout_views": views, "quality": quality}


def train(work_dir, set_id, steps=DEFAULT_STEPS, chunk=None):
    """Train to `steps`, resuming from the last export. With `chunk`, stop after that many more steps
    so long jobs can be queued as several shorter ones."""
    out = splat_dir(work_dir, set_id)
    result = read_json(out / "sfm.json")
    if not result:
        raise SplatError(f"{set_id}: no SfM result yet")
    if result.get("status") != "passed":
        LOG.info(f"{set_id}: skipped, SfM gate failed ({result.get('reason')})")
        return None
    record_file = out / "splat.json"
    record = read_json(record_file, {})
    if record.get("steps") == steps and (out / "scene.splat").exists():
        LOG.info(f"{set_id}: already trained to {steps} steps")
        return record
    with SetLock(out / "set.lock"):
        dataset = out / "dataset"
        train_dir = out / "train"
        train_dir.mkdir(exist_ok=True)
        init = dataset / "init.ply"
        init.unlink(missing_ok=True)
        done = [(i, p) for i, p in exports(train_dir) if i <= steps]
        start = 0
        if done and done[-1][0] < steps:
            start = done[-1][0]
            shutil.copyfile(done[-1][1], init)
            LOG.info(f"{set_id}: resuming from step {start}")
        elapsed = record.get("train_seconds_so_far", 0) if start else 0
        stop = steps
        if chunk:
            stop = min(steps, (start + chunk) // EXPORT_EVERY * EXPORT_EVERY or EXPORT_EVERY)
        if not done or done[-1][0] < steps:
            command = [str(tools.require("brush")), str(dataset), "--total-steps", str(steps), "--start-iter", str(start),
                       "--export-every", str(EXPORT_EVERY), "--export-path", str(train_dir),
                       "--export-name", "export_{iter}.ply", "--max-resolution", str(LONG_SIDE),
                       "--max-splats", str(MAX_SPLATS), "--eval-split-every", str(HOLDOUT_EVERY),
                       "--eval-every", str(steps), "--eval-save-to-disk"]
            LOG.info("$ " + " ".join(command))
            started = time.time()
            stop_file = train_dir / f"export_{stop}.ply" if stop < steps else None
            try:
                code, stopped = run_brush(command, out, stop_file)
            finally:
                elapsed += time.time() - started
                record.update(train_seconds_so_far=round(elapsed), steps_done=max(
                    [i for i, _ in exports(train_dir) if i <= steps], default=0), steps_target=steps)
                write_json(record_file, record)
                init.unlink(missing_ok=True)
            if code:
                raise SplatError(f"{set_id}: brush exited with {code}")
            if stopped:
                LOG.info(f"{set_id}: chunk done at step {stop} of {steps} ({elapsed / 60:.1f} min so far)")
                return record
        finished = [(i, p) for i, p in exports(train_dir) if i <= steps]
        if not finished:
            raise SplatError(f"{set_id}: brush wrote no export")
        last_step, last = finished[-1]
        if last_step < steps:
            raise SplatError(f"{set_id}: training stopped at step {last_step}")
        stats = ply_to_splat(last, out / "scene.splat")
        for step, path in finished[:-1]:
            path.unlink()
        quality = {}
        for eval_dir in (train_dir / f"eval_{steps}", out / f"eval_{steps}"):
            if eval_dir.is_dir():
                quality = holdout_quality(eval_dir, dataset / "images")
                break
        record = {"set_id": set_id, "steps": steps, "train_seconds": round(elapsed),
                  "train_seconds_so_far": round(elapsed), "steps_done": steps, "steps_target": steps,
                  "ply": last.name, "ply_mb": round(last.stat().st_size / 1e6, 1), **stats, **quality,
                  "at": now()}
        write_json(record_file, record)
        held = f", held-out PSNR {quality['holdout_psnr']} dB ({quality['quality']})" if quality else ""
        LOG.info(f"{set_id}: trained {steps} steps in {elapsed / 60:.1f} min, {stats['gaussians']} gaussians, "
              f"scene.splat {stats['splat_mb']} MB{held}")
        return record



PLY_TYPES = {"float": "f4", "float32": "f4", "double": "f8", "float64": "f8", "uchar": "u1", "uint8": "u1",
             "char": "i1", "int8": "i1", "short": "i2", "int16": "i2", "ushort": "u2", "uint16": "u2",
             "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4", "half": "f2", "float16": "f2"}


def read_ply(path):
    import numpy as np

    with open(path, "rb") as f:
        if f.readline().strip() != b"ply":
            raise SplatError(f"{path} is not a ply file")
        fmt, count, props, in_vertex = None, 0, [], False
        while True:
            line = f.readline().decode("ascii", "replace").strip()
            if line == "end_header":
                break
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "format":
                fmt = parts[1]
            elif parts[0] == "element":
                in_vertex = parts[1] == "vertex"
                if in_vertex:
                    count = int(parts[2])
            elif parts[0] == "property" and in_vertex:
                if parts[1] == "list":
                    raise SplatError("list properties in the vertex element are not supported")
                props.append((parts[2], PLY_TYPES[parts[1]]))
        if fmt != "binary_little_endian":
            raise SplatError(f"unsupported ply format {fmt}")
        dtype = np.dtype([(name, "<" + kind) for name, kind in props])
        return np.frombuffer(f.read(dtype.itemsize * count), dtype=dtype, count=count)


def ply_to_splat(ply_path, target, limit=WEB_SPLATS):
    """Write the 32-byte-per-splat format most web viewers read, biggest and most opaque first."""
    import numpy as np

    v = read_ply(ply_path)
    total = len(v)
    pos = np.stack([v["x"], v["y"], v["z"]], axis=1).astype(np.float32)
    scale = np.exp(np.stack([v["scale_0"], v["scale_1"], v["scale_2"]], axis=1).astype(np.float32))
    alpha = 1.0 / (1.0 + np.exp(-v["opacity"].astype(np.float32)))
    colour = 0.5 + SH_C0 * np.stack([v["f_dc_0"], v["f_dc_1"], v["f_dc_2"]], axis=1).astype(np.float32)
    rot = np.stack([v["rot_0"], v["rot_1"], v["rot_2"], v["rot_3"]], axis=1).astype(np.float32)
    rot /= np.linalg.norm(rot, axis=1, keepdims=True) + 1e-12
    keep = np.isfinite(pos).all(axis=1) & np.isfinite(scale).all(axis=1) & (alpha > 0.02)
    order = np.argsort(-(scale.prod(axis=1) * alpha) * keep)[:min(limit, int(keep.sum()))]
    out = np.zeros(len(order), dtype=[("pos", "<f4", 3), ("scale", "<f4", 3), ("rgba", "u1", 4), ("rot", "u1", 4)])
    out["pos"] = pos[order]
    out["scale"] = scale[order]
    out["rgba"][:, :3] = np.clip(colour[order] * 255, 0, 255).astype(np.uint8)
    out["rgba"][:, 3] = np.clip(alpha[order] * 255, 0, 255).astype(np.uint8)
    out["rot"] = np.clip(rot[order] * 128 + 128, 0, 255).astype(np.uint8)
    write_atomic(target, out.tobytes())
    return {"gaussians": total, "web_gaussians": int(len(order)),
            "splat_mb": round(Path(target).stat().st_size / 1e6, 1)}



def status(work_dir, plan_file):
    plan = read_json(plan_file, {"sets": []})
    for row in plan["sets"]:
        out = splat_dir(work_dir, row["set_id"])
        s = read_json(out / "sfm.json", {})
        t = read_json(out / "splat.json", {})
        state = s.get("status", "skip: " + row["skip"] if row["skip"] else "pending")
        trained = f"{t['steps']} steps {t['train_seconds'] / 60:.0f} min {t['splat_mb']} MB" if t.get("steps") else ""
        print(f"{row['set_id']}  {row['kind']:<12} {row['usable']:>3} frames  score {row['score']:>5}  "
              f"{state:<8} {s.get('reason', '')}  {trained}")


def run(args, immich_factory):
    work = args.work
    plan_file = work / "splat_plan.json"
    if args.plan:
        plan = make_plan(args.candidates, args.cache, types_file=work / "types.json", work_dir=work)
        write_json(plan_file, plan)
        if not plan["sets"]:
            print("No object scan, walk or mixed sets to make splats from.")
        for row in plan["sets"]:
            print(f"{row['set_id']}  {row['kind']:<12} {row['usable']:>3} frames  parallax {row['parallax_pairs']:>3}  "
                  f"overlap {row['sequential_overlap']:.2f}  sharp {row['sharpness']}  score {row['score']:>5}  "
                  f"{row['skip']}")
        return
    if args.status:
        status(work, plan_file)
        return
    if args.stereo_video:
        args.sets = [from_stereo_video(work, args.stereo_video)["set_id"]] + list(args.sets or [])
    if args.sets and args.sfm:
        tools.require("colmap")
    if args.sets and args.train:
        tools.require("brush")
    failures = 0
    groups_file = Path(args.candidates).with_name("groups.json")
    for set_id in args.sets or []:
        if (args.sfm or args.train) and is_group(set_id, groups_file, work / "types.json"):
            LOG.info(f"{set_id}: skipped, a group photo set (best-shot merge, not a splat)")
            continue
        try:
            if args.prepare or args.sfm or args.train:
                if not (splat_dir(work, set_id) / "frames.json").exists() or args.prepare:
                    prepare(work, set_id, args.candidates, immich_factory)
            if args.sfm or args.train:
                sfm(work, set_id, gpu=args.gpu, threads=args.threads or limits.workers(), force=args.force)
            if args.train:
                train(work, set_id, steps=args.steps, chunk=args.chunk)
            if args.convert:
                found = exports(splat_dir(work, set_id) / "train")
                if found:
                    LOG.info(ply_to_splat(found[-1][1], splat_dir(work, set_id) / "scene.splat"))
        except SplatError as err:
            failures += 1
            LOG.error(f"{set_id}: {err}")
    if failures:
        raise SplatError(f"{failures} set(s) failed")
