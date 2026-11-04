"""Find photos that restoration could help, with cheap checks on previews.

Each photo gets a few measurements from its preview and the start of its original:
sharpness, noise, JPEG quality, resolution, a faded or black-and-white scan look and
dust or scratches. Each problem found adds a step to a suggested chain and to the
expected gain. Nothing is changed or uploaded; the result is a ranked list to review.

Scores are appended to work/enhance/scores.jsonl as they are made, so a scan can be
stopped and started again, and only the best candidates are kept in memory.
"""

import heapq
import io
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

import cv2
import numpy as np

LOG = logging.getLogger("fuseshot")
VERSION = 2
SIDE = 1024
THUMB_SIDE = 320
MIN_GAIN = 15
KEEP = 500
HEAD_BYTES = 1 << 16
# Long side, in pixels, below which a photo looks soft on a large screen.
WANT_LONG_SIDE = 2400
STEPS = {
    "scratches": ("lama", "Paint out dust and scratches"),
    "jpeg": ("fbcnn", "Remove JPEG blocking"),
    "noise": ("scunet", "Denoise"),
    "blur": ("nafnet", "Sharpen (deblur)"),
    "faded": ("ddcolor", "Colourise or restore colour (optional)"),
    "small": ("realesrgan", "Upscale"),
}
WEIGHTS = {"scratches": 0.6, "jpeg": 0.8, "noise": 0.7, "blur": 0.6, "faded": 0.4, "small": 0.7}
NOISE_KERNEL = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)
STANDARD_LUMA = np.array([
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99], np.float32)


def clip01(x):
    return float(min(1.0, max(0.0, x)))


def shrink(img, side=SIDE):
    scale = side / max(img.shape[:2])
    if scale >= 1:
        return img
    return cv2.resize(img, (round(img.shape[1] * scale), round(img.shape[0] * scale)), interpolation=cv2.INTER_AREA)


def sharpness(grey):
    """Variance of the Laplacian, and the share of pixels on a clear edge (flat scenes score low
    on both and are not called blurry)."""
    lap = cv2.Laplacian(grey, cv2.CV_32F).var()
    mag = cv2.magnitude(cv2.Sobel(grey, cv2.CV_32F, 1, 0), cv2.Sobel(grey, cv2.CV_32F, 0, 1))
    return float(lap), float((mag > 40).mean())


def noise_sigma(grey):
    """Immerkaer's fast noise estimate, on the smoother parts of the picture only."""
    g = grey.astype(np.float32)
    response = np.abs(cv2.filter2D(g, -1, NOISE_KERNEL))[1:-1, 1:-1]
    edges = cv2.Canny(cv2.GaussianBlur(grey, (0, 0), 2), 20, 60)[1:-1, 1:-1]
    flat = cv2.dilate(edges, np.ones((5, 5), np.uint8)) == 0
    if flat.mean() < 0.1:
        flat = np.ones_like(flat)
    return float(np.sqrt(np.pi / 2) * response[flat].mean() / 6)


