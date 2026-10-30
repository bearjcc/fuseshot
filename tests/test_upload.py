"""Uploads go to a stand-in Immich server here, never a real one."""

import hashlib
import json
import threading
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

from fuseshot import config, upload
from fuseshot.immich import Immich

KEY = "not-a-real-key"


class FakeImmich:
    """Answers the few calls an upload makes and remembers every request."""

    def __init__(self, major=3):
        self.major = major
        self.requests = []
        self.assets = {}
        self.albums = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                fake.requests.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                if self.headers.get("x-api-key") != KEY:
                    return self.reply(401, {"message": "Invalid API key"})
                return fake.answer(self, body)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = handle_any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def answer(self, handler, body):
        method, path = handler.command, handler.path
        if method == "GET" and path == "/api/server/version":
            return handler.reply(200, {"major": self.major, "minor": 0, "patch": 0})
        if method == "POST" and path == "/api/assets":
            form = BytesParser(policy=HTTP).parsebytes(
                b"Content-Type: " + handler.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
            parts = {p.get_param("name", header="content-disposition"): p for p in form.iter_parts()}
            has_device = "deviceId" in parts or "deviceAssetId" in parts
            if has_device != (self.major < 3):
                return handler.reply(400, {"message": "wrong device fields"})
            data = parts["assetData"].get_payload(decode=True)
            checksum = hashlib.sha1(data).hexdigest()
            if checksum in self.assets:
                return handler.reply(200, {"id": self.assets[checksum]["id"], "status": "duplicate"})
            asset = {"id": f"00000000-0000-4000-8000-{len(self.assets):012d}", "fields": {
                name: part.get_content() for name, part in parts.items() if name not in ("assetData", "sidecarData")},
                "sidecar": parts["sidecarData"].get_payload(decode=True).decode() if "sidecarData" in parts else None,
                "filename": parts["assetData"].get_filename()}
            self.assets[checksum] = asset
            return handler.reply(201, {"id": asset["id"], "status": "created"})
        if method == "GET" and path == "/api/albums":
            return handler.reply(200, [{"id": a["id"], "albumName": a["albumName"]} for a in self.albums])
        if method == "POST" and path == "/api/albums":
            album = {"id": f"album-{len(self.albums)}", "albumName": json.loads(body)["albumName"], "assets": []}
            self.albums.append(album)
            return handler.reply(201, album)
        if method == "PUT" and path.startswith("/api/albums/") and path.endswith("/assets"):
            album = next(a for a in self.albums if a["id"] == path.split("/")[3])
            album["assets"] += json.loads(body)["ids"]
            return handler.reply(200, [{"id": i, "success": True} for i in json.loads(body)["ids"]])
        return handler.reply(404, {"message": "not here"})

    def close(self):
        self.server.shutdown()


class FakeReview:
    """Just enough of the review state for an upload: kept items and their result files."""

    def __init__(self, work_dir):
        self.work_dir = Path(work_dir)
        self.groups_dir = self.work_dir / "groups"
        self.loops_dir = self.work_dir / "actions"
        self.splats_dir = self.work_dir / "splats"
        self.found = []
        self.sets = {}

    def add(self, kind, set_id, library="immich", decision="kept", fill=False):
        if kind == "group":
            path = self.groups_dir / set_id / "merged.jpg"
        elif kind == "action":
            path = self.loops_dir / set_id / "animation.webp"
        elif kind == "splat":
            path = self.splats_dir / set_id / "scene.splat"
        else:
            path = self.work_dir / set_id / "previews" / ("cylindrical" + ("_fill" if fill else "") + ".jpg")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".splat":
            path.write_bytes(b"splat")
        else:
            Image.new("RGB", (64, 32), (len(self.found) * 40, 90, 120)).save(path)
        self.found.append({"kind": kind, "id": set_id, "key": f"{kind}:{set_id}", "library": library,
                           "decision": decision, "taken": "2024-03-09 14:00:00",
                           "preview": path.stem if kind == "stitch" else None})
        self.sets[set_id] = {"set_id": set_id, "start_local": "2024-03-09 14:00:00",
                             "start_utc": "2024-03-09T01:00:00+00:00", "lat": -36.8485, "lon": 174.7633,
                             "asset_ids": ["a", "b", "c", "d"]}

    def items(self):
        return {"items": self.found}

    def decisions(self):
        return {}

    def last_decision(self, decisions, key, kind):
        return {}

    def candidates(self):
        return list(self.sets.values())


@pytest.fixture
def fake_immich():
    server = FakeImmich()
    yield server
    server.close()


@pytest.fixture
def review(home):
    found = FakeReview(home / "work")
    found.add("stitch", "P00001", fill=True)
    found.add("group", "P00002")
    found.add("action", "P00003")
    found.add("splat", "P00004")
    found.add("stitch", "P00005", library="folder")
    found.add("group", "P00006", decision="discarded")
    return found


def test_off_unless_turned_on(home):
    assert upload.settings() == {"immich": False, "album": "Fuseshot"}
    (home / "fuseshot.toml").write_text('[upload]\nimmich = true\nalbum = ""\n')
    config.setup(home)
    assert upload.settings() == {"immich": True, "album": None}


def test_kept_immich_results_become_new_assets(review, fake_immich):
    sent = upload.Uploader(review, Immich(fake_immich.url, KEY), log=lambda m: None).run()
    assert sorted(s["key"] for s in sent) == ["action:P00003", "group:P00002", "stitch:P00001"]
    assets = {a["filename"]: a for a in fake_immich.assets.values()}
    assert set(assets) == {"2024-03-09 P00001 panorama.jpg", "2024-03-09 P00002 group.jpg",
                           "2024-03-09 P00003 loop.webp"}
    pano = assets["2024-03-09 P00001 panorama.jpg"]
    assert pano["fields"]["fileCreatedAt"] == "2024-03-09T14:00:00+13:00"
    assert "deviceId" not in pano["fields"]
    assert "<exif:DateTimeOriginal>2024-03-09T14:00:00+13:00</exif:DateTimeOriginal>" in pano["sidecar"]
    assert "<exif:GPSLatitude>36,50.910000S</exif:GPSLatitude>" in pano["sidecar"]
    assert "<exif:GPSLongitude>174,45.798000E</exif:GPSLongitude>" in pano["sidecar"]
    assert "from 4 photos" in pano["sidecar"] and "AI model" in pano["sidecar"]
    group = assets["2024-03-09 P00002 group.jpg"]
    assert "faces come from different shots" in group["sidecar"] and "AI model" not in group["sidecar"]
    assert fake_immich.albums[0]["albumName"] == "Fuseshot"
    assert sorted(fake_immich.albums[0]["assets"]) == sorted(a["id"] for a in assets.values())


def test_checksum_header_matches_the_file(review, fake_immich):
    upload.Uploader(review, Immich(fake_immich.url, KEY), log=lambda m: None).run()
    posts = [r for r in fake_immich.requests if r[1] == "/api/assets"]
    assert {r[2]["x-immich-checksum"] for r in posts} == set(fake_immich.assets)


def test_running_again_sends_nothing(review, fake_immich):
    first = upload.Uploader(review, Immich(fake_immich.url, KEY), log=lambda m: None)
    first.run()
    before = len(fake_immich.requests)
    assert upload.Uploader(review, Immich(fake_immich.url, KEY), log=lambda m: None).run() == []
    assert len(fake_immich.requests) == before
    record = json.loads((review.work_dir / "uploads.json").read_text())
    assert len(record["items"]["group:P00002"]) == 1
    assert record["albums"] == {"Fuseshot": "album-0"}


def test_lost_record_gets_immichs_duplicate_answer(review, fake_immich):
    upload.Uploader(review, Immich(fake_immich.url, KEY), log=lambda m: None).run()
    (review.work_dir / "uploads.json").unlink()
    sent = upload.Uploader(review, Immich(fake_immich.url, KEY), album=None, log=lambda m: None).run()
    assert {s["status"] for s in sent} == {"duplicate"}
    assert len(fake_immich.assets) == 3


def test_older_immich_gets_device_fields(review):
    server = FakeImmich(major=2)
    try:
        sent = upload.Uploader(review, Immich(server.url, KEY), album=None, log=lambda m: None).run()
        assert len(sent) == 3
        assert all(a["fields"]["deviceId"] == "fuseshot" for a in server.assets.values())
        assert not server.albums
    finally:
        server.close()


def test_only_adds_never_changes_or_deletes(review, fake_immich):
    upload.Uploader(review, Immich(fake_immich.url, KEY), log=lambda m: None).run()
    for method, path, _, _ in fake_immich.requests:
        assert method in ("GET", "POST", "PUT")
        assert method != "PUT" or path.startswith("/api/albums/")
        assert method != "POST" or path in ("/api/assets", "/api/albums")


def test_dry_run_sends_nothing(review, fake_immich):
    sent = upload.Uploader(review, Immich(fake_immich.url, KEY), log=lambda m: None).run(dry_run=True)
    assert len(sent) == 3
    assert fake_immich.requests == []
    assert not (review.work_dir / "uploads.json").exists()


def test_keep_uploads_after_the_undo_window(home, monkeypatch):
    from fuseshot import review as review_module

    monkeypatch.setattr(review_module, "UPLOAD_DELAY_S", 0)
    page = review_module.Review(home / "work")
    done = threading.Event()
    asked = []

    class Stub:
        def run(self, keys):
            asked.append(keys)
            done.set()

    page.export_later({"group:P00002"})
    assert not done.wait(0.2)
    page.uploader = Stub()
    page.export_later({"group:P00002"})
    assert done.wait(5)
    assert asked == [{"group:P00002"}]
