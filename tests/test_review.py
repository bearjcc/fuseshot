"""The review page is reachable from other machines only with its token, and changes need the same origin."""

import http.client
import json
import os
import socket
import subprocess
import sys
import time

import pytest

TOKEN = "test-token-123"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(home):
    port = free_port()
    env = dict(os.environ, FUSESHOT_HOME=str(home), FUSESHOT_REVIEW_TOKEN=TOKEN)
    proc = subprocess.Popen([sys.executable, "-m", "fuseshot", "review", "--host", "0.0.0.0", "--port", str(port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield port
    proc.terminate()
    proc.wait(10)
    assert TOKEN not in proc.stderr.read().decode()


def request(port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    response.read()
    return response


def signed_in(port):
    cookie = request(port, "GET", f"/?token={TOKEN}").getheader("Set-Cookie").split(";")[0]
    return {"Cookie": cookie}


def test_page_needs_the_token(server):
    assert request(server, "GET", "/").status == 401
    assert request(server, "GET", "/?token=wrong").status == 401


def test_token_link_sets_a_cookie_and_drops_the_token(server):
    response = request(server, "GET", f"/?token={TOKEN}")
    assert response.status == 303
    assert TOKEN not in response.getheader("Location")
    cookie = response.getheader("Set-Cookie")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert request(server, "GET", "/", signed_in(server)).status == 200


def test_changes_need_json_and_the_same_origin(server):
    headers = signed_in(server)
    body = json.dumps({"set_id": "P00001", "decision": "kept"})
    form = dict(headers, **{"Content-Type": "application/x-www-form-urlencoded"})
    assert request(server, "POST", "/decision", form, body).status == 403
    evil = dict(headers, **{"Content-Type": "application/json", "Origin": "https://evil.example"})
    assert request(server, "POST", "/decision", evil, body).status == 403


@pytest.mark.parametrize("path", ["/static/..%2F..%2Fpyproject.toml", "/vendor/../../fuseshot.toml",
                                  "/img/..%2F..%2Ffuseshot.toml/x", "/frame/..%2F..%2Fcache/assets.sqlite",
                                  "/splatfile/..%2F..%2F/fuseshot.toml"])
def test_paths_stay_inside(server, home, path):
    (home / "fuseshot.toml").write_text("secret = 1\n", encoding="utf-8")
    assert request(server, "GET", path, signed_in(server)).status == 404


def test_pages_say_where_the_source_is(server):
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=10)
    conn.request("GET", "/", headers=signed_in(server))
    assert "github.com/bearjcc/fuseshot" in conn.getresponse().read().decode()


def test_starting_the_page_renames_old_decisions_and_keeps_a_backup(home):
    work = home / "work"
    work.mkdir(exist_ok=True)
    old = {"P00001": [{"decision": "approved", "note": "", "at": "2026-01-01T00:00:00+00:00"}]}
    (work / "reviews.json").write_text(json.dumps(old), encoding="utf-8")
    original = (work / "reviews.json").read_bytes()
    port = free_port()
    env = dict(os.environ, FUSESHOT_HOME=str(home))
    proc = subprocess.Popen([sys.executable, "-m", "fuseshot", "review", "--port", str(port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        assert (work / "reviews.json.bak").read_bytes() == original
        assert json.loads((work / "reviews.json").read_text())["P00001"][0]["decision"] == "kept"
    finally:
        proc.terminate()
        proc.wait(10)
