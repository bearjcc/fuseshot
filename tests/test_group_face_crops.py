"""group.face_crops and write_jpeg."""

import cv2
import numpy as np

from fuseshot import group


def test_face_crops_writes_files(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    images = [np.full((200, 250, 3), n * 40, np.uint8) for n in range(2)]
    analysis = {
        "faces": [
            {"frame": 0, "box": [80, 60, 30, 35], "id": 1},
            {"frame": 1, "box": [82, 62, 30, 35], "id": 1},
        ],
        "tracks": [{"id": 1, "faces": [0, 1]}],
    }
    for f in analysis["faces"]:
        f.setdefault("points", [0.0] * 10)
    group.face_crops(out, images, analysis)
    crops = list((out / "faces").glob("*.jpg"))
    assert crops


def test_write_jpeg_roundtrip(tmp_path):
    path = tmp_path / "x.jpg"
    img = np.full((50, 60, 3), 128, np.uint8)
    group.write_jpeg(path, img)
    loaded = cv2.imread(str(path))
    assert loaded is not None and loaded.shape == img.shape
