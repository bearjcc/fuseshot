"""splat helpers: gate_settings, SetLock, video_rows."""

import json
import os

import pytest

from fuseshot import splat


def test_gate_settings_matches_module_constants():
    g = splat.gate_settings()
    assert g["min_registered_ratio"] == splat.GATE_RATIO


def test_set_lock_exclusive(tmp_path):
    lock = tmp_path / "lock"
    with splat.SetLock(lock):
        assert lock.is_file()
        with pytest.raises(OSError):
            os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    assert not lock.exists()


def test_video_rows_lists_stereo_sets(tmp_path):
    work = tmp_path / "work"
    out = splat.splat_dir(work, "SV1")
    out.mkdir(parents=True)
    (out / "frames.json").write_text(json.dumps({
        "set_id": "SV1", "video": "clip.mp4", "frames": [{"index": 0}, {"index": 1}],
    }), encoding="utf-8")
    rows = splat.video_rows(work)
    assert rows and rows[0]["set_id"] == "SV1"
