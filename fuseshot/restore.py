"""Run the suggested restoration chains on enhance candidates, on the GPU.

`fuseshot enhance-run` copies the originals of the best candidates into
work/enhance/results/<id>/ and queues one job (`enhance-apply`) that runs the steps.
The job reads and writes only those folders, so it needs no Immich access. Steps go
one model at a time over every photo that needs it, so each model is loaded once.
Every step is saved as it finishes, and a rerun carries on from the last one saved.

Models load through spandrel (MIT). The default steps use only its core architectures;
DDColor and CodeFormer come from optional packs and load through spandrel_extra_arches.
LaMa reuses the TorchScript model of the AI fill. Large images are cut into tiles.
"""

import json
import time
from pathlib import Path

import cv2
import numpy as np

from . import assets, log
from .files import write_atomic

LOG = log.get(__name__)
VERSION = 1
STEP_MODELS = {"lama": "big-lama", "fbcnn": "fbcnn-color", "scunet": "scunet-real-psnr",
               "nafnet": "nafnet-gopro-width64", "ddcolor": "ddcolor-modelscope",
               "realesrgan": "realesrgan-x4plus", "codeformer": "codeformer"}
# Steps from an optional pack (packs.py): used only once it is installed.
PACK_STEPS = {"ddcolor": "colourise", "codeformer": "face"}
FACE_SIDE = 512
ORDER = list(STEP_MODELS)
TILE = 512
OVERLAP = 32
# Upscaling stops at this long side; a 4x model on a 1000 px photo would go past it.
MAX_UPSCALED_SIDE = 3000
VIEW_SIDE = 1600
# Below this 1 - SSIM, the enhanced photo looks the same as the original and is not shown.
MIN_DIFFERENCE = 0.02


def results_dir(enhance_dir):
    return Path(enhance_dir) / "results"


def ordered(chain):
    return [s for s in ORDER if s in chain]


def usable(chain):
    """The chain without steps whose optional pack is not installed."""
    from . import packs

    return [s for s in ordered(chain) if s not in PACK_STEPS or packs.installed(PACK_STEPS[s])]


def faces(bgr):
    """Face boxes (x, y, w, h) from YuNet, the detector the group shots use."""
    h, w = bgr.shape[:2]
    s = min(1.0, 1280 / max(h, w))
    small = cv2.resize(bgr, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA) if s < 1 else bgr
    detector = cv2.FaceDetectorYN.create(str(assets.model("yunet")), "", small.shape[1::-1], 0.8)
    found = detector.detect(small)[1]
    return [tuple(int(v / s) for v in f[:4]) for f in (found if found is not None else [])]


