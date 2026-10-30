"""Find action shots and turn them into short stabilised loops.

An action shot is a quick run of photos from a camera that barely moves while
something in the frame does. Three checks decide it, all on the cached verify
matches and grey thumbnails:

1. Neighbouring frames line up under one homography with many inliers, and the
   frame corners move less than MAX_SHIFT of the long side. Panoramas fail this
   because the camera turns; object scans fail it because the camera walks round
   the subject and the background no longer fits one homography.
2. The frames were taken close together (median gap and span limits) and are not
   an exposure bracket.
3. After warping one frame onto the next, a solid patch still differs: a median of
   at least MIN_MEDIAN_MOVING and at most MAX_MOVING of the overlap. Less than that
   is a repeat of the same still photo; more means the match is wrong or the light
   changed. Over the whole run the corners may drift at most MAX_DRIFT, so a slow
   pan with people in it stays a panorama.
4. The still, moving run covers at least MIN_COVER of the set's shots. A panorama
   with a few repeated frames in it is reported as containing a burst instead.

Copies of one moment (RAW and JPEG, edited copies) count as one shot.

Burst-mode runs (frames well under a second apart) never become panorama
candidates, so detect keeps them in bursts.json and they are checked here too.
They have no cached verify matches, so neighbouring frames are matched on grey
copies of the Immich previews, fetched once into the same thumbnail cache.

Loops are built from the colour Immich previews. Every frame is registered to the
middle frame on its background, cropped to the area all frames cover and written
as an animated WebP and an MP4, plus a GIF when that stays small.
"""

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np

from . import log
from .files import write_atomic
from .immich import ImmichError
from .progress import Progress

LOG = log.get(__name__)

MEASURE_VERSION = 1
MAX_SHIFT = 0.10
MIN_INLIERS = 25
MIN_INLIER_SHARE = 0.35
MAX_MEDIAN_GAP_S = 5.0
MAX_SPAN_S = 120.0
MIN_MOVING = 0.02
MIN_MEDIAN_MOVING = 0.04
MAX_MOVING = 0.60
MAX_DRIFT = 0.25
MIN_RUN = 3
MIN_COVER = 0.6
SAME_SHIFT = 0.003
LOOP_WIDTH = 960
GIF_LIMIT = 4 << 20
THUMB_SIZE = 1024
NO_MATCHES = {"sizes": [], "pairs": [], "stats": [], "H": []}


def set_key(asset_ids):
    digest = hashlib.sha1(" ".join(asset_ids).encode()).hexdigest()[:16]
    return f"{digest}_v{MEASURE_VERSION}"


def thumb_path(cache_dir, asset_id):
    return Path(cache_dir) / "thumbs" / asset_id[:2] / f"{asset_id}.jpg"


def read_grey(path):
    return cv2.imread(str(path), cv2.IMREAD_GRAYSCALE) if path.exists() else None


def load_sets(candidates_file):
    """Candidate sets plus the burst-mode runs detect kept beside them."""
    candidates_file = Path(candidates_file)
    data = json.loads(candidates_file.read_text(encoding="utf-8"))
    sets = list(data["candidates"] if isinstance(data, dict) else data)
    bursts_file = candidates_file.with_name("bursts.json")
    if bursts_file.exists():
        sets += json.loads(bursts_file.read_text(encoding="utf-8"))["candidates"]
    return sets


def fetch_thumbs(immich, cache_dir, asset_ids):
    """Grey previews, long side THUMB_SIZE, in the cache verify uses.

    Returns the ids Immich has no preview for, such as assets deleted since the
    last sync.
    """
    missing = []
    for asset_id in asset_ids:
        path = thumb_path(cache_dir, asset_id)
        if path.exists():
            continue
        try:
            raw = immich.thumbnail(asset_id, "preview")
        except ImmichError as err:
            if "failed with 404" not in str(err):
                raise
            missing.append(asset_id)
            continue
        grey = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
        if grey is None:
            missing.append(asset_id)
            continue
        scale = THUMB_SIZE / max(grey.shape)
        if scale < 1:
            grey = cv2.resize(grey, (round(grey.shape[1] * scale), round(grey.shape[0] * scale)),
                              interpolation=cv2.INTER_AREA)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, cv2.imencode(".jpg", grey, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes())
    return missing


