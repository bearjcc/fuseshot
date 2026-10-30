"""Find group photos and merge the best version of each face into one shot.

A group set is a short run of photos of several people from a camera that stays
put. Such sets make poor splats and object scans (the camera hardly moves and the
people do), so they are checked before the action, object scan and splat
classes. The check uses the cached grey Immich previews and verify matches:

1. The run is short: a median gap of at most MAX_MEDIAN_GAP_S between shots and
   a span of at most MAX_SPAN_S, and it is not an exposure bracket.
2. At least MIN_RUN neighbouring shots in a row, and at least MIN_RUN_SHARE of
   the set, line up under one homography (fit of at least MIN_FIT) with the frame
   corners moving less than MAX_SHIFT of the long side. Only that run is merged.
   The run must hold still as a whole: chained together, its frame centres stay
   within MAX_RUN_DRIFT of the frame width and its frames cover less than
   MAX_RUN_COVERAGE of the largest frame. A slow pan passes the neighbour test
   but fails this one.
3. At least MIN_FACE_SHARE of the shots in that run show MIN_FACES or more faces
   (OpenCV's YuNet detector, faces at least MIN_FACE_PX on the 1024 px preview).
4. The rest of the set adds no panorama coverage: chaining neighbouring shots,
   no stretch covers MAX_SET_COVERAGE or more of its largest frame.
4. The faces stay put: once one shot is warped onto the next, at least
   MIN_STEADY_SHARE of the faces land within half a face width of a face in the
   next shot. A pair of people running or jumping fails this and stays action.

The merge works on the originals, scaled to WORK_SIZE on the long side:

- Every shot is registered to the base shot on its background (action.register).
- Faces are found with YuNet, and the same person is followed from shot to shot
  by position after registration together with an SFace embedding.
- Each face is scored on open eyes, smile and head pose (MediaPipe Face
  Landmarker blendshapes and transform), sharpness and exposure of the aligned
  face crop. A softer donor face is never swapped in.
- The base is the shot whose faces score best overall. For every other person
  the best version is moved onto the base: the donor shot is warped by the
  background homography plus a similarity fit of the five face landmarks, then
  blended in over the face and a margin around it (FACE_MASK) with Poisson
  cloning. A swap is dropped when the landmark fit or the match around the
  edge of the patch is poor.

Results go to work/groups/<set>/: merged.jpg, base.jpg, overlay.jpg (face boxes
labelled with the shot they came from), faces/ crops for the review page and
group.json, which records the source shot of every face and marks the image as
a composite for any later upload. choices.json holds the faces picked by hand on
the review page; a re-merge reuses analysis.json and only redoes the blending.
"""

import hashlib
import importlib.util
import json
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path

import cv2
import numpy as np

from . import action, assets, log
from .files import write_atomic

LOG = log.get(__name__)

MIN_FACES = 2
MIN_FACE_SHARE = 0.6
MIN_FACE_PX = 12
FACE_SCORE = 0.75
MAX_SHIFT = 0.15
MIN_FIT = 0.35
MIN_RUN = 3
MIN_RUN_SHARE = 0.5
MIN_STEADY_SHARE = 0.7
MAX_RUN_DRIFT = 0.25
MAX_RUN_COVERAGE = 1.25
MAX_SET_COVERAGE = 1.5
MAX_MEDIAN_GAP_S = 10.0
MAX_SPAN_S = 120.0
MIN_SHOTS = 2
WORK_SIZE = 3000
REGISTER_SIZE = 1500
MAX_FRAMES = 12
MIN_TRACK_SHARE = 0.5
SAME_PERSON = 0.30
SWAP_GAIN = 0.10
MAX_LANDMARK_ERROR = 0.12
MAX_EDGE_ERROR = 0.14
MIN_SHARP_RATIO = 0.7
FACE_MASK = (0.62, 0.8)
MAX_SCALE_CHANGE = 0.15
MAX_TURN_DEG = 12
METHOD = ("best-shot face merge: YuNet faces, SFace identities, MediaPipe Face Landmarker "
          "eyes and pose, background homography plus landmark similarity, Poisson blend")
MODELS = {"detector": "yunet", "recogniser": "sface", "landmarker": "face-landmarker"}


def rules():
    return {"min_faces": MIN_FACES, "min_face_share": MIN_FACE_SHARE, "min_face_px": MIN_FACE_PX,
            "face_score": FACE_SCORE, "max_shift": MAX_SHIFT, "min_fit": MIN_FIT,
            "min_run": MIN_RUN, "min_run_share": MIN_RUN_SHARE, "min_steady_share": MIN_STEADY_SHARE,
            "max_run_drift": MAX_RUN_DRIFT, "max_run_coverage": MAX_RUN_COVERAGE,
            "max_set_coverage": MAX_SET_COVERAGE,
            "max_median_gap_s": MAX_MEDIAN_GAP_S, "max_span_s": MAX_SPAN_S}