def face_boxes(bgr, margin=0.6):
    """Square boxes around each face with room for the hair and chin, inside the picture."""
    h, w = bgr.shape[:2]
    boxes = []
    for x, y, fw, fh in faces(bgr):
        side = int(max(fw, fh) * (1 + 2 * margin))
        if side < 48:
            continue
        cx, cy = x + fw // 2, y + fh // 2
        side = min(side, h, w)
        x0, y0 = min(max(0, cx - side // 2), w - side), min(max(0, cy - side // 2), h - side)
        boxes.append((x0, y0, side))
    return boxes


def feather(side):
    """A soft round mask for pasting a redrawn face back."""
    r = np.hypot(*np.meshgrid(np.linspace(-1, 1, side), np.linspace(-1, 1, side)))
    return np.clip((1 - r) / 0.25, 0, 1).astype(np.float32)


def step_file(folder, n, step):
    return folder / f"step{n}-{step}.png"


def read_image(path):
    img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        from PIL import Image

        with Image.open(path) as im:
            img = cv2.cvtColor(np.asarray(im.convert("RGB")), cv2.COLOR_RGB2BGR)
    return img


def tile_boxes(h, w, tile=TILE, overlap=OVERLAP):
    if h <= tile and w <= tile:
        return [(0, h, 0, w)]
    step = tile - overlap
    ys = sorted({*range(0, max(1, h - tile), step), max(0, h - tile)})
    xs = sorted({*range(0, max(1, w - tile), step), max(0, w - tile)})
    return [(y, min(h, y + tile), x, min(w, x + tile)) for y in ys for x in xs]


def tiled(run, img, scale=1, tile=TILE, overlap=OVERLAP):
    """Apply run (float RGB HxWx3 -> float RGB at scale) tile by tile, feathering the overlaps."""
    h, w = img.shape[:2]
    acc = np.zeros((h * scale, w * scale, 3), np.float32)
    weight = np.zeros((h * scale, w * scale), np.float32)
    for y0, y1, x0, x1 in tile_boxes(h, w, tile, overlap):
        out = run(img[y0:y1, x0:x1])
        th, tw = out.shape[:2]
        ry = np.minimum(np.arange(th) + 1, np.arange(th)[::-1] + 1).astype(np.float32)
        rx = np.minimum(np.arange(tw) + 1, np.arange(tw)[::-1] + 1).astype(np.float32)
        taper = np.minimum(np.minimum.outer(ry, rx) / (overlap * scale), 1.0)
        acc[y0 * scale:y1 * scale, x0 * scale:x1 * scale] += out * taper[..., None]
        weight[y0 * scale:y1 * scale, x0 * scale:x1 * scale] += taper
    return acc / np.maximum(weight, 1e-6)[..., None]


def dust_mask(bgr):
    """The small, sharp marks the scan counted as dust, grown a little to cover their edges."""
    grey = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    marks = ((cv2.morphologyEx(grey, cv2.MORPH_TOPHAT, kernel) > 45) |
             (cv2.morphologyEx(grey, cv2.MORPH_BLACKHAT, kernel) > 45)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(marks, connectivity=8)
    keep = np.zeros(count, bool)
    keep[1:] = (stats[1:, cv2.CC_STAT_AREA] >= 2) & (stats[1:, cv2.CC_STAT_AREA] <= 150)
    return cv2.dilate(keep[labels].astype(np.uint8), np.ones((3, 3), np.uint8)) > 0


def ssim_difference(a, b, side=1024):
    """1 - SSIM on grey copies at most `side` px long: 0 for the same picture."""
    h, w = b.shape[:2]
    a = cv2.resize(a, (w, h), interpolation=cv2.INTER_AREA)
    s = min(1.0, side / max(h, w))
    size = (max(1, round(w * s)), max(1, round(h * s)))
    x = cv2.resize(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), size, interpolation=cv2.INTER_AREA).astype(np.float64)
    y = cv2.resize(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), size, interpolation=cv2.INTER_AREA).astype(np.float64)
    blur = lambda v: cv2.GaussianBlur(v, (11, 11), 1.5)  # noqa: E731
    mx, my = blur(x), blur(y)
    vx, vy, cxy = blur(x * x) - mx * mx, blur(y * y) - my * my, blur(x * y) - mx * my
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ssim = ((2 * mx * my + c1) * (2 * cxy + c2)) / ((mx * mx + my * my + c1) * (vx + vy + c2))
    return float(1 - ssim.mean())


class Models:
    """Loads one model at a time and records its peak GPU memory."""

    def __init__(self, torch, device):
        self.torch, self.device = torch, device
        self.vram = {}

    def load(self, step):
        name = STEP_MODELS[step]
        if step in PACK_STEPS:
            from . import packs

            if not packs.installed(PACK_STEPS[step]):
                raise RuntimeError(f"{step} needs the {packs.PACKS[PACK_STEPS[step]]['title']} pack; "
                                   "install it in Settings")
        path = assets.model(name, fetch=step not in PACK_STEPS)
        if step == "lama":
            return self.torch.jit.load(str(path), map_location=self.device).eval()
        import spandrel

        if step in PACK_STEPS:
            import spandrel_extra_arches

            spandrel_extra_arches.install(ignore_duplicates=True)

        found = spandrel.ModelLoader(device=self.device).load_from_file(str(path))
        if not isinstance(found, spandrel.ImageModelDescriptor):
            raise RuntimeError(f"{path.name} is not an image-to-image model")
        found.eval()
        if self.device == "cuda" and found.supports_half:
            found.model.half()
        return found

    def apply(self, step, model, bgr):
        torch = self.torch
        if step == "lama":
            from .fill import inpaint

            hole = dust_mask(bgr)
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255
            out = inpaint(model, torch, self.device, rgb, hole) if hole.any() else rgb
            return cv2.cvtColor((np.clip(out, 0, 1) * 255).round().astype(np.uint8), cv2.COLOR_RGB2BGR)
        if step == "ddcolor":
            return self.colourise(model, bgr)
        if step == "codeformer":
            return self.repair_faces(model, bgr)
        half = self.device == "cuda" and model.supports_half
        need = model.size_requirements

        def run(tile):
            h, w = tile.shape[:2]
            ph, pw = max(0, need.minimum - h), max(0, need.minimum - w)
            ph, pw = ph + (-(h + ph)) % need.multiple_of, pw + (-(w + pw)) % need.multiple_of
            t = np.pad(tile, ((0, ph), (0, pw), (0, 0)), mode="reflect")
            x = torch.from_numpy(np.ascontiguousarray(t.transpose(2, 0, 1)))[None].to(self.device)
            with torch.inference_mode():
                y = model(x.half() if half else x)
            out = y[0].float().clamp(0, 1).permute(1, 2, 0).cpu().numpy()
            return out[:h * model.scale, :w * model.scale]

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255
        out = tiled(run, rgb, model.scale)
        out = cv2.cvtColor((out * 255).round().astype(np.uint8), cv2.COLOR_RGB2BGR)
        long_side = max(out.shape[:2])
        if long_side > MAX_UPSCALED_SIDE:
            s = MAX_UPSCALED_SIDE / long_side
            out = cv2.resize(out, (round(out.shape[1] * s), round(out.shape[0] * s)), interpolation=cv2.INTER_AREA)
        return out


    def tensor(self, img):
        return self.torch.from_numpy(np.ascontiguousarray(img.transpose(2, 0, 1)))[None].to(self.device)

    def colourise(self, model, bgr):
        """DDColor works on its own small copy and keeps the photo's own lightness."""
        grey = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)[..., None] / 255
        with self.torch.inference_mode():
            out = model(self.tensor(grey).float())
        rgb = out[0].float().clamp(0, 1).permute(1, 2, 0).cpu().numpy()
        return cv2.cvtColor((rgb * 255).round().astype(np.uint8), cv2.COLOR_RGB2BGR)

    def repair_faces(self, model, bgr):
        out = bgr.astype(np.float32)
        for x, y, side in face_boxes(bgr):
            crop = cv2.resize(bgr[y:y + side, x:x + side], (FACE_SIDE, FACE_SIDE), interpolation=cv2.INTER_CUBIC)
            rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB).astype(np.float32) / 255
            with self.torch.inference_mode():
                fixed = model(self.tensor(rgb).float())[0].float().clamp(0, 1).permute(1, 2, 0).cpu().numpy()
            fixed = cv2.cvtColor((fixed * 255).astype(np.float32), cv2.COLOR_RGB2BGR)
            fixed = cv2.resize(fixed, (side, side), interpolation=cv2.INTER_AREA)
            mask = feather(side)[..., None]
            out[y:y + side, x:x + side] = fixed * mask + out[y:y + side, x:x + side] * (1 - mask)
        return np.clip(out, 0, 255).round().astype(np.uint8)


