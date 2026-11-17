"""action.match helpers without full verify pipeline."""

import numpy as np

from fuseshot import action


def test_corner_shift_identity_is_zero():
    assert action.corner_shift(np.eye(3), (100, 80)) == 0.0


def test_pair_homography_reads_npz_stats(tmp_path):
    ids = ["a", "b", "c"]
    path = tmp_path / "cache" / "verify" / f"{action.set_key(ids)}.npz"
    path.parent.mkdir(parents=True)
    pairs = np.array([[0, 1]], dtype=np.int32)
    H = np.array([np.eye(3).ravel()], dtype=np.float64)
    stats = np.array([[200, 120, 0]], dtype=np.float64)
    sizes = np.array([[160, 120]], dtype=np.float64)
    np.savez(path, pairs=pairs, H=H, stats=stats, sizes=sizes)
    matches = action.load_matches(tmp_path / "cache", ids)
    H, count, inl = action.pair_homography(matches, 0, 1)
    assert count == 200 and inl == 120 and H is not None


def test_frame_pairs_dedupes_copies():
    thumbs = {0: np.zeros((80, 100), np.uint8), 1: np.zeros((80, 100), np.uint8),
              2: np.zeros((80, 100), np.uint8)}
    matches = {"sizes": [[100, 80]] * 3, "pairs": np.zeros((0, 2), int), "stats": np.zeros((0, 3)),
               "H": np.zeros((0, 9))}
    kept, pairs, copies = action.frame_pairs(matches, [0, 1, 2], thumbs)
    assert len(kept) >= 2


def test_moving_share_on_identical_thumbs():
    a = np.random.default_rng(1).integers(0, 255, (60, 80), dtype=np.uint8)
    b = a.copy()
    share = action.moving_share(a, b, np.eye(3))
    assert share is not None and share < 0.1
