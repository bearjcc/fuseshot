"""Doctor and fetch helpers."""

import json

import pytest
from PIL import Image

from fuseshot import doctor, fetch
from conftest import write_frames


def test_doctor_reports_home_ok(home, monkeypatch):
    monkeypatch.setattr(doctor, "_python_has", lambda *a, **k: False)
    rows = doctor.check()
    home_row = next(r for r in rows["checks"] if r["name"] == "home folder")
    assert home_row["state"] == "ok"


def test_fetch_load_candidate_round_trip(tmp_path):
    cand = {"set_id": "S9", "asset_ids": ["a", "b"]}
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"candidates": [cand]}), encoding="utf-8")
    assert fetch.load_candidate(path, "S9") == cand
    assert fetch.asset_ids(cand) == ["a", "b"]
    with pytest.raises(SystemExit):
        fetch.load_candidate(path, "missing")


def test_needs_turning_respects_exif(tmp_path):
    img = Image.new("RGB", (20, 10), (1, 2, 3))
    exif = Image.Exif()
    exif[0x0112] = 6
    path = tmp_path / "rot.jpg"
    img.save(path, exif=exif.tobytes())
    assert fetch.needs_turning(path)
    import shutil

    plain = tmp_path / "plain.jpg"
    shutil.copy(write_frames(tmp_path / "p", count=1)[0], plain)
    assert not fetch.needs_turning(plain)
