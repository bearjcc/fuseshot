"""Full-size export temp dirs and locking."""

from pathlib import Path
from uuid import UUID

import numpy as np
from PIL import Image

from fuseshot import stitch
from conftest import minimal_pto


def _stub_render_full_internals(monkeypatch):
    class MockRunner:
        def __init__(self, log_file):
            self.log_file = Path(log_file)

        def __call__(self, tool, *args, cwd=None, check=True):
            if tool == "nona":
                prefix = Path(args[args.index("-o") + 1])
                Image.fromarray(np.uint8(np.random.rand(40, 50, 4) * 255), "RGBA").save(
                    prefix.parent / f"{prefix.name}0000.tif"
                )
            if tool == "pano_modify" and "-o" in args:
                dest = Path(args[args.index("-o") + 1])
                src = Path(args[-1])
                if src.is_file():
                    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                else:
                    minimal_pto(dest, pairs=6)
            return ""

    def fake_blend(run, tmp, work, layers, outer, inner, orientation, fuse):
        flat = Image.new("RGB", (50, 40), (10, 20, 30))
        return flat, flat.copy(), flat.convert("RGBA"), None, "single", 0, None

    monkeypatch.setattr(stitch, "Runner", MockRunner)
    monkeypatch.setattr(stitch, "blend", fake_blend)


def _minimal_set(tmp_path, name="cylindrical"):
    set_dir = tmp_path / "P1"
    previews = set_dir / "previews"
    previews.mkdir(parents=True)
    minimal_pto(previews / f"{name}.pto", pairs=8)
    Image.new("RGB", (80, 40), (1, 2, 3)).save(previews / f"{name}.jpg")
    (set_dir / "batch.json").write_text("{}", encoding="utf-8")
    return set_dir, name


def test_render_full_uses_unique_tmp_not_shared_full_tmp(tmp_path, monkeypatch):
    set_dir, name = _minimal_set(tmp_path)
    _stub_render_full_internals(monkeypatch)
    ids = iter(range(1, 4))
    monkeypatch.setattr(stitch.uuid, "uuid4", lambda: UUID(int=next(ids)))
    stitch.render_full(set_dir, name, log=None)
    stitch.render_full(set_dir, name, log=None)
    assert not (set_dir / "full_tmp").exists()
    assert not list(set_dir.glob("full_tmp_*"))
    assert (set_dir / "full" / f"{name}.jpg").is_file()


def test_full_render_lock_is_one_per_set_dir(tmp_path):
    set_a = tmp_path / "S1"
    set_b = tmp_path / "S2"
    lock_a1 = stitch._full_render_lock(set_a)
    lock_a2 = stitch._full_render_lock(set_a)
    lock_b = stitch._full_render_lock(set_b)
    assert lock_a1 is lock_a2
    assert lock_a1 is not lock_b
    assert lock_a1.acquire(blocking=False)
    assert not lock_a2.acquire(blocking=False)
    lock_a1.release()