@contextmanager
def quiet_opencv():
    # Newer OpenCV warns that the DNN engine ignores the (default) target; it is harmless.
    cv_log = getattr(getattr(cv2, "utils", None), "logging", None)
    level = cv_log.getLogLevel() if cv_log else None
    if cv_log:
        cv_log.setLogLevel(cv_log.LOG_LEVEL_ERROR)
    try:
        yield
    finally:
        if cv_log:
            cv_log.setLogLevel(level)


@contextmanager
def quiet_native_stderr():
    """Hide what native libraries print straight to stderr (MediaPipe's start-up notes)."""
    try:
        saved = os.dup(2)
    except OSError:
        yield
        return
    try:
        with open(os.devnull, "w") as null:
            os.dup2(null.fileno(), 2)
            yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)


def detector(size=(320, 320), min_score=FACE_SCORE):
    with quiet_opencv():
        return cv2.FaceDetectorYN.create(str(assets.model(MODELS["detector"])), "", size, min_score, 0.3, 5000)


def find_faces(det, image, min_px):
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    h, w = image.shape[:2]
    det.setInputSize((w, h))
    _, faces = det.detect(image)
    if faces is None:
        return np.zeros((0, 15), np.float32)
    return faces[np.minimum(faces[:, 2], faces[:, 3]) >= min_px]


def preview(cache_dir, asset_id):
    """Colour preview when one is cached, else the grey one verify uses."""
    colour = Path(cache_dir) / "colour" / asset_id[:2] / f"{asset_id}.jpg"
    path = colour if colour.exists() else action.thumb_path(cache_dir, asset_id)
    return cv2.imread(str(path), cv2.IMREAD_COLOR) if path.exists() else None


def steady_share(faces_a, faces_b, H, scale_a, scale_b):
    """Share of faces in one shot that land on a face in the next once warped."""
    if not len(faces_a):
        return None
    centres = (faces_a[:, :2] + faces_a[:, 2:4] / 2) * scale_a
    moved = cv2.perspectiveTransform(centres[None].astype(np.float64), H)[0]
    if not len(faces_b):
        return 0.0
    targets = (faces_b[:, :2] + faces_b[:, 2:4] / 2) * scale_b
    sizes = faces_a[:, 2] * scale_a
    near = np.linalg.norm(moved[:, None] - targets[None], axis=2).min(axis=1)
    return float(np.mean(near <= 0.5 * sizes))


def check(candidate, cache_dir, det, immich=None):
    """Group check for one set. Uses the cached matches and previews."""
    ids = candidate["asset_ids"]
    ids = ids if isinstance(ids, list) else ids.split()
    out = {"set_id": candidate["set_id"], "group": False}
    gap, span = candidate.get("median_gap_s") or 0, candidate.get("span_s") or 0
    if candidate.get("bracket") or candidate.get("verified_type") == "bracket":
        out["reason"] = "exposure bracket"
        return out
    if gap > MAX_MEDIAN_GAP_S or span > MAX_SPAN_S:
        out["reason"] = f"too slow ({gap:.0f} s gaps over {span:.0f} s)"
        return out
    frames = list(range(len(ids)))
    matches = action.load_matches(cache_dir, ids)
    if matches is None:
        if immich is not None:
            action.fetch_thumbs(immich, cache_dir, ids)
        matches = action.NO_MATCHES
    thumbs = {i: action.read_grey(action.thumb_path(cache_dir, ids[i])) for i in frames}
    if sum(t is not None for t in thumbs.values()) < MIN_SHOTS:
        out["reason"] = "previews missing"
        return out
    kept, pairs, copies = action.frame_pairs(matches, frames, thumbs)
    faces, scales = {}, {}
    for i in kept:
        image = preview(cache_dir, ids[i])
        if image is None or thumbs[i] is None:
            continue
        scales[i] = thumbs[i].shape[1] / image.shape[1]
        faces[i] = find_faces(det, image, MIN_FACE_PX / scales[i])
    still = [r for r in pairs if "H" in r and r["shift"] <= MAX_SHIFT and r["fit"] >= MIN_FIT]
    run = longest_run(kept, still)
    counts = [len(faces[i]) if i in faces else None for i in kept]
    seen = [len(faces[i]) for i in run if i in faces]
    face_share = sum(c >= MIN_FACES for c in seen) / max(1, len(seen))
    steady = []
    for r in still:
        a, b = r["pair"]
        if a in run and b in run and a in faces and b in faces and len(faces[a]) >= MIN_FACES:
            share = steady_share(faces[a], faces[b], np.reshape(r["H"], (3, 3)), scales[a], scales[b])
            if share is not None:
                steady.append(share)
    steady_mean = float(np.mean(steady)) if steady else 0.0
    size = next(t.shape[::-1] for t in thumbs.values() if t is not None)
    linked = {tuple(r["pair"]): r for r in pairs if "H" in r and r["fit"] >= MIN_FIT}
    drift, run_cover = spread(chains(run, linked)[0], size)
    set_cover = max(spread(c, size)[1] for c in chains(kept, linked))
    out.update(shots=len(kept), copies=copies, frames=run, faces=counts, face_share=round(face_share, 2),
               still_run=len(run), steady_share=round(steady_mean, 2), run_drift=round(drift, 2),
               run_coverage=round(run_cover, 2), set_coverage=round(set_cover, 2))
    reasons = []
    if drift > MAX_RUN_DRIFT or run_cover >= MAX_RUN_COVERAGE:
        reasons.append(f"camera pans across the run ({drift:.0%} drift, {run_cover:.2f} frames covered)")
    if set_cover >= MAX_SET_COVERAGE:
        reasons.append(f"shots cover {set_cover:.2f} frames, a panorama")
    if len(run) < min(MIN_RUN, max(MIN_SHOTS, len(kept))) or len(run) < MIN_RUN_SHARE * len(kept):
        reasons.append(f"camera still for only {len(run)} of {len(kept)} shots in a row")
    if face_share < MIN_FACE_SHARE:
        reasons.append(f"{face_share:.0%} of still shots show {MIN_FACES}+ faces")
    if steady_mean < MIN_STEADY_SHARE and face_share >= MIN_FACE_SHARE:
        reasons.append(f"only {steady_mean:.0%} of faces stay put")
    if reasons:
        out["reason"] = "; ".join(reasons)
        return out
    median = int(np.median(seen))
    out.update(group=True, reason=f"{median} faces in {len(run)} of {len(kept)} shots from a still camera, "
                                  f"{steady_mean:.0%} of faces stay put")
    return out


