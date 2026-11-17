"""Immich client behaviour beyond upload tests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


from fuseshot.immich import Immich, multipart

KEY = "k"


class PagedImmich:
    def __init__(self):
        self.pages = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _handler(self):
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/api/server/version":
                    return self._json(200, {"major": 2, "minor": 0, "patch": 0})
                if self.path.startswith("/api/assets/") and self.path.endswith("/original"):
                    data = b"jpeg bytes"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length) if length else b""
                if self.path == "/api/search/metadata":
                    srv.pages += 1
                    if srv.pages == 1:
                        return self._json(200, {"assets": {"items": [{"id": "1"}], "nextPage": "2"}})
                    return self._json(200, {"assets": {"items": [{"id": "2"}]}})
                self.send_response(404)
                self.end_headers()

            def _json(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("x-api-key", KEY)
                self.end_headers()
                self.wfile.write(data)

        return H


def test_multipart_builds_form():
    body, ctype = multipart({"a": "1"}, [("f", "x.jpg", b"data")])
    assert b"multipart/form-data" in ctype.encode() or "boundary" in ctype
    assert b"x.jpg" in body and b"data" in body


def test_iter_assets_pages():
    fake = PagedImmich()
    client = Immich(fake.url, KEY, timeout=3)
    items = list(client.iter_assets("timeline"))
    assert len(items) == 2 and fake.pages == 2


def test_download_original_writes(tmp_path):
    fake = PagedImmich()
    client = Immich(fake.url, KEY, timeout=3)
    dest = tmp_path / "dl.jpg"
    nbytes = client.download_original("0001", dest)
    assert nbytes == len(dest.read_bytes()) == len(b"jpeg bytes")


def test_retries_on_500(monkeypatch):
    import io
    import urllib.error

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] < 2:
            raise urllib.error.HTTPError(request.full_url, 503, "x", {}, None)
        return io.BytesIO(json.dumps({"major": 1}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = Immich("http://127.0.0.1:1", "k", timeout=1)
    assert client.major_version() == 1
    assert calls["n"] == 2
