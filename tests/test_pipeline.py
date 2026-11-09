import json
import subprocess

import pytest

from fuseshot import pipeline, runtime, settings_web


def test_fuseshot_argv_frozen(home, monkeypatch):
    monkeypatch.setattr(runtime.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime.sys, "executable", "/app/Fuseshot.exe")
    assert runtime.fuseshot_argv(["run"]) == ["/app/Fuseshot.exe", "--home", str(home), "run"]


def test_fuseshot_argv_dev(home, monkeypatch):
    monkeypatch.delattr(runtime.sys, "frozen", raising=False)
    out = runtime.fuseshot_argv(["sync"], home=home)
    assert out[0] == runtime.sys.executable
    assert "-m" in out and "fuseshot" in out
    assert out[-1] == "sync"


def test_checkpoint_writes_paused(home, monkeypatch):
    pipeline.touch_stop()
    assert pipeline.checkpoint("after sync")
    data = json.loads(pipeline.state_path().read_text(encoding="utf-8"))
    assert data["state"] == "paused"
    assert data["paused_at"] == "after sync"


def test_start_uses_fuseshot_argv(home, monkeypatch):
    calls = []

    class Proc:
        def __init__(self, cmd, **kw):
            calls.append(cmd)
            self._code = 0

        def poll(self):
            return None

        def wait(self):
            return self._code

    monkeypatch.setattr(settings_web, "needs_setup", lambda *a, **k: False)
    monkeypatch.setattr(subprocess, "Popen", Proc)
    pipeline.start()
    assert calls
    assert calls[0][-1] == "run"
    assert "-m" in calls[0]


def test_start_scan_delegates(home, monkeypatch):
    got = []

    monkeypatch.setattr(pipeline, "active", lambda: False)
    monkeypatch.setattr(pipeline, "start", lambda: got.append(1) or {"started": True, "log": "x"})
    assert settings_web.start_scan()["started"]
    assert got


def test_pause_requires_active(home):
    with pytest.raises(settings_web.SetupError):
        pipeline.pause()


def test_build_ps1_native_helper():
    from pathlib import Path

    text = Path(__file__).resolve().parents[1].joinpath("packaging", "windows", "build.ps1").read_text(encoding="utf-8")
    assert "Invoke-NativeCommand" in text
    assert "ErrorActionPreference = 'Continue'" in text
    assert "LASTEXITCODE" in text