def jpeg_quality(head):
    """The libjpeg quality that matches the luminance table in a JPEG's header, or None."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(head)) as img:
            tables = getattr(img, "quantization", None) or {}
    except Exception:
        return None
    if 0 not in tables:
        return None
    luma = np.array(tables[0], np.float32)
    if luma.size != 64:
        return None
    scale = float(np.mean(luma * 100 / STANDARD_LUMA))
    quality = (200 - scale) / 2 if scale <= 100 else 5000 / scale
    return int(round(min(100, max(1, quality))))


def colour_look(img):
    """('mono' | 'sepia' | None, mean chroma) from an 8-bit BGR image."""
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32) - [0, 128, 128]
    a, b = lab[..., 1], lab[..., 2]
    chroma = float(np.sqrt(a * a + b * b).mean())
    if chroma < 3:
        return "mono", chroma
    spread = float(np.std(np.degrees(np.arctan2(b, a))[np.hypot(a, b) > 2])) if (np.hypot(a, b) > 2).any() else 0
    hue = float(np.degrees(np.arctan2(b.mean(), a.mean())))
    if chroma < 30 and 40 < hue < 95 and spread < 20:
        return "sepia", chroma
    return None, chroma


def specks(grey):
    """Share of the picture covered by small, sharp light or dark marks (dust, scratches)."""
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    top = cv2.morphologyEx(grey, cv2.MORPH_TOPHAT, kernel)
    black = cv2.morphologyEx(grey, cv2.MORPH_BLACKHAT, kernel)
    marks = ((top > 45) | (black > 45)).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(marks, connectivity=8)
    small = stats[1:, cv2.CC_STAT_AREA] if count > 1 else np.array([])
    small = small[(small >= 2) & (small <= 150)]
    return float(small.sum() / grey.size)


def original_noise(img, size=None):
    """Noise in the original, in grey levels. The preview is a shrunk copy, which averages
    white noise down by about the shrink factor, so the estimate is scaled back up."""
    preview = shrink(img, 1600)
    factor = max(size) / max(preview.shape[:2]) if size and all(size) else 1.0
    return noise_sigma(cv2.cvtColor(preview, cv2.COLOR_BGR2GRAY)) * min(4.0, max(1.0, factor))


def measure(img, head=None, size=None):
    """The measurements for one photo: img is its preview (BGR), head the first bytes of the
    original (for the JPEG tables) and size its (width, height)."""
    small = shrink(img)
    grey = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    lap, edges = sharpness(grey)
    look, chroma = colour_look(small)
    return {
        "laplacian": round(lap, 1),
        "edges": round(edges, 4),
        "noise": round(original_noise(img, size), 2),
        "jpeg_quality": jpeg_quality(head) if head else None,
        "look": look,
        "chroma": round(chroma, 1),
        "specks": round(specks(grey), 5),
        "long_side": max(size) if size and all(size) else None,
    }


def problems(m, camera=True):
    """{problem: (severity 0-1, reason)} for a set of measurements."""
    found = {}
    q = m.get("jpeg_quality")
    if q is not None and q < 75:
        found["jpeg"] = (clip01((75 - q) / 45), f"JPEG saved at quality {q}")
    if m["noise"] > 1.5:
        found["noise"] = (clip01((m["noise"] - 1.5) / 3), f"noise around {m['noise']:.1f} levels")
    if m["laplacian"] < 120 and m["edges"] > 0.01:
        found["blur"] = (clip01((120 - m["laplacian"]) / 110), f"soft detail (sharpness {m['laplacian']:.0f})")
    long_side = m.get("long_side")
    if long_side and long_side < WANT_LONG_SIDE:
        found["small"] = (clip01((WANT_LONG_SIDE - long_side) / 1800), f"only {long_side} px on the long side")
    scan = m["look"] is not None and not camera
    if m["look"] is not None:
        what = "black and white" if m["look"] == "mono" else "faded sepia"
        found["faded"] = (0.8 if scan else 0.3, f"{what}{', no camera details (a scan?)' if scan else ''}")
    if m["specks"] > 0.0004 and (scan or m["look"] is not None):
        found["scratches"] = (clip01(m["specks"] / 0.004), f"dust or scratch marks ({m['specks'] * 100:.2f}% of the picture)")
    return found


def score(m, camera=True):
    """(expected gain 0-100, chain of steps, reasons)."""
    found = problems(m, camera)
    keep = 1.0
    for name, (sev, _) in found.items():
        keep *= 1 - WEIGHTS[name] * sev
    chain = [STEPS[name][0] for name in STEPS if name in found]
    reasons = [found[name][1] for name in STEPS if name in found]
    return round((1 - keep) * 100, 1), chain, reasons


def recent_photos(db_path, limit=None):
    """Photos in the cache, newest first: (id, file name, width, height, has camera details, taken)."""
    db = sqlite3.connect(db_path)
    try:
        sql = ("SELECT id, file_name, mime, width, height, make, model, taken_utc FROM assets "
               "WHERE type = 'IMAGE' AND COALESCE(is_trashed, 0) = 0 ORDER BY taken_utc DESC")
        rows = db.execute(sql + (" LIMIT ?" if limit else ""), (limit,) if limit else ()).fetchall()
    finally:
        db.close()
    return [{"id": r[0], "file_name": r[1], "mime": r[2] or "", "size": (r[3] or 0, r[4] or 0),
             "camera": bool((r[5] or "").strip() or (r[6] or "").strip()), "taken": r[7]} for r in rows]


def done_ids(scores_file):
    if not scores_file.exists():
        return set()
    done = set()
    with scores_file.open(encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("v") == VERSION:
                done.add(row["id"])
    return done


def thumb_path(out_dir, asset_id):
    return Path(out_dir) / "thumbs" / f"{asset_id}.jpg"


def score_one(library, photo):
    data = library.thumbnail(photo["id"], "preview")
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("preview could not be read")
    head = None
    if "jpeg" in photo["mime"] or photo["file_name"].lower().endswith((".jpg", ".jpeg")):
        head = library.original_head(photo["id"], HEAD_BYTES)
        head = head[1] if isinstance(head, tuple) else head
    m = measure(img, head, photo["size"])
    gain, chain, reasons = score(m, photo["camera"])
    return img, {"v": VERSION, "id": photo["id"], "file_name": photo["file_name"], "taken": photo["taken"],
                 "gain": gain, "chain": chain, "reasons": reasons, "measures": m}


def scan(db_path, library, out_dir, limit=None, stop_file=None):
    """Score photos, newest first, skipping those already scored; then write the ranking."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    scores_file = out_dir / "scores.jsonl"
    done = done_ids(scores_file)
    todo = [p for p in recent_photos(db_path, limit) if p["id"] not in done]
    LOG.info(f"Enhance scan: {len(done)} already scored, {len(todo)} to go")
    started, failed = time.time(), 0
    with scores_file.open("a", encoding="utf-8") as f:
        for n, photo in enumerate(todo, 1):
            if stop_file and Path(stop_file).exists():
                LOG.info("Stopped; run again to carry on")
                break
            try:
                img, row = score_one(library, photo)
            except Exception as err:
                failed += 1
                LOG.warning(f"  {photo['id']}: {err}")
                continue
            if row["gain"] >= MIN_GAIN:
                write_thumb(img, thumb_path(out_dir, photo["id"]))
            f.write(json.dumps(row) + "\n")
            if n % 100 == 0:
                f.flush()
                LOG.info(f"  {n}/{len(todo)} scored, {(time.time() - started) / n:.2f} s each")
    return rank(scores_file, out_dir / "candidates.json")


