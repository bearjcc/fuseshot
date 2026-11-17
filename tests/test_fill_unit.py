"""LaMa fill helpers without loading torch."""

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from fuseshot import fill
from fuseshot.stitch import preview_name


def test_hole_mask_uses_background_at_edge():
    image = np.zeros((100, 80, 3), np.uint8)
    image[:] = fill.BACKGROUND[::-1]
    image[40:60, 30:50] = (200, 100, 50)
    mask = fill.hole_mask(Path("unused"), "x", image)
    assert mask.shape == (100, 80)
    assert mask[0, 0] and not mask[45, 40]


def test_tiles_cover_image():
    rects = fill.tiles(2000, 3000)
    assert rects
    h_hi = max(r[1] for r in rects)
    w_hi = max(r[3] for r in rects)
    assert h_hi >= 2000 and w_hi >= 3000


def test_fill_rejects_huge_hole_without_force(tmp_path, monkeypatch):
    set_dir = tmp_path / "P1"
    previews = set_dir / "previews"
    previews.mkdir(parents=True)
    name = preview_name("cylindrical", "level")
    blank = np.full((200, 200, 3), fill.BACKGROUND[::-1], np.uint8)
    cv2.imwrite(str(previews / f"{name}.jpg"), blank)
    (set_dir / "batch.json").write_text("{}", encoding="utf-8")

    class FakeTorch:
        class cuda:
            @staticmethod
            def is_available():
                return False

            @staticmethod
            def reset_peak_memory_stats():
                pass

            @staticmethod
            def empty_cache():
                pass

            @staticmethod
            def max_memory_allocated():
                return 0

            @staticmethod
            def max_memory_reserved():
                return 0

    FakeTorch.jit = type("J", (), {"load": staticmethod(lambda *a, **k: type("M", (), {"eval": lambda self: self})())})

    monkeypatch.setitem(__import__("sys").modules, "torch", FakeTorch)
    monkeypatch.setattr("fuseshot.fill.assets.model", lambda name: tmp_path / "m.pt")
    (tmp_path / "m.pt").write_bytes(b"0")
    with pytest.raises(RuntimeError, match="invent most"):
        fill.fill(set_dir, name, device="cpu", force=False)


def test_fill_updates_renders_json_when_torch_mocked(tmp_path, monkeypatch):
    set_dir = tmp_path / "P2"
    previews = set_dir / "previews"
    previews.mkdir(parents=True)
    name = preview_name("cylindrical", "level")
    art = np.zeros((120, 160, 3), np.uint8)
    art[50:70, 50:70] = (255, 0, 0)
    art[0, :] = fill.BACKGROUND[::-1]
    cv2.imwrite(str(previews / f"{name}.jpg"), art)
    (set_dir / "batch.json").write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(fill, "inpaint", lambda *a, **k: np.zeros_like(a[3]))
    monkeypatch.setattr("fuseshot.fill.assets.model", lambda name: tmp_path / "model.pt")
    (tmp_path / "model.pt").write_bytes(b"fake")

    class FakeTorch:
        class cuda:
            @staticmethod
            def is_available():
                return False

            @staticmethod
            def reset_peak_memory_stats():
                pass

            @staticmethod
            def empty_cache():
                pass

            @staticmethod
            def max_memory_allocated():
                return 0

            @staticmethod
            def max_memory_reserved():
                return 0

    FakeTorch.jit = type("J", (), {"load": staticmethod(lambda *a, **k: type("M", (), {"eval": lambda self: self})())})

    monkeypatch.setitem(__import__("sys").modules, "torch", FakeTorch)
    info = fill.fill(set_dir, name, device="cpu", force=True)
    assert info["ai_filled"]
    renders = json.loads((previews / "renders.json").read_text(encoding="utf-8"))
    assert renders[name]["fill"]["model"] == fill.MODEL_NAME
    assert (previews / f"{name}_fill.jpg").is_file()
