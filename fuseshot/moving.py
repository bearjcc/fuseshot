"""Find sets where the subject itself moves: people, animals, vehicles.

A splat needs a still scene; a bird hopping between shots or a plane crossing the
sky leaves floaters and a smeared subject. These sets read better as a loop.

Each thumbnail goes through EfficientDet-Lite2 (Apache-2.0, run with MediaPipe).
Detections are only a hint: a class must cover enough of the frame in enough shots.
Automatic loop routing needs that class to move relative to the camera, estimated
from neighbouring frames with a homography on the background (cached verify matches
or fresh ORB matches on the thumbnails). If that motion cannot be measured reliably,
only presence is recorded and splat eligibility is left unchanged.

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

VERSION = 3
MIN_SCORE = 0.3
MIN_BOX = 0.02
MIN_FRAMES = 0.4
MIN_INDEPENDENT = 0.015
MIN_INDEPENDENT_FRAC = 0.45
MIN_RELIABLE_FRAC = 0.4
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
        "min_independent": MIN_INDEPENDENT,
        "min_independent_frac": MIN_INDEPENDENT_FRAC,
        "min_reliable_frac": MIN_RELIABLE_FRAC,
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


def eligible_pairs(boxes):
    return sum(1 for i in range(len(boxes) - 1) if boxes[i] and boxes[i + 1])


def warp_norm(cx, cy, H, w, h):
    import cv2
    import numpy as np

    if H is None or w <= 0 or h <= 0:
        return None
    pt = np.float64([[[cx * w, cy * h]]])
    out = cv2.perspectiveTransform(pt, H)[0, 0]
    return float(out[0] / w), float(out[1] / h)


def independent_motion_stats(per_frame, class_name, poses):
    """How often the class moves beyond what the homography predicts between neighbours."""
    boxes = lead_box(per_frame, class_name)
    reliable = independent = 0
    for i in range(len(boxes) - 1):
        if boxes[i] is None or boxes[i + 1] is None:
            continue
        pose = poses[i] if i < len(poses) else None
        if not pose or not pose.get("reliable"):
            continue
        pred = warp_norm(boxes[i]["cx"], boxes[i]["cy"], pose["H"], pose["w"], pose["h"])
        if pred is None:
            continue
        reliable += 1
        residual = math.hypot(boxes[i + 1]["cx"] - pred[0], boxes[i + 1]["cy"] - pred[1])
        if residual >= MIN_INDEPENDENT:
            independent += 1
    return reliable, (independent / reliable if reliable else 0.0)


def min_reliable_pairs(boxes):
    eligible = eligible_pairs(boxes)
    if eligible < 2:
        return eligible or 2
    return max(2, math.ceil(MIN_RELIABLE_FRAC * eligible))


def judge(per_frame, poses=None):
    """per_frame: detections per shot; poses: background motion between neighbours."""
    counts = {}
    for frame in per_frame:
        for b in frame:
            name = b["class"]
            counts[name] = counts.get(name, 0) + 1
    shots = len(per_frame)
    top_class = max(counts.items(), key=lambda kv: kv[1], default=(None, 0))[0]
    poses = poses or []
    reliable, independent_frac = independent_motion_stats(per_frame, top_class, poses) if top_class else (0, 0.0)
    return verdict(counts, shots, per_frame, top_class, reliable, independent_frac)


def verdict(counts, shots, per_frame, subject, reliable_pairs, independent_frac):
    top = max(counts.items(), key=lambda kv: kv[1], default=(None, 0))
    presence = shots > 0 and top[1] >= MIN_FRAMES * shots
    boxes = lead_box(per_frame, subject) if subject else []
    need = min_reliable_pairs(boxes)
    uncertain = presence and reliable_pairs < need
    moving = (
        presence
        and not uncertain
        and reliable_pairs >= need
        and independent_frac >= MIN_INDEPENDENT_FRAC
    )
    return {
        "moving": moving,
        "subject": subject if moving else None,
        "shots": shots,
        "counts": counts,
        "presence_only": presence and not moving,
        "uncertain": uncertain,
        "independent_frac": round(float(independent_frac), 3),
        "reliable_pairs": reliable_pairs,
        "reliable_needed": need,
    }


def pair_poses(indices, greys, cache_dir, asset_ids):
    """Homography from each frame onto the next, from verify cache or ORB on thumbnails."""
    from .action import load_matches, match_thumbs, pair_homography, MIN_INLIERS, MIN_INLIER_SHARE

    matches = load_matches(cache_dir, asset_ids)
    poses = []
    for a, b in zip(indices[:-1], indices[1:]):
        H, count, inliers = (None, 0, 0)
        if matches is not None:
            H, count, inliers = pair_homography(matches, a, b)
        grey_a, grey_b = greys[len(poses)], greys[len(poses) + 1]
        if H is None and grey_a is not None and grey_b is not None:
            H, count, inliers = match_thumbs(grey_a, grey_b)
        reliable = H is not None and inliers >= MIN_INLIERS and inliers / max(count, 1) >= MIN_INLIER_SHARE
        h, w = (grey_a.shape[:2] if grey_a is not None else (0, 0))
        poses.append({"H": H, "reliable": reliable, "w": int(w), "h": int(h)})
    return poses


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
    from .action import ids_key, load_sets, thumb_path, read_grey

    before = read(out_file)
    old = before["sets"] if before.get("rules") == rules() else {}
    detect = detector().detect
    sets = {}
    for c in load_sets(candidates_file):
        if set_ids and c["set_id"] not in set_ids:
            continue
        key = ids_key(c)
        known = old.get(c["set_id"], {})
        if known.get("ids") == key and "independent_frac" in known:
            sets[c["set_id"]] = {**known, "ids": key}
            continue
        paths = [thumb_path(cache_dir, a) for a in c["asset_ids"]]
        indices = [i for i, p in enumerate(paths) if p.exists()]
        greys = [read_grey(paths[i]) for i in indices]
        per_frame = [frame_detections(detect, paths[i]) for i in indices]
        poses = pair_poses(indices, greys, cache_dir, c["asset_ids"]) if len(indices) > 1 else []
        sets[c["set_id"]] = {**judge(per_frame, poses), "ids": key}
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
