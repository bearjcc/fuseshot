"""blend_layers merges two synthetic RGBA tiles."""


import numpy as np

from fuseshot import blend


def rgba_tiff(path, offset=(0, 0), size=(80, 60)):
    arr = np.zeros((size[1], size[0], 4), dtype=np.uint8)
    arr[..., 3] = 255
    arr[..., :3] = (40 + offset[0], 90, 140)
    import cv2
    cv2.imwrite(str(path), cv2.cvtColor(arr, cv2.COLOR_RGBA2BGRA))


def test_blend_layers_writes_png(tmp_path):
    a = tmp_path / "layer0000.tif"
    b = tmp_path / "layer0001.tif"
    rgba_tiff(a, offset=(0, 0))
    rgba_tiff(b, offset=(20, 0))
    dest = tmp_path / "blend.png"
    seams = blend.blend_layers([a, b], dest, [1.0, 1.5])
    assert dest.is_file()
    assert seams is not None