def load_matches(cache_dir, asset_ids):
    path = Path(cache_dir) / "verify" / f"{set_key(asset_ids)}.npz"
    if not path.exists():
        return None
    with np.load(path) as z:
        return {k: z[k] for k in ("sizes", "pairs", "stats", "H")}


def corner_shift(H, size):
    w, h = (float(v) for v in size)
    corners = np.float64([[0, 0], [w, 0], [w, h], [0, h]])
    moved = cv2.perspectiveTransform(corners[None], H)[0]
    return float(np.mean(np.linalg.norm(moved - corners, axis=1)) / max(w, h))


def pair_homography(matches, i, j):
    """Homography taking frame i onto frame j from the cache, or None."""
    for n, (a, b) in enumerate(matches["pairs"]):
        if (a, b) == (i, j) or (a, b) == (j, i):
            count, inliers, _ = (int(v) for v in matches["stats"][n])
            H = matches["H"][n].reshape(3, 3).astype(np.float64)
            if not np.all(np.isfinite(H)) or abs(H[2, 2]) < 1e-9 or inliers < MIN_INLIERS:
                return None, count, inliers
            H = H / H[2, 2]
            if (a, b) == (j, i):
                H = np.linalg.inv(H)
            return H, count, inliers
    return None, 0, 0


