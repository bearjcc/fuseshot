"""Keep, discard and edit, and reading reviews.json files saved under the old names."""

import json

import pytest
from PIL import Image

from fuseshot import jobs, review as review_module
from fuseshot.review import Review, decision_name, discarded, migrate_reviews

OLD = {
    "P00001": [{"decision": "approved", "projection": "cylindrical", "orientation": "level", "note": "", "at": "2026-01-01T00:00:00+00:00"}],
    "P00002": [{"decision": "needs-work", "note": "sky seam on the left", "at": "2026-01-01T00:00:01+00:00"}],
    "P00003": [{"kind": "group", "decision": "rejected", "note": "", "at": "2026-01-01T00:00:02+00:00"}],
}


def stitched(work, set_id):
    set_dir = work / set_id
    (set_dir / "previews").mkdir(parents=True)
    (set_dir / "optimised.pto").write_text("p\n")
    Image.new("RGB", (64, 32), (90, 120, 150)).save(set_dir / "previews" / "cylindrical-level.jpg")
    (set_dir / "batch.json").write_text(json.dumps({"set_id": set_id, "status": "ok", "error_px": 1.5}))
    return set_dir


def test_old_names_read_as_the_new_ones_without_writing(tmp_path):
    (tmp_path / "reviews.json").write_text(json.dumps(OLD))
    before = (tmp_path / "reviews.json").read_bytes()
    data = Review(tmp_path).decisions()
    assert data["P00001"][0]["decision"] == "kept"
    assert data["P00003"][0]["decision"] == "discarded"
    assert data["P00002"][0]["decision"] is None
    assert data["P00002"][0]["was"] == "needs-work"
    assert data["P00002"][0]["note"] == "sky seam on the left"
    assert (tmp_path / "reviews.json").read_bytes() == before


def test_migration_keeps_a_backup_and_runs_once(tmp_path):
    path = tmp_path / "reviews.json"
    path.write_text(json.dumps(OLD))
    original = path.read_bytes()
    changed, backup = migrate_reviews(path)
    assert changed == 3
    assert backup == tmp_path / "reviews.json.bak"
    assert backup.read_bytes() == original
    saved = json.loads(path.read_text())
    assert [r["decision"] for r in saved["P00001"] + saved["P00002"] + saved["P00003"]] == ["kept", None, "discarded"]
    assert saved["P00002"][0]["note"] == "sky seam on the left"
    assert migrate_reviews(path) == (0, None)
    assert backup.read_bytes() == original


def test_migration_never_overwrites_an_older_backup(tmp_path):
    path = tmp_path / "reviews.json"
    (tmp_path / "reviews.json.bak").write_text("older")
    path.write_text(json.dumps(OLD))
    _, backup = migrate_reviews(path)
    assert backup.name == "reviews.json.bak.2"
    assert (tmp_path / "reviews.json.bak").read_text() == "older"


def test_a_page_from_before_the_update_still_decides():
    assert decision_name("approved") == "kept"
    assert decision_name("rejected") == "discarded"
    with pytest.raises(ValueError):
        decision_name("needs-work")
    with pytest.raises(ValueError):
        decision_name("maybe")


def test_keep_exports_and_discard_does_not(tmp_path, monkeypatch):
    stitched(tmp_path, "P00001")
    page = Review(tmp_path)
    asked = []
    monkeypatch.setattr(page, "export_later", asked.append)
    page.decide({"set": "P00001", "decision": "discarded"})
    assert asked == []
    page.decide({"set": "P00001", "decision": "kept", "projection": "cylindrical", "orientation": "level"})
    assert asked == [{"stitch:P00001"}]


def test_undo_takes_back_a_keep(tmp_path, monkeypatch):
    stitched(tmp_path, "P00001")
    page = Review(tmp_path)
    monkeypatch.setattr(page, "export_later", lambda keys: None)
    record = page.decide({"set": "P00001", "decision": "kept"})
    page.undo({"records": [{"set": "P00001", "kind": "stitch", "at": record["at"], "decision": "kept"}]})
    assert page.decisions().get("P00001", []) == []


def test_discarded_sets_are_left_alone_by_later_runs(tmp_path):
    (tmp_path / "reviews.json").write_text(json.dumps({
        "P00001": [{"decision": "kept", "at": "1"}, {"decision": "rejected", "at": "2"}],
        "P00002": [{"decision": "discarded", "at": "1"}, {"decision": "kept", "at": "2"}],
        "P00003": [{"kind": "group", "decision": "discarded", "at": "1"}],
    }))
    assert discarded(tmp_path) == {"P00001"}
    assert discarded(tmp_path, "group") == {"P00003"}


class StubRunner:
    def __init__(self):
        self.sent = []
        self.state = "queued"

    def submit(self, kind, name, args, gpu=False):
        self.sent.append((kind, name, args))
        return 41

    def status(self, task):
        return self.state


