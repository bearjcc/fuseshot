"""Find sets where the subject itself moves: people, animals, vehicles.

A splat needs a still scene; a bird hopping between shots or a plane crossing the
sky leaves floaters and a smeared subject. These sets read better as a loop.

Each thumbnail goes through EfficientDet-Lite2 (Apache-2.0, run with MediaPipe).
Detections are only a hint: a class must cover at least MIN_BOX of the frame in
at least MIN_FRAMES of the shots and must shift enough between neighbouring frames
to count as motion (statues and static displays stay eligible for splats).

Boats are left out on purpose: moored boats are common and hold still.

Detection misses some subjects (insects are not among the COCO classes), so a
set can also be marked by hand with `fuseshot moving --mark`.
"""
import json
import math
from pathlib import Path

from . import log
from .files import write_atomic

LOG = log.get(__name__)

VERSION = 2
MIN_SCORE = 0.3
MIN_BOX = 0.02
MIN_FRAMES = 0.4
MIN_DISPLACE = 0.02
MIN_MOTION_FRAC = 0.45
LIVING = {"person", "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"}
VEHICLES = {"airplane", "bicycle", "motorcycle", "car", "bus", "train", "truck"}
MOVING = LIVING | VEHICLES


def rules():
    """What the stored per-shot counts depend on."""
    return {
        "version": VERSION,
        "min_score": MIN_SCORE,
        "min_box": MIN_BOX,
        "min_frames": MIN_FRAMES,
        "min_displace": MIN_DISPLACE,
        "min_motion_frac": MIN_MOTION_FRAC,
        "classes": sorted(MOVING),
    }


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


def frame_detections(detect, path):
    """Moving-class boxes large enough to matter on one picture."""
    import mediapipe as mp
    import numpy as np
    from PIL import Image

    rgb = np.asarray(Image.open(path).convert("RGB"))
    height, width = rgb.shape[:2]
    found = []
    for d in detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)).detections:
        name, box = d.categories[0].category_name, d.bounding_box
        area = box.width * box.height
        if name in MOVING and area >= MIN_BOX * width * height:
            found.append({
                "class": name,
                "cx": (box.origin_x + box.width / 2) / width,
                "cy": (box.origin_y + box.height / 2) / height,
                "frac": area / (width * height),
            })
    return found


def lead_box(per_frame, class_name):
    """The largest detection of class_name on each shot (None when absent)."""
    out = []
    for frame in per_frame:
        boxes = [b for b in frame if b["class"] == class_name]
        out.append(max(boxes, key=lambda b: b["frac"]) if boxes else None)
    return out


def motion_frac(per_frame, class_name):
    """Share of neighbouring frames where that class shifts enough to suggest real motion."""
    boxes = lead_box(per_frame, class_name)
    pairs = moving = 0
    prev = None
    for box in boxes:
        if box is None:
            prev = None
            continue
        if prev is not None:
            pairs += 1
            if math.hypot(box["cx"] - prev["cx"], box["cy"] - prev["cy"]) >= MIN_DISPLACE:
                moving += 1
        prev = box
    return moving / pairs if pairs else 0.0


def judge(per_frame):
    """per_frame: detections per shot from frame_detections."""
    counts = {}
    for frame in per_frame:
        for b in frame:
            name = b["class"]
            counts[name] = counts.get(name, 0) + 1
    shots = len(per_frame)
    top_class = max(counts.items(), key=lambda kv: kv[1], default=(None, 0))[0]
    mf = motion_frac(per_frame, top_class) if top_class else 0.0
    return verdict(counts, shots, mf)


def verdict(counts, shots, motion_frac=0.0):
    top = max(counts.items(), key=lambda kv: kv[1], default=(None, 0))
    presence = shots > 0 and top[1] >= MIN_FRAMES * shots
    moving = presence and motion_frac >= MIN_MOTION_FRAC
    return {
        "moving": moving,
        "subject": top[0] if moving else None,
        "shots": shots,
        "counts": counts,
        "presence_only": presence and not moving,
        "motion_frac": round(float(motion_frac), 3),
    }


def read(out_file):
    try:
        found = json.loads(Path(out_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"rules": None, "sets": {}, "marked": [], "unmarked": []}
    found.setdefault("marked", [])
    found.setdefault("unmarked", [])
    return found


def read_overrides(work_dir):
    path = Path(work_dir) / "moving_overrides.json" if work_dir else None
    if not path or not path.exists():
        return {"marked": [], "unmarked": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"marked": [], "unmarked": []}
    data.setdefault("marked", [])
    data.setdefault("unmarked", [])
    return data


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
        if known.get("ids") == key and "motion_frac" in known:
            v = verdict(known["counts"], known["shots"], known.get("motion_frac", 0.0))
            sets[c["set_id"]] = {**v, "ids": key}
            continue
        paths = [thumb_path(cache_dir, a) for a in c["asset_ids"]]
        per_frame = [frame_detections(detect, p) for p in paths if p.exists()]
        sets[c["set_id"]] = {**judge(per_frame), "ids": key}
    result = {
        "rules": rules(),
        "sets": {**old, **sets},
        "marked": before["marked"],
        "unmarked": before.get("unmarked", []),
    }
    write_atomic(out_file, json.dumps(result, indent=1))
    log(f"{sum(v['moving'] for v in sets.values())} of {len(sets)} sets have a moving subject")
    return result


def mark(out_file, set_ids, moving=True):
    found = read(out_file)
    marked = set(found["marked"])
    unmarked = set(found["unmarked"])
    if moving:
        marked |= set(set_ids)
        unmarked -= set(set_ids)
    else:
        marked -= set(set_ids)
        unmarked |= set(set_ids)
    found["marked"] = sorted(marked)
    found["unmarked"] = sorted(unmarked)
    write_atomic(out_file, json.dumps(found, indent=1))
    return found


def write_override(work_dir, set_id, choice):
    """choice: auto | loop | splat — stored in work/moving_overrides.json on the review page."""
    path = Path(work_dir) / "moving_overrides.json"
    data = read_overrides(work_dir)
    marked, unmarked = set(data["marked"]), set(data["unmarked"])
    marked.discard(set_id)
    unmarked.discard(set_id)
    if choice == "loop":
        marked.add(set_id)
    elif choice == "splat":
        unmarked.add(set_id)
    write_atomic(path, json.dumps({"marked": sorted(marked), "unmarked": sorted(unmarked)}, indent=2))
    return {"set": set_id, "moving": choice}


def moving_sets(out_file, work_dir=None):
    found = read(out_file) if Path(out_file).exists() else {"sets": {}, "marked": [], "unmarked": []}
    auto = {k for k, v in found.get("sets", {}).items() if v.get("moving")}
    marked = set(found.get("marked", []))
    unmarked = set(found.get("unmarked", []))
    if work_dir:
        ov = read_overrides(work_dir)
        marked |= set(ov.get("marked", []))
        unmarked |= set(ov.get("unmarked", []))
    return (auto | marked) - unmarked


def moving_choice(out_file, work_dir, set_id):
    """Current override for the review page: auto, loop, or splat."""
    if work_dir:
        ov = read_overrides(work_dir)
        if set_id in ov.get("marked", []):
            return "loop"
        if set_id in ov.get("unmarked", []):
            return "splat"
    found = read(out_file) if Path(out_file).exists() else {"sets": {}}
    if found.get("sets", {}).get(set_id, {}).get("moving"):
        return "auto-loop"
    return "auto"