def moving_share(first, second, H):
    """Share of the overlap that still differs once first is warped onto second."""
    h, w = second.shape
    warped = cv2.warpPerspective(first, H, (w, h), flags=cv2.INTER_LINEAR)
    valid = cv2.warpPerspective(np.full(first.shape, 255, np.uint8), H, (w, h),
                                flags=cv2.INTER_NEAREST)
    valid = cv2.erode(valid, np.ones((15, 15), np.uint8)) > 0
    if valid.mean() < 0.3:
        return None
    a = cv2.GaussianBlur(warped, (0, 0), 2.5).astype(np.float32)
    b = cv2.GaussianBlur(second, (0, 0), 2.5).astype(np.float32)
    a = (a - a[valid].mean()) / (a[valid].std() + 1e-3)
    b = (b - b[valid].mean()) / (b[valid].std() + 1e-3)
    diff = np.abs(a - b)
    moving = ((diff > 0.6) & valid).astype(np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    moving = cv2.morphologyEx(moving, cv2.MORPH_OPEN, kernel)
    return float(moving[valid].mean())


def match_thumbs(first, second):
    """Homography from first to second when the cache has no pair for them."""
    orb = cv2.ORB_create(1500)
    k1, d1 = orb.detectAndCompute(first, None)
    k2, d2 = orb.detectAndCompute(second, None)
    if d1 is None or d2 is None:
        return None, 0, 0
    good = [m[0] for m in cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(d1, d2, k=2)
            if len(m) == 2 and m[0].distance < 0.8 * m[1].distance]
    if len(good) < 12:
        return None, len(good), 0
    p1 = np.float32([k1[m.queryIdx].pt for m in good])
    p2 = np.float32([k2[m.trainIdx].pt for m in good])
    H, mask = cv2.findHomography(p1, p2, cv2.RANSAC, 4.0, maxIters=2000, confidence=0.995)
    inliers = 0 if mask is None else int(mask.sum())
    if H is None or inliers < MIN_INLIERS:
        return None, len(good), inliers
    return H, len(good), inliers


def measure(matches, thumbs, i, j):
    H, count, inliers = pair_homography(matches, i, j)
    if H is None and count == 0 and thumbs[i] is not None and thumbs[j] is not None:
        H, count, inliers = match_thumbs(thumbs[i], thumbs[j])
    row = {"pair": [i, j], "inliers": inliers, "matches": count}
    if H is not None:
        row["shift"] = round(corner_shift(H, thumbs[i].shape[::-1] if thumbs[i] is not None
                                          else matches["sizes"][i]), 4)
        row["fit"] = round(inliers / max(count, 1), 3)
        row["H"] = [round(float(v), 6) for v in H.ravel()]
        steady = row["shift"] <= MAX_SHIFT and row["fit"] >= MIN_INLIER_SHARE
        if steady and thumbs[i] is not None and thumbs[j] is not None:
            share = moving_share(thumbs[i], thumbs[j], H)
            if share is not None:
                row["moving"] = round(share, 4)
    return row


def frame_pairs(matches, frames, thumbs):
    """Measure neighbouring frames, folding copies of one moment into one frame."""
    kept, pairs, copies = [frames[0]], [], 0
    for j in frames[1:]:
        row = measure(matches, thumbs, kept[-1], j)
        if row.get("shift", 1) < SAME_SHIFT and row.get("moving", 1) < SAME_SHIFT:
            copies += 1
            continue
        kept.append(j)
        pairs.append(row)
    return kept, pairs, copies


def still(row):
    return "moving" in row


def runs(pairs, frames, size):
    """Stretches of frames joined by still-camera pairs."""
    found, start = [], None
    for n, row in enumerate(pairs + [{}]):
        if still(row):
            start = n if start is None else start
        elif start is not None:
            found.append((start, n))
            start = None
    out = []
    for a, b in found:
        members = frames[a:b + 1]
        if len(members) >= MIN_RUN:
            out.append({"frames": members,
                        "moving": [pairs[k]["moving"] for k in range(a, b)],
                        "drift": round(drift([pairs[k] for k in range(a, b)], size), 3)})
    return out


def drift(rows, size):
    """Largest corner movement from the first frame of a run to any later one."""
    total, worst = np.eye(3), 0.0
    for row in rows:
        total = np.reshape(row["H"], (3, 3)) @ total
        worst = max(worst, corner_shift(total, size))
    return worst


def is_action_run(run):
    moving = [m for m in run["moving"] if MIN_MOVING <= m <= MAX_MOVING]
    return (len(moving) * 2 >= len(run["moving"]) and len(moving) >= 2
            and MIN_MEDIAN_MOVING <= float(np.median(run["moving"])) <= MAX_MOVING
            and run["drift"] <= MAX_DRIFT)


def classify(candidate, cache_dir, frames=None, immich=None):
    """Return the action measurement for a candidate, or for some of its frames.
    Without cached matches the frames are matched on their thumbnails, fetched
    first when an Immich client is given."""
    ids = candidate["asset_ids"]
    frames = list(range(len(ids))) if frames is None else frames
    out = {"set_id": candidate["set_id"], "frames": len(frames), "action": False}
    reasons = []
    if len(frames) < MIN_RUN:
        reasons.append("fewer than three frames")
    if candidate.get("bracket") or candidate.get("verified_type") == "bracket":
        reasons.append("exposure bracket")
    gap, span = candidate.get("median_gap_s") or 0, candidate.get("span_s") or 0
    if gap > MAX_MEDIAN_GAP_S or span > MAX_SPAN_S:
        reasons.append(f"too slow ({gap:.0f} s gaps over {span:.0f} s)")
    matches = None if reasons else load_matches(cache_dir, ids)
    if matches is None and not reasons:
        missing = []
        if immich is not None:
            missing = fetch_thumbs(immich, cache_dir, [ids[i] for i in frames])
        if all(thumb_path(cache_dir, ids[i]).exists() for i in frames):
            matches = NO_MATCHES
        elif missing:
            reasons.append(f"{len(missing)} preview(s) missing in Immich")
        else:
            reasons.append("no cached matches")
    if reasons:
        out["reason"] = "; ".join(reasons)
        return out
    thumbs = {i: read_grey(thumb_path(cache_dir, ids[i])) for i in frames}
    kept, pairs, copies = frame_pairs(matches, frames, thumbs)
    size = next((t.shape[::-1] for t in thumbs.values() if t is not None), (1024, 768))
    found = runs(pairs, kept, size)
    action = [r for r in found if is_action_run(r)]
    out.update(shots=len(kept), copies=copies, pairs=pairs, runs=found)
    best = max(action, key=lambda r: len(r["frames"])) if action else None
    if best and len(best["frames"]) >= MIN_COVER * len(kept):
        out.update(action=True, run=best["frames"],
                   reason=f"{len(best['frames'])} of {len(kept)} shots from a still camera, "
                          f"median {np.median(best['moving']):.0%} of the frame moving")
    elif best:
        out.update(burst=best["frames"],
                   reason=f"only {len(best['frames'])} of {len(kept)} shots form a moving burst")
    elif len(kept) < MIN_RUN:
        out["reason"] = "copies of fewer than three shots"
    elif found and all(r["drift"] > MAX_DRIFT for r in found):
        out["reason"] = "camera pans slowly"
    elif found:
        out["reason"] = "still camera but little moves"
    else:
        out["reason"] = "camera moves between frames"
    return out


def scan(candidates_file, cache_dir, out_file, plan_file=None, immich=None, log=LOG.info):
    """Classify every candidate set, burst-mode run and planned stitch job."""
    candidates = load_sets(candidates_file)
    by_id = {c["set_id"]: c for c in candidates}
    started = time.time()
    old = {}
    if Path(out_file).exists():
        try:
            before = json.loads(Path(out_file).read_text(encoding="utf-8"))
            if before.get("rules") == rules():
                old = before.get("sets", {})
        except ValueError:
            pass
    sets, todo = {}, []
    for c in candidates:
        key = ids_key(c)
        if old.get(c["set_id"], {}).get("ids") == key:
            sets[c["set_id"]] = old[c["set_id"]]
        else:
            todo.append((c, key))
    if len(sets):
        log(f"{len(sets)} sets already looked at for action shots, {len(todo)} to go")
    with Progress("looking for action shots", len(todo), unit="sets") as bar:
        for n, (c, key) in enumerate(todo, 1):
            sets[c["set_id"]] = dict(classify(c, cache_dir, immich=immich), ids=key)
            bar.update()
            if n % 500 == 0:
                write_atomic(out_file, json.dumps({"rules": rules(), "sets": sets, "jobs": {}, "partial": True}))
    sets = {c["set_id"]: sets[c["set_id"]] for c in candidates}
    jobs = {}
    if plan_file and Path(plan_file).exists():
        plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))
        for job in plan["jobs"]:
            c = by_id.get(job["set_id"])
            if c:
                frames = [f - 1 for f in job["frames"]]
                jobs[job["key"]] = classify(c, cache_dir, frames)
                jobs[job["key"]]["stitch_type"] = job["type"]
    result = {"rules": rules(), "sets": sets, "jobs": jobs,
              "seconds": round(time.time() - started, 1)}
    write_atomic(out_file, json.dumps(result, indent=1))
    log(f"{sum(v['action'] for v in sets.values())} of {len(sets)} sets and "
        f"{sum(v['action'] for v in jobs.values())} of {len(jobs)} stitch jobs look like action shots")
    return result


