"""blend.plan_layers and read_layer without enblend."""

import numpy as np
from PIL import Image

from fuseshot import blend


def rgba_tiff(path, size=(64, 48), colour=(100, 120, 140)):
    arr = np.zeros((*size, 4), dtype=np.uint8)
    arr[..., 0] = colour[0]
    arr[..., 1] = colour[1]
    arr[..., 2] = colour[2]
    arr[..., 3] = 255
    Image.fromarray(arr, "RGBA").save(path.with_suffix(".png"))
    import cv2
    bgra = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGRA)
    cv2.imwrite(str(path), bgra)


def test_plan_layers_drops_duplicate(tmp_path):
    a = tmp_path / "layer0000.tif"
    b = tmp_path / "layer0001.tif"
    rgba_tiff(a)
    rgba_tiff(b)
    kept, dropped, contained = blend.plan_layers([a, b], [1.0, 2.0])
    assert dropped == 1
    assert len(kept) == 1
    assert not contained


def test_read_layer_normalises_uint8(tmp_path):
    path = tmp_path / "one.tif"
    rgba_tiff(path, colour=(50, 60, 70))
    rgb, mask = blend.read_layer(path)
    assert rgb.shape[2] == 3
    assert mask.shape == rgb.shape[:2]
    assert mask[10, 10]
