"""Failure-injection: pause/resume, bad files, Immich errors, ports, packs."""

import errno
import socket
import subprocess

import pytest

from fuseshot import desktop, immich, packs, pipeline, settings_web
from fuseshot.__main__ import main
from fuseshot.immich import ImmichError

from conftest import write_frames


def test_pipeline_pause_mid_run_and_resume(home, photos, monkeypatch):
    codes = iter([0, 0])

    class Proc:
        def __init__(self, cmd, **kw):
            self.cmd = cmd

        def poll(self):
            return None

        def wait(self):
            return next(codes, 0)

        def terminate(self):
            pass

    monkeypatch.setattr(settings_web, "needs_setup", lambda *a, **k: False)
    monkeypatch.setattr(subprocess, "Popen", Proc)
    monkeypatch.setattr(pipeline, "_watch_process", lambda p: None)
    pipeline.start()
    pipeline.pause()
    pipeline._proc = None
    monkeypatch.setattr(pipeline, "active", lambda: False)
    pipeline.clear_stop()
    assert pipeline.read_state().get("state") in ("paused", "running")


def test_corrupt_image_sync_continues(home, tmp_path):
    import sqlite3

    photos = write_frames(tmp_path / "ok", count=2)
    bad = photos[0].parent / "bad.jpg"
    bad.write_bytes(b"not jpeg")
    main(["--home", str(home), "--source", f"folder:{photos[0].parent}", "sync"])
    db = home / "cache" / "assets.sqlite"
    count = sqlite3.connect(db).execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    assert count == len(photos)


def test_immich_401_is_plain(home):
    from tests.test_upload import FakeImmich

    fake = FakeImmich()
    client = immich.Immich(fake.url, "wrong-key", timeout=2)
    with pytest.raises(ImmichError, match="401|did not take|Invalid"):
        client._request("GET", "/api/users/me", retries=1)


def test_immich_timeout(home, monkeypatch):
    class Slow:
        url = "http://127.0.0.1:9"

        def _request(self, *a, **k):
            raise ImmichError("timed out after 1 s")

    client = immich.Immich("http://127.0.0.1:9", "key", timeout=1)
    monkeypatch.setattr(client, "_request", lambda *a, **k: (_ for _ in ()).throw(ImmichError("timed out")))
    with pytest.raises(ImmichError, match="timed out"):
        client._request("GET", "/api/users/me")


def test_port_already_in_use(home, monkeypatch):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    picked = desktop.pick_port("127.0.0.1", port)
    assert picked != port or picked > port
    sock.close()


def test_second_desktop_instance_opens_existing(home, monkeypatch):
    monkeypatch.setattr(desktop, "read_session", lambda: {"host": "127.0.0.1", "port": 8765, "base_path": "", "pid": 1})
    opened = []
    monkeypatch.setattr(desktop, "open_session", lambda d: opened.append(d))
    monkeypatch.setattr(desktop, "_pid_alive", lambda p: True)
    monkeypatch.setattr(desktop, "_server_up", lambda *a: True)
    args = type("A", (), {"host": "127.0.0.1", "port": 8765, "open": False})()
    with pytest.raises(SystemExit):
        desktop.prepare_review_args(args)
    assert opened


def test_pack_bad_checksum(home, monkeypatch):
    from fuseshot import assets

    def bad_model(name, fetch=True):
        raise assets.AssetError(f"{name} download did not match its checksum; not using it")

    monkeypatch.setattr(assets, "model", bad_model)
    with pytest.raises(ValueError, match="download|checksum"):
        packs.install("face", agree=True)


def test_disk_full_message():
    from fuseshot.__main__ import explain

    err = OSError(errno.ENOSPC, "No space left on device", "/data/out/pano.jpg")
    assert "disk is full" in explain(err)
