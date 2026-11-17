"""group.swap and choose_base with synthetic faces."""

import numpy as np

from fuseshot import group


def _face(x, y):
    pts = np.array([x, y, 30, 35, 0.9] + [float(v) for v in (
        x, y, x + 30, y, x + 15, y - 5, x + 10, y + 20, x + 20, y + 20
    )], dtype=np.float32)
    return {"box": [x, y, 30, 35], "points": [float(v) for v in pts[5:15]]}


def test_swap_blends_donor_face():
    base = np.full((200, 250, 3), 100, np.uint8)
    donor = np.full((200, 250, 3), 180, np.uint8)
    bf, df = _face(80, 60), _face(82, 62)
    H = np.eye(3)
    out, info = group.swap(base, donor, bf, df, H)
    assert out is not None and info["landmark_error"] >= 0


def test_choose_base_picks_best_scoring_frame():
    analysis = {
        "faces": [
            {"frame": 0, "score": 0.3, "sharp": 1, "exposure": 0.5, "turn": 0, "eyes": 0.5},
            {"frame": 1, "score": 0.9, "sharp": 1, "exposure": 0.5, "turn": 0, "eyes": 0.5},
        ],
        "tracks": [{"faces": [0]}, {"faces": [1]}],
        "homographies": [np.eye(3).tolist() for _ in range(2)],
    }
    assert group.choose_base(analysis, 2) == 1
