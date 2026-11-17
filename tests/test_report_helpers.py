"""report.merge_verified and site matching helpers."""


from fuseshot import report


def test_band_and_changed_type():
    assert report.band(0.9) == "high"
    assert report.band(0.6) == "medium"
    assert report.band(0.2) == "low"
    cand = {"guessed_type": "single-row rotation", "verified_type": "multi-row grid"}
    assert report.changed_type(cand)


def test_link_and_merge_verified():
    assert report.link("https://immich/", "file-abc") == ""
    assert "photos" in report.link("https://immich/", "real-id")
    cands = [{"set_id": "S1", "asset_ids": ["a1", "a2"], "guessed_type": "single-row rotation"}]
    results = {
        "S1": {
            "verified_type": "single-row rotation",
            "verified_confidence": 0.9,
            "motion_model": "rotation",
            "stitch_plan": ["rotation"],
            "coverage_h": 0.5,
            "coverage_v": 0.3,
            "rows": 1,
            "sub_sets": [{
                "type": "single-row rotation", "frames": [1, 2], "frames_count": 2, "quality": 1,
                "confidence": 0.9,
            }],
            "junk_frames": [],
            "splat_candidate": False,
            "quality": 1,
            "reason": "ok",
        },
    }
    report.merge_verified(cands, results, "https://immich/")
    assert cands[0]["verified_type"] == "single-row rotation"
    assert cands[0]["best_link"].endswith("/photos/a1")
