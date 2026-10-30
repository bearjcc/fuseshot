import os

import pytest

from fuseshot import config, tools
from fuseshot.__main__ import main


def test_home_from_environment(home):
    assert config.home() == home
    assert config.settings_path() == home / "fuseshot.toml"
    assert config.settings() == {}


def test_settings_file_is_read(home):
    (home / "fuseshot.toml").write_text('output = "/photos/out"\n[jobs]\nrunner = "local"\n', encoding="utf-8")
    config.setup(home)
    assert config.settings()["output"] == "/photos/out"
    assert config.section("jobs") == {"runner": "local"}
    assert config.section("missing") == {}


def test_broken_settings_name_the_file(home):
    (home / "fuseshot.toml").write_text("output = \n", encoding="utf-8")
    with pytest.raises(config.SettingsError, match="fuseshot.toml"):
        config.setup(home)


def test_tool_found_from_environment(home, tmp_path, monkeypatch):
    folder = tmp_path / "hugin" / "bin"
    folder.mkdir(parents=True)
    program = folder / ("nona" + tools.EXE)
    program.write_text("")
    monkeypatch.setenv("FUSESHOT_HUGIN", str(folder))
    assert tools.find("hugin") == program
    assert "cpfind" in tools.missing_parts("hugin", program) or tools.shutil.which("cpfind")


def test_tool_found_under_home(home):
    folder = home / "tools" / "brush-1.0"
    folder.mkdir(parents=True)
    program = folder / ("brush_app" + tools.EXE)
    program.write_text("")
    assert tools.find("brush") == program


def test_missing_tool_says_where_to_get_it(home, monkeypatch):
    monkeypatch.setenv("FUSESHOT_COLMAP", str(home / "nowhere"))
    with pytest.raises(tools.ToolMissing, match="FUSESHOT_COLMAP"):
        tools.require("colmap")


def test_unknown_settings_are_named(home, capsys):
    (home / "fuseshot.toml").write_text('outptu = "/photos/out"\n[jobs]\nrunner = "local"\n', encoding="utf-8")
    config.setup(home)
    assert config.unknown_keys() == ["outptu"]
    with pytest.raises(SystemExit):
        main(["--home", str(home), "--source", f"folder:{home / 'nowhere'}", "sync"])
    assert "outptu" in capsys.readouterr().err


def test_known_settings_pass_quietly(home):
    (home / "fuseshot.toml").write_text('output = "/photos/out"\n[jobs]\nrunner = "local"\n', encoding="utf-8")
    config.setup(home)
    assert config.unknown_keys() == []


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions and a normal user")
def test_home_that_cannot_be_written_says_so(home, tmp_path, capsys):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        assert "--home" in config.home_problem(locked / "fuseshot")
        with pytest.raises(SystemExit):
            main(["--home", str(locked / "fuseshot"), "--source", f"folder:{tmp_path}", "sync"])
        err = capsys.readouterr().err
        assert "cannot write to the home folder" in err and "Errno" not in err
    finally:
        locked.chmod(0o755)


def test_home_that_does_not_exist_yet_is_fine(tmp_path):
    assert config.home_problem(tmp_path / "new" / "fuseshot") is None


def test_help_lists_the_everyday_commands_first(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    listed = [line.split()[0] for line in out.splitlines() if line.startswith("    ") and not line.startswith("     ")]
    assert listed[:5] == ["init", "run", "review", "export", "doctor"]


def test_missing_list_names_the_step_that_makes_it(home, capsys):
    with pytest.raises(SystemExit):
        main(["--home", str(home), "--source", f"folder:{home}", "group", "--scan"])
    err = capsys.readouterr().err
    assert "fuseshot detect" in err and "Errno" not in err
