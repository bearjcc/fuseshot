"""Exercise review.py data paths without a browser."""

import json

from fuseshot import upload
from fuseshot.review import Review
from conftest import stitched_set


def test_items_and_failures_surface(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    stitched_set(work, "P1")
    cand = tmp_path / "output" / "candidates.json"
    cand.parent.mkdir(parents=True)
    cand.write_text(json.dumps({"candidates": [{
        "set_id": "P1", "library": "folder", "asset_ids": ["a"], "type": "single-row rotation",
    }]}), encoding="utf-8")
    page = Review(work, candidates_file=cand, output_dir=tmp_path / "output")
    data = page.items()
    assert data["items"]
    assert page.failures() == []
    assert "Review" in page.failures_page() or "failures" in page.failures_page().lower()
    assert upload.enabled() is False
