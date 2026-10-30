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
