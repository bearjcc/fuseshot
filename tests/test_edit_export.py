"""Edit pipeline and export paths with mocks."""

import json


from fuseshot import edit, export
from fuseshot.review import Review
from conftest import stitched_set


def test_missing_originals_lists_gaps(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    set_dir = stitched_set(work, "P1")
    (set_dir / "manifest.json").write_text(json.dumps([{"original": "missing/shot.jpg"}]), encoding="utf-8")
    assert edit.missing_originals(set_dir) == ["shot.jpg"]


def test_edit_run_renders_with_mocked_stitch(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    set_dir = stitched_set(work, "P2")
    monkeypatch.setattr(edit.stitch, "render", lambda *a, **k: {"size": [640, 320], "crop_size": [600, 300],
                                                                 "crop_rect": [0.1, 0.1, 0.9, 0.9]})
    out = edit.run(work, "P2", "equirectangular", "level", queued="test-1")
    assert out["state"] == "done"
    record = json.loads((set_dir / "batch.json").read_text(encoding="utf-8"))
    assert record["projection"] == "equirectangular"
    assert (set_dir / "edit" / "done.json").is_file()


def test_export_result_file_and_copy(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    stitched_set(work, "P3")
    page = Review(work, candidates_file=tmp_path / "candidates.json", output_dir=tmp_path / "output")
    (tmp_path / "output").mkdir(exist_ok=True)
    (tmp_path / "candidates.json").write_text(
        json.dumps({"candidates": [{"set_id": "P3", "library": "folder", "asset_ids": ["a"]}]}), encoding="utf-8"
    )
    page.decide({"set": "P3", "decision": "kept", "projection": "cylindrical", "orientation": "level"})
    item = next(i for i in page.items()["items"] if i["id"] == "P3")
    decisions = page.decisions()
    path = export.result_file(page, item, decisions, full=False)
    assert path and path.name.endswith(".jpg")
    dest = tmp_path / "out"
    written = export.export(page, dest, keys={item["key"]}, libraries=("folder",))
    assert written[0].exists()
