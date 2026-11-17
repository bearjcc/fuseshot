"""Review POST: override, undo, set type."""

import json
import threading
import urllib.error
import urllib.request

from fuseshot.review import Review, make_handler
from conftest import stitched_set

from http.server import ThreadingHTTPServer


def post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode())


def server_for(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    stitched_set(work, "P1")
    cand = tmp_path / "output" / "candidates.json"
    cand.parent.mkdir(parents=True)
    (work / "types.json").write_text("{}", encoding="utf-8")
    cand.write_text(json.dumps({"candidates": [{
        "set_id": "P1", "library": "folder", "asset_ids": ["a"], "verified_type": "single-row rotation",
    }]}), encoding="utf-8")
    review = Review(work, candidates_file=cand, output_dir=tmp_path / "output")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(review))
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, base


def test_override_and_type_post(tmp_path):
    httpd, base = server_for(tmp_path)
    code, kept = post(base + "/decision", {"set": "P1", "decision": "kept", "projection": "cylindrical", "orientation": "level"})
    assert code == 200
    code, undo = post(base + "/undo", {"records": [{"set": "P1", "at": kept["at"], "decision": "kept"}]})
    assert code == 200 and undo.get("undone")
    code, typed = post(base + "/type", {"set": "P1", "type": "panorama"})
    assert code == 200
    httpd.shutdown()