def chains(frames, linked):
    """Homographies into the first frame of each unbroken stretch of linked neighbours."""
    out = [{frames[0]: np.eye(3)}]
    current = np.eye(3)
    for a, b in zip(frames, frames[1:]):
        r = linked.get((a, b))
        if r is None:
            out.append({b: np.eye(3)})
            current = np.eye(3)
            continue
        current = current @ np.linalg.inv(np.reshape(r["H"], (3, 3)))
        out[-1][b] = current
    return out


def spread(chain, size, grid=400):
    """Drift of frame centres (share of frame width) and union area over the largest frame."""
    w, h = size
    corners = np.float64([[[0, 0], [w, 0], [w, h], [0, h]]])
    polys = [cv2.perspectiveTransform(corners, H)[0] for H in chain.values()]
    if len(polys) < 2:
        return 0.0, 1.0
    pts = np.vstack(polys)
    low, high = pts.min(0), pts.max(0)
    if not np.isfinite(pts).all() or (high - low).max() > 20 * max(w, h):
        return 0.0, 1.0
    k = grid / max(w, h)
    cols, rows = (np.ceil((high - low) * k).astype(int) + 2)
    mask = np.zeros((rows, cols), np.uint8)
    for p in polys:
        cv2.fillPoly(mask, [np.round((p - low) * k).astype(np.int32)], 1)
    largest = max(abs(cv2.contourArea(p.astype(np.float32))) for p in polys)
    centres = [p.mean(0) for p in polys]
    drift = max(np.linalg.norm(c - centres[0]) for c in centres) / w
    return float(drift), float(mask.sum() / (k * k) / max(largest, 1.0))


def longest_run(kept, still):
    """Longest stretch of neighbouring shots joined by still pairs (a single shot if none)."""
    linked = {tuple(r["pair"]) for r in still}
    best, current = kept[:1], kept[:1]
    for a, b in zip(kept, kept[1:]):
        current = current + [b] if (a, b) in linked else [b]
        if len(current) > len(best):
            best = current
    return best


def _ids_key(candidate):
    ids = candidate["asset_ids"]
    ids = ids if isinstance(ids, list) else ids.split()
    return hashlib.sha1(" ".join(map(str, ids)).encode()).hexdigest()[:16]


