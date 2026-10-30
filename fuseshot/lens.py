"""Crop factors per camera and focal length, read once from original EXIF.

Immich keeps the real focal length but not the 35 mm equivalent, so field of view
needs the crop factor. One original per (make, model, focal length) is streamed to
a temporary file, its EXIF read, and the file deleted.
"""

import json
import statistics
import tempfile
import warnings
from pathlib import Path

from PIL import Image

from . import formats, log
from .files import write_atomic
from .progress import Progress

LOG = log.get(__name__)

formats.register()

EXIF_IFD = 0x8769
FOCAL_LENGTH = 0x920A
FOCAL_35 = 0xA405
PLANE_X_RES = 0xA20E
PLANE_UNIT = 0xA210
PIXEL_X = 0xA002
PIXEL_Y = 0xA003
UNIT_MM = {2: 25.4, 3: 10.0, 4: 1.0}

# Cameras whose files lack the 35 mm tag: crop factor from the maker's stated
# 35 mm equivalent range, keyed by model and narrowed by focal range in mm.
PUBLISHED = {
    "iPhone": [(0, 99, 7.6)],
    "iPhone 4": [(0, 99, 7.6)],
    "C700UZ": [(0, 99, 6.44)],
    "C3100Z,C3020Z": [(0, 99, 4.85)],
    "DSC-T50": [(0, 99, 6.0)],
    "DSC-W530": [(0, 99, 5.53)],
    "DiMAGE X60": [(0, 99, 5.87)],
    "DiMAGE S404": [(0, 99, 4.9)],
    "KODAK DX7440 ZOOM DIGITAL CAMERA": [(0, 99, 5.9)],
    "XT1710-02": [(0, 99, 6.3)],
    "moto z3": [(0, 99, 6.9)],
    "LM-G710": [(0, 3, 6.7), (3, 99, 7.4)],
}


def lens_key(make, model, focal):
    return f"{(make or '').strip()}|{(model or '').strip()}|{round(float(focal), 2)}"


def crop_from_exif(path):
    with Image.open(path) as im:
        exif = im.getexif()
        ifd = exif.get_ifd(EXIF_IFD)
        width = max(im.size)
    focal = float(ifd.get(FOCAL_LENGTH) or 0)
    f35 = float(ifd.get(FOCAL_35) or 0)
    if focal > 0 and f35 > 0:
        return f35 / focal
    res, unit = ifd.get(PLANE_X_RES), ifd.get(PLANE_UNIT)
    pixels = max(int(ifd.get(PIXEL_X) or 0), int(ifd.get(PIXEL_Y) or 0)) or width
    if res and unit in UNIT_MM and float(res) > 0:
        sensor_mm = pixels / float(res) * UNIT_MM[unit]
        if 2 < sensor_mm < 60:
            return 36.0 / sensor_mm
    return None


def update_crop_factors(immich, db, candidates, cache_dir, log=LOG.info):
    path = Path(cache_dir) / "crop_factors.json"
    known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    ids = [a for c in candidates for a in c["asset_ids"]]
    samples = {}
    for k in range(0, len(ids), 900):
        chunk = ids[k:k + 900]
        q = (f"SELECT id, make, model, focal_length FROM assets WHERE type = 'IMAGE' AND focal_length > 0 "
             f"AND id IN ({','.join('?' * len(chunk))})")
        for r in db.execute(q, chunk):
            samples.setdefault(lens_key(r["make"], r["model"], r["focal_length"]), r["id"])
    todo = [k for k in samples if k not in known]
    log(f"{len(samples)} camera/focal combinations, {len(todo)} to read")
    with tempfile.TemporaryDirectory() as tmp:
        for n, key in enumerate(todo, 1):
            dest = Path(tmp) / "original"
            try:
                immich.download_original(samples[key], dest)
                known[key] = crop_from_exif(dest)
            except Exception as err:
                log(f"  {key}: {type(err).__name__}")
                known[key] = None
            finally:
                dest.unlink(missing_ok=True)
            if n % 20 == 0:
                log(f"  {n}/{len(todo)}")
    path.write_text(json.dumps(known, indent=1, sort_keys=True), encoding="utf-8")
    return known


def focal35_from_head(data):
    """35 mm equivalent focal length from the first bytes of an original, or 0."""
    for marker in (b"Exif\x00\x00II*\x00", b"Exif\x00\x00MM\x00*"):
        start = data.find(marker)
        if start >= 0:
            exif = Image.Exif()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    exif.load(data[start:])
                    return float(exif.get_ifd(EXIF_IFD).get(FOCAL_35) or 0)
                except Exception:
                    return 0.0
    return 0.0


def update_frame_focals(immich, candidates, cache_dir, log=LOG.info):
    """Per-frame 35 mm focal length; phones change it with digital zoom at a fixed lens.

    Only the head of each original is requested (an HTTP range), nothing is stored but the number.
    """
    path = Path(cache_dir) / "focal35.json"
    known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    todo = list(dict.fromkeys(a for c in candidates for a in c["asset_ids"] if a not in known))
    if todo:
        log(f"{len(todo)} frames to read the 35 mm focal length from")
    with Progress("reading focal lengths", len(todo), unit="frames") as bar:
        for n, asset_id in enumerate(todo, 1):
            try:
                known[asset_id] = focal35_from_head(immich.original_head(asset_id, 1 << 17)[1])
            except Exception as err:
                log(f"  {asset_id}: {type(err).__name__}")
                known[asset_id] = 0.0
            bar.update()
            if n % 2000 == 0:
                write_atomic(path, json.dumps(known))
    write_atomic(path, json.dumps(known))
    return known


def crop_factor(factors, make, model, focal):
    """Crop factor for one frame, falling back to published values or the same camera."""
    if not focal:
        return None
    value = factors.get(lens_key(make, model, focal))
    if value:
        return value
    for lo, hi, crop in PUBLISHED.get((model or "").strip(), []):
        if lo <= focal < hi:
            return crop
    prefix = f"{(make or '').strip()}|{(model or '').strip()}|"
    same = [v for k, v in factors.items() if k.startswith(prefix) and v]
    if same and max(same) / min(same) < 1.15:
        return statistics.median(same)
    return None
