"""Pure geometry helpers in group.py."""

import numpy as np

from fuseshot import group


def test_spread_reports_low_drift_for_identity():
    chain = {"a": np.eye(3), "b": np.eye(3)}
    drift, ratio = group.spread(chain, (200, 150))
    assert drift < 0.01
    assert 0.9 < ratio < 1.1


def test_pick_frames_downsamples():
    frames = list(range(30))
    picked = group.pick_frames(frames, limit=5)
    assert len(picked) == 5
    assert picked[0] == 0


def test_landmark_turn_centred_nose():
    face = np.zeros(15, dtype=np.float32)
    face[4:6] = (40, 50)
    face[6:8] = (60, 50)
    face[8:10] = (50, 55)
    assert group.landmark_turn(face) < 5.0
