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

    def items(self, candidates, decisions):
        plan = self.plan_keys()
        out = []
        for set_id, c in candidates.items():
            state = self.status(c, plan.get(set_id))
            if state:
                out.append(self.item(set_id, c, state, decisions))
        return out

    def item(self, set_id, c, state, decisions):
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
            "w": 4 * max(len(strip), 1), "h": 3, "type": base_type(c) or c.get("guessed_type") or "panorama",
            "guess": c.get("guessed_type"), "checked": "verified_type" in c, "confidence": confidence,
            "frames": count, "taken": c.get("start_local"), "camera": c.get("camera"), "place": c.get("places"),
            "link": c.get("best_link") or c.get("first_link"), **library_fields(c),
            "status": state, "flags": flags, "score": 0,
            "order": STATUS_ORDER[state],
            **self.review.decision_fields(decisions, set_id, KIND),
        }

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
