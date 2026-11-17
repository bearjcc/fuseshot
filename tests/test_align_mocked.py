"""align() with Hugin tools mocked end-to-end."""

import json
import shutil
from pathlib import Path

from fuseshot import stitch
from conftest import minimal_pto, write_frames


def test_align_rotation_model(tmp_path, monkeypatch):
    photos = write_frames(tmp_path / "photos", count=3)
    set_dir = tmp_path / "S"
    set_dir.mkdir()
    manifest = [{"image": str(p), "width": 1000, "height": 750, "file": p.name} for p in photos]
    (set_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def fake_solve(set_dir, run, model, auto_error, count):
        src = set_dir / "auto.pto"
        shutil.copy(src, set_dir / f"optimised_{model}.pto")
        return {
            "error_px": 2.0,
            "kept_solution": "refined",
            "photometric": True,
            "largest_group": count,
        }

    monkeypatch.setattr(stitch, "solve", fake_solve)

    class Run:
        def __init__(self, log_file):
            self.log_file = Path(log_file)
            self.steps = []

        def __call__(self, tool, *args, cwd=None, check=True):
            cwd = Path(cwd or set_dir)
            if tool == "checkpto":
                return "Mean error : 2.0 pixels"
            if tool == "cpfind":
                minimal_pto(cwd / "points.pto", pairs=12)
                return ""
            if "-o" in args:
                dest = Path(args[args.index("-o") + 1])
                src_name = str(args[-1])
                src = cwd / src_name if not Path(src_name).is_absolute() else Path(src_name)
                if tool == "cpclean" and not src.exists():
                    src = cwd / "points.pto"
                target = dest if dest.is_absolute() else cwd / dest.name
                if src.resolve() == target.resolve():
                    return ""
                if src.exists():
                    shutil.copy(src, target)
                else:
                    minimal_pto(target, pairs=10)
            return ""

    monkeypatch.setattr(stitch, "Runner", Run)
    layout = stitch.align(set_dir, model="rotation", threads=1)
    assert layout["model"]["chosen"] == "rotation"
    assert (set_dir / "layout.json").is_file()
    assert (set_dir / "optimised.pto").is_file()