def ids_key(candidate):
    """A short fingerprint of the photos in a set, so an unchanged set is not looked at twice."""
    ids = candidate["asset_ids"]
    ids = ids if isinstance(ids, list) else ids.split()
    return hashlib.sha1(" ".join(map(str, ids)).encode()).hexdigest()[:16]


def rules():
    return {"max_shift": MAX_SHIFT, "min_inliers": MIN_INLIERS,
            "min_inlier_share": MIN_INLIER_SHARE, "max_median_gap_s": MAX_MEDIAN_GAP_S,
            "max_span_s": MAX_SPAN_S, "moving": [MIN_MOVING, MIN_MEDIAN_MOVING, MAX_MOVING],
            "max_drift": MAX_DRIFT, "min_frames": MIN_RUN, "min_cover": MIN_COVER}


def colour_preview(immich, cache_dir, asset_id):
    path = Path(cache_dir) / "colour" / asset_id[:2] / f"{asset_id}.jpg"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, immich.thumbnail(asset_id, "preview"))
    return cv2.imread(str(path), cv2.IMREAD_COLOR)


def register(images, reference):
    """Homographies taking each image onto the reference image's background."""
    orb = cv2.ORB_create(5000)
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    grey = [cv2.cvtColor(im, cv2.COLOR_BGR2GRAY) for im in images]
    features = [orb.detectAndCompute(g, None) for g in grey]

    def link(i, j):
        (k1, d1), (k2, d2) = features[i], features[j]
        if d1 is None or d2 is None:
            return None
        good = [m[0] for m in matcher.knnMatch(d1, d2, k=2)
                if len(m) == 2 and m[0].distance < 0.75 * m[1].distance]
        if len(good) < MIN_INLIERS:
            return None
        p1 = np.float32([k1[m.queryIdx].pt for m in good])
        p2 = np.float32([k2[m.trainIdx].pt for m in good])
        H, mask = cv2.findHomography(p1, p2, cv2.RANSAC, 3.0, maxIters=4000, confidence=0.999)
        if H is None or mask.sum() < MIN_INLIERS:
            return None
        return H

    result = [None] * len(images)
    result[reference] = np.eye(3)
    for step in (1, -1):
        n = reference + step
        while 0 <= n < len(images):
            H = link(n, reference)
            if H is None and result[n - step] is not None:
                near = link(n, n - step)
                H = None if near is None else result[n - step] @ near
            result[n] = H
            n += step
    return result


