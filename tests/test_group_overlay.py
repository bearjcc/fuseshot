"""group overlay and head_box."""

import numpy as np

from fuseshot import group


def test_head_box_expands_face():
    box = (50, 40, 20, 24)
    x0, y0, x1, y1 = group.head_box(box, (200, 300))
    assert x0 < 50 < x1
    assert y1 - y0 > 24


def test_overlay_draws_outline():
    image = np.zeros((120, 160, 3), dtype=np.uint8)
    record = {"people": [{"box": [40, 30, 30, 30], "source_frame": 0, "id": 1, "swapped": False}]}
    out = group.overlay(image, record)
    assert out.shape == image.shape
    assert out.sum() > 0
