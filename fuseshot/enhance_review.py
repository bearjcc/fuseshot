"""Blind A/B review of restored photos, and what Keep does with the result.

Each finished result that clearly differs from its original is shown beside it, with
the sides shuffled, biggest expected gain first. The answer is one of: keep the
enhanced photo, keep the original, or the face looks wrong (kept as the original and
run again with the face pass when the Face repair pack is installed).

Answers are appended to work/enhance/decisions.jsonl, one JSON object per line in the
record shape Choice uses (type, id, item, direction, value, label, note, ts, file), so
Choice or Sextant can read them. The latest line for a photo wins.

Keeping the enhanced photo never changes the original. With uploads on, the result is
added to Immich as a new asset and stacked with the original; otherwise it is copied to
work/enhance/kept/.
"""

import hashlib
import json
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import log, restore

LOG = log.get(__name__)
PICKS = {
    "enhanced": ("right", 1, "Keep enhanced"),
    "original": ("left", 0, "Keep original"),
    "face": ("down", -1, "Face looks wrong"),
}
_lock = threading.Lock()


def decisions_file(enhance_dir):
    return Path(enhance_dir) / "decisions.jsonl"


def decisions(enhance_dir):
    """The latest decision for each photo id."""
    path = decisions_file(enhance_dir)
    latest = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") == "decision" and row.get("item"):
                latest[row["item"]] = row
    return latest


def waiting(result, decision):
    """No answer yet, or Face looks wrong and the face pass has finished since."""
    if decision is None:
        return True
    return (decision.get("value") == PICKS["face"][1] and "codeformer" in result["chain"]
            and _utc(result.get("finished")) > _utc(decision.get("ts")))


def _utc(stamp):
    try:
        return datetime.fromisoformat(stamp).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def queue(enhance_dir):
    """Results waiting for an answer: finished, clearly different, biggest gain first."""
    done = decisions(enhance_dir)
    return [r for r in restore.results(enhance_dir) if r.get("differs") and waiting(r, done.get(r["id"]))]


def record(enhance_dir, asset_id, pick, enhanced_side, note="", file=None):
    direction, value, label = PICKS[pick]
    row = {"type": "decision", "id": uuid.uuid4().hex, "item": asset_id, "direction": direction,
           "value": value, "label": label, "note": note,
           "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "file": file,
           "deck": "fuseshot-enhance", "enhanced_side": enhanced_side}
    path = decisions_file(enhance_dir)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def kept_name(job):
    day = (job.get("taken") or "")[:10]
    stem = Path(job.get("file_name") or job["id"]).stem
    return f"{day} {stem} enhanced.jpg".strip()


def keep(enhance_dir, asset_id, client=None, log_fn=LOG.info):
    """Send the enhanced copy on: a new Immich asset stacked with the original when a
    client is given, a file in work/enhance/kept/ otherwise. Returns where it went."""
    folder = restore.results_dir(enhance_dir) / asset_id
    job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    path = folder / "enhanced.jpg"
    if client is None:
        dest = Path(enhance_dir) / "kept" / kept_name(job)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        log_fn(f"{asset_id}: enhanced copy saved to {dest}")
        return {"file": str(dest)}
    data = path.read_bytes()
    checksum = hashlib.sha1(data).hexdigest()
    taken = job.get("taken") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    answer = client.upload(data, kept_name(job), taken, checksum=checksum,
                           device_fields=client.major_version() < 3)
    client.create_stack([answer["id"], asset_id])
    log_fn(f"{asset_id}: enhanced copy uploaded as {answer['id']} and stacked with the original")
    return {"asset_id": answer["id"]}


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Compare enhanced photos</title>
<link rel="stylesheet" href="{base}/static/review.css?v={v}">
{base_script}
</head><body class="editor">
<div class="viewer ed">
  <div class="v-main">
    <div class="ab-stage" id="stage">
      <figure><img id="left" alt="photo A"><figcaption>A</figcaption></figure>
      <figure><img id="right" alt="photo B"><figcaption>B</figcaption></figure>
    </div>
    <div class="v-top">
      <a class="icon-btn" href="{base}/enhance" title="Back to the candidates (Esc)" data-icon="arrowLeft"></a>
      <div class="v-title"><b>Compare</b><span id="count"></span></div>
    </div>
    <div class="v-bottom"><div class="v-actions">
      <button class="act discard" data-pick="face" title="Face looks wrong (F): keep the original"><span data-icon="close"></span><span class="lbl">Face looks wrong</span></button>
      <button class="act" data-side="left" title="Keep A (1)"><span class="lbl">Keep A</span></button>
      <button class="act" data-side="right" title="Keep B (2)"><span class="lbl">Keep B</span></button>
    </div></div>
  </div>
</div>
<div id="toasts" class="toasts" aria-live="polite"></div>
<script type="module" src="{base}/static/compare.js?v={v}"></script>
</body></html>
"""
