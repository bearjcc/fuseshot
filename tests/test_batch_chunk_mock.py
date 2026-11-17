"""batch.run_chunk and print_status."""

import json

from fuseshot import batch
from conftest import stitched_set


def test_run_chunk_skips_done_and_runs_pending(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    plan = tmp_path / "plan.json"
    job = {
        "key": "C1",
        "set_id": "C1",
        "type": "single-row rotation",
        "frames": [1, 2],
        "asset_ids": ["a", "b"],
        "stitch_plan": ["rotation"],
    }
    plan.write_text(json.dumps({"jobs": [job], "chunks": [["C1"]]}), encoding="utf-8")
    stitched_set(work, "C1")
    old = work / "C1" / "batch.json"
    old.write_text(json.dumps({"status": "stitched", "seconds": {"total": 1}}), encoding="utf-8")
    calls = []

    def fake_run(immich, j, w, db, old=None):
        calls.append(j["key"])
        return {"status": "stitched", "seconds": {"total": 2}, "key": j["key"]}

    monkeypatch.setattr(batch, "run_job", fake_run)
    monkeypatch.setattr(batch.tools, "require", lambda n: "hugin")
    batch.run_chunk(None, plan, 0, work, tmp_path / "db.sqlite")
    assert calls == []

    old.unlink()
    batch.run_chunk(None, plan, 0, work, tmp_path / "db.sqlite")
    assert calls == ["C1"]


def test_print_status_summarises_plan(tmp_path, capsys):
    work = tmp_path / "work"
    stitched_set(work, "P1")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"jobs": [{"key": "P1"}], "chunks": [["P1"]]}), encoding="utf-8")
    (work / "P1" / "batch.json").write_text(
        json.dumps({"status": "stitched", "seconds": {"total": 42}, "disk_mb": 10}),
        encoding="utf-8",
    )
    batch.print_status(plan, work)
    out = capsys.readouterr().out
    assert "stitched" in out and "42" in out
