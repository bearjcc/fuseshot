"""group.check with mocked matches and a fake YuNet detector."""


import cv2
import numpy as np

from fuseshot import action, group


def _write_thumbs(cache, asset_ids, shape=(120, 160)):
    for aid in asset_ids:
        path = action.thumb_path(cache, aid)
        path.parent.mkdir(parents=True, exist_ok=True)
        grey = np.full(shape, 128, np.uint8)
        cv2.imwrite(str(path), grey)


def _write_matches(cache, asset_ids):
    key = action.set_key(asset_ids)
    path = cache / "verify" / f"{key}.npz"
    path.parent.mkdir(parents=True, exist_ok=True)
    pairs = np.array([[0, 1], [1, 2], [2, 3]], dtype=np.int32)
    H = np.stack([np.eye(3, dtype=np.float64)] * len(pairs))
    stats = np.array([[200, 120, 0], [200, 120, 0], [200, 120, 0]], dtype=np.float64)
    sizes = np.array([[shape[1], shape[0]] for shape in ([(120, 160)] * 4)], dtype=np.float64)
    np.savez(path, pairs=pairs, H=H, stats=stats, sizes=sizes)


class FakeDetector:
    def setInputSize(self, size):
        pass

    def detect(self, image):
        h, w = image.shape[:2]
        faces = np.array([
            [w * 0.25, h * 0.2, 40, 50, 0.95] + [0] * 10,
            [w * 0.55, h * 0.2, 40, 50, 0.93] + [0] * 10,
        ], dtype=np.float32)
        return len(faces), faces


def test_check_marks_still_group_photos(tmp_path):
    cache = tmp_path / "cache"
    ids = ["a1", "b2", "c3", "d4"]
    _write_thumbs(cache, ids)
    _write_matches(cache, ids)
    for aid in ids:
        colour = cache / "colour" / aid[:2] / f"{aid}.jpg"
        colour.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(colour), np.full((240, 320, 3), 180, np.uint8))
    cand = {
        "set_id": "G1",
        "asset_ids": ids,
        "median_gap_s": 2,
        "span_s": 30,
    }
    out = group.check(cand, cache, FakeDetector())
    assert out.get("group") or "faces" in str(out.get("reason", ""))


def test_steady_share_counts_aligned_faces():
    a = np.array([[10, 10, 40, 40, 0.9] + [0] * 10], dtype=np.float32)
    b = np.array([[12, 12, 40, 40, 0.9] + [0] * 10], dtype=np.float32)
    share = group.steady_share(a, b, np.eye(3), 1.0, 1.0)
    assert share is not None and share > 0.5
