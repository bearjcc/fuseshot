"""stitch.solve copies projects with a mocked Runner."""

import shutil
from pathlib import Path

from fuseshot import stitch
from conftest import minimal_pto


def test_solve_keeps_auto_when_error_high(tmp_path):
    set_dir = tmp_path / "set"
    set_dir.mkdir()
    minimal_pto(set_dir / "auto.pto", pairs=10)
    shutil.copy(set_dir / "auto.pto", set_dir / "refine_clean_rotation.pto")

    class Run:
        steps = []

        def __call__(self, tool, *args, cwd=None, check=True):
            cwd = Path(cwd)
            if tool == "checkpto":
                return "Mean error : 99.0 pixels"
            if "-o" in args:
                dest = Path(args[args.index("-o") + 1])
                src = Path(args[-1])
                if src.exists():
                    shutil.copy(src, dest)
                else:
                    minimal_pto(dest, pairs=8)
            return ""

    score = stitch.solve(set_dir, Run(), "rotation", auto_error=5.0, count=3)
    assert score["kept_solution"] == "auto"
    assert score["largest_group"] >= 1
