"""The review list is pushed to open pages as it changes."""

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

from PIL import Image

from fuseshot.events import Changes
from fuseshot.review import Review, make_handler


def listing(*keys, running=None):
    return {"items": [{"key": k, "n": n} for k, n in keys], "running": running}


def test_only_differences_are_published():
    now = {"data": listing(("a", 1), ("b", 1))}
    changes = Changes(lambda: now["data"])
    assert changes.recompute() is None
    assert changes.recompute() is None
    now["data"] = listing(("a", 2), ("c", 1))
    assert changes.recompute() == 1
    version, news, behind = changes.wait(0, 0)
    assert (version, behind) == (1, False)
    assert news[0][1]["changed"] == [{"key": "a", "n": 2}, {"key": "c", "n": 1}]
    assert news[0][1]["removed"] == ["b"]
    now["data"] = listing(("a", 2), ("c", 1), running={"stage": "stitch"})
    assert changes.recompute() == 2


def test_a_page_too_far_behind_starts_again():
    changes = Changes(lambda: listing())
    for n in range(60):
        changes.publish({"n": n})
    assert changes.wait(0, 0)[2]
    assert not changes.wait(59, 0)[2]
    assert changes.wait(60, 0) == (60, [], False)


def read_events(response, count):
    events, event = [], {}
    for raw in response:
        line = raw.decode("utf-8").rstrip("\n")
        if not line:
            if "event" in event:
                events.append(event)
            event = {}
            if len(events) == count:
                return events
        elif not line.startswith(":"):
            field, _, value = line.partition(": ")
            event[field] = value
    return events


def test_the_page_hears_about_a_new_set(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    review = Review(work)
    review.watch_changes(every=0.2, gap=0.05)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(review))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/events"
        with urllib.request.urlopen(url, timeout=10) as response:
            assert response.headers["Content-Type"].startswith("text/event-stream")
            assert read_events(response, 1)[0]["event"] == "hello"
            for _ in range(100):
                if review.changes.state is not None:
                    break
                time.sleep(0.05)
            set_dir = work / "F00001"
            (set_dir / "previews").mkdir(parents=True)
            (set_dir / "optimised.pto").write_text("p\n")
            Image.new("RGB", (64, 32)).save(set_dir / "previews" / "cylindrical-level.jpg")
            (set_dir / "batch.json").write_text(json.dumps({"set_id": "F00001", "status": "ok"}))
            event = read_events(response, 1)[0]
            assert event["event"] == "items"
            keys = [i["key"] for i in json.loads(event["data"])["changed"]]
            assert "stitch:F00001" in keys
    finally:
        server.shutdown()
        server.server_close()
