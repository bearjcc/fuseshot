"""Sets found by detect that are not stitched yet, shown on the review page.

They show up once detect has written candidates.json, while the overlap check and
stitching are still going, so the unwanted ones can be discarded early. A discard
is saved as a "found" decision for the set: batch reads it before each set starts,
and a stitching task still waiting in the queue is taken off it."""

import io
import json
import threading
from pathlib import Path

from . import jobs, log

LOG = log.get(__name__)

KIND = "found"
STRIP = 4
SIDE = 320
STATUS_ORDER = {"stitching": 0, "queued": 1, "found": 2}


def strip_frames(count, n=STRIP):
    """Indexes of up to n frames spread evenly over a set of count frames."""
    if count <= n:
        return list(range(count))
    return sorted({round(i * (count - 1) / (n - 1)) for i in range(n)})


# What the full pipeline makes of each detected type, as the found card names it.
LABELS = {"action": "action loop", "group": "group photo", "object scan": "splat", "not": "not a panorama"}
# Metadata guesses that rest on something measured (exposure, GPS, focal length). The
# others only count frames, and a burst from a still camera looks the same.
MEASURED_GUESSES = ("exposure bracket", "linear walk", "multi-zoom layered")
NEUTRAL = "set"


def small_homography(first, second, need=20):
    """Homography between two small frames, or None. SIFT finds enough points on a
    thumbnail where ORB does not."""
    import cv2
    import numpy as np

    sift = cv2.SIFT_create(800)
    k1, d1 = sift.detectAndCompute(first, None)
    k2, d2 = sift.detectAndCompute(second, None)
    if d1 is None or d2 is None or len(k1) < 2 or len(k2) < 2:
        return None
    good = [m[0] for m in cv2.BFMatcher().knnMatch(d1, d2, k=2) if len(m) == 2 and m[0].distance < 0.75 * m[1].distance]
    if len(good) < need:
        return None
    p1 = np.float32([k1[m.queryIdx].pt for m in good])
    p2 = np.float32([k2[m.trainIdx].pt for m in good])
    H, mask = cv2.findHomography(p1, p2, cv2.RANSAC, 4.0)
    if H is None or mask is None or mask.sum() < need or mask.sum() < 0.35 * len(good):
        return None
    return H


