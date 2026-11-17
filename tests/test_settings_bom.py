"""Settings files saved with a UTF-8 BOM must load."""

from fuseshot import config, settings_web


def test_config_load_strips_utf8_bom(home):
    path = home / "fuseshot.toml"
    path.write_bytes(b"\xef\xbb\xbfoutput = \"/photos/out\"\n")
    config.setup(home)
    config._settings = None
    assert config.settings()["output"] == "/photos/out"


def test_settings_web_save_reads_bom(home):
    path = home / "fuseshot.toml"
    path.write_bytes(b"\xef\xbb\xbfoutput = \"/old\"\n")
    pics = home / "pics"
    pics.mkdir()
    config.setup(home)
    body = {
        "immich": {"on": False, "url": "", "key": ""},
        "folders": [{"path": str(pics), "recursive": False}],
        "output": str(home / "out"),
        "outputs": {k: True for k in settings_web.OUTPUTS},
    }
    settings_web.save(body)
    config._settings = None
    assert config.settings()["output"] == str(home / "out")