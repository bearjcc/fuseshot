"""group.scaled and middle_size."""


import numpy as np
from PIL import Image

from fuseshot import group


def test_scaled_scales_boxes_and_homographies():
    analysis = {
        "size": [400, 300],
        "faces": [{"box": [10, 20, 30, 40], "points": [1, 2, 3], "centre": [5, 6]}],
        "homographies": [np.eye(3).tolist()],
    }
    out = group.scaled(analysis, 2.0)
    assert out["size"] == [800, 600]
    assert out["faces"][0]["box"][0] == 20.0


def test_middle_size_reads_manifest_image(tmp_path):
    img = tmp_path / "m.jpg"
    Image.new("RGB", (320, 240), (1, 2, 3)).save(img)
    manifest = [{"image": str(img)}]
    assert group.middle_size(manifest) == (320, 240)
