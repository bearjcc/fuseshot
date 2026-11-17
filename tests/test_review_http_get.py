"""Broad GET coverage on the review HTTP handler."""

import json
import threading
import urllib.request

from fuseshot.review import Review, make_handler
from conftest import stitched_set

from http.server import ThreadingHTTPServer


def test_many_get_routes(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    stitched_set(work, "P1")
    cand = tmp_path / "output" / "candidates.json"
    cand.parent.mkdir(parents=True)
    cand.write_text(json.dumps({"candidates": [{
        "set_id": "P1", "library": "folder", "asset_ids": ["a", "b"],
        "verified_type": "single-row rotation", "guessed_type": "single-row rotation",
        "start_local": "2024-01-01", "camera": "X", "count": 2, "quality": 1.0,
        "verified_confidence": 0.9, "motion_model": "rotation", "verify_reason": "",
    }]}), encoding="utf-8")
    review = Review(work, candidates_file=cand, output_dir=tmp_path / "out")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(review))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    paths = ["/", "/tables", "/set/P1", "/failures", "/enhance", "/api/items", "/api/pipeline",
             "/api/settings", "/api/enhance/queue"]
    for path in paths:
        with urllib.request.urlopen(base + path, timeout=15) as resp:
            assert resp.status == 200
    server.shutdown()
