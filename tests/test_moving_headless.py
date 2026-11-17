"""Moving scan must not abort a headless run when MediaPipe cannot open GPU libs."""

import json

import pytest

from fuseshot.__main__ import main

from conftest import write_frames

LIGHT = ("run", "--no-stitch", "--no-loops", "--no-groups")


def test_moving_scan_survives_detector_failure(home, photos, monkeypatch):
    folder = photos[0].parent

    def broken():
        raise OSError(2, "libEGL.so.1: cannot open shared object file")

    monkeypatch.setattr("fuseshot.moving.detector", broken)
    main(["--home", str(home), "--source", f"folder:{folder}", *LIGHT])
    assert json.loads((home / "output" / "candidates.json").read_text())["candidates"]


def test_moving_step_skips_when_detector_fails(tmp_path, monkeypatch):
    cand = tmp_path / "candidates.json"
    cand.write_text('{"candidates": []}', encoding="utf-8")
    out = tmp_path / "moving.json"
    from fuseshot import moving

    def broken():
        raise OSError("no EGL")

    lines = []
    monkeypatch.setattr(moving, "detector", broken)
    moving.scan(cand, tmp_path / "cache", out, log=lines.append)
    assert any("skipped" in line.lower() for line in lines)