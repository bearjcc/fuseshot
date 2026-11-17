"""batch.run_job and repair paths with stitch mocked."""

import json


from fuseshot import batch
from conftest import stitched_set


def test_run_job_success(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    db = tmp_path / "cache" / "db.sqlite"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"")
    job = {
        "key": "J1",
        "set_id": "J1",
        "type": "single-row rotation",
        "frames": [1, 2, 3],
        "asset_ids": ["a", "b", "c"],
        "stitch_plan": ["rotation"],
    }
    monkeypatch.setattr(batch, "fetch_set", lambda *a, **k: stitched_set(work, "J1"))
    monkeypatch.setattr(batch, "focal_hints", lambda *a, **k: {})
    monkeypatch.setattr(batch, "align", lambda *a, **k: {
        "model": {
            "chosen": "rotation", "poor_fit": False, "reason": "ok",
            "tried": {"rotation": {"error_px": 2.0, "largest_group": 3}},
        },
        "connected_groups": 1,
        "steps": [{"peak_mb": 10}],
    })
    monkeypatch.setattr(batch, "render_preview", lambda d, r, fuse: {"peak_mb": 5, "size": [100, 50]})
    monkeypatch.setattr(batch, "check_studio", lambda *a, **k: None)
    monkeypatch.setattr(batch, "clean_up", lambda d: None)
    monkeypatch.setattr(batch, "make_parts", lambda *a, **k: [])
    rec = batch.run_job(object(), job, work, db)
    assert rec["status"] == "stitched"
    assert (work / "J1" / "batch.json").is_file()


def test_run_job_records_failure(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    db = tmp_path / "db.sqlite"
    db.write_bytes(b"")
    job = {"key": "F1", "set_id": "F1", "type": "single-row rotation", "frames": [1], "asset_ids": ["a"],
           "stitch_plan": []}
    monkeypatch.setattr(batch, "focal_hints", lambda *a, **k: {})
    monkeypatch.setattr(batch, "fetch_set", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fetch broke")))
    rec = batch.run_job(None, job, work, db)
    assert rec["status"] == "failed"
    assert "fetch broke" in rec["error"]


def test_repair_job_and_undo(tmp_path, monkeypatch):
    work = tmp_path / "work"
    set_dir = stitched_set(work, "R1")
    record = json.loads((set_dir / "batch.json").read_text(encoding="utf-8"))
    record.update({"frames": 3, "model": "rotation", "status": "stitched", "seconds": {}})
    monkeypatch.setattr(batch, "repair", lambda *a, **k: {"kept": [1, 2, 3], "left_out": {}, "error_px": 1.0})
    monkeypatch.setattr(batch, "level", lambda d: None)
    monkeypatch.setattr(batch, "upright", lambda d: None)
    monkeypatch.setattr(batch, "render_preview", lambda d, r, fuse: {"peak_mb": 1})
    monkeypatch.setattr(batch, "check_studio", lambda *a, **k: None)
    monkeypatch.setattr(batch, "make_parts", lambda *a, **k: [])
    out = batch.repair_job(work, "R1", record)
    assert out and out.get("repair")
    (set_dir / "full.pto").write_text("full", encoding="utf-8")
    batch.undo_repair(set_dir)
    assert (set_dir / "optimised.pto").read_text(encoding="utf-8") == "full"


def test_repair_chunk_skips_discarded(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"jobs": [{"key": "P1"}, {"key": "P2"}], "chunks": [["P1", "P2"]]}), encoding="utf-8")
    stitched_set(work, "P1")
    stitched_set(work, "P2")
    for key in ("P1", "P2"):
        p = work / key / "batch.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data.update({"status": "stitched", "model": "rotation"})
        p.write_text(json.dumps(data), encoding="utf-8")
    (work / "reviews.json").write_text(json.dumps({
        "P1": [{"decision": "discarded"}],
    }), encoding="utf-8")
    calls = []
    def fake_repair(w, k, r):
        calls.append(k)
        r = dict(r)
        r.setdefault("frames", 3)
        return r

    monkeypatch.setattr(batch, "repair_job", fake_repair)
    batch.repair_chunk(plan, work)
    assert calls == ["P2"]
