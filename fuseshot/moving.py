"""Find sets where the subject itself moves: people, animals, vehicles.

A splat needs a still scene; a bird hopping between shots or a plane crossing the
sky leaves floaters and a smeared subject. These sets read better as a loop.

Each thumbnail goes through EfficientDet-Lite2 (Apache-2.0, run with MediaPipe).
A set counts as moving when one of the moving classes covers at least
MIN_BOX of the frame in at least MIN_FRAMES of its shots. Boats are left out
on purpose: moored boats are common and hold still.

Detection misses some subjects (insects are not among the COCO classes), so a
set can also be marked by hand with `fuseshot moving --mark`.
"""
import json
from pathlib import Path

from . import log
from .files import write_atomic

LOG = log.get(__name__)

VERSION = 1
MIN_SCORE = 0.3
MIN_BOX = 0.02
MIN_FRAMES = 0.4
LIVING = {"person", "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"}
VEHICLES = {"airplane", "bicycle", "motorcycle", "car", "bus", "train", "truck"}
MOVING = LIVING | VEHICLES


def rules():
    """What the stored per-shot counts depend on; MIN_FRAMES is applied when reading."""
    return {"version": VERSION, "min_score": MIN_SCORE, "min_box": MIN_BOX, "classes": sorted(MOVING)}


def detector():
    try:
        from mediapipe.tasks.python import BaseOptions, vision
    except ImportError:
        from .jobs import extra_hint

        raise RuntimeError("finding moving subjects needs mediapipe: " + extra_hint("group", "mediapipe")) from None
    from .assets import model

    options = vision.ObjectDetectorOptions(
        base_options=BaseOptions(model_asset_path=str(model("efficientdet-lite2"))),
        score_threshold=MIN_SCORE, max_results=10)
    return vision.ObjectDetector.create_from_options(options)


def frame_classes(detect, path):
    """Moving classes that cover enough of one picture."""
    import mediapipe as mp
    import numpy as np
    from PIL import Image

    rgb = np.asarray(Image.open(path).convert("RGB"))
    height, width = rgb.shape[:2]
    found = set()
    for d in detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)).detections:
        name, box = d.categories[0].category_name, d.bounding_box
        if name in MOVING and box.width * box.height >= MIN_BOX * width * height:
            found.add(name)
    return found


def judge(per_frame):
    """per_frame: one set of class names per shot."""
    counts = {}
    for names in per_frame:
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    return verdict(counts, len(per_frame))


def verdict(counts, shots):
    top = max(counts.items(), key=lambda kv: kv[1], default=(None, 0))
    moving = shots > 0 and top[1] >= MIN_FRAMES * shots
    return {"moving": moving, "subject": top[0] if moving else None, "shots": shots, "counts": counts}


def read(out_file):
    try:
        found = json.loads(Path(out_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"rules": None, "sets": {}, "marked": []}
    found.setdefault("marked", [])
    return found


def scan(candidates_file, cache_dir, out_file, set_ids=None, log=LOG.info):
    from .action import ids_key, load_sets, thumb_path

    before = read(out_file)
    old = before["sets"] if before.get("rules") == rules() else {}
    detect = detector().detect
    sets = {}
    for c in load_sets(candidates_file):
        if set_ids and c["set_id"] not in set_ids:
            continue
        key = ids_key(c)
        known = old.get(c["set_id"], {})
        if known.get("ids") == key:
            sets[c["set_id"]] = dict(verdict(known["counts"], known["shots"]), ids=key)
            continue
        paths = [thumb_path(cache_dir, a) for a in c["asset_ids"]]
        sets[c["set_id"]] = dict(judge([frame_classes(detect, p) for p in paths if p.exists()]), ids=key)
    result = {"rules": rules(), "sets": {**old, **sets}, "marked": before["marked"]}
    write_atomic(out_file, json.dumps(result, indent=1))
    log(f"{sum(v['moving'] for v in sets.values())} of {len(sets)} sets have a moving subject")
    return result


def mark(out_file, set_ids, moving=True):
    found = read(out_file)
    marked = set(found["marked"])
    marked = marked | set(set_ids) if moving else marked - set(set_ids)
    found["marked"] = sorted(marked)
    write_atomic(out_file, json.dumps(found, indent=1))
    return found


def moving_sets(out_file):
    found = read(out_file)
    return {k for k, v in found["sets"].items() if v.get("moving")} | set(found["marked"])
