import os
from unittest import mock

import pytest

from fuseshot import desktop, runtime, tools


def test_runtime_paths_not_frozen(monkeypatch):
    monkeypatch.delattr(runtime.sys, "frozen", raising=False)
    assert not runtime.frozen()
    assert runtime.install_dir().is_dir()
    assert runtime.packaged_tool_bins() == []


def test_pick_port_localhost():
    port = desktop.pick_port("127.0.0.1", 49152)
    assert 49152 <= port <= 49175 or port > 49152


def test_session_roundtrip(home, monkeypatch):
    monkeypatch.setattr(desktop, "_server_up", lambda *a, **k: True)
    monkeypatch.setattr(desktop, "_pid_alive", lambda pid: pid == os.getpid())
    desktop.write_session("127.0.0.1", 8765)
    data = desktop.read_session()
    assert data["port"] == 8765
    desktop.clear_session()
    assert desktop.read_session() is None


def test_prepare_exits_when_session_live(home, monkeypatch):
    monkeypatch.setattr(desktop, "read_session", lambda: {"host": "127.0.0.1", "port": 9, "base_path": ""})
    opened = []
    monkeypatch.setattr(desktop, "open_session", lambda d: opened.append(d))
    args = mock.Mock(host="127.0.0.1", port=8765, open=False)
    with pytest.raises(SystemExit):
        desktop.prepare_review_args(args)
    assert opened


def test_tools_finds_packaged_hugin(tmp_path, monkeypatch):
    bindir = tmp_path / "tools" / "hugin" / "bin"
    bindir.mkdir(parents=True)
    name = "nona.exe" if os.name == "nt" else "nona"
    (bindir / name).write_bytes(b"")
    monkeypatch.setattr(runtime, "frozen", lambda: True)
    monkeypatch.setattr(runtime, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(runtime, "packaged_tool_bins", lambda: [bindir])
    found = tools.find("hugin")
    assert found is not None and found.name.startswith("nona")
