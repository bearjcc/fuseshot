"""batch.make_plan and save_plan."""

import json

from fuseshot import batch


def test_make_plan_orders_by_confidence(tmp_path):
    verified = tmp_path / "verified.json"
    candidates = tmp_path / "candidates.json"
    verified.write_text(json.dumps({
        "S1": {
            "sub_sets": [{
                "type": "single-row rotation",
                "confidence": 0.9,
                "quality": 2,
                "frames": [1, 2, 3],
                "coverage_h": 0.8,
                "coverage_v": 0.5,
            }],
        },
        "S2": {
            "sub_sets": [{
                "type": "single-row rotation",
                "confidence": 0.5,
                "quality": 1,
                "frames": [1, 2],
            }],
        },
    }), encoding="utf-8")
    candidates.write_text(json.dumps({
        "candidates": [
            {"set_id": "S1", "asset_ids": ["a", "b", "c"]},
            {"set_id": "S2", "asset_ids": ["d", "e"]},
        ],
    }), encoding="utf-8")
    jobs = batch.make_plan(verified, candidates, min_confidence=0.4, exclude=set())
    assert jobs[0]["set_id"] == "S1"
    plan = tmp_path / "plan.json"
    batch.save_plan(jobs, plan, chunk_size=5)
    data = json.loads(plan.read_text(encoding="utf-8"))
    assert len(data["chunks"]) == 1
    assert len(data["jobs"]) == 2
