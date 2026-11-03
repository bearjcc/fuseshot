"""The stitch plan's order, and discards made while sets wait their turn."""

import json

from fuseshot import batch, tools


def sub(kind, confidence, quality, frames=(1, 2, 3)):
    return {"type": kind, "confidence": confidence, "quality": quality, "frames": list(frames)}


def write_inputs(tmp_path, verified):
    ids = [f"a{n}" for n in range(1, 7)]
    candidates = {"candidates": [{"set_id": s, "asset_ids": ids} for s in verified]}
    (tmp_path / "candidates.json").write_text(json.dumps(candidates))
    (tmp_path / "verified.json").write_text(json.dumps({s: {"sub_sets": subs} for s, subs in verified.items()}))
    return tmp_path / "verified.json", tmp_path / "candidates.json"


def test_plan_puts_the_likeliest_sets_first(tmp_path):
    files = write_inputs(tmp_path, {
        "P1": [sub("bracket", 0.8, 0.9)],
        "P2": [sub("single-row rotation", 0.95, 0.2)],
        "P3": [sub("single-row rotation", 0.95, 0.6), sub("multi-row grid", 0.76, 0.9, (4, 5, 6))],
        "P4": [sub("single-row rotation", 0.5, 1.0)],
    })
    jobs = batch.make_plan(*files, min_confidence=0.75, exclude=set())
    assert [j["key"] for j in jobs] == ["P3-1", "P2", "P1", "P3-2"]


def decide(work, set_id, kind, decision="discarded"):
    path = work / "reviews.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data.setdefault(set_id, []).append({"kind": kind, "decision": decision, "at": "2026-11-04T00:00:00+00:00"})
    path.write_text(json.dumps(data))


def test_discards_are_read_again_before_each_set(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    jobs = [{"key": k, "set_id": k.split("-")[0], "type": "bracket", "frames": [1]}
            for k in ("P1", "P2-1", "P2-2", "P3", "P4")]
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"jobs": jobs, "chunks": [[j["key"] for j in jobs]]}))
    monkeypatch.setattr(tools, "require", lambda name: None)
    ran = []

    def run_job(immich, job, work_dir, db_file):
        ran.append(job["key"])
        if job["key"] == "P1":  # discarded on the page while P1 was stitching
            decide(work, "P2", "found")
            decide(work, "P3", "stitch")
        return {"status": "stitched", "seconds": {"total": 1}}

    monkeypatch.setattr(batch, "run_job", run_job)
    batch.run_chunk(None, plan, 0, work, None)
    assert ran == ["P1", "P4"]


def test_undoing_a_found_discard_lets_the_set_be_made(tmp_path):
    work = tmp_path
    decide(work, "P2", "found")
    assert batch.left_out(work, {"key": "P2-1", "set_id": "P2"})
    (work / "reviews.json").write_text("{}")
    assert not batch.left_out(work, {"key": "P2-1", "set_id": "P2"})
