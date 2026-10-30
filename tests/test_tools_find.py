"""Hugin is found in Program Files wherever Windows puts it."""

import ntpath
import os

from fuseshot import tools


def test_hugin_found_under_program_files(tmp_path, monkeypatch, home):
    bin_dir = tmp_path / "Programs" / "Hugin" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / ("nona" + tools.EXE)).write_bytes(b"")
    monkeypatch.delenv("FUSESHOT_HUGIN", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(tools.os, "name", "nt")
    monkeypatch.setattr(os.path, "expandvars", ntpath.expandvars)
    monkeypatch.setenv("ProgramW6432", str(tmp_path / "Programs").replace("/", "\\"))
    monkeypatch.setattr(tools, "_program_in", lambda folder, p: (bin_dir / p) if str(folder).replace("\\", "/").endswith(
        "Programs/Hugin/bin") and (bin_dir / p).exists() else None)
    assert tools.find("hugin") == bin_dir / "nona"
