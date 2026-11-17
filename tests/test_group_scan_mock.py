"""group.scan resume path with mocked check."""

import json

from fuseshot import group


def test_scan_reuses_cached_ids(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps({"candidates": [{
        "set_id": "G1", "asset_ids": ["x", "y"],
        "median_gap_s": 1, "span_s": 10,
    }]}), encoding="utf-8")
    out = tmp_path / "groups.json"
    key = group._ids_key({"asset_ids": ["x", "y"]})
    out.write_text(json.dumps({
        "rules": group.rules(),
        "sets": {"G1": {"group": False, "ids": key, "reason": "cached"}},
    }), encoding="utf-8")

    calls = []

    def fake_check(cand, cache_dir, det, immich=None):
        calls.append(cand["set_id"])
        return {"set_id": cand["set_id"], "group": False, "reason": "test"}

    monkeypatch.setattr(group, "check", fake_check)
    monkeypatch.setattr(group, "detector", lambda: None)
    result = group.scan(candidates, cache, out)
    assert calls == []
    assert result["sets"]["G1"]["reason"] == "cached"
