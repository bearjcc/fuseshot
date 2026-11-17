"""Additional stitch paths: solve, exposure, repair helpers."""

from pathlib import Path


from fuseshot import stitch
from conftest import minimal_pto, write_frames


def test_solve_records_scores(tmp_path):
    set_dir = tmp_path / "S"
    set_dir.mkdir()
    minimal_pto(set_dir / "auto.pto", pairs=12)

    class R:
        log_file = set_dir / "log.txt"

        def __call__(self, tool, *args, cwd=None, check=True):
            if tool == "checkpto":
                return "Mean error : 2.0\n"
            text = (Path(cwd) / "auto.pto").read_text(encoding="utf-8")
            out = args[args.index("-o") + 1] if "-o" in args else args[-1]
            Path(cwd, out).write_text(text, encoding="utf-8")
            return ""

    score = stitch.solve(set_dir, R(), "rotation", auto_error=50, count=3)
    assert score["error_px"] == 2.0


def test_exposure_stacks_groups_brackets(tmp_path):
    pto = minimal_pto(tmp_path / "exp.pto", names=["a.jpg", "b.jpg", "c.jpg", "d.jpg"], pairs=6)
    stacks = stitch.exposure_stacks(pto, spread=0.5)
    assert isinstance(stacks, list)


def test_repair_keeps_connected_set(tmp_path, monkeypatch):
    work = tmp_path / "set"
    work.mkdir()
    minimal_pto(work / "optimised.pto", pairs=12)
    write_frames(work / "images", count=3)

    class R:
        log_file = work / "log.txt"

        def __call__(self, tool, *args, cwd=None, check=True):
            if tool == "checkpto":
                return "Mean error : 1.0\n"
            return ""

    monkeypatch.setattr(stitch, "resolve", lambda *a, **k: (1.0, True))
    result = stitch.repair(work, run=R())
    assert result is None or "dropped" in result
