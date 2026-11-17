"""Review.queue_fill validation."""

import pytest

from fuseshot import review
from conftest import stitched_set


def test_queue_fill_requires_rendered_name(tmp_path):
    work = tmp_path / "work"
    stitched_set(work, "Q1")
    rev = review.Review(work)
    with pytest.raises(ValueError, match="render"):
        rev.queue_fill({"set": "Q1", "name": "not-there"})


def test_queue_fill_submits_job(tmp_path, monkeypatch):
    work = tmp_path / "work"
    stitched_set(work, "Q2")
    rev = review.Review(work)

    class Runner:
        def submit(self, *a, **k):
            return "fill-task"

        def status(self, task):
            return "queued"

    monkeypatch.setattr("fuseshot.jobs.runner", lambda: Runner())
    out = rev.queue_fill({"set": "Q2", "name": "cylindrical-level"})
    assert out["task"] == "fill-task"
