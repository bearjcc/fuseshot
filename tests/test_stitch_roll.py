"""Roll and orientation helpers."""

from fuseshot import stitch


def test_roll_group_picks_centre():
    rolls = [1.0, 50.0, 2.0]
    turn, count = stitch.roll_group(rolls, centre=1, tolerance=10)
    assert count >= 1


def test_choose_turn_portrait():
    rolls = [0.0, 90.0, 5.0]
    scores = [1.0, 0.5, 1.2]
    portrait = [False, True, False]
    turn, share, reason = stitch.choose_turn(rolls, scores, portrait)
    assert isinstance(reason, str)
