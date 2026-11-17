"""edit.run error and refetch paths."""

import json
import pytest

from fuseshot import edit
from conftest import stitched_set


def test_fetch_missing_raises_without_batch_plan(tmp_path):
    work = tmp_path / "work"
    set_dir = stitched_set(work, "P9")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"jobs": []}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="batch plan"):
        edit.fetch_missing(set_dir, "P9", plan, lambda: None, tmp_path / "db.sqlite")


def test_edit_run_unknown_projection(tmp_path):
    work = tmp_path / "work"
    stitched_set(work, "P1")
    with pytest.raises(ValueError, match="unknown"):
        edit.run(work, "P1", "not-a-projection", "level")


def test_edit_run_records_failure(tmp_path, monkeypatch):
    work = tmp_path / "work"
    set_dir = stitched_set(work, "P2")
    monkeypatch.setattr(edit.stitch, "render", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        edit.run(work, "P2", "cylindrical", "level")
    done = json.loads((set_dir / "edit" / "done.json").read_text(encoding="utf-8"))
    assert done["state"] == "failed" and "boom" in done["error"]


def test_edit_realign_fetches_missing(tmp_path, monkeypatch):
    work = tmp_path / "work"
    set_dir = stitched_set(work, "P3")
    (set_dir / "manifest.json").write_text(json.dumps([{"original": "gone.jpg"}]), encoding="utf-8")
    fetched = []
    monkeypatch.setattr(edit, "fetch_missing", lambda *a, **k: fetched.append(1))
    monkeypatch.setattr(edit.stitch, "align", lambda *a, **k: {
        "model": {
            "chosen": "rotation", "poor_fit": False, "reason": "ok",
            "tried": {"rotation": {"error_px": 1}},
        },
    })
    monkeypatch.setattr(edit.stitch, "render", lambda *a, **k: {"size": [10, 10], "crop_size": [10, 10], "crop_rect": []})
    edit.run(work, "P3", "cylindrical", "level", model="rotation", plan_file=tmp_path / "plan.json",
             library=lambda: None, db_file=tmp_path / "db.sqlite")
    assert fetched
