"""Stereo photos: side-by-side pairs from stereo cameras and MPO files, made into wigglegrams.

The XREAL Beam Pro saves its 3D photos as SV_*.jpg with both views side by side
(two 3840x2880 halves) and its 3D video the same way at 32:9. Fujifilm's REAL 3D
cameras and some phones save MPO files, a JPEG with the second view after it,
which Pillow reads as two frames. A wide JPEG on its own is just as likely to be a
panorama, so side by side is only assumed with a stereo camera's name or make.

A wigglegram flips between the two views. It looks steady when the two line up on
the subject, so the right view is shifted until the subject sits in the same place
in both: a face if OpenCV's YuNet face finder sees one, else the most eye-catching part
(OpenCV's spectral residual saliency), else the middle. The vertical offset between
the lenses comes from features matched across the whole frame.
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import cv2
import numpy as np

from . import action, assets, group, log
from .files import write_atomic

LOG = log.get(__name__)

SBS_RATIOS = (8 / 3, 32 / 9)
STEREO_MAKES = ("XREAL",)
STEREO_PREFIXES = ("SV_",)
MPO_TYPES = (".mpo",)
WORK_WIDTH = 1280
NEAR_SUBJECT = 0.18
DELAY_MS = 140
KEY_PREFIX = "wiggle-"


def key(asset_id):
    return KEY_PREFIX + asset_id


def kind(file_name, make=None, width=None, height=None, mime=None):
    """'mpo', 'sbs' or None, from what the cache knows of a photo."""
    name = (file_name or "").upper()
    if Path(name).suffix.lower() in MPO_TYPES or (mime or "").lower() in ("image/mpo", "image/x-mpo"):
        return "mpo"
    named = name.startswith(STEREO_PREFIXES) or any(m in (make or "").upper() for m in STEREO_MAKES)
    if not named:
        return None
    if not (width and height):
        return "sbs"
    ratio = max(width, height) / min(width, height)
    return "sbs" if any(abs(ratio - r) < 0.03 for r in SBS_RATIOS) else None


def split(image):
    """Left and right halves of a side-by-side frame."""
    w = image.shape[1] // 2
    return image[:, :w], image[:, w:2 * w]


def read_pair(path):
    """(left, right) BGR views of a stereo photo: an MPO's two frames, or the halves of
    a side-by-side JPEG."""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        if getattr(im, "n_frames", 1) >= 2:
            views = []
            for n in (0, 1):
                im.seek(n)
                views.append(cv2.cvtColor(np.asarray(im.convert("RGB")), cv2.COLOR_RGB2BGR))
            return tuple(views)
        image = cv2.cvtColor(np.asarray(ImageOps.exif_transpose(im).convert("RGB")), cv2.COLOR_RGB2BGR)
    if image.shape[1] < image.shape[0]:
        raise ValueError("not side by side")
    return split(image)


def subject(image):
    """(x, y, how) of what the picture is of: the biggest face, the most salient spot,
    or the middle."""
    h, w = image.shape[:2]
    face = biggest_face(image)
    if face is not None:
        return face[0], face[1], "face"
    if hasattr(cv2, "saliency"):
        ok, salient = cv2.saliency.StaticSaliencySpectralResidual_create().computeSaliency(image)
        if ok and salient is not None and salient.max() > 0:
            blurred = cv2.GaussianBlur(salient.astype(np.float32), (0, 0), max(3, w // 30))
            y, x = np.unravel_index(int(np.argmax(blurred)), blurred.shape)
            return float(x), float(y), "salient"
    return w / 2, h / 2, "centre"


def biggest_face(image):
    """Middle of the biggest face YuNet finds, or None (also when its model can't be had)."""
    try:
        finder = group.detector((image.shape[1], image.shape[0]))
    except (assets.AssetError, OSError, cv2.error) as e:
        LOG.debug("no face finder: %s", e)
        return None
    _, found = finder.detect(image)
    if found is None or not len(found):
        return None
    x, y, fw, fh = max(found, key=lambda f: f[2] * f[3])[:4]
    return float(x + fw / 2), float(y + fh / 2)


def offsets(left, right, at):
    """(dx, dy) to move the right view by so the subject at `at` lines up with the left:
    dy from every match, dx from the matches near the subject."""
    orb = cv2.ORB_create(4000)
    k1, d1 = orb.detectAndCompute(cv2.cvtColor(left, cv2.COLOR_BGR2GRAY), None)
    k2, d2 = orb.detectAndCompute(cv2.cvtColor(right, cv2.COLOR_BGR2GRAY), None)
    if d1 is None or d2 is None:
        return 0.0, 0.0, 0
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(d1, d2, k=2)
    good = [m[0] for m in pairs if len(m) == 2 and m[0].distance < 0.75 * m[1].distance]
    if len(good) < 8:
        return 0.0, 0.0, len(good)
    p1 = np.float32([k1[m.queryIdx].pt for m in good])
    p2 = np.float32([k2[m.trainIdx].pt for m in good])
    shift = p1 - p2
    # Stereo pairs only differ sideways; matches that jump up or down are wrong.
    dy = float(np.median(shift[:, 1]))
    sane = np.abs(shift[:, 1] - dy) < max(4.0, left.shape[0] * 0.01)
    p1, shift = p1[sane], shift[sane]
    if not len(shift):
        return 0.0, dy, 0
    near = np.hypot(p1[:, 0] - at[0], p1[:, 1] - at[1]) < NEAR_SUBJECT * left.shape[1]
    use = shift[near] if near.sum() >= 5 else shift
    return float(np.median(use[:, 0])), dy, int(len(use))


def wiggle_frames(left, right, width=action.LOOP_WIDTH):
    """The two views lined up on the subject and cut to what both show, at most width wide."""
    if left.shape != right.shape:
        right = cv2.resize(right, (left.shape[1], left.shape[0]), interpolation=cv2.INTER_AREA)
    scale = min(1.0, WORK_WIDTH / left.shape[1])
    small = [cv2.resize(v, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) for v in (left, right)]
    x, y, how = subject(small[0])
    dx, dy, used = offsets(small[0], small[1], (x, y))
    h, w = small[0].shape[:2]
    moved = cv2.warpAffine(small[1], np.float32([[1, 0, dx], [0, 1, dy]]), (w, h), borderMode=cv2.BORDER_CONSTANT)
    left_edge, right_edge = int(np.ceil(max(0, dx))), int(np.floor(min(w, w + dx)))
    top, bottom = int(np.ceil(max(0, dy))), int(np.floor(min(h, h + dy)))
    if right_edge - left_edge < w // 2 or bottom - top < h // 2:
        raise RuntimeError("the views are too far apart to line up")
    out = min(1.0, width / (right_edge - left_edge))
    size = (int((right_edge - left_edge) * out) // 2 * 2, int((bottom - top) * out) // 2 * 2)
    frames = [cv2.resize(v[top:bottom, left_edge:right_edge], size, interpolation=cv2.INTER_AREA)
              for v in (small[0], moved)]
    return frames, {"subject": how, "subject_at": [round(x / w, 3), round(y / h, 3)],
                    "disparity": round(dx / w, 4), "vertical": round(dy / h, 4), "matches": used}


def make(path, out_dir, candidate, log=LOG.info):
    """Write the wigglegram of one stereo photo to out_dir as a loop."""
    left, right = read_pair(path)
    frames, found = wiggle_frames(left, right)
    reason = {"face": "lined up on the face", "salient": "lined up on what stands out",
              "centre": "lined up on the middle"}[found["subject"]]
    return action.write_loop(frames, out_dir, DELAY_MS,
                             {"style": "wiggle", "asset_ids": candidate["asset_ids"], "candidate": candidate,
                              "reason": f"stereo photo, {reason}", **found}, log=log, poster_at=0)


def find(db):
    """Stereo photos in the cache as [(asset_id, kind, path or None, candidate)]."""
    has_files = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'source_files'").fetchone()
    query = ("SELECT a.id, a.file_name, a.mime, a.make, a.model, a.width, a.height, a.taken_utc, a.local_time, "
             + ("s.path" if has_files else "NULL") + " FROM assets a"
             + (" LEFT JOIN source_files s ON s.id = a.id" if has_files else "")
             + " WHERE COALESCE(a.is_trashed, 0) = 0 AND COALESCE(a.type, 'IMAGE') = 'IMAGE'")
    found = []
    for asset_id, name, mime, make, model, width, height, taken, local, path in db.execute(query):
        how = kind(name, make, width, height, mime)
        if not how:
            continue
        candidate = {"set_id": key(asset_id), "name": name, "start_local": (local or "")[:19].replace("T", " ") or None,
                     "start_utc": taken, "camera": f"{make or ''} {model or ''}".strip(), "asset_ids": [asset_id],
                     "library": "folder" if path else "immich",
                     "library_path": str(Path(path).parent) if path else None}
        found.append((asset_id, how, path, candidate))
    return found


def build(db_path, library, cache_dir, actions_dir, log=LOG.info):
    """Make a wigglegram of every stereo photo that has none yet."""
    db = sqlite3.connect(db_path)
    try:
        found = find(db)
    finally:
        db.close()
    made = failed = 0
    for asset_id, _, path, candidate in found:
        out_dir = Path(actions_dir) / key(asset_id)
        if (out_dir / "action.json").exists() or (out_dir / "failed.json").exists():
            continue
        try:
            if path:
                make(path, out_dir, candidate, log=log)
            else:
                Path(cache_dir).mkdir(parents=True, exist_ok=True)
                handle, temp = tempfile.mkstemp(suffix=Path(candidate["name"] or "x.jpg").suffix, dir=cache_dir)
                os.close(handle)
                try:
                    library.download_original(asset_id, temp)
                    make(temp, out_dir, candidate, log=log)
                finally:
                    Path(temp).unlink(missing_ok=True)
            made += 1
        except Exception as err:
            failed += 1
            out_dir.mkdir(parents=True, exist_ok=True)
            write_atomic(out_dir / "failed.json", json.dumps({"error": str(err)}))
            log(f"{key(asset_id)}: {err}")
    log(f"{len(found)} stereo photos, {made} wigglegrams made, {failed} failed")
    return made