def base_type(c):
    return (c.get("verified_type") or "").split(" (+")[0]


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class Found:
    """The found-set part of the review page: which sets to show, their thumbnails, and
    what a discard does to the stitching queue."""

    def __init__(self, review, library=None, plan_file=None, runner=None):
        self.review = review
        self.library = library
        self.plan_file = Path(plan_file) if plan_file else review.work_dir / "batch_plan.json"
        self.runner = runner
        self.thumbs = review.cache_dir / "found"
        self.slots = threading.BoundedSemaphore(2)
        self.threads = []
        self.guesses = {}

    def plan_keys(self):
        """The stitch plan's keys for each set it holds."""
        keys = {}
        for job in read_json(self.plan_file, {}).get("jobs", []):
            keys.setdefault(job.get("set_id", job["key"]), []).append(job["key"])
        return keys

    def status(self, c, keys):
        """found, queued or stitching while the set has no result yet; None once it has one
        or the check left it out."""
        if c.get("source") == "burst":
            return None
        if not keys:
            return "found" if "verified_type" not in c else None
        from . import batch

        work = self.review.work_dir
        if any((batch.load_record(work, k) or {}).get("status") in batch.DONE for k in keys):
            return None
        return "stitching" if any((work / k).is_dir() for k in keys) else "queued"

    def items(self, candidates, decisions, actions=None, types=None):
        plan = self.plan_keys()
        out = []
        for set_id, c in candidates.items():
            state = self.status(c, plan.get(set_id))
            if state:
                label, checked = self.what(set_id, c, actions or {}, types or {})
                out.append(self.item(set_id, c, state, decisions, label, checked))
        return out

    def what(self, set_id, c, actions, types):
        """The set's type as the classifier names it, and whether that is the checked
        answer: a type picked on the page, a group or action found by their scans, or
        the overlap check's verdict. Before those, a cheap guess, or just "set"."""
        review = self.review
        chosen = (types.get(set_id) or {}).get("type")
        group = (review.groups().get(set_id) or {}).get("group")
        action = (actions.get(set_id) or {}).get("action")
        if chosen or group or action or "verified_type" in c:
            detected = chosen or review.detected_type(set_id, c, actions)
            return (LABELS.get(detected) or base_type(c) or "panorama"), True
        return self.guess(set_id, c), False

    def guess(self, set_id, c):
        guessed = c.get("guessed_type") or ""
        measured = next((g for g in MEASURED_GUESSES if guessed.startswith(g)), None)
        if measured:
            return measured
        if set_id not in self.guesses:
            looked = self.look(c)
            if looked is None:
                return NEUTRAL
            self.guesses[set_id] = looked
        return self.guesses[set_id]

    def look(self, c):
        """A guess from the frames on the card, once their thumbnails are here: a still
        camera with something moving is an action shot, a camera that turned is a
        panorama. None while a thumbnail is missing."""
        import cv2

        from . import action

        ids = c.get("asset_ids") or []
        paths = [self.thumbs / ids[i][:2] / f"{ids[i]}.jpg" for i in strip_frames(len(ids))]
        if len(paths) < 2:
            return NEUTRAL
        if not all(p.exists() for p in paths):
            return None
        frames = [cv2.imread(str(p), cv2.IMREAD_GRAYSCALE) for p in paths]
        quick = ((c.get("median_gap_s") or 0) <= action.MAX_MEDIAN_GAP_S
                 and (c.get("span_s") or 0) <= action.MAX_SPAN_S and len(ids) >= action.MIN_RUN)
        for first, second in zip(frames, frames[1:]):
            if first is None or second is None or first.shape != second.shape:
                continue
            first, second = (cv2.resize(f, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC) for f in (first, second))
            H = small_homography(first, second)
            if H is None:
                continue
            if action.corner_shift(H, first.shape[::-1]) > action.MAX_SHIFT:
                return "panorama"
            moving = action.moving_share(first, second, H)
            if quick and moving is not None and moving >= action.MIN_MEDIAN_MOVING:
                return LABELS["action"]
            return NEUTRAL
        return NEUTRAL

    def item(self, set_id, c, state, decisions, label=NEUTRAL, checked=False):
        from .review import library_fields

        count = len(c.get("asset_ids") or [])
        frames = strip_frames(count)
        base = f"{self.review.base}/found/{set_id}"
        strip = [f"{base}/{i}" for i in frames]
        confidence = c.get("verified_confidence", c.get("confidence"))
        flags = [f"low confidence ({confidence})"] if isinstance(confidence, (int, float)) and confidence < 0.5 else []
        return {
            "key": f"{KIND}:{set_id}", "kind": KIND, "id": set_id, "href": None,
            "thumb": strip[0] if strip else None, "strip": strip, "screen": None, "full": None,
            "w": 4, "h": 3, "type": label, "checked": checked, "confidence": confidence,
            "frames": count, "taken": c.get("start_local"), "camera": c.get("camera"), "place": c.get("places"),
            "link": c.get("best_link") or c.get("first_link"), **library_fields(c),
            "status": state, "flags": flags, "score": 0,
            "order": STATUS_ORDER[state],
            **self.review.decision_fields(decisions, set_id, KIND),
        }

    def carry(self, items, decisions):
        """A set discarded while it was still stitching stays discarded when its result comes in."""
        for item in items:
            if item["kind"] != "stitch" or item.get("decision"):
                continue
            set_id = self.review.batch_record(item["id"]).get("set_id") or item["id"].split("-")[0]
            fields = self.review.decision_fields(decisions, set_id, KIND)
            if fields["decision"] == "discarded":
                item.update(fields)
        return items

    def check(self, set_id):
        """Raise KeyError unless set_id is a set detect found."""
        if not any(c["set_id"] == set_id for c in self.review.candidates()):
            raise KeyError(set_id)

    def thumb_file(self, set_id, index):
        """A small JPEG of frame index of the set, fetched from its source once and kept."""
        c = next((c for c in self.review.candidates() if c["set_id"] == set_id), None)
        ids = (c or {}).get("asset_ids") or []
        if not index.isdigit() or int(index) >= len(ids):
            raise KeyError(index)
        asset_id = ids[int(index)]
        path = self.thumbs / asset_id[:2] / f"{asset_id}.jpg"
        if path.exists():
            return path
        if self.library is None:
            raise KeyError(asset_id)
        with self.slots:
            if not path.exists():
                self.save_thumb(asset_id, path)
        return path

    def save_thumb(self, asset_id, path):
        from PIL import Image, ImageOps

        try:
            data = self.library.thumbnail(asset_id, "thumbnail")
            with Image.open(io.BytesIO(data)) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((SIDE, SIDE))
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".part")
                im.save(tmp, "JPEG", quality=82)
                tmp.replace(path)
        except Exception as err:  # noqa: BLE001 - any source failure just means no thumbnail
            LOG.debug(f"no thumbnail for {asset_id}: {err}")
            raise KeyError(asset_id) from err

    def discarded(self, set_ids):
        """Take stitching tasks for these sets off the queue if they have not started."""
        plan = self.plan_keys()
        names = [f"stitch-{k}" for s in set_ids for k in plan.get(s, [])]
        if not names:
            return
        thread = threading.Thread(target=self.cancel, args=(names,), daemon=True)
        thread.start()
        self.threads = [t for t in self.threads if t.is_alive()] + [thread]

    def cancel(self, names):
        try:
            removed = (self.runner or jobs.runner()).cancel(names)
        except Exception as err:  # noqa: BLE001 - the discard holds anyway; batch skips the set
            LOG.warning(f"could not take discarded sets off the queue: {err}")
            return []
        if removed:
            LOG.info(f"took {len(removed)} discarded set(s) off the stitching queue")
        return removed
