"""group.check early exits without MediaPipe."""


from fuseshot import group


def test_bracket_candidate_skips_detector():
    out = group.check({"set_id": "B1", "asset_ids": ["a", "b"], "bracket": True}, "cache", det=None)
    assert out["reason"] == "exposure bracket"
    assert not out["group"]


def test_slow_timeline_rejects():
    out = group.check({
        "set_id": "S1",
        "asset_ids": ["a", "b", "c"],
        "median_gap_s": 999,
        "span_s": 999,
    }, "cache", det=None)
    assert "too slow" in out["reason"]
