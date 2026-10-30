"""EXIF for finished results: the first frame's capture time, camera and place.

A panorama or merged group photo is a new file, so on its own it has no capture
time and photo apps file it under the day it was made. Before a result is
written out, the first frame's date and time (with its sub-seconds and time
zone), camera make and model and GPS position are put into it. The EXIF block
is swapped into the JPEG as it stands; the picture is not encoded again."""

from datetime import datetime
from pathlib import Path

from . import log

LOG = log.get(__name__)

SOFTWARE = "Fuseshot"
# tags copied from the first frame's EXIF sub-IFD
SUB_TAGS = (0x9003, 0x9004, 0x9010, 0x9011, 0x9012, 0x9291, 0x9292)
# GPS tags copied as they are; the rest (processing method and the like) are left out
GPS_TAGS = (1, 2, 3, 4, 5, 6, 7, 12, 13, 16, 17, 18, 29)


def _text(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return value.strip("\x00 ").strip() if isinstance(value, str) and value.strip("\x00 ").strip() else None


def _stamp(iso):
    """'2025-03-01T14:02:03.000Z' as EXIF writes it: '2025:03:01 14:02:03'."""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")[:19]).strftime("%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _rational(value, denominator=10000):
    from PIL.TiffImagePlugin import IFDRational

    return IFDRational(round(value * denominator), denominator)


def _dms(value):
    value = abs(value)
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    seconds = (value - degrees - minutes / 60) * 3600
    return (_rational(degrees, 1), _rational(minutes, 1), _rational(seconds, 1000))


def frame_tags(entry):
    """{"make", "model", "sub": {tag: value}, "gps": {tag: value}} for one manifest
    entry: from the original's EXIF, else from what the photo library said about it."""
    from .sources import read_exif

    exif, sub, gps = {}, {}, {}
    for path in (entry.get("original"), entry.get("image")):
        if not path or not Path(path).exists():
            continue
        try:
            _, _, exif, sub, gps = read_exif(path)
        except Exception as err:
            LOG.debug(f"{path}: no EXIF ({err})")
            continue
        if sub.get(0x9003) or exif.get(0x010F):
            break
    tags = {"make": _text(exif.get(0x010F)) or entry.get("make"),
            "model": _text(exif.get(0x0110)) or entry.get("model"),
            "sub": {t: sub[t] for t in SUB_TAGS if sub.get(t) is not None},
            "gps": {t: gps[t] for t in GPS_TAGS if gps.get(t) is not None} if gps.get(2) and gps.get(4) else {}}
    if not _text(tags["sub"].get(0x9003)):
        stamp = _text(exif.get(0x0132)) or (_stamp(entry["taken"]) if entry.get("taken") else None)
        if stamp:
            tags["sub"][0x9003] = stamp
    if not tags["gps"] and entry.get("latitude") is not None and entry.get("longitude") is not None:
        lat, lon = float(entry["latitude"]), float(entry["longitude"])
        tags["gps"] = {1: "N" if lat >= 0 else "S", 2: _dms(lat),
                       3: "E" if lon >= 0 else "W", 4: _dms(lon)}
    return tags


def exif_block(tags, size=None):
    """The EXIF bytes (as Pillow writes them) for a result of size (width, height)
    carrying these tags."""
    from PIL import Image, TiffTags

    # Pillow has no type for ComponentsConfiguration and would write it as bytes, not UNDEFINED
    TiffTags.TAGS_V2_GROUPS[0x8769].setdefault(
        0x9101, TiffTags.TagInfo(0x9101, "ComponentsConfiguration", TiffTags.UNDEFINED, 4))
    out = Image.Exif()
    out[0x0213] = 1
    if tags.get("make"):
        out[0x010F] = tags["make"]
    if tags.get("model"):
        out[0x0110] = tags["model"]
    out[0x0112] = 1
    out[0x0131] = SOFTWARE
    sub = dict(tags.get("sub") or {})
    if sub.get(0x9003):
        out[0x0132] = sub[0x9003]
        sub.setdefault(0x9004, sub[0x9003])
    sub.update({0x9000: b"0232", 0x9101: b"\x01\x02\x03\x00", 0xA001: 1})
    if size:
        sub.update({0xA002: size[0], 0xA003: size[1]})
    out.get_ifd(0x8769).update(sub)
    if tags.get("gps"):
        ifd = out.get_ifd(0x8825)
        ifd.update({0: b"\x02\x02\x00\x00", **tags["gps"]})
    return out.tobytes()


def with_exif(jpeg, exif):
    """The JPEG bytes with their EXIF block replaced by exif; the image data is untouched."""
    if jpeg[:2] != b"\xff\xd8" or not exif:
        return jpeg
    payload = exif if exif.startswith(b"Exif\x00\x00") else b"Exif\x00\x00" + exif
    if len(payload) + 2 > 0xFFFF:
        return jpeg
    segment = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    head, i = [], 2
    while i + 4 <= len(jpeg) and jpeg[i] == 0xFF:
        marker = jpeg[i + 1]
        if marker == 0xDA or not 0xE0 <= marker <= 0xFE:
            break
        length = int.from_bytes(jpeg[i + 2:i + 4], "big")
        part = jpeg[i:i + 2 + length]
        if not (marker == 0xE1 and part[4:10] == b"Exif\x00\x00"):
            head.append(part)
        i += 2 + length
    if head and head[0][1] == 0xE0:
        head.insert(1, segment)
    else:
        head.insert(0, segment)
    return b"\xff\xd8" + b"".join(head) + jpeg[i:]


def stamped(path, manifest):
    """The bytes of a finished JPEG with the first frame's EXIF put in, or as they
    are when there is nothing to add or the file is not a JPEG."""
    data = Path(path).read_bytes()
    if not manifest or Path(path).suffix.lower() not in (".jpg", ".jpeg"):
        return data
    try:
        from PIL import Image

        with Image.open(path) as img:
            size = img.size
        return with_exif(data, exif_block(frame_tags(manifest[0]), size))
    except Exception as err:
        LOG.info(f"{Path(path).name}: EXIF not added ({err})")
        return data
