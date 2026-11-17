"""edit.queue_fill via LocalRunner."""

import subprocess

from fuseshot import edit, jobs
from conftest import stitched_set


def test_queue_fill_local_runner_runs_subprocess(tmp_path, monkeypatch, home):
    work = tmp_path / "work"
    stitched_set(work, "L1")
    calls = []

    class Local(jobs.LocalRunner):
        pass

    monkeypatch.setattr(jobs, "runner", lambda: Local())
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(jobs, "argv", lambda cmd, parts: ["python", "-m", "fuseshot"] + parts)
    edit.queue_fill(work, "L1", "cylindrical-level")
    assert calls
