"""parts.py split, subset, and levelling."""

from conftest import minimal_pto

from fuseshot import parts


def test_subset_keeps_selected_frames(tmp_path):
    src = minimal_pto(tmp_path / "full.pto", names=["a.jpg", "b.jpg", "c.jpg", "d.jpg"], pairs=10)
    out = tmp_path / "sub.pto"
    parts.subset(src, [0, 2], out)
    frames, _ = parts.frame_info(out)
    assert len(frames) == 2


def test_level_turn_from_up_vector():
    yaw, tilt = parts.level_turn([0.0, 0.0, 1.0])
    assert abs(yaw) < 1
    assert abs(tilt) < 1


def test_median_even_count():
    assert parts.median([1.0, 2.0, 3.0, 4.0]) == 2.5
