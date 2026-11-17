"""splat.colmap subprocess boundary."""

import subprocess

import pytest

from fuseshot import splat


def test_colmap_raises_on_failure(tmp_path, monkeypatch):
    log = tmp_path / "c.log"

    def fail_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="bad news\n", stderr="")

    monkeypatch.setattr(splat.tools, "require", lambda n: "colmap")
    monkeypatch.setattr(splat.subprocess, "run", fail_run)
    with pytest.raises(splat.SplatError, match="bad news"):
        splat.colmap(["version"], log)
    assert "colmap version" in log.read_text(encoding="utf-8")
