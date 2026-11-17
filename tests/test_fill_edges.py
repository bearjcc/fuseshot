"""fill.py error and seam paths with torch mocked."""


import cv2
import numpy as np
import pytest

from fuseshot import fill
from fuseshot.stitch import preview_name


def test_hole_mask_seams_channel(tmp_path):
    image = np.zeros((80, 100, 3), np.uint8)
    image[20:60, 25:75] = (200, 100, 50)
    mask = fill.hole_mask(tmp_path, "x_seams", image)
    assert mask.shape == (80, 100)


def test_fill_rejects_missing_preview(tmp_path, monkeypatch):
    set_dir = tmp_path / "S"
    (set_dir / "previews").mkdir(parents=True)
    name = preview_name("cylindrical", "level")
    monkeypatch.setitem(__import__("sys").modules, "torch", object())
    with pytest.raises(RuntimeError, match="no preview"):
        fill.fill(set_dir, name)


def test_inpaint_called_for_small_hole(tmp_path, monkeypatch):
    set_dir = tmp_path / "S"
    previews = set_dir / "previews"
    previews.mkdir(parents=True)
    name = preview_name("cylindrical", "level")
    art = np.zeros((100, 120, 3), np.uint8)
    art[:, 0] = fill.BACKGROUND[::-1]
    art[40:60, 40:60] = (255, 0, 0)
    cv2.imwrite(str(previews / f"{name}.jpg"), art)
    (set_dir / "batch.json").write_text("{}", encoding="utf-8")
    seen = []

    def track_inpaint(*args, **kwargs):
        seen.append(True)
        return np.zeros_like(args[3])

    monkeypatch.setattr(fill, "inpaint", track_inpaint)
    monkeypatch.setattr("fuseshot.fill.assets.model", lambda n: tmp_path / "m.pt")
    (tmp_path / "m.pt").write_bytes(b"x")

    class FakeTorch:
        class cuda:
            @staticmethod
            def is_available():
                return False

        jit = type("J", (), {"load": staticmethod(lambda *a, **k: type("M", (), {"eval": lambda s: s})())})

    monkeypatch.setitem(__import__("sys").modules, "torch", FakeTorch)
    fill.fill(set_dir, name, device="cpu", force=True)
    assert seen