def common_rect(mask):
    """Largest-ish upright rectangle inside a mask, by trimming the worst edge."""
    ys, xs = np.nonzero(mask)
    top, bottom, left, right = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    while bottom - top > 16 and right - left > 16:
        box = mask[top:bottom, left:right]
        edges = {"top": box[0].mean(), "bottom": box[-1].mean(),
                 "left": box[:, 0].mean(), "right": box[:, -1].mean()}
        side = min(edges, key=edges.get)
        if edges[side] >= 0.999:
            break
        if side == "top":
            top += 1
        elif side == "bottom":
            bottom -= 1
        elif side == "left":
            left += 1
        else:
            right -= 1
    return top, bottom, left, right


def animate(immich, cache_dir, asset_ids, out_dir, gap_s=0.5, log=LOG.info):
    """Write animation.webp, animation.mp4 (and a small GIF) for one action run."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    images = [colour_preview(immich, cache_dir, a) for a in asset_ids]
    keep = [i for i, im in enumerate(images) if im is not None]
    images = [images[i] for i in keep]
    h, w = images[len(images) // 2].shape[:2]
    images = [im if im.shape[:2] == (h, w) else cv2.resize(im, (w, h), interpolation=cv2.INTER_AREA)
              for im in images]
    reference = len(images) // 2
    homographies = register(images, reference)
    frames, mask = [], np.ones((h, w), bool)
    for im, H in zip(images, homographies):
        if H is None:
            continue
        frames.append(cv2.warpPerspective(im, H, (w, h), flags=cv2.INTER_LINEAR))
        valid = cv2.warpPerspective(np.full((h, w), 255, np.uint8), H, (w, h),
                                    flags=cv2.INTER_NEAREST)
        mask &= valid > 0
    if len(frames) < MIN_RUN or mask.mean() < 0.4:
        raise RuntimeError(f"only {len(frames)} frames registered, "
                           f"{mask.mean():.0%} of the frame shared")
    top, bottom, left, right = common_rect(cv2.erode(mask.astype(np.uint8), np.ones((5, 5))) > 0)
    scale = min(1.0, LOOP_WIDTH / (right - left))
    size = (int((right - left) * scale) // 2 * 2, int((bottom - top) * scale) // 2 * 2)
    frames = [cv2.resize(f[top:bottom, left:right], size, interpolation=cv2.INTER_AREA)
              for f in frames]
    delay = int(min(700, max(200, gap_s * 1000)))
    poster = cv2.imencode(".jpg", frames[len(frames) // 2], [cv2.IMWRITE_JPEG_QUALITY, 88])[1]
    write_atomic(out_dir / "poster.jpg", poster.tobytes())
    written = write_webp(frames, out_dir / "animation.webp", delay)
    written += write_mp4(frames, out_dir / "animation.mp4", delay)
    written += write_gif(frames, out_dir / "animation.gif", delay)
    info = {"asset_ids": asset_ids, "frames": len(frames), "used": [asset_ids[i] for i in keep],
            "size": list(size), "delay_ms": delay, "crop": [int(left), int(top), int(right), int(bottom)],
            "shared": round(float(mask.mean()), 3), "files": written,
            "made": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    write_atomic(out_dir / "action.json", json.dumps(info, indent=1))
    log(f"{out_dir.name}: {len(frames)} frames, {size[0]}x{size[1]}, {', '.join(written)}")
    return info


def _pil(frames):
    from PIL import Image
    return [Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)) for f in frames]


def _temp(path):
    return path.with_name(path.stem + ".tmp" + path.suffix)


def write_webp(frames, path, delay):
    images, tmp = _pil(frames), _temp(path)
    images[0].save(tmp, format="WEBP", save_all=True, append_images=images[1:],
                   duration=delay, loop=0, quality=80, method=4)
    tmp.replace(path)
    return [path.name]


def write_mp4(frames, path, delay):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    tmp = _temp(path)
    h, w = frames[0].shape[:2]
    command = [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-s", f"{w}x{h}", "-r", f"{1000 / delay:.4f}", "-i", "-",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
               "-movflags", "+faststart", "-r", "30", str(tmp)]
    loops = max(1, round(3000 / (delay * len(frames))))
    data = b"".join(f.tobytes() for f in frames) * loops
    run = subprocess.run(command, input=data, capture_output=True)
    if run.returncode or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return []
    tmp.replace(path)
    return [path.name]


def write_gif(frames, path, delay):
    images, tmp = _pil(frames), _temp(path)
    small = [im.resize((im.width * 2 // 3, im.height * 2 // 3)) for im in images]
    small[0].save(tmp, format="GIF", save_all=True, append_images=small[1:],
                  duration=delay, loop=0, optimize=True)
    if tmp.stat().st_size > GIF_LIMIT:
        tmp.unlink()
        path.unlink(missing_ok=True)
        return []
    tmp.replace(path)
    return [path.name]


def build(immich, scan_file, cache_dir, work_dir, candidates_file, keys=None,
          chunk=None, chunk_size=20, stop_file=None, log=LOG.info):
    """Make loops for action sets from a scan, skipping ones already made and group photos,
    which become best-shot merges instead."""
    found = json.loads(Path(scan_file).read_text(encoding="utf-8"))
    by_id = {c["set_id"]: c for c in load_sets(candidates_file)}
    checks = {**found["sets"], **found["jobs"]}
    groups_file = Path(scan_file).with_name("groups.json")
    groups = json.loads(groups_file.read_text(encoding="utf-8"))["sets"] if groups_file.exists() else {}
    todo = sorted(k for k, v in checks.items()
                  if v["action"] and not groups.get(v["set_id"], {}).get("group"))
    if keys:
        todo = [k for k in keys if k in checks]
    elif chunk is not None:
        todo = todo[chunk * chunk_size:(chunk + 1) * chunk_size]
    made = failed = 0
    for key in todo:
        if stop_file and Path(stop_file).exists():
            log("stop file found, stopping")
            break
        out_dir = Path(work_dir) / "actions" / key
        if (out_dir / "action.json").exists():
            continue
        c = by_id[checks[key]["set_id"]]
        run = checks[key].get("run") or list(range(len(c["asset_ids"])))
        try:
            animate(immich, cache_dir, [c["asset_ids"][i] for i in run], out_dir,
                    gap_s=c.get("median_gap_s") or 0.5, log=log)
            made += 1
        except Exception as err:
            failed += 1
            out_dir.mkdir(parents=True, exist_ok=True)
            write_atomic(out_dir / "failed.json", json.dumps({"error": str(err)}))
            log(f"{key}: {err}")
    log(f"made {made}, failed {failed}, of {len(todo)}")
