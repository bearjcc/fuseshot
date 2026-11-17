"""level() and repair() with mocked Hugin runner."""

import shutil
from pathlib import Path

from fuseshot import stitch
from conftest import minimal_pto


def mock_runner_factory(set_dir):
    class Run:
        def __call__(self, tool, *args, cwd=None, check=True):
            cwd = Path(cwd or set_dir)
            if "-o" in args:
                dest = Path(args[args.index("-o") + 1])
                src = Path(args[-1])
                if src.is_file():
                    shutil.copy(src, dest)
                else:
                    minimal_pto(dest, pairs=8)
            if tool == "checkpto":
                return "Mean error : 2.5 pixels"
            return ""
    return Run()


def test_repair_keeps_project(tmp_path):
    set_dir = tmp_path / "S"
    set_dir.mkdir()
    minimal_pto(set_dir / "optimised.pto", pairs=12)
    run = mock_runner_factory(set_dir)
    stitch.repair(set_dir, run=run, model="rotation")
    assert (set_dir / "optimised.pto").is_file()


def test_level_adjusts_roll(tmp_path):
    set_dir = tmp_path / "S"
    set_dir.mkdir()
    pto = minimal_pto(set_dir / "optimised.pto", pairs=12)
    text = pto.read_text(encoding="utf-8")
    text = text.replace("r0 ", "r5 ", 1)
    pto.write_text(text, encoding="utf-8")
    run = mock_runner_factory(set_dir)
    stitch.level(set_dir, run=run, tolerance=10.0)
    assert (set_dir / "optimised.pto").exists()
