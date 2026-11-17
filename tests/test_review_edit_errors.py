"""Review.edit and render validation without HTTP server."""

import pytest

from fuseshot import review
from conftest import stitched_set


def test_edit_rejects_unknown_projection(tmp_path):
    work = tmp_path / "work"
    stitched_set(work, "E1")
    rev = review.Review(work)
    with pytest.raises(ValueError, match="unknown"):
        rev.edit({"set": "E1", "projection": "bad", "orientation": "level"})


def test_edit_requires_existing_preview(tmp_path):
    work = tmp_path / "work"
    set_dir = stitched_set(work, "E2")
    (set_dir / "previews" / "cylindrical-level.jpg").unlink()
    rev = review.Review(work)
    with pytest.raises(ValueError, match="nothing rendered"):
        rev.edit({"set": "E2", "projection": "cylindrical", "orientation": "level",
                  "from": "cylindrical-level.jpg"})


def test_render_returns_metadata(tmp_path, monkeypatch):
    work = tmp_path / "work"
    stitched_set(work, "E3")
    rev = review.Review(work)
    monkeypatch.setattr("fuseshot.stitch.render", lambda *a, **k: {
        "size": [10, 10], "crop_size": [10, 10], "crop_rect": [],
    })
    info = rev.render({"set": "E3", "projection": "cylindrical", "orientation": "level"})
    assert info["rendered"] is not None
