"""Additional review HTTP GET routes."""

import json
import threading
import urllib.request

from fuseshot.review import Review, make_handler
from conftest import stitched_set

from http.server import ThreadingHTTPServer


def get(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


def test_settings_and_pipeline_json(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    stitched_set(work, "P1")
    cand = tmp_path / "output" / "candidates.json"
    cand.parent.mkdir(parents=True)
    cand.write_text(json.dumps({"candidates": [{
        "set_id": "P1", "library": "folder", "asset_ids": ["a"], "type": "single-row rotation",
    }]}), encoding="utf-8")
    review = Review(work, candidates_file=cand, output_dir=tmp_path / "output")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(review))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    code, items = get(base + "/api/items")
    assert code == 200 and items["items"]
    code, settings = get(base + "/api/settings")
    assert code == 200 and "immich" in settings
    code, pipe = get(base + "/api/pipeline")
    assert code == 200
    with urllib.request.urlopen(base + "/failures", timeout=10) as resp:
        body = resp.read()
        assert resp.status == 200
        assert b"<" in body[:20]
    server.shutdown()
