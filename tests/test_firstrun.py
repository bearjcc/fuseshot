"""fuseshot init, the run that finds nothing, and the computer-sized limits."""

import tomllib

import pytest

from fuseshot import config, explain, limits
from fuseshot.__main__ import main
from fuseshot.firstrun import to_toml
from fuseshot.sources import FolderSource


def test_settings_survive_the_round_trip():
    settings = {"output": "C:\\Users\\dad\\Pictures\\Fuseshot",
                "sources": [{"type": "folder", "path": "D:\\Takeout\\Google Photos", "recursive": True},
                            {"type": "folder", "path": "/media/dad's drive", "recursive": True}],
                "tools": {"hugin": "C:\\Program Files\\Hugin\\bin"}, "jobs": {"limit": 3, "on": False}}
    assert tomllib.loads(to_toml(settings)) == settings


def test_init_writes_recursive_folders_and_keeps_other_settings(home, tmp_path, capsys):
    photos, out = tmp_path / "photos", tmp_path / "results"
    photos.mkdir()
    (home / "fuseshot.toml").write_text('[tools]\nhugin = "/opt/hugin/bin"\n', encoding="utf-8")
    main(["init", str(photos), "--output-folder", str(out)])
    written = tomllib.loads((home / "fuseshot.toml").read_text(encoding="utf-8"))
    assert written["sources"] == [{"type": "folder", "path": str(photos.resolve()), "recursive": True}]
    assert written["output"] == str(out) and written["tools"] == {"hugin": "/opt/hugin/bin"}
    config._settings = None
    main(["init"])
    assert "already lists photo folders" in capsys.readouterr().out


def test_init_refuses_results_among_the_originals(home, tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    with pytest.raises(SystemExit, match="inside the photo folder"):
        main(["init", str(photos), "--output-folder", str(photos / "Fuseshot")])
    assert not (home / "fuseshot.toml").exists()


def test_photos_in_subfolders_are_pointed_out(tmp_path):
    (tmp_path / "2023" / "June").mkdir(parents=True)
    (tmp_path / "2023" / "June" / "IMG_0001.JPG").write_bytes(b"")
    lines = explain.no_photos([FolderSource(tmp_path)])
    assert "--recursive" in lines[0]
    assert "No photos" in explain.no_photos([FolderSource(tmp_path, recursive=True)])[0]


def test_overrides_win_and_small_computers_get_floors(monkeypatch):
    monkeypatch.setattr(limits, "_memory", lambda: (4096, 1500))
    monkeypatch.setattr(limits, "cores", lambda: 2)
    assert limits.workers() == 1 and limits.tool_mb() == limits.TOOL_FLOOR_MB and limits.align_threads() == 1
    monkeypatch.setattr(limits, "_memory", lambda: (262144, 250000))
    monkeypatch.setattr(limits, "cores", lambda: 16)
    assert limits.workers() == 15 and limits.budget_mb() == 131072 and limits.align_threads() == 8
    monkeypatch.setattr(limits, "WORKERS", 3)
    monkeypatch.setattr(limits, "MEMORY_MB", 6000)
    assert limits.workers() == 3 and limits.tool_mb() == 6000 and limits.align_threads() == 3
