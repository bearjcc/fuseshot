"""group.check early exits and rejection reasons."""

import numpy as np

from fuseshot import group


class TwoFaceDetector:
    def setInputSize(self, size):
        pass

    def detect(self, image):
        h, w = image.shape[:2]
        faces = np.array([
            [w * 0.25, h * 0.3, 50, 60, 0.95] + [0] * 10,
            [w * 0.55, h * 0.3, 50, 60, 0.92] + [0] * 10,
        ], dtype=np.float32)
        return len(faces), faces


def test_check_rejects_bracket_and_slow_sets():
    det = TwoFaceDetector()
    base = {"set_id": "G", "asset_ids": ["a", "b"], "median_gap_s": 1, "span_s": 5}
    out = group.check({**base, "bracket": True}, "cache", det)
    assert "bracket" in out["reason"]
    out = group.check({**base, "median_gap_s": 99}, "cache", det)
    assert "too slow" in out["reason"]


def test_check_missing_previews(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    cand = {"set_id": "G", "asset_ids": ["x"], "median_gap_s": 1, "span_s": 5}
    out = group.check(cand, cache, TwoFaceDetector())
    assert out["reason"] == "previews missing"


def test_landmark_turn_and_crop_quality():
    face = np.zeros(15, dtype=np.float32)
    face[4:8] = (10, 20, 90, 20)
    face[8:10] = (55, 40)
    turn = group.landmark_turn(face)
    assert 0 <= turn <= 60
    aligned = np.full((120, 120, 3), 140, np.uint8)
    sharp, exposure = group.crop_quality(aligned)
    assert sharp >= 0 and 0 <= exposure <= 1


def test_pick_frames_limits_count():
    frames = list(range(20))
    picked = group.pick_frames(frames, limit=5)
    assert len(picked) <= 5
    assert group.pick_frames(frames[:3], limit=5) == [0, 1, 2]
