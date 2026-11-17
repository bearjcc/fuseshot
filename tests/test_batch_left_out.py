"""batch.left_out reads review discards."""

import json

from fuseshot import batch


def test_left_out_sees_discarded_sets(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "reviews.json").write_text(json.dumps({
        "P1": [{"decision": "discarded", "at": "2026-01-01T00:00:00+00:00"}],
    }), encoding="utf-8")
    job = {"key": "P1", "set_id": "P1"}
    assert batch.left_out(work, job)
    assert not batch.left_out(work, {"key": "P2", "set_id": "P2"})
