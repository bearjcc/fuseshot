"""group.compose base-only path (no swap)."""

import json

import numpy as np

from fuseshot import group


def test_compose_writes_group_json(tmp_path, monkeypatch):
    out = tmp_path / "G1"
    out.mkdir()
    images = [np.full((300, 400, 3), 100 + n * 20, np.uint8) for n in range(3)]
    manifest = [{"asset_id": f"a{n}", "image": f"x{n}.jpg"} for n in range(3)]
    analysis = {
        "size": [400, 300],
        "homographies": [np.eye(3).tolist() for _ in range(3)],
        "tracks": [{"id": 0, "faces": [0, 1]}],
        "faces": [
            {"frame": 0, "box": [80, 70, 35, 40], "score": 0.9, "eyes": 0.8, "turn": 5,
             "sharp": 100, "exposure": 0.7, "smile": 0.2,
             "points": [80, 70, 115, 70, 97, 85, 88, 100, 108, 100]},
            {"frame": 1, "box": [82, 72, 35, 40], "score": 0.7, "eyes": 0.7, "turn": 6,
             "sharp": 80, "exposure": 0.7, "smile": 0.1,
             "points": [82, 72, 117, 72, 99, 87, 90, 102, 110, 102]},
        ],
    }
    monkeypatch.setattr(group, "swap", lambda *a, **k: (None, "skip"))
    group.compose(out, images, manifest, analysis, {"base": 0}, log=lambda *a: None)
    assert (out / "group.json").is_file()
    assert json.loads((out / "group.json").read_text())["base_frame"] == 0
