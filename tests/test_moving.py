import json

import numpy as np
import pytest

from fuseshot import moving, splat


def det(klass, cx=0.5, cy=0.5, frac=0.05):
    return {"class": klass, "cx": cx, "cy": cy, "frac": frac}


W, H = 1200, 900


def pan_pose(dx_norm, dy_norm=0.0, reliable=True):
    Hm = np.eye(3, dtype=np.float64)
    Hm[0, 2] = dx_norm * W
    Hm[1, 2] = dy_norm * H
    return {"H": Hm, "reliable": reliable, "w": W, "h": H}


def still_poses(n):
    return [pan_pose(0.0) for _ in range(n - 1)]


def test_presence_without_independent_motion_is_not_moving():
    frames = [[det("person")] for _ in range(5)]
    out = moving.judge(frames, still_poses(5))
    assert out["presence_only"]
    assert not out["moving"]
    assert not out.get("uncertain")


def test_orbit_statue_compensated_by_homography():
    """Box shift that matches camera pan should not auto-route to loops."""
    frames = [
        [det("person", 0.30, 0.5)],
        [det("person", 0.40, 0.5)],
        [det("person", 0.50, 0.5)],
        [det("person", 0.60, 0.5)],
        [det("person", 0.70, 0.5)],
    ]
    poses = [pan_pose(0.10), pan_pose(0.10), pan_pose(0.10), pan_pose(0.10)]
    out = moving.judge(frames, poses)
    assert out["presence_only"]
    assert not out["moving"]


def test_static_vehicle_display_with_still_camera():
    frames = [[det("car", 0.5, 0.6)] for _ in range(6)]
    assert not moving.judge(frames, still_poses(6))["moving"]


def test_genuine_independent_motion_still_camera():
    frames = [
        [det("bird", 0.15, 0.5)],
        [det("bird", 0.35, 0.5)],
        [det("bird", 0.55, 0.5)],
        [det("bird", 0.75, 0.5)],
        [det("bird", 0.9, 0.5)],
    ]
    out = moving.judge(frames, still_poses(5))
    assert out["moving"]
    assert out["subject"] == "bird"


def test_camera_pan_static_subject():
    frames = [
        [det("person", 0.70, 0.5)],
        [det("person", 0.60, 0.5)],
        [det("person", 0.50, 0.5)],
        [det("person", 0.40, 0.5)],
        [det("person", 0.30, 0.5)],
    ]
    poses = [pan_pose(-0.10)] * 4
    out = moving.judge(frames, poses)
    assert not out["moving"]
    assert out["presence_only"]


def test_extra_motion_beyond_camera_motion():
    frames = [
        [det("person", 0.3, 0.5)],
        [det("person", 0.42, 0.5)],
        [det("person", 0.70, 0.5)],
        [det("person", 0.82, 0.5)],
        [det("person", 0.94, 0.5)],
    ]
    poses = [pan_pose(0.05), pan_pose(0.05), pan_pose(0.05), pan_pose(0.05)]
    out = moving.judge(frames, poses)
    assert out["moving"]


def test_uncertain_when_poses_unreliable():
    frames = [[det("bird", 0.2 + 0.15 * i, 0.5)] for i in range(5)]
    poses = [pan_pose(0.15, reliable=False)] * 4
    out = moving.judge(frames, poses)
    assert out["uncertain"]
    assert not out["moving"]
    assert out["presence_only"]


def test_no_poses_means_no_auto_motion():
    frames = [[det("bird", 0.2 + 0.15 * i, 0.5)] for i in range(5)]
    out = moving.judge(frames, [])
    assert not out["moving"]
    assert out["presence_only"]


def test_sparse_presence_is_not_moving():
    assert not moving.judge([[det("bird")], [], [], [det("bird")]], still_poses(4))["moving"]


def test_motion_frac_tracks_one_class():
    frames = [
        [det("person", 0.2, 0.5), det("car", 0.8, 0.5)],
        [det("person", 0.45, 0.5), det("car", 0.8, 0.5)],
        [det("person", 0.70, 0.5), det("car", 0.8, 0.5)],
        [det("person", 0.95, 0.5), det("car", 0.8, 0.5)],
    ]
    out = moving.judge(frames, still_poses(4))
    assert out["moving"]
    assert out["subject"] == "person"


def test_warp_norm_matches_pan():
    pred = moving.warp_norm(0.3, 0.5, pan_pose(0.1)["H"], W, H)
    assert pred is not None
    assert abs(pred[0] - 0.4) < 0.01
    assert abs(pred[1] - 0.5) < 0.01


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
    (tmp_path / "verified.json").write_text(json.dumps({"P1": {"pair_kinds": {"parallax": 8}}}))
    (tmp_path / "moving.json").write_text(json.dumps({"rules": moving.rules(), "sets": {"P1": {"moving": True}}}))
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
        "sets": {"S1": {"moving": False, "presence_only": True, "uncertain": True}},
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
