"""Call review page builders and helpers without a browser."""

import json

from fuseshot.review import Review, decision_name
from conftest import stitched_set


def page(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    stitched_set(work, "P1")
    stitched_set(work, "P2")
    cand = tmp_path / "output" / "candidates.json"
    cand.parent.mkdir(parents=True)
    cand.write_text(json.dumps({
        "candidates": [
            {"set_id": "P1", "library": "folder", "asset_ids": ["a", "b"], "verified_type": "single-row rotation",
             "guessed_type": "single-row rotation", "median_gap_s": 2, "span_s": 20, "start_local": "2024-03-09",
             "camera": "Example Camera", "count": 2, "quality": 1.0, "verified_confidence": 0.9,
             "motion_model": "rotation", "verify_reason": "ok", "stitch_plan": ""},
            {"set_id": "P2", "library": "folder", "asset_ids": ["c"], "verified_type": "bracket",
             "guessed_type": "bracket", "start_local": "2024-03-10", "camera": "Cam", "count": 1,
             "quality": 0.5, "verified_confidence": 0.8, "motion_model": "rotation"},
        ],
    }), encoding="utf-8")
    (work / "types.json").write_text("{}", encoding="utf-8")
    return Review(work, candidates_file=cand, output_dir=tmp_path / "out")


def test_html_tables_and_app(tmp_path):
    review = page(tmp_path)
    assert "<" in review.app_page()
    assert "<" in review.failures_page()
    assert review.items()["items"]
    assert isinstance(review.stitched_table(), str)
    assert isinstance(review.loops_table(), str)
    assert isinstance(review.groups_table(), str)
    assert isinstance(review.splats_table(), str)
    html = review.candidates_table("type=panoramas")
    assert "P1" in html


def test_decide_override_and_types(tmp_path, monkeypatch):
    review = page(tmp_path)
    monkeypatch.setattr(review, "export_later", lambda keys: None)
    row = review.decide({
        "set": "P1", "decision": "kept", "projection": "cylindrical", "orientation": "level",
    })
    assert row["decision"] == "kept"
    review.override({"set": "P1", "model": "rotation"})
    assert review.overrides().get("P1", {}).get("model") == "rotation"
    review.set_type({"set": "P1", "type": "panorama"})
    assert review.type_overrides().get("P1")["type"] == "panorama"
    cands = {c["set_id"]: c for c in review.candidates()}
    detected = review.detected_type("P1", cands["P1"], review.actions())
    assert review.type_select("P1", detected, review.type_overrides())


def test_stitch_metadata_helpers(tmp_path):
    review = page(tmp_path)
    sid = "P1"
    assert review.batch_record(sid)["status"] == "stitched"
    assert review.default_preview(sid)
    assert review.thumb_name(sid)
    assert review.versions(sid)
    assert review.fills(sid) is not None
    assert "<" in review.set_page(sid)
    data = review.items()
    item = next(i for i in data["items"] if i.get("id") == sid)
    assert review.item_detail(item.get("kind", "stitch"), sid)["detected"]
    assert decision_name("kept") == "kept"
