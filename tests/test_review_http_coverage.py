"""Review HTTP: failures, retry, edit, moving override, enhance A/B."""

import json
import threading
import urllib.error
import urllib.request

from fuseshot.review import Review, make_handler
from conftest import stitched_set

from http.server import ThreadingHTTPServer


def post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode())


def review_with_failures(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    stitched_set(work, "P1")
    fail = stitched_set(work, "F1")
    (fail / "batch.json").write_text(json.dumps({
        "status": "failed", "error": "align blew up", "attempts": 2, "failed_at": "2026-01-01T00:00:00+00:00",
    }), encoding="utf-8")
    cand = tmp_path / "output" / "candidates.json"
    cand.parent.mkdir(parents=True)
    cand.write_text(json.dumps({"candidates": [{
        "set_id": "P1", "library": "folder", "asset_ids": ["a"],
        "verified_type": "single-row rotation", "guessed_type": "single-row rotation",
        "start_local": "2024-01-01", "camera": "X", "count": 1, "quality": 1.0,
        "verified_confidence": 0.9, "motion_model": "rotation",
    }]}), encoding="utf-8")
    return Review(work, candidates_file=cand, output_dir=tmp_path / "output")


def server(review):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(review))
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, base


def test_failures_list_retry_and_page(tmp_path):
    review = review_with_failures(tmp_path)
    rows = review.failures()
    assert any(r["key"] == "F1" for r in rows)
    html = review.failures_page()
    assert "F1" in html and "retry" in html.lower()
    httpd, base = server(review)
    code, body = post(base + "/retry", {"kind": "stitch", "key": "F1"})
    assert code == 200 and body.get("retry")
    with urllib.request.urlopen(base + "/failures", timeout=10) as resp:
        assert resp.status == 200
    httpd.shutdown()


def test_set_moving_override_post(tmp_path):
    review = review_with_failures(tmp_path)
    httpd, base = server(review)
    code, body = post(base + "/moving", {"set": "P1", "moving": "loop"})
    assert code == 200 and body["moving"] == "loop"
    httpd.shutdown()


def test_edit_post_queues_render(tmp_path, monkeypatch):
    review = review_with_failures(tmp_path)
    monkeypatch.setattr(review, "export_later", lambda *a: None)

    class FakeJobs:
        def submit(self, *a, **k):
            return "task-1"

    monkeypatch.setattr("fuseshot.jobs.runner", lambda: FakeJobs())
    monkeypatch.setattr("fuseshot.edit.run", lambda *a, **k: {"state": "done"})

    httpd, base = server(review)
    code, body = post(base + "/edit", {
        "set": "P1", "projection": "cylindrical", "orientation": "level",
    })
    assert code == 200
    httpd.shutdown()


def test_retry_unknown_key_returns_404(tmp_path):
    review = review_with_failures(tmp_path)
    httpd, base = server(review)
    code, body = post(base + "/retry", {"kind": "stitch", "key": "missing"})
    assert code == 404
    httpd.shutdown()


def test_enhance_decide_rejects_bad_pick(tmp_path):
    review = review_with_failures(tmp_path)
    httpd, base = server(review)
    code, body = post(base + "/enhance/decide", {"id": "x", "pick": "nope", "enhanced_side": "left"})
    assert code == 400 and "error" in body
    httpd.shutdown()


def test_enhance_compare_and_decide(tmp_path, monkeypatch):
    review = review_with_failures(tmp_path)
    enhance = review.enhance_dir()
    enhance.mkdir(parents=True)
    results = enhance / "results"
    asset = "asset-1"
    row_dir = results / asset
    row_dir.mkdir(parents=True)
    (row_dir / "result.json").write_text(json.dumps({
        "id": asset, "chain": ["nafnet"], "differs": True, "gain": 0.5,
        "finished": "2026-01-01T00:00:00+00:00",
    }), encoding="utf-8")
    from PIL import Image
    for name in ("view-original.jpg", "view-enhanced.jpg", "enhanced.jpg"):
        Image.new("RGB", (32, 24), (1, 2, 3)).save(row_dir / name)

    httpd, base = server(review)
    with urllib.request.urlopen(base + "/enhance/compare", timeout=10) as resp:
        assert resp.status == 200
    code, picked = post(base + "/enhance/decide", {"id": asset, "pick": "original", "enhanced_side": "right"})
    assert code == 200
    httpd.shutdown()