def write_thumb(img, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.jpg")
    cv2.imwrite(str(tmp), shrink(img, THUMB_SIDE), [cv2.IMWRITE_JPEG_QUALITY, 85])
    os.replace(tmp, path)


def rank(scores_file, candidates_file, keep=KEEP, min_gain=MIN_GAIN):
    """Read the scores a line at a time, keep the best few, and write them biggest gain first."""
    best, total, fine = [], 0, 0
    if scores_file.exists():
        with scores_file.open(encoding="utf-8") as f:
            for i, line in enumerate(f):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("v") != VERSION:
                    continue
                total += 1
                if row["gain"] < min_gain:
                    fine += 1
                    continue
                item = (row["gain"], -i, row)
                if len(best) < keep:
                    heapq.heappush(best, item)
                else:
                    heapq.heappushpop(best, item)
    rows = [r for _, _, r in sorted(best, reverse=True)]
    result = {"version": VERSION, "scored": total, "fine": fine, "min_gain": min_gain, "candidates": rows}
    tmp = Path(str(candidates_file) + ".tmp")
    tmp.write_text(json.dumps(result, indent=1), encoding="utf-8")
    os.replace(tmp, candidates_file)
    LOG.info(f"Enhance: {len(rows)} candidates of {total} scored ({fine} look fine)")
    return result


def load(out_dir):
    path = Path(out_dir) / "candidates.json"
    if not path.exists():
        return {"scored": 0, "fine": 0, "candidates": []}
    return json.loads(path.read_text(encoding="utf-8"))
