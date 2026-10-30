"""Download the originals of one candidate set and convert them for stitching."""

import json
from pathlib import Path

from . import formats, log, raw
from .sources import FOLDER_PREFIX

LOG = log.get(__name__)

STITCHABLE = {".jpg", ".jpeg", ".tif", ".tiff", ".png"}


def load_candidate(candidates_file, set_id):
    data = json.loads(Path(candidates_file).read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else data["candidates"]
    for item in items:
        if item["set_id"] == set_id:
            return item
    raise SystemExit(f"{set_id} not found in {candidates_file}")


def asset_ids(candidate):
    ids = candidate["asset_ids"]
    return ids if isinstance(ids, list) else ids.split()


def to_jpeg(src, dest, quality=95):
    """Convert a camera raw, HEIC/HEIF or anything else Pillow reads to JPEG, keeping EXIF.
    Raws are developed in full, one at a time, and only for the frames being stitched."""
    from PIL import Image, ImageOps

    if raw.is_raw(src):
        raw.to_jpeg(src, dest, quality=quality)
        return
    formats.register()
    part = Path(str(dest) + ".part")
    with Image.open(src) as img:
        exif = img.getexif()
        img = ImageOps.exif_transpose(img)
        if exif:
            exif[0x0112] = 1
        img.convert("RGB").save(part, "JPEG", quality=quality, exif=exif.tobytes() if exif else b"",
                                subsampling=0)
    part.replace(dest)


def needs_turning(path):
    """True when a JPEG/TIFF relies on its EXIF orientation tag; Hugin ignores the tag."""
    from PIL import Image

    with Image.open(path) as img:
        return img.getexif().get(0x0112, 1) not in (0, 1)


def fetch_set(immich, candidate, work_dir, f35_hints=None):
    set_dir = Path(work_dir) / candidate["set_id"]
    originals = set_dir / "originals"
    images = set_dir / "images"
    originals.mkdir(parents=True, exist_ok=True)
    images.mkdir(parents=True, exist_ok=True)
    manifest = []
    total = 0
    for index, asset_id in enumerate(asset_ids(candidate), 1):
        info = immich.asset_info(asset_id)
        name = info.get("originalFileName") or f"{asset_id}.bin"
        original = originals / f"{index:02d}_{name}"
        if not original.exists():
            total += immich.download_original(asset_id, original)
        image = original
        converted = images / (original.stem + ".jpg")
        if converted.exists() or original.suffix.lower() not in STITCHABLE or needs_turning(original):
            image = converted
            if not image.exists():
                try:
                    to_jpeg(original, image)
                except Exception as err:
                    LOG.warning(f"  {name}: could not be converted ({err}); using its preview instead")
                    part = Path(str(image) + ".part")
                    part.write_bytes(immich.thumbnail(asset_id, "preview"))
                    part.replace(image)
        exif = info.get("exifInfo") or {}
        manifest.append({
            "index": index,
            "asset_id": asset_id,
            "file": name,
            "original": str(original),
            "image": str(image),
            "taken": info.get("localDateTime"),
            "make": exif.get("make"),
            "model": exif.get("model"),
            "latitude": exif.get("latitude"),
            "longitude": exif.get("longitude"),
            "lens": exif.get("lensModel"),
            "focal_length": exif.get("focalLength"),
            "width": exif.get("exifImageWidth"),
            "height": exif.get("exifImageHeight"),
            "f35_hint": (f35_hints or {}).get(asset_id),
        })
        LOG.info(f"  {index:2d} {name}")
    (set_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    # photos from a folder on this computer are read, not downloaded
    verb = "read" if all(m["asset_id"].startswith(FOLDER_PREFIX) for m in manifest) else "downloaded"
    LOG.info(f"{candidate['set_id']}: {len(manifest)} files, {total / 1e6:.1f} MB {verb}")
    return set_dir, manifest
