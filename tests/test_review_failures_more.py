"""Review failures and edit_state."""

import json
import pytest

from fuseshot import review
from conftest import stitched_set


def test_failure_path_and_retry(tmp_path, monkeypatch):
    work = tmp_path / "work"
    fail = stitched_set(work, "X1")
    (fail / "batch.json").write_text(json.dumps({
        "status": "failed", "error": "oops", "attempts": 1,
    }), encoding="utf-8")
    rev = review.Review(work)
    rows = rev.failures()
    assert rows and rows[0]["key"] == "X1"
    path = rev.failure_path("stitch", "X1")
    assert path == fail / "batch.json"
    monkeypatch.setattr("fuseshot.retry.request", lambda p: None)
    assert rev.retry_failure({"kind": "stitch", "key": "X1"})["retry"]


def test_edit_state_done(tmp_path):
    work = tmp_path / "work"
    set_dir = stitched_set(work, "E1")
    edit_dir = set_dir / "edit"
    edit_dir.mkdir()
    (edit_dir / "done.json").write_text(json.dumps({
        "queued": "2026-01-01T00:00:00+00:00", "state": "done",
    }), encoding="utf-8")
    rev = review.Review(work)
    state = rev.edit_state("E1", {"at": "2026-01-01T00:00:00+00:00", "task": "t1"})
    assert state["state"] == "done"


def test_failure_path_missing_raises():
    rev = review.Review("/tmp/nowhere")
    with pytest.raises(KeyError):
        rev.failure_path("stitch", "nope")
