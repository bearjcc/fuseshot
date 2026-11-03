"""Make a new version of one stitched result with settings picked on the review page.

The review page queues this as a job (so a new alignment or a full-size render runs
on the job runner, Pueue when it is set up, and not in the page server). The version
on screen when Edit was pressed is kept in edit/previous.jpg for the B compare, and
edit/done.json tells the page the new version is ready to review again.
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, jobs, limits, log, stitch
from .files import write_atomic

LOG = log.get(__name__)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else (default if default is not None else {})


def missing_originals(set_dir):
    names = [re.split(r"[\\/]", e.get("original") or "")[-1] for e in read_json(set_dir / "manifest.json", [])]
    return [n for n in names if n and not (set_dir / "originals" / n).is_file()]


def fetch_missing(set_dir, key, plan_file, library, db_file):
    """Download the frames the batch run tidied away, so the set can be aligned again."""
    from .batch import focal_hints
    from .fetch import fetch_set

    job = next((j for j in read_json(plan_file, {"jobs": []}).get("jobs", []) if j.get("key") == key), None)
    if job is None:
        raise RuntimeError(f"{key} is not in the batch plan, so its frames cannot be fetched again; "
                           "loose parts are edited through the set they came from")
    hints = focal_hints(db_file, Path(db_file).parent, job["asset_ids"])
    fetch_set(library(), {"set_id": key, "asset_ids": job["asset_ids"]}, set_dir.parent, f35_hints=hints)


def queue_fill(work, key, name):
    """AI fill goes through the runner as its own GPU job, the same way the page queues it."""
    from .review import Review

    runner = jobs.runner()
    if isinstance(runner, jobs.LocalRunner):
        # A local runner lives in the page server; this job is a child of it, so run the fill here.
        import subprocess

        code = subprocess.run(jobs.argv("fill", ["fill", key, "--name", name]), env=jobs.child_env(),
                              cwd=config.home()).returncode
        if code:
            raise RuntimeError(f"AI fill of {name} failed (exit {code})")
        return None
    return Review(work).queue_fill({"set": key, "name": name})["task"]


def run(work, key, projection, orientation, model=None, fill=False, plan=None, plan_file=None,
        library=None, db_file=None, queued=None):
    set_dir = Path(work) / key
    if not (set_dir / "optimised.pto").exists():
        raise RuntimeError(f"{key} has not been stitched")
    if projection not in stitch.PROJECTIONS or orientation not in stitch.ORIENTATIONS:
        raise ValueError("unknown projection or orientation")
    result = {"set": key, "projection": projection, "orientation": orientation, "model": model,
              "fill": fill, "queued": queued, "started": now()}
    t0 = time.time()
    try:
        record = read_json(set_dir / "batch.json")
        if model:
            if missing_originals(set_dir):
                LOG.info(f"{key}: fetching the frames again for a new alignment")
                fetch_missing(set_dir, key, plan_file, library, db_file)
            LOG.info(f"{key}: aligning again with the {model} model")
            layout = stitch.align(set_dir, model=model, plan=plan, threads=limits.align_threads())
            chosen = layout["model"]["tried"][layout["model"]["chosen"]]
            record.update({"model": layout["model"]["chosen"], "model_reason": layout["model"]["reason"],
                           "poor_fit": layout["model"]["poor_fit"], "error_px": chosen["error_px"]})
            record["status"] = "poor fit" if layout["model"]["poor_fit"] else "stitched"
        LOG.info(f"{key}: rendering {projection} ({orientation})")
        info = stitch.render(set_dir, projection, orientation)
        name = stitch.preview_name(projection, orientation)
        record.update({"projection": projection, "orientation": orientation, "preview": info.get("size"),
                       "crop": info.get("crop_size"), "crop_rect": info.get("crop_rect"),
                       "preview_file": f"previews/{name}.jpg", "edited": now()})
        write_atomic(set_dir / "batch.json", json.dumps(record, indent=1))
        if fill:
            result["fill_task"] = queue_fill(work, key, name)
        result.update({"state": "done", "preview": name})
    except Exception as err:
        result.update({"state": "failed", "error": str(err)[:400]})
        raise
    finally:
        result.update({"finished": now(), "seconds": round(time.time() - t0, 1)})
        (set_dir / "edit").mkdir(exist_ok=True)
        write_atomic(set_dir / "edit" / "done.json", json.dumps(result, indent=1))
    return result