def scan(candidates_file, cache_dir, out_file, immich=None, log=LOG.info):
    """Run the group check over every candidate set and burst-mode run. Sets already checked with the same
    photos and rules are kept from the last scan; the file is saved as it goes, so a stopped scan resumes."""
    from .progress import Progress

    sets_in = action.load_sets(candidates_file)
    started = time.time()
    old = {}
    if Path(out_file).exists():
        before = json.loads(Path(out_file).read_text(encoding="utf-8"))
        if before.get("rules") == rules():
            old = before.get("sets", {})
    sets = {}
    det = None

    def save():
        result = {"rules": rules(), "sets": sets, "seconds": round(time.time() - started, 1),
                  "made": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        write_atomic(out_file, json.dumps(result, indent=1))
        return result

    todo = []
    for c in sets_in:
        key = _ids_key(c)
        if old.get(c["set_id"], {}).get("ids") == key:
            sets[c["set_id"]] = old[c["set_id"]]
        else:
            todo.append((c, key))
    if todo:
        det = detector()
        with Progress("looking for group photos", len(todo), unit="sets") as bar:
            for n, (c, key) in enumerate(todo, 1):
                sets[c["set_id"]] = dict(check(c, cache_dir, det, immich), ids=key)
                bar.update()
                if n % 200 == 0:
                    save()
    result = save()
    log(f"{sum(bool(v.get('group')) for v in sets.values())} of {len(sets)} sets look like group photos "
        f"({len(todo)} checked now, {time.time() - started:.0f} s)")
    return result


def load_groups(groups_file):
    path = Path(groups_file)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("sets", {})


def is_group(set_id, groups_file, types_file=None):
    """True when the scan or a type chosen on the review page makes the set a group."""
    if types_file and Path(types_file).exists():
        chosen = json.loads(Path(types_file).read_text(encoding="utf-8")).get(set_id, {}).get("type")
        if chosen:
            return chosen == "group"
    return bool(load_groups(groups_file).get(set_id, {}).get("group"))


# Merge

class Models:
    def __init__(self):
        try:
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions, vision
        except ImportError:
            from .jobs import extra_hint

            raise RuntimeError("merging group photos needs mediapipe: " + extra_hint("group", "mediapipe")) from None

        self.mp = mp
        self.detector = detector((640, 640), 0.7)
        with quiet_opencv():
            self.recogniser = cv2.FaceRecognizerSF.create(str(assets.model(MODELS["recogniser"])), "")
        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(assets.model(MODELS["landmarker"]))),
            output_face_blendshapes=True, output_facial_transformation_matrixes=True, num_faces=1,
            min_face_detection_confidence=0.3, min_face_presence_confidence=0.3)
        # The first detection prints the rest of MediaPipe's start-up notes, so run it on a blank image.
        with quiet_native_stderr():
            self.landmarker = vision.FaceLandmarker.create_from_options(options)
            blank = np.zeros((64, 64, 3), np.uint8)
            self.landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=blank))

    def close(self):
        # Closing here rather than at interpreter exit avoids a noisy error from MediaPipe.
        self.landmarker.close()

    def eyes_and_pose(self, image, face):
        """Eye openness 0..1, smile 0..1 and head turn in degrees from MediaPipe, or Nones."""
        x, y, w, h = face[:4]
        m = 0.8 * max(w, h)
        x0, y0 = int(max(0, x - m)), int(max(0, y - m))
        x1, y1 = int(min(image.shape[1], x + w + m)), int(min(image.shape[0], y + h + m))
        crop = image[y0:y1, x0:x1]
        if crop.size == 0:
            return None, None, None
        scale = 480 / max(crop.shape[:2])
        crop = cv2.resize(crop, None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA)
        rgb = np.ascontiguousarray(crop[:, :, ::-1])
        found = self.landmarker.detect(self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb))
        if not found.face_blendshapes:
            return None, None, None
        shapes = {b.category_name: b.score for b in found.face_blendshapes[0]}
        blink = max(shapes.get("eyeBlinkLeft", 0), shapes.get("eyeBlinkRight", 0))
        eyes = float(np.clip((0.75 - blink) / 0.5, 0, 1))
        smile = (shapes.get("mouthSmileLeft", 0) + shapes.get("mouthSmileRight", 0)) / 2
        smile = float(np.clip(smile - max(0.0, shapes.get("jawOpen", 0) - smile - 0.3), 0, 1))
        turn = None
        if found.facial_transformation_matrixes:
            R = np.asarray(found.facial_transformation_matrixes[0])[:3, :3]
            R = R / np.linalg.norm(R[:, 2])
            turn = float(math.degrees(math.acos(float(np.clip(R[2, 2], -1, 1)))))
        return eyes, smile, turn


def landmark_turn(face):
    """Rough head turn from YuNet's five points: nose offset sideways from between the eyes."""
    right_eye, left_eye, nose = face[4:6], face[6:8], face[8:10]
    span = np.linalg.norm(left_eye - right_eye) + 1e-6
    return float(min(60.0, 90 * abs(nose[0] - (right_eye[0] + left_eye[0]) / 2) / span))


def crop_quality(aligned):
    grey = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
    inner = grey[24:96, 20:92]
    sharp = float(cv2.Laplacian(inner, cv2.CV_64F).var())
    mean = float(inner.mean()) / 255
    clipped = float(np.mean((inner < 8) | (inner > 247)))
    exposure = float(np.clip(1 - 2 * abs(mean - 0.5) - 2 * clipped, 0, 1))
    return sharp, exposure