def test_edit_queues_a_new_version_and_keeps_the_old_one(tmp_path, monkeypatch):
    set_dir = stitched(tmp_path, "P00001")
    runner = StubRunner()
    monkeypatch.setattr(jobs, "runner", lambda: runner)
    page = Review(tmp_path)
    page.decide({"set": "P00001", "decision": "kept", "projection": "cylindrical", "orientation": "level"})
    reply = page.edit({"set": "P00001", "projection": "equirectangular", "orientation": "level", "fill": True})
    kind, name, args = runner.sent[0]
    assert (kind, name) == ("edit", "edit-P00001")
    assert args[:6] == ["edit", "P00001", "--projection", "equirectangular", "--orientation", "level"]
    assert "--fill" in args and "--model" not in args
    assert (set_dir / "edit" / "previous.jpg").read_bytes() == (set_dir / "previews" / "cylindrical-level.jpg").read_bytes()
    fields = page.decision_fields(page.decisions(), "P00001", "stitch")
    assert fields["decision"] == "editing" and fields["edit"]["task"] == 41
    with pytest.raises(ValueError):
        page.edit({"set": "P00001", "projection": "equirectangular", "orientation": "level"})

    (set_dir / "edit" / "done.json").write_text(json.dumps({"state": "done", "queued": reply["record"]["at"]}))
    fields = page.decision_fields(page.decisions(), "P00001", "stitch")
    assert fields["decision"] is None
    assert fields["edit"]["state"] == "done"
    assert page.previous_info("P00001")["url"].endswith("/previous/P00001/previous.jpg")
    assert page.previous_file("P00001", "previous.jpg").exists()
    with pytest.raises(KeyError):
        page.previous_file("P00001", "../batch.json")


def test_an_edit_that_stops_early_goes_back_to_review(tmp_path, monkeypatch):
    stitched(tmp_path, "P00001")
    runner = StubRunner()
    monkeypatch.setattr(jobs, "runner", lambda: runner)
    page = Review(tmp_path)
    page.edit({"set": "P00001", "projection": "cylindrical", "orientation": "level"})
    runner.state = "lost"
    fields = page.decision_fields(page.decisions(), "P00001", "stitch")
    assert fields["decision"] is None and fields["edit"]["state"] == "failed"


def test_edit_refuses_bad_settings(tmp_path, monkeypatch):
    stitched(tmp_path, "P00001")
    monkeypatch.setattr(jobs, "runner", lambda: StubRunner())
    page = Review(tmp_path)
    with pytest.raises(ValueError):
        page.edit({"set": "P00001", "projection": "fisheye", "orientation": "level"})
    with pytest.raises(ValueError):
        page.edit({"set": "P00001", "kind": "group", "projection": "cylindrical", "orientation": "level"})
    with pytest.raises(ValueError):
        page.edit({"set": "P00001", "projection": "cylindrical", "orientation": "level", "from": "../x.jpg"})
    assert review_module.EDITING not in json.dumps(page.decisions())


def test_running_stage_shows_until_finished(tmp_path):
    import json
    import time

    from fuseshot import progress
    from fuseshot.review import Review

    page = Review(tmp_path)
    assert page.running() is None
    progress.STATE = tmp_path / "progress.json"
    try:
        with progress.Progress("checking overlap", 10, unit="sets") as bar:
            bar.update(4)
            bar._save()
            running = page.running()
            assert running["stage"] == "checking overlap" and running["done"] == 4 and running["total"] == 10
        assert page.running() is None
        stale = json.loads((tmp_path / "progress.json").read_text())
        stale.update(finished=False, at=time.time() - 3600)
        (tmp_path / "progress.json").write_text(json.dumps(stale))
        assert page.running() is None
    finally:
        progress.STATE = None


@pytest.mark.parametrize("record, settings, expected", [
    ({"crop": True}, {}, "cylindrical-level_crop.jpg"),  # Keep from the viewer as it opens
    ({"crop": False}, {}, "cylindrical-level.jpg"),  # the whole canvas picked in the viewer
    ({}, {}, "cylindrical-level_crop.jpg"),  # Keep with the keyboard from the grid
    ({"crop": False}, {"crop": False}, "cylindrical-level.jpg"),
    ({}, {"crop": False}, "cylindrical-level.jpg"),
])
def test_kept_panorama_is_cropped_to_fit_unless_asked(tmp_path, monkeypatch, record, settings, expected):
    from fuseshot import config
    from fuseshot.export import result_file

    set_dir = stitched(tmp_path, "P00001")
    Image.new("RGB", (60, 28)).save(set_dir / "previews" / "cylindrical-level_crop.jpg")
    monkeypatch.setattr(config, "_settings", settings)
    page = Review(tmp_path)
    monkeypatch.setattr(page, "export_later", lambda keys: None)
    page.decide({"set": "P00001", "decision": "kept", "projection": "cylindrical", "orientation": "level", **record})
    item = {"kind": "stitch", "id": "P00001", "key": "stitch:P00001", "preview": "cylindrical-level"}
    assert result_file(page, item, page.decisions(), full=False).name == expected
    # a keep saved before crop-to-fit was the default is exported cropped too
    page.decisions_file.write_text(json.dumps({"P00001": [{"decision": "kept", "crop": False, "fill": False}]}))
    old = result_file(page, item, page.decisions(), full=False).name
    assert old == ("cylindrical-level.jpg" if settings else "cylindrical-level_crop.jpg")


def test_ai_fill_is_never_picked_by_default(tmp_path, monkeypatch):
    page = Review(tmp_path)
    monkeypatch.setattr(page, "export_later", lambda keys: None)
    stitched(tmp_path, "P00001")
    record = page.decide({"set": "P00001", "decision": "kept", "projection": "cylindrical", "orientation": "level"})
    assert record["fill"] is False and record["uncropped"] is False


def test_a_file_held_open_elsewhere_is_tried_again_on_windows(monkeypatch):
    from fuseshot import files

    monkeypatch.setattr(files.os, "name", "nt")
    monkeypatch.setattr(files.time, "sleep", lambda s: None)
    calls = []

    def busy():
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("in use")
        return "done"

    assert files.retry_busy(busy) == "done"
    assert len(calls) == 3
    with pytest.raises(PermissionError):
        files.retry_busy(lambda: (_ for _ in ()).throw(PermissionError("in use")), tries=2)
