import json

from fuseshot import moving


def test_judge_needs_half_the_shots():
    assert moving.judge([{"bird"}, {"bird"}, set(), set()])["moving"]
    assert not moving.judge([{"bird"}, set(), set(), set()])["moving"]
    assert moving.judge([{"person"}, {"person"}, set(), set(), set()])["moving"]
    assert moving.judge([{"person"}, {"person"}, {"person"}])["subject"] == "person"
    assert not moving.judge([])["moving"]


def test_boats_are_not_moving():
    assert "boat" not in moving.MOVING


def test_marks_join_scanned_sets(tmp_path):
    out = tmp_path / "moving.json"
    out.write_text(json.dumps({"rules": moving.rules(), "sets": {"P1": {"moving": True}, "P2": {"moving": False}}}))
    moving.mark(out, ["P3"])
    assert moving.moving_sets(out) == {"P1", "P3"}
    moving.mark(out, ["P3"], moving=False)
    assert moving.moving_sets(out) == {"P1"}


def test_missing_file_means_none():
    assert moving.moving_sets("nowhere/moving.json") == set()