def pending(folder):
    """The job for one photo: (chain, the original's path), or None when it is finished."""
    job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    if (folder / "result.json").exists():
        done = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        if done.get("v") == VERSION and done.get("chain") == job["chain"]:
            return None
    return job


def latest(folder, chain, upto):
    """The newest saved image before step `upto` (the original when none)."""
    for n in range(upto - 1, -1, -1):
        path = step_file(folder, n, chain[n])
        if path.exists():
            return path
    return folder / json.loads((folder / "job.json").read_text(encoding="utf-8"))["original"]


def finish(folder, job, seconds):
    chain = job["chain"]
    original = read_image(folder / job["original"])
    enhanced = read_image(latest(folder, chain, len(chain)))
    cv2.imwrite(str(folder / "enhanced.jpg"), enhanced, [cv2.IMWRITE_JPEG_QUALITY, 95])
    for name, img in (("view-original.jpg", original), ("view-enhanced.jpg", enhanced)):
        h, w = enhanced.shape[:2]
        s = min(1.0, VIEW_SIDE / max(h, w))
        view = cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(folder / name), view, [cv2.IMWRITE_JPEG_QUALITY, 90])
    diff = ssim_difference(original, enhanced)
    result = {"v": VERSION, "id": job["id"], "chain": chain, "gain": job.get("gain"),
              "difference": round(diff, 4), "differs": diff >= MIN_DIFFERENCE,
              "size": [enhanced.shape[1], enhanced.shape[0]], "seconds": round(seconds, 1),
              "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    write_atomic(folder / "result.json", json.dumps(result, indent=2))
    return result


def apply(enhance_dir, device="auto", stop_file=None):
    """Run every unfinished chain under enhance_dir/results. Returns the finished results."""
    import torch

    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    root = results_dir(enhance_dir)
    jobs = {f: j for f in sorted(root.iterdir()) if (f / "job.json").exists() and (j := pending(f))}
    LOG.info(f"Enhance: {len(jobs)} photos to finish on {device}")
    models, started = Models(torch, device), {f: time.time() for f in jobs}
    for step in ORDER:
        todo = [(f, j) for f, j in jobs.items() if step in j["chain"]
                and not step_file(f, j["chain"].index(step), step).exists()]
        if not todo:
            continue
        if device == "cuda":
            torch.cuda.reset_peak_memory_stats()
        model, t0, biggest = models.load(step), time.time(), 0
        for folder, job in todo:
            if stop_file and Path(stop_file).exists():
                LOG.info("Stopped; run again to carry on")
                return []
            n = job["chain"].index(step)
            img = read_image(latest(folder, job["chain"], n))
            biggest = max(biggest, img.shape[0] * img.shape[1])
            cv2.imwrite(str(step_file(folder, n, step)), models.apply(step, model, img))
            LOG.info(f"{job['id']}: {step} done")
        peak = round(torch.cuda.max_memory_allocated() / 2**20) if device == "cuda" else None
        models.vram[step] = {"model": STEP_MODELS[step], "peak_mb": peak, "tile": TILE,
                             "largest_mp": round(biggest / 1e6, 1), "photos": len(todo),
                             "seconds": round(time.time() - t0, 1)}
        LOG.info(f"{step}: {len(todo)} photos, peak {peak} MB, {time.time() - t0:.0f} s")
        del model
        if device == "cuda":
            torch.cuda.empty_cache()
    done = [finish(f, j, time.time() - started[f]) for f, j in jobs.items()]
    if models.vram:
        old = root / "vram.json"
        known = json.loads(old.read_text(encoding="utf-8")) if old.exists() else {}
        write_atomic(old, json.dumps({**known, **models.vram, "device": device}, indent=2))
    return done


def prepare(enhance_dir, library, candidates, top=20):
    """Copy the originals of the best `top` candidates (with a chain) and write their jobs."""
    root = results_dir(enhance_dir)
    chosen = [c for c in candidates if usable(c.get("chain") or [])][:top]
    for c in chosen:
        folder = root / c["id"]
        folder.mkdir(parents=True, exist_ok=True)
        suffix = Path(c.get("file_name") or "x.jpg").suffix.lower() or ".jpg"
        original = "original" + suffix
        if not (folder / original).exists():
            library.download_original(c["id"], folder / original)
        job = {"id": c["id"], "chain": usable(c["chain"]), "gain": c["gain"], "original": original,
               "file_name": c.get("file_name"), "taken": c.get("taken")}
        write_atomic(folder / "job.json", json.dumps(job, indent=2))
    return chosen


def results(enhance_dir):
    """Finished results, biggest gain first."""
    root = results_dir(enhance_dir)
    found = []
    for path in root.glob("*/result.json") if root.exists() else []:
        try:
            found.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(found, key=lambda r: -(r.get("gain") or 0))


def add_face_pass(enhance_dir, asset_id):
    """Run the result again with CodeFormer at the end. False when it already had one."""
    folder = results_dir(enhance_dir) / asset_id
    job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    if "codeformer" in job["chain"]:
        return False
    job["chain"] = ordered(job["chain"] + ["codeformer"])
    write_atomic(folder / "job.json", json.dumps(job, indent=2))
    return True
