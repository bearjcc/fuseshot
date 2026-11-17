"""action.register on synthetic frames."""

import cv2
import numpy as np

from fuseshot import action


def test_register_aligns_similar_frames():
    rng = np.random.default_rng(0)
    base = rng.integers(0, 255, (200, 250, 3), np.uint8)
    warp = cv2.warpAffine(base, np.float32([[1, 0, 3], [0, 1, 2]]), (250, 200))
    found = action.register([warp, base], reference=1)
    assert len(found) == 2
    assert np.allclose(found[1], np.eye(3), atol=1e-3)
    assert found[0] is not None
