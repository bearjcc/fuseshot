"""Exposure stacks and crop helpers."""

from pathlib import Path

from conftest import minimal_pto

from fuseshot import stitch


def test_exposure_stacks_groups_by_yaw(tmp_path):
    pto = minimal_pto(tmp_path / "e.pto", names=["a.jpg", "b.jpg", "c.jpg"], pairs=4)
    stacks = stitch.exposure_stacks(pto, spread=50)
    assert stacks and all(isinstance(s, list) for s in stacks)


def test_crop_rect_maps_outer_to_inner():
    outer = (10, 110, 20, 90)
    inner = (30, 90, 30, 70)
    rect = stitch.crop_rect(outer, inner)
    assert len(rect) == 4
    assert 0.0 <= rect[0] <= 1.0


def test_default_orientation_from_pto(tmp_path):
    pto = minimal_pto(tmp_path / "o.pto")
    assert stitch.default_orientation(pto) in ("level", "centred", "vertical")


def test_coverage_uses_pano_line(tmp_path, monkeypatch):
    set_dir = tmp_path / "S"
    set_dir.mkdir()
    minimal_pto(set_dir / "optimised.pto", pairs=12)

    class MockRunner:
        def __init__(self, log_file):
            self.steps = []

        def __call__(self, tool, *args, **kwargs):
            dest = Path(args[args.index("-o") + 1])
            dest.write_text('p f2 w2000 h800 v120.0\n', encoding="utf-8")

    monkeypatch.setattr(stitch, "Runner", MockRunner)
    h, v = stitch.coverage(set_dir)
    assert h == 120.0
    assert v > 0
