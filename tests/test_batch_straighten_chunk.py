"""batch.straighten_chunk."""

import json

from fuseshot import batch
from conftest import stitched_set


def test_straighten_chunk_calls_straighten_job(tmp_path, monkeypatch):
    work = tmp_path / "work"
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"jobs": [{"key": "T1"}], "chunks": [["T1"]]}), encoding="utf-8")
    stitched_set(work, "T1")
    rec = work / "T1" / "batch.json"
    rec.write_text(json.dumps({"status": "stitched", "seconds": {}}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(batch, "straighten_job", lambda d, r: calls.append(d) or None)
    batch.straighten_chunk(plan, 0, work)
    assert calls
