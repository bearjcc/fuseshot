import json

import pytest

from fuseshot import moving, splat


def det(klass, cx=0.5, cy=0.5, frac=0.05):
    return {"class": klass, "cx": cx, "cy": cy, "frac": frac}


def test_presence_without_motion_is_not_moving():
    static = [[det("person")] for _ in range(5)]
    out = moving.judge(static)
    assert out["presence_only"]
    assert not out["moving"]
    assert out["motion_frac"] == 0.0


def test_displacement_counts_as_motion():
    frames = [
        [det("bird", 0.15, 0.5)],
        [det("bird", 0.35, 0.5)],
        [det("bird", 0.55, 0.5)],
        [det("bird", 0.75, 0.5)],
        [det("bird", 0.9, 0.5)],
    ]
    assert moving.judge(frames)["moving"]
    assert moving.judge(frames)["subject"] == "bird"


def test_sparse_presence_is_not_moving():
    assert not moving.judge([[det("bird")], [], [], [det("bird")]])["moving"]


def test_one_static_vehicle_display():
    frames = [[det("car", 0.5, 0.6)] for _ in range(6)]
    assert not moving.judge(frames)["moving"]


def test_motion_frac_tracks_one_class():
    frames = [
        [det("person", 0.2, 0.5), det("car", 0.8, 0.5)],
        [det("person", 0.25, 0.5), det("car", 0.8, 0.5)],
        [det("person", 0.8, 0.5), det("car", 0.8, 0.5)],
        [det("person", 0.85, 0.5), det("car", 0.8, 0.5)],
    ]
    out = moving.judge(frames)
    assert out["moving"]
    assert out["subject"] == "person"


def test_boats_are_not_moving():
    assert "boat" not in moving.MOVING


def test_marks_and_unmarks(tmp_path):
    out = tmp_path / "moving.json"
    out.write_text(json.dumps({"rules": moving.rules(), "sets": {"P1": {"moving": True}, "P2": {"moving": False}}}))
    moving.mark(out, ["P3"])
    assert moving.moving_sets(out) == {"P1", "P3"}
    moving.mark(out, ["P3"], moving=False)
    assert moving.moving_sets(out) == {"P1"}
    data = json.loads(out.read_text())
    assert "P3" in data["unmarked"]


def test_work_overrides(tmp_path):
    out = tmp_path / "moving.json"
    work = tmp_path / "work"
    work.mkdir()
    out.write_text(json.dumps({"rules": moving.rules(), "sets": {"P1": {"moving": True}}}))
    moving.write_override(work, "P2", "loop")
    moving.write_override(work, "P1", "splat")
    assert moving.moving_sets(out, work) == {"P2"}
    moving.write_override(work, "P1", "auto")
    assert moving.moving_sets(out, work) == {"P1", "P2"}


def test_moving_choice(tmp_path):
    out = tmp_path / "moving.json"
    work = tmp_path / "work"
    work.mkdir()
    out.write_text(json.dumps({"rules": moving.rules(), "sets": {"P1": {"moving": True}}}))
    assert moving.moving_choice(out, work, "P1") == "auto-loop"
    moving.write_override(work, "P1", "splat")
    assert moving.moving_choice(out, work, "P1") == "splat"


def test_splat_plan_skips_moving(tmp_path):
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps({
        "candidates": [{
            "set_id": "P1",
            "asset_ids": [f"a{i}" for i in range(10)],
            "verified_type": "object scan/walk-around",
            "junk_frames": [],
        }],
    }))
    verified = tmp_path / "verified.json"
    verified.write_text(json.dumps({"P1": {"pair_kinds": {"parallax": 8}}}))
    moving_file = tmp_path / "moving.json"
    moving_file.write_text(json.dumps({"rules": moving.rules(), "sets": {"P1": {"moving": True}}}))
    row = splat.make_plan(candidates, tmp_path / "cache", work_dir=tmp_path / "work")["sets"][0]
    assert "subject moves" in (row.get("skip") or "")


def test_splat_plan_static_presence_not_skipped(tmp_path):
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps({
        "candidates": [{
            "set_id": "S1",
            "asset_ids": [f"a{i}" for i in range(10)],
            "verified_type": "object scan/walk-around",
            "junk_frames": [],
        }],
    }))
    (tmp_path / "verified.json").write_text(json.dumps({"S1": {"pair_kinds": {"parallax": 8}}}))
    (tmp_path / "moving.json").write_text(json.dumps({
        "rules": moving.rules(),
        "sets": {"S1": {"moving": False, "presence_only": True}},
    }))
    row = splat.make_plan(candidates, tmp_path / "cache")["sets"][0]
    assert not row.get("skip") or "subject moves" not in row["skip"]


def test_explicit_unmark(tmp_path):
    out = tmp_path / "moving.json"
    out.write_text(json.dumps({"rules": moving.rules(), "sets": {"P1": {"moving": True}}}))
    moving.mark(out, ["P1"], moving=False)
    assert moving.moving_sets(out) == set()


def test_missing_file_means_none():
    assert moving.moving_sets("nowhere/moving.json") == set()


def test_review_moving_override(tmp_path):
    from fuseshot.review import Review

    work = tmp_path / "work"
    work.mkdir()
    cand = tmp_path / "candidates.json"
    cand.write_text(json.dumps({"candidates": []}))
    page = Review(work, cand)
    page.set_moving({"set": "P1", "moving": "splat"})
    ov = json.loads((work / "moving_overrides.json").read_text())
    assert "P1" in ov["unmarked"]
    page.set_moving({"set": "P1", "moving": "auto"})
    ov = json.loads((work / "moving_overrides.json").read_text())
    assert "P1" not in ov["unmarked"]
