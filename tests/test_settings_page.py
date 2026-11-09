"""The settings page, and the first-run setup it stands in for when nothing is set up."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from fuseshot import config, settings_web
from fuseshot.review import Review, make_handler

KEY = "abc123secretkey"


@pytest.fixture
def site(home, tmp_path):
    review = Review(tmp_path / "work", base_path="/fuseshot")
    review.reconfigure = lambda: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(review))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/fuseshot"
    server.shutdown()
    server.server_close()


def ask(url, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json", **(headers or {})} if body is not None else dict(headers or {})
    request = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


def test_a_fresh_home_asks_for_setup(site):
    status, text = ask(site + "/api/items")
    assert status == 200 and json.loads(text)["setup"] is True
    status, text = ask(site + "/settings")
    assert status == 200 and "settings.js" not in text and "review.js" in text
    found = json.loads(ask(site + "/api/settings")[1])
    assert found["needs_setup"] and found["can_change"] and not found["immich"]["key_saved"]


def test_saving_keeps_the_key_out_of_the_settings_file(site, home):
    (home / "fuseshot.toml").write_text("# my notes\nmax_megapixels = 40\n", encoding="utf-8")
    body = {"immich": {"on": True, "url": "http://photos.local:2283/", "key": KEY},
            "outputs": {"splats": False}}
    status, text = ask(site + "/settings", body)
    assert status == 200, text
    assert KEY not in text
    saved = (home / "fuseshot.toml").read_text(encoding="utf-8")
    assert KEY not in saved and "# my notes" in saved and "max_megapixels = 40" in saved
    assert "http://photos.local:2283" in saved and "splats = false" in saved
    assert (home / "secrets" / "immich_key").read_text(encoding="utf-8") == KEY
    assert (home / "fuseshot.toml.bak").is_file()
    found = json.loads(ask(site + "/api/settings")[1])
    assert found["immich"]["key_saved"] and not found["needs_setup"]
    assert KEY not in json.dumps(found)
    assert config.wants("panoramas") and not config.wants("splats")
    assert json.loads(ask(site + "/api/items")[1])["setup"] is False


def test_folders_are_checked_and_kept_apart_from_the_output(site, tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    bad = {"folders": [{"path": str(tmp_path / "nowhere")}]}
    assert ask(site + "/settings", bad)[0] == 400
    inside = {"folders": [{"path": str(photos), "recursive": True}], "output": str(photos / "out")}
    assert ask(site + "/settings", inside)[0] == 400
    good = {"folders": [{"path": str(photos), "recursive": True}], "output": str(tmp_path / "results")}
    status, text = ask(site + "/settings", good)
    assert status == 200, text
    assert json.loads(text)["folders"] == [{"path": str(photos), "recursive": True}]
    assert ask(site + "/settings", {"folders": [], "colour": "red"})[0] == 400
    assert ask(site + "/settings", {"folders": []})[0] == 400


def test_immich_needs_a_key_and_a_plain_address(site):
    assert ask(site + "/settings", {"immich": {"on": True, "url": "http://photos.local"}})[0] == 400
    with_login = {"immich": {"on": True, "url": "http://me:pw@photos.local", "key": KEY}}
    assert ask(site + "/settings", with_login)[0] == 400
    assert ask(site + "/settings", {"immich": {"on": True, "url": "ftp://photos.local", "key": KEY}})[0] == 400


def test_the_folder_browser_lists_folders_only(site, tmp_path):
    (tmp_path / "pick" / "Trip").mkdir(parents=True)
    (tmp_path / "pick" / ".hidden").mkdir()
    (tmp_path / "pick" / "photo.jpg").write_bytes(b"x")
    status, text = ask(site + "/settings/browse", {"path": str(tmp_path / "pick")})
    found = json.loads(text)
    assert status == 200 and found["folders"] == ["Trip"] and found["parent"] == str(tmp_path)
    assert ask(site + "/settings/browse", {"path": "relative/path"})[0] == 400
    assert json.loads(ask(site + "/settings/browse", {})[1])["roots"]


def test_changes_through_a_proxy_need_a_token(site, tmp_path):
    proxied = {"X-Forwarded-For": "100.64.0.9"}
    status, _ = ask(site + "/settings/browse", {"path": str(tmp_path)}, proxied)
    assert status == 403
    status, _ = ask(site + "/settings", {"folders": [{"path": str(tmp_path)}]}, {"Tailscale-User-Login": "x@y"})
    assert status == 403
    assert json.loads(ask(site + "/api/settings", headers=proxied)[1])["can_change"] is False


def test_changes_from_another_site_are_refused(site, tmp_path):
    status, _ = ask(site + "/settings", {"folders": [{"path": str(tmp_path)}]}, {"Origin": "http://evil.example"})
    assert status == 403


def test_a_saved_key_or_settings_skip_setup(home):
    assert settings_web.needs_setup()
    assert not settings_web.needs_setup(cli_sources=True)
    (home / "secrets").mkdir()
    (home / "secrets" / "immich_key").write_text(KEY, encoding="utf-8")
    assert not settings_web.needs_setup()


def test_testing_immich_reports_who_signed_in(home, monkeypatch):
    calls = []

    def fake(self, method, path, **kw):
        calls.append((path, self.timeout))
        return {"/api/users/me": {"name": "Sam"}, "/api/assets/statistics": {"total": 12}}[path]

    monkeypatch.setattr("fuseshot.immich.Immich._request", fake)
    found = settings_web.test_immich({"url": "http://photos.local", "key": KEY})
    assert found == {"ok": True, "user": "Sam", "assets": 12, "images": None, "videos": None}
    assert all(timeout <= 10 for _, timeout in calls)
    with pytest.raises(settings_web.SetupError):
        settings_web.test_immich({"url": "http://photos.local"})


def test_every_output_reports_its_status(home):
    found = settings_web.output_status()
    assert set(found) == set(config.OUTPUTS)
    assert found["composites"]["ready"]
    for status in found.values():
        assert status["ready"] or status["hints"]


def test_only_one_scan_at_a_time(home, monkeypatch):
    started = []

    class Alive:
        def __init__(self, cmd, **kw):
            started.append(cmd)

        def poll(self):
            return None

        def wait(self):
            return 0

    from fuseshot import pipeline

    monkeypatch.setattr(pipeline, "_proc", None)
    monkeypatch.setattr(pipeline.subprocess, "Popen", Alive)
    monkeypatch.setattr(pipeline, "_watch_process", lambda proc: None)
    monkeypatch.setattr(settings_web, "needs_setup", lambda *a, **k: False)
    with pytest.raises(settings_web.SetupError):
        settings_web.start_scan(running=True)
    assert settings_web.start_scan()["started"]
    assert started[0][-1] == "run"
    with pytest.raises(settings_web.SetupError):
        settings_web.start_scan()
    assert len(started) == 1
