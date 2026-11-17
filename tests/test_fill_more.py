"""fill rejection paths and peak_ram."""

import sys

import cv2
import numpy as np
import pytest

from fuseshot import fill
from fuseshot.stitch import preview_name


class FakeTorch:
    class cuda:
        @staticmethod
        def is_available():
            return False

    jit = type("J", (), {"load": staticmethod(lambda *a, **k: type("M", (), {"eval": lambda s: s})())})


def test_fill_rejects_huge_hole(tmp_path, monkeypatch):
    set_dir = tmp_path / "S"
    previews = set_dir / "previews"
    previews.mkdir(parents=True)
    name = preview_name("cylindrical", "level")
    art = np.full((80, 80, 3), fill.BACKGROUND[::-1], np.uint8)
    art[38:42, 38:42] = (200, 100, 50)
    monkeypatch.setattr(fill, "hole_mask", lambda *a, **k: np.ones((80, 80), dtype=bool))
    cv2.imwrite(str(previews / f"{name}.jpg"), art)
    monkeypatch.setitem(__import__("sys").modules, "torch", FakeTorch)
    monkeypatch.setattr("fuseshot.fill.assets.model", lambda n: tmp_path / "m.pt")
    (tmp_path / "m.pt").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="empty"):
        fill.fill(set_dir, name)


def test_fill_cuda_unavailable_raises(tmp_path, monkeypatch):
    set_dir = tmp_path / "S"
    previews = set_dir / "previews"
    previews.mkdir(parents=True)
    name = preview_name("cylindrical", "level")
    cv2.imwrite(str(previews / f"{name}.jpg"), np.zeros((40, 40, 3), np.uint8))
    monkeypatch.setitem(__import__("sys").modules, "torch", FakeTorch)
    with pytest.raises(RuntimeError, match="CUDA"):
        fill.fill(set_dir, name, device="cuda")


@pytest.mark.skipif(sys.platform != "linux", reason="peak_ram uses Linux /proc")
def test_peak_ram_mb_reads_proc():
    mb = fill.peak_ram_mb()
    assert mb is None or mb > 0