def load_frames(manifest, size=WORK_SIZE):
    images = []
    for entry in manifest:
        image = cv2.imdecode(np.fromfile(entry["image"], np.uint8), cv2.IMREAD_COLOR)
        scale = size / max(image.shape[:2])
        if scale < 1:
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        images.append(image)
    h, w = images[len(images) // 2].shape[:2]
    return [im if im.shape[:2] == (h, w) else cv2.resize(im, (w, h), interpolation=cv2.INTER_AREA)
            for im in images]


def register_all(images, reference):
    scale = min(1.0, REGISTER_SIZE / max(images[0].shape[:2]))
    small = [cv2.resize(im, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) for im in images]
    S = np.diag([scale, scale, 1.0])
    found = action.register(small, reference)
    return [None if H is None else np.linalg.inv(S) @ H @ S for H in found]


def pick_frames(frames, limit=MAX_FRAMES):
    if len(frames) <= limit:
        return frames
    picks = np.linspace(0, len(frames) - 1, limit).round().astype(int)
    return [frames[i] for i in sorted(set(picks))]


def analyse(images, models, log=LOG.info):
    """Faces, identities and scores for every frame, plus homographies to the middle frame."""
    started = time.time()
    min_px = max(20, round(max(images[0].shape[:2]) * 0.012))
    reference = len(images) // 2
    homographies = register_all(images, reference)
    faces = []
    for n, image in enumerate(images):
        found = find_faces(models.detector, image, min_px) if homographies[n] is not None else []
        for face in found:
            aligned = models.recogniser.alignCrop(image, face)
            feature = models.recogniser.feature(aligned)[0]
            sharp, exposure = crop_quality(aligned)
            eyes, smile, turn = models.eyes_and_pose(image, face)
            centre = (face[:2] + face[2:4] / 2).astype(np.float64)
            mapped = cv2.perspectiveTransform(centre[None, None], homographies[n])[0, 0]
            faces.append({"frame": n, "box": [round(float(v), 1) for v in face[:4]],
                          "points": [round(float(v), 1) for v in face[4:14]], "det": round(float(face[14]), 3),
                          "centre": [round(float(v), 1) for v in mapped], "feature": feature,
                          "sharp": round(sharp, 1), "exposure": round(exposure, 3),
                          "eyes": None if eyes is None else round(eyes, 3),
                          "smile": None if smile is None else round(smile, 3),
                          "turn": round(turn if turn is not None else landmark_turn(face), 1),
                          "pose_from": "mediapipe" if turn is not None else "yunet"})
    tracks = track(faces, len(images))
    for f in faces:
        f.pop("feature")
    log(f"  {len(faces)} faces in {len(images)} frames, {len(tracks)} people, {time.time() - started:.0f} s")
    return {"reference": reference, "size": [images[0].shape[1], images[0].shape[0]],
            "homographies": [None if H is None else [round(float(v), 8) for v in H.ravel()]
                             for H in homographies],
            "faces": faces, "tracks": tracks, "seconds": round(time.time() - started, 1)}


def track(faces, frame_count):
    """Group faces into people: close together after registration and alike to SFace."""
    order = sorted(range(len(faces)), key=lambda k: (faces[k]["frame"], faces[k]["box"][0]))
    people = []
    for k in order:
        f = faces[k]
        best, best_cost = None, None
        for p in people:
            if any(faces[m]["frame"] == f["frame"] for m in p["members"]):
                continue
            size = np.mean([faces[m]["box"][2] for m in p["members"]])
            dist = np.linalg.norm(np.array(f["centre"]) - p["centre"]) / size
            feature = p["feature"] / (np.linalg.norm(p["feature"]) + 1e-9)
            alike = float(np.dot(feature, f["feature"] / (np.linalg.norm(f["feature"]) + 1e-9)))
            if dist > 1.5 or (dist > 0.6 and alike < 0.45) or alike < SAME_PERSON - 0.1:
                continue
            cost = dist - alike
            if best_cost is None or cost < best_cost:
                best, best_cost = p, cost
        if best is None:
            people.append({"members": [k], "centre": np.array(f["centre"]), "feature": f["feature"].copy()})
        else:
            best["members"].append(k)
            n = len(best["members"])
            best["centre"] = best["centre"] + (np.array(f["centre"]) - best["centre"]) / n
            best["feature"] = best["feature"] + f["feature"]
    keep = [p for p in people if len(p["members"]) >= max(2, MIN_TRACK_SHARE * frame_count)]
    keep.sort(key=lambda p: p["centre"][0])
    return [{"id": n, "faces": sorted(p["members"], key=lambda m: faces[m]["frame"])}
            for n, p in enumerate(keep)]


def face_scores(analysis):
    """Score every face in its track: eyes, facing, relative sharpness, exposure, smile."""
    faces = analysis["faces"]
    for t in analysis["tracks"]:
        best_sharp = max(faces[m]["sharp"] for m in t["faces"]) or 1.0
        for m in t["faces"]:
            f = faces[m]
            eyes = 0.5 if f["eyes"] is None else f["eyes"]
            facing = float(np.clip(1 - f["turn"] / 40, 0, 1))
            sharp = math.sqrt(f["sharp"] / best_sharp)
            smile = 0.5 if f.get("smile") is None else f["smile"]
            f["score"] = round(0.4 * eyes + 0.2 * facing + 0.2 * sharp + 0.1 * f["exposure"]
                               + 0.1 * smile, 3)


def choose_base(analysis, frame_count):
    faces = analysis["faces"]
    totals = np.zeros(frame_count)
    for t in analysis["tracks"]:
        for m in t["faces"]:
            totals[faces[m]["frame"]] += faces[m]["score"]
    return int(np.argmax(totals))


def homography(analysis, frame, base):
    """Frame onto base, through the middle frame both were registered to."""
    Hs = analysis["homographies"]
    if Hs[frame] is None or Hs[base] is None:
        return None
    return np.linalg.inv(np.reshape(Hs[base], (3, 3))) @ np.reshape(Hs[frame], (3, 3))


def head_box(box, shape):
    x, y, w, h = box
    x0, y0 = int(max(0, x - 0.55 * w)), int(max(0, y - 0.7 * h))
    x1, y1 = int(min(shape[1], x + 1.55 * w)), int(min(shape[0], y + 1.45 * h))
    return x0, y0, x1, y1


def swap(base_image, donor_image, base_face, donor_face, H, base_others=(), donor_others=()):
    """Blend the donor's face, with a margin around it, onto the base. Returns (image, info) or (None, reason)."""
    base_pts = np.reshape(base_face["points"], (5, 2))
    donor_pts = cv2.perspectiveTransform(np.reshape(donor_face["points"], (1, 5, 2)).astype(np.float64), H)[0]
    A, _ = cv2.estimateAffinePartial2D(donor_pts, base_pts, method=cv2.LMEDS)
    if A is None:
        return None, "no landmark fit"
    scale = math.hypot(A[0, 0], A[1, 0])
    turn = abs(math.degrees(math.atan2(A[1, 0], A[0, 0])))
    fitted = donor_pts @ A[:, :2].T + A[:, 2]
    eye_span = np.linalg.norm(base_pts[0] - base_pts[1]) + 1e-6
    error = float(np.mean(np.linalg.norm(fitted - base_pts, axis=1)) / eye_span)
    if abs(scale - 1) > MAX_SCALE_CHANGE or turn > MAX_TURN_DEG:
        return None, f"face moved too much (scale {scale:.2f}, turn {turn:.0f} deg)"
    if error > MAX_LANDMARK_ERROR:
        return None, f"landmark fit off by {error:.0%} of the eye span"
    x0, y0, x1, y1 = head_box(base_face["box"], base_image.shape)
    w, h = x1 - x0, y1 - y0
    M = np.vstack([A, [0, 0, 1]]) @ H
    T = np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1]], np.float64)
    patch = cv2.warpPerspective(donor_image, T @ M, (w, h), flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_REFLECT)
    valid = cv2.warpPerspective(np.full(donor_image.shape[:2], 255, np.uint8), T @ M, (w, h),
                                flags=cv2.INTER_NEAREST)
    bx, by, bw, bh = base_face["box"]
    centre = (int(bx + bw / 2 - x0), int(by + bh * 0.5 - y0))
    axes = (int(bw * FACE_MASK[0]), int(bh * FACE_MASK[1]))
    mask = np.zeros((h, w), np.uint8)
    cv2.ellipse(mask, centre, axes, 0, 0, 360, 255, -1)
    mask[:2], mask[-2:], mask[:, :2], mask[:, -2:] = 0, 0, 0, 0
    mask &= cv2.erode(valid, np.ones((5, 5), np.uint8))
    ellipse = mask.copy()
    # keep the neighbours' heads out of the patch, as they stand in either shot
    heads = [box for box in base_others]
    for box in donor_others:
        x, y, bw2, bh2 = box
        corners = np.array([[[x, y], [x + bw2, y], [x, y + bh2], [x + bw2, y + bh2]]], np.float64)
        mapped = cv2.perspectiveTransform(corners, M)[0]
        lo, hi = mapped.min(axis=0), mapped.max(axis=0)
        heads.append([lo[0], lo[1], hi[0] - lo[0], hi[1] - lo[1]])
    for x, y, bw2, bh2 in heads:
        cv2.rectangle(mask, (int(x - 0.3 * bw2 - x0), int(y - 0.8 * bh2 - y0)),
                      (int(x + 1.3 * bw2 - x0), int(y + 1.2 * bh2 - y0)), 0, -1)
    core = np.zeros_like(mask)
    cv2.rectangle(core, (int(bx - x0), int(by - y0)), (int(bx + bw - x0), int(by + bh - y0)), 255, -1)
    mask |= core & ellipse
    if mask.mean() < 20:
        return None, "patch falls outside the donor frame"
    target = base_image[y0:y1, x0:x1]
    ring = cv2.subtract(mask, cv2.erode(mask, np.ones((9, 9), np.uint8)))
    soften = max(3, int(bw * 0.04) | 1)
    a = cv2.GaussianBlur(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY), (soften, soften), 0).astype(np.float32)
    b = cv2.GaussianBlur(cv2.cvtColor(target, cv2.COLOR_BGR2GRAY), (soften, soften), 0).astype(np.float32)
    edge = ring > 0
    shift = float(np.median(a[edge] - b[edge]))
    edge_error = float(np.mean(np.abs(a[edge] - shift - b[edge])) / 255)
    if edge_error > MAX_EDGE_ERROR:
        return None, f"edges differ by {edge_error:.0%} after blending would start"
    ys, xs = np.nonzero(mask)
    rect_centre = (int(x0 + xs.min() + (xs.max() - xs.min() + 1) // 2),
                   int(y0 + ys.min() + (ys.max() - ys.min() + 1) // 2))
    out = cv2.seamlessClone(patch, base_image, mask, rect_centre, cv2.NORMAL_CLONE)
    return out, {"landmark_error": round(error, 3), "edge_error": round(edge_error, 3),
                 "scale": round(scale, 3), "turn_deg": round(turn, 1), "patch": [x0, y0, x1, y1]}


COLOURS = [(80, 200, 255), (120, 255, 120), (255, 160, 80), (200, 120, 255), (90, 90, 255),
           (255, 255, 90), (255, 90, 200), (160, 255, 255)]


def overlay(image, record):
    out = image.copy()
    thick = max(2, round(max(image.shape[:2]) / 800))
    for p in record["people"]:
        x, y, w, h = (int(v) for v in p["box"])
        colour = COLOURS[p["source_frame"] % len(COLOURS)]
        cv2.rectangle(out, (x, y), (x + w, y + h), colour, thick)
        label = f"{p['id']}: #{p['source_frame'] + 1}" + ("" if p["swapped"] else " base")
        if p.get("rejected"):
            label += " kept"
        cv2.putText(out, label, (x, max(12, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5 * thick, colour, thick)
    return out


def write_jpeg(path, image, quality=92):
    write_atomic(path, cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])[1].tobytes())


def face_crops(out_dir, images, analysis):
    crops = out_dir / "faces"
    crops.mkdir(exist_ok=True)
    for t in analysis["tracks"]:
        for m in t["faces"]:
            f = analysis["faces"][m]
            x0, y0, x1, y1 = head_box(f["box"], images[f["frame"]].shape)
            crop = images[f["frame"]][y0:y1, x0:x1]
            scale = 160 / max(crop.shape[:2])
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            write_jpeg(crops / f"{t['id']}_{f['frame']}.jpg", crop, 85)


def compose(out_dir, images, manifest, analysis, choices, log=LOG.info, full=False):
    """Pick the base and the best face per person, blend, and write the outputs.
    full writes only merged_full.jpg, for a merge made again at full size."""
    face_scores(analysis)
    faces = analysis["faces"]
    base = int(choices.get("base", choose_base(analysis, len(images))))
    merged = images[base].copy()
    people = []
    for t in analysis["tracks"]:
        members = {faces[m]["frame"]: faces[m] for m in t["faces"]}
        base_face = members.get(base)
        options = [{"frame": fr, "score": f["score"], "eyes": f["eyes"], "smile": f.get("smile"),
                    "turn": f["turn"], "sharp": f["sharp"]} for fr, f in sorted(members.items())]
        picked = choices.get("faces", {}).get(str(t["id"]))
        best = max(members, key=lambda fr: members[fr]["score"])
        if picked is not None and int(picked) in members:
            best = int(picked)
        elif base_face is not None and members[best]["score"] < base_face["score"] + SWAP_GAIN:
            best = base
        entry = {"id": t["id"], "box": (base_face or members[best])["box"], "options": options,
                 "chosen_by": "hand" if picked is not None else "score", "source_frame": base, "swapped": False}
        if base_face is None:
            entry["rejected"] = "not in the base frame"
        elif best != base and members[best]["sharp"] < MIN_SHARP_RATIO * base_face["sharp"]:
            entry["rejected"] = "donor face is softer than the base"
        elif best != base:
            H = homography(analysis, best, base)
            base_others = [f["box"] for f in faces if f["frame"] == base and f is not base_face]
            donor_others = [f["box"] for f in faces if f["frame"] == best and f is not members[best]]
            result, info = (None, "donor frame did not register") if H is None else swap(
                merged, images[best], base_face, members[best], H, base_others, donor_others)
            if result is None:
                entry["rejected"] = info
            else:
                merged = result
                entry.update(source_frame=best, swapped=True, blend=info)
        entry["source_asset"] = manifest[entry["source_frame"]]["asset_id"]
        people.append(entry)
    swapped = sum(p["swapped"] for p in people)
    record = {"set_id": out_dir.name, "composite": True, "method": METHOD, "base_frame": base,
              "base_asset": manifest[base]["asset_id"], "frames": [m["asset_id"] for m in manifest],
              "size": analysis["size"], "people": people, "swapped": swapped,
              "made": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "choices": choices,
              "models": {k: assets.MODELS[v]["file"] for k, v in MODELS.items()}, "rules": {"swap_gain": SWAP_GAIN, "max_landmark_error": MAX_LANDMARK_ERROR,
                                          "max_edge_error": MAX_EDGE_ERROR,
                                          "min_sharp_ratio": MIN_SHARP_RATIO}}
    if full:
        write_jpeg(out_dir / "merged_full.jpg", merged, 95)
        return record
    write_jpeg(out_dir / "base.jpg", images[base])
    write_jpeg(out_dir / "merged.jpg", merged, 94)
    write_jpeg(out_dir / "overlay.jpg", overlay(merged, record))
    write_atomic(out_dir / "group.json", json.dumps(record, indent=1))
    rejected = [p for p in people if p.get("rejected")]
    log(f"{out_dir.name}: base #{base + 1}, {len(people)} people, {swapped} faces swapped, "
        f"{len(rejected)} kept from the base")
    return record


def merge(immich, candidate, groups_file, work_dir, log=LOG.info):
    """Fetch the originals of a group set (once), analyse them (once) and compose the merge."""
    from .fetch import fetch_set

    started = time.time()
    set_id = candidate["set_id"]
    out_dir = Path(work_dir) / "groups" / set_id
    manifest_file = out_dir / "manifest.json"
    analysis_file = out_dir / "analysis.json"
    if not analysis_file.exists() and importlib.util.find_spec("mediapipe") is None:
        from .jobs import extra_hint

        raise RuntimeError("merging group photos needs mediapipe: " + extra_hint("group", "mediapipe"))
    out_dir.mkdir(parents=True, exist_ok=True)
    if not manifest_file.exists():
        ids = candidate["asset_ids"]
        ids = ids if isinstance(ids, list) else ids.split()
        frames = load_groups(groups_file).get(set_id, {}).get("frames") or list(range(len(ids)))
        subset = dict(candidate, asset_ids=[ids[i] for i in pick_frames(frames)])
        fetch_set(immich, subset, Path(work_dir) / "groups")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    images = load_frames(manifest)
    if analysis_file.exists():
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
    else:
        models = Models()
        try:
            analysis = analyse(images, models, log)
        finally:
            models.close()
        write_atomic(analysis_file, json.dumps(analysis, indent=1))
        face_crops(out_dir, images, analysis)
    choices_file = out_dir / "choices.json"
    choices = json.loads(choices_file.read_text(encoding="utf-8")) if choices_file.exists() else {}
    record = compose(out_dir, images, manifest, analysis, choices, log)
    record["seconds"] = round(time.time() - started, 1)
    write_atomic(out_dir / "group.json", json.dumps(record, indent=1))
    return record


def scaled(analysis, factor):
    """The analysis with every position scaled by factor, for frames loaded larger."""
    S = np.diag([factor, factor, 1.0])
    out = dict(analysis, size=[round(v * factor) for v in analysis["size"]])
    out["faces"] = [dict(f, box=[v * factor for v in f["box"]], points=[v * factor for v in f["points"]],
                         centre=[v * factor for v in f["centre"]]) for f in analysis["faces"]]
    out["homographies"] = [None if H is None else list((S @ np.reshape(H, (3, 3)) @ np.linalg.inv(S)).ravel())
                           for H in analysis["homographies"]]
    return out


def full_size(out_dir, log=LOG.info):
    """Make the merge again from the originals at their own resolution, as far as the
    memory budget and the megapixel cap allow, with the same base and the same face
    for every person as merged.jpg. Writes merged_full.jpg and returns it; kept
    until merged.jpg changes."""
    from . import limits

    out_dir = Path(out_dir)
    merged, target = out_dir / "merged.jpg", out_dir / "merged_full.jpg"
    if target.exists() and target.stat().st_mtime >= merged.stat().st_mtime:
        return target
    started = time.time()
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    analysis = json.loads((out_dir / "analysis.json").read_text(encoding="utf-8"))
    record = json.loads((out_dir / "group.json").read_text(encoding="utf-8"))
    width, height = middle_size(manifest)
    frames = len(manifest) + 4  # every frame, plus the merge and the blending copies
    scale = limits.full_scale(width, height, bytes_per_px=3 * frames)
    size = round(max(width, height) * scale)
    if size <= max(analysis["size"]):
        write_atomic(target, merged.read_bytes())
        return target
    images = load_frames(manifest, size)
    factor = images[0].shape[1] / analysis["size"][0]
    choices = {"base": record["base_frame"],
               "faces": {str(p["id"]): p["source_frame"] for p in record["people"]}}
    full = compose(out_dir, images, manifest, scaled(analysis, factor), choices, log=lambda m: None, full=True)
    swapped = {p["id"] for p in full["people"] if p["swapped"]}
    lost = [p["id"] for p in record["people"] if p["swapped"] and p["id"] not in swapped]
    log(f"{out_dir.name}: full size merge at {images[0].shape[1]} x {images[0].shape[0]}, "
        f"{time.time() - started:.0f} s" + (f"; people {lost} kept from the base at this size" if lost else ""))
    return target


def middle_size(manifest):
    """Width and height of the middle frame, which the others are fitted to."""
    from PIL import Image

    with Image.open(manifest[len(manifest) // 2]["image"]) as img:
        return img.size
