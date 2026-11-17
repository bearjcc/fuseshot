"""Review HTTP routes for decide, discard, and settings auth."""

import json
import threading
import urllib.error
import urllib.request

from fuseshot.review import Review, make_handler
from conftest import stitched_set

from http.server import ThreadingHTTPServer


def ask(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode())


def test_discard_and_keep_routes(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    stitched_set(work, "P1")
    cand = tmp_path / "output" / "candidates.json"
    cand.parent.mkdir(parents=True)
    cand.write_text(json.dumps({"candidates": [{"set_id": "P1", "library": "folder", "asset_ids": ["a"]}]}), encoding="utf-8")
    review = Review(work, candidates_file=cand, output_dir=tmp_path / "output")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(review))
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    code, data = ask(base + "api/items")
    assert code == 200
    code, kept = ask(base + "decision", {"set": "P1", "decision": "kept", "projection": "cylindrical", "orientation": "level"})
    assert code == 200 and kept["decision"] == "kept"
    code, drop = ask(base + "decision", {"set": "P1", "decision": "discarded"})
    assert code == 200
    server.shutdown()
