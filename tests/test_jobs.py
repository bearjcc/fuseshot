import json
import subprocess
import sys
import time

from fuseshot import jobs


def wait(runner, task, seconds=60):
    end = time.time() + seconds
    while runner.status(task) in ("queued", "running") and time.time() < end:
        time.sleep(0.1)
    return runner.status(task)


def test_local_runner_is_the_default(home):
    assert isinstance(jobs.runner(), jobs.LocalRunner)


def test_local_runner_runs_and_logs(home):
    runner = jobs.LocalRunner()
    task = runner.submit("fill", "check", ["--version"])
    assert wait(runner, task) == "done"
    assert "fuseshot" in (home / "cache" / "jobs" / "check.log").read_text(encoding="utf-8")


def test_local_runner_reports_failure(home):
    runner = jobs.LocalRunner()
    task = runner.submit("fill", "broken", ["no-such-command"])
    assert wait(runner, task) == "failed"
    assert runner.active("broken") is None


def test_job_line_carries_the_home_folder(home):
    line = jobs.shell_line("group", ["group", "G00001"])
    assert str(home) in line and "G00001" in line


def test_local_runner_drops_a_job_that_has_not_started(home, monkeypatch):
    monkeypatch.setattr(jobs, "argv", lambda kind, args: [sys.executable, "-c", *args])
    runner = jobs.LocalRunner()
    first = runner.submit("x", "slow", ["import time; time.sleep(1)"])
    second = runner.submit("x", "stitch-P1", ["open('ran', 'w')"])
    end = time.time() + 10
    while runner.status(first) == "queued" and time.time() < end:
        time.sleep(0.02)
    assert runner.cancel({"stitch-P1", "slow"}) == [second]
    assert wait(runner, first) == "done"
    time.sleep(0.3)
    assert runner.status(second) == "cancelled"
    assert not (home / "ran").exists()


class FakePueue:
    """Stands in for the pueue program: a running task, a queued one, and one queued under another name."""

    def __init__(self):
        self.tasks = {"7": {"label": "fuseshot:stitch-P1", "status": {"Running": {}}},
                      "8": {"label": "fuseshot:stitch-P2", "status": "Queued"},
                      "9": {"label": "fuseshot:edit-P2", "status": {"Queued": {"enqueued_at": "x"}}}}
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd[1:])
        if cmd[1] == "remove":
            for task in cmd[2:]:
                self.tasks.pop(task)
        out = json.dumps({"tasks": self.tasks}) if cmd[1] == "status" else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")


def test_pueue_runner_removes_only_tasks_not_yet_started(monkeypatch):
    fake = FakePueue()
    monkeypatch.setattr(jobs.subprocess, "run", fake)
    runner = jobs.PueueRunner({"pueue": "pueue", "label_prefix": "fuseshot:"})
    assert runner.cancel({"stitch-P1", "stitch-P2"}) == [8]
    assert ["remove", "8"] in fake.calls
    assert set(fake.tasks) == {"7", "9"}
    assert runner.cancel({"stitch-P3"}) == []
    assert not any(call[0] == "remove" for call in fake.calls[3:])
