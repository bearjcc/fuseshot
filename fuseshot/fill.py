"""Fill the empty border of a rendered preview with LaMa inpainting.

An alternative to cropping: the parts of the canvas no photo covers are painted
in by big-lama (Suvorov et al., "Resolution-robust Large Mask Inpainting with
Fourier Convolutions", 2021), run locally. Real pixels are never replaced: the
fill is composited only where the render's coverage mask is empty (grown by two
pixels to take in the soft render edge), plus the seam bands with --seams.
Every result keeps a mask of the invented pixels and is marked ai_filled in the
set's record, so an upload can label it.

Runs as a background job (python -m fuseshot fill <set> --name <preview>) in a
Python that has torch: the fill extra, or the fill_python setting under [jobs].
The model is loaded for the job and released when it ends.
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from . import assets
from .files import write_atomic

MODEL_NAME = "big-lama"
WORK_SIDE = 1536
TILE = 1024
OVERLAP = 128
GROW = 2
BACKGROUND = (24, 24, 24)
MAX_FILL_SHARE = 0.5


def peak_ram_mb():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None
        return round(counters.PeakWorkingSetSize / 2**20)
    import resource
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)


def hole_mask(preview_dir, name, image):
    """True where no photo covers the canvas."""
    import cv2
    import numpy as np
    cover_file = preview_dir / f"{name}_cover.png"
    if cover_file.exists():
        cover = cv2.imread(str(cover_file), cv2.IMREAD_GRAYSCALE)
        if cover is not None and cover.shape == image.shape[:2]:
            return cover < 128
    # older renders have no coverage mask: take the flat background colour that
    # touches the edge of the canvas
    flat = np.all(np.abs(image.astype(np.int16) - np.array(BACKGROUND[::-1])) <= 6, axis=2)
    count, labels = cv2.connectedComponents(flat.astype(np.uint8), connectivity=4)
    edge = set(np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])))
    edge.discard(0)
    return np.isin(labels, list(edge)) & flat


def tiles(h, w):
    if h <= TILE + OVERLAP and w <= TILE + OVERLAP:
        return [(0, h, 0, w)]
    step = TILE - OVERLAP
    ys = list(range(0, max(1, h - TILE), step)) + [max(0, h - TILE)]
    xs = list(range(0, max(1, w - TILE), step)) + [max(0, w - TILE)]
    return [(y, min(h, y + TILE), x, min(w, x + TILE)) for y in sorted(set(ys)) for x in sorted(set(xs))]


def inpaint(model, torch, device, image, hole):
    """LaMa over the tiles that contain holes; image is float RGB 0..1."""
    import numpy as np
    h, w = hole.shape
    out = image.copy()
    acc = np.zeros_like(image)
    weight = np.zeros((h, w), np.float32)
    for y0, y1, x0, x1 in tiles(h, w):
        m = hole[y0:y1, x0:x1]
        if not m.any():
            continue
        tile = out[y0:y1, x0:x1]
        th, tw = m.shape
        ph, pw = (-th) % 8, (-tw) % 8
        img = np.pad(tile, ((0, ph), (0, pw), (0, 0)), mode="reflect")
        msk = np.pad(m, ((0, ph), (0, pw)), mode="reflect")
        ti = torch.from_numpy(np.ascontiguousarray(img.transpose(2, 0, 1)))[None].to(device)
        tm = torch.from_numpy(msk.astype(np.float32))[None, None].to(device)
        with torch.inference_mode():
            res = model(ti, tm)[0].permute(1, 2, 0).float().cpu().numpy()[:th, :tw]
        # taper tile edges so overlapping tiles merge without a step
        ry = np.minimum(np.arange(th) + 1, np.arange(th)[::-1] + 1).astype(np.float32)
        rx = np.minimum(np.arange(tw) + 1, np.arange(tw)[::-1] + 1).astype(np.float32)
        taper = np.minimum(np.minimum.outer(ry, rx) / OVERLAP, 1.0)
        acc[y0:y1, x0:x1] += np.clip(res, 0, 1) * taper[..., None]
        weight[y0:y1, x0:x1] += taper
    done = weight > 0
    out[done] = acc[done] / weight[done][:, None]
    return np.where(hole[..., None], out, image)


def fill(set_dir, name, device="auto", seams=False, force=False):
    import cv2
    import numpy as np
    try:
        import torch
    except ImportError:
        from .jobs import extra_hint

        raise RuntimeError("AI fill needs torch: " + extra_hint("fill", "torch")) from None
    t0 = time.time()
    set_dir = Path(set_dir)
    preview_dir = set_dir / "previews"
    image = cv2.imread(str(preview_dir / f"{name}.jpg"), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"no preview {name}.jpg in {set_dir.name}")
    hole = hole_mask(preview_dir, name, image)
    hole = cv2.dilate(hole.astype(np.uint8), np.ones((2 * GROW + 1, 2 * GROW + 1), np.uint8)) > 0
    seam_file = preview_dir / f"{name}_seams.png"
    if seams and seam_file.exists():
        band = cv2.imread(str(seam_file), cv2.IMREAD_GRAYSCALE)
        if band is not None and band.shape == hole.shape:
            hole |= band > 128
    share = float(hole.mean())
    if share > MAX_FILL_SHARE and not force:
        raise RuntimeError(f"{share:.0%} of {name} is empty; filling more than {MAX_FILL_SHARE:.0%} would "
                           "invent most of the picture (a frame is probably misplaced; repair the set first)")
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available to torch; use --device cpu (slower) or install a CUDA build "
                           "of torch")
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()
    model = torch.jit.load(str(assets.model("big-lama")), map_location=device).eval()
    h, w = hole.shape
    scale = min(1.0, WORK_SIDE / max(h, w))
    small = (max(8, round(w * scale)), max(8, round(h * scale)))
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32) / 255
    rgb_s = cv2.resize(rgb, small, interpolation=cv2.INTER_AREA)
    hole_s = cv2.resize(hole.astype(np.uint8), small, interpolation=cv2.INTER_NEAREST) > 0
    hole_s = cv2.dilate(hole_s.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    t1 = time.time()
    painted = inpaint(model, torch, device, rgb_s, hole_s)
    model_seconds = time.time() - t1
    vram = None
    if device == "cuda":
        vram = {"allocated_mb": round(torch.cuda.max_memory_allocated() / 2**20),
                "reserved_mb": round(torch.cuda.max_memory_reserved() / 2**20)}
    del model
    if device == "cuda":
        torch.cuda.empty_cache()
    up = cv2.resize(painted, (w, h), interpolation=cv2.INTER_CUBIC)
    result = np.where(hole[..., None], np.clip(up, 0, 1), rgb)
    out = cv2.cvtColor((result * 255 + 0.5).astype(np.uint8), cv2.COLOR_RGB2BGR)
    # same picture with the invented pixels tinted and outlined
    show = out.astype(np.float32)
    show[hole] = show[hole] * 0.55 + np.array([200, 40, 220], np.float32) * 0.45
    edge = cv2.morphologyEx(hole.astype(np.uint8), cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)) > 0
    show[edge] = (255, 0, 255)
    show = show.astype(np.uint8)
    for target, data, args in ((f"{name}_fill.jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 92]),
                               (f"{name}_fillshow.jpg", show, [cv2.IMWRITE_JPEG_QUALITY, 85]),
                               (f"{name}_fill_mask.png", hole.astype(np.uint8) * 255, [])):
        tmp = preview_dir / f".{target}.tmp{Path(target).suffix}"
        if not cv2.imwrite(str(tmp), data, args):
            raise RuntimeError(f"could not write {target}")
        os.replace(tmp, preview_dir / target)
    info = {
        "ai_filled": True,
        "model": MODEL_NAME,
        "file": f"previews/{name}_fill.jpg",
        "mask": f"previews/{name}_fill_mask.png",
        "filled_share": round(share, 4),
        "seams": bool(seams),
        "work_size": list(small),
        "device": device,
        "vram_peak": vram,
        "ram_peak_mb": peak_ram_mb(),
        "model_seconds": round(model_seconds, 1),
        "seconds": round(time.time() - t0, 1),
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    index_file = preview_dir / "renders.json"
    renders = json.loads(index_file.read_text(encoding="utf-8")) if index_file.exists() else {}
    renders.setdefault(name, {})["fill"] = info
    write_atomic(index_file, json.dumps(renders, indent=2))
    record_file = set_dir / "batch.json"
    if record_file.exists():
        record = json.loads(record_file.read_text(encoding="utf-8"))
        record.setdefault("fill", {})[name] = info
        write_atomic(record_file, json.dumps(record, indent=1))
    queue_file = set_dir / "fill_queue.json"
    if queue_file.exists():
        queue = json.loads(queue_file.read_text(encoding="utf-8"))
        if name in queue:
            queue[name].update({"state": "done", "finished": info["created"]})
            write_atomic(queue_file, json.dumps(queue, indent=1))
    return info
