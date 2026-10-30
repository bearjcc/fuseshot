"""Google Takeout: pair each photo with the JSON file Takeout writes beside it.

Takeout keeps the capture time and place in a JSON sidecar and mangles its name:
long names are cut at 46 characters, newer exports add
".supplemental-metadata" (itself often cut short, ".supplemental-metad.json"),
a second photo with the same name becomes IMG_1234(1).jpg next to
IMG_1234.jpg(1).json, and edited copies ("-edited", or the word in another
language) share the original's sidecar. The JSON files are only ever read for
dates and places; they are never photos.

The matching rules are ported from immich-go's Google Photos reader
(https://github.com/simulot/immich-go, adapters/googlePhotos/matchers.go,
AGPL-3.0, by simulot and contributors), applied from the most common rule to the
least, as there.
"""

import bisect
import json
import os
from datetime import datetime, timezone

TRUNCATE = 46
SUPPLEMENTAL = "supplemental-metadata"
VIDEO_TYPES = {".mov", ".mp4", ".m4v", ".3gp", ".avi", ".mkv", ".mts", ".m2ts", ".webm", ".wmv", ".mpg"}
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif", ".webp", ".jxl", ".gif", ".bmp",
               ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf", ".avif"}


def ext(name):
    """The extension the way Go's path.Ext sees it: from the last dot, or empty."""
    dot = name.rfind(".")
    return name[dot:] if dot >= 0 else ""


def strip_ext(name):
    e = ext(name)
    return name[:-len(e)] if e else name


def is_media(extension):
    return extension.lower() in IMAGE_TYPES or extension.lower() in VIDEO_TYPES


def file_index(name):
    """IMG_1234(1).jpg -> (IMG_1234.jpg, '1'); a name with no (number) -> (name, '')."""
    p1 = name.rfind("(")
    if p1 >= 0:
        p2 = name.rfind(")")
        if p2 > p1:
            index = name[p1 + 1:p2]
            if index.isdigit():
                return name[:p1] + name[p2 + 1:], index
    return name, ""


def drop_supplemental(json_name):
    """IMG.jpg.supplemental-metad.json -> IMG.jpg.json; any cut-short form of the word counts."""
    p2 = json_name.rfind(".")
    if p2 > 1:
        p1 = json_name.rfind(".", 0, p2)
        if p1 > 1 and SUPPLEMENTAL.startswith(json_name[p1 + 1:p2]):
            return json_name[:p1] + json_name[p2:]
    return json_name


def match_fast_track(json_name, file_name):
    return strip_ext(json_name) == file_name


def match_normal(json_name, file_name):
    file_name, file_idx = file_index(file_name)
    json_name, json_idx = file_index(json_name)
    if file_idx != json_idx:
        return False
    json_name = strip_ext(drop_supplemental(json_name))
    if json_name == file_name:
        return True
    if len(file_name.encode("utf-8")) > TRUNCATE:
        if len(file_name) > TRUNCATE:
            return file_name[:TRUNCATE] == json_name
        stem = strip_ext(file_name)
        return stem[:-1] == json_name
    return False


def match_live_photo_video(json_name, file_name):
    """The video half of a Live Photo shares the still's sidecar."""
    if ext(file_name).lower() not in VIDEO_TYPES:
        return False
    file_name, file_idx = file_index(file_name)
    json_name, json_idx = file_index(json_name)
    if file_idx != json_idx:
        return False
    json_name = strip_ext(drop_supplemental(json_name))
    still = ext(json_name)
    if still.lower() not in IMAGE_TYPES:
        return False
    return json_name[:-len(still)] == strip_ext(file_name)


def match_forgotten_duplicates(json_name, file_name):
    json_name, file_name = strip_ext(json_name), strip_ext(file_name)
    return file_name.startswith(json_name) and len(file_name) - len(json_name) < 10


def match_edited_name(json_name, file_name):
    """IMG.jpg.json also describes IMG-edited.jpg (and -bearbeitet, -modifié, a cut-short -edi...)."""
    if file_index(file_name)[1]:
        return False
    base = strip_ext(json_name)
    p1 = base.rfind(".")
    if p1 > 1 and SUPPLEMENTAL.startswith(base[p1 + 1:]):
        base = json_name[:p1]
    extension = ext(base)
    if extension and is_media(extension):
        base = base[:-len(extension)]
        file_name = strip_ext(file_name)
    return file_name.startswith(base)


MATCHERS = (("fast track", match_fast_track), ("normal", match_normal), ("live photo video", match_live_photo_video),
            ("forgotten duplicate", match_forgotten_duplicates), ("edited", match_edited_name))


def _keys(json_name):
    """Names a matching file may start with, for finding candidates without trying every file."""
    plain, _ = file_index(json_name)
    base = strip_ext(drop_supplemental(plain))
    keys = {strip_ext(json_name), base, strip_ext(base)}
    return {k for k in keys if k}


def pair(names):
    """Sidecar for each media file among the names in one folder: {file name: json name}.

    Candidates come from a sorted list by prefix, so a folder of tens of thousands of
    photos is paired without comparing every JSON with every photo."""
    jsons = sorted(n for n in names if n.lower().endswith(".json"))
    if not jsons:
        return {}
    files = sorted(n for n in names if is_media(ext(n)))
    unmatched = set(files)
    found = {}
    for _, matcher in MATCHERS:
        for json_name in jsons:
            for key in _keys(json_name):
                for n in range(bisect.bisect_left(files, key), len(files)):
                    candidate = files[n]
                    if not candidate.startswith(key):
                        break
                    if candidate in unmatched and matcher(json_name, candidate):
                        found[candidate] = json_name
                        unmatched.discard(candidate)
    return found


def _timestamp(block):
    try:
        value = int((block or {}).get("timestamp"))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(value, timezone.utc) if value > 0 else None


def _place(block):
    try:
        lat, lon = float(block["latitude"]), float(block["longitude"])
    except (TypeError, ValueError, KeyError):
        return None
    return None if lat == 0 and lon == 0 else (lat, lon)


def read(path):
    """What a sidecar knows: capture time (UTC), place, original name and the device, or {} if unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    origin = data.get("googlePhotosOrigin") or {}
    device = ((origin.get("mobileUpload") or {}).get("deviceType")
              or ("scanner" if origin.get("photoScan") is not None else None))
    return {
        "taken": _timestamp(data.get("photoTakenTime")),
        "place": _place(data.get("geoDataExif")) or _place(data.get("geoData")),
        "title": data.get("title") if isinstance(data.get("title"), str) else None,
        "device": device,
    }


def sidecars(folder, names):
    """{file name: full path of its sidecar} for the photos in one folder."""
    return {name: os.path.join(folder, json_name) for name, json_name in pair(names).items()}
