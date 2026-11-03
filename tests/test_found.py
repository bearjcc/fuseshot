"""Sets found by detect show on the review page until their result is in, and a discard
keeps them from being stitched."""

import io
import json

import pytest
from PIL import Image

from fuseshot import batch
from fuseshot.found import strip_frames
from fuseshot.review import Review


def candidate(set_id, count=6, **extra):
    return {"set_id": set_id, "guessed_type": "panorama", "confidence": 0.8, "count": count,
            "asset_ids": [f"{set_id.lower()}-a{n:02d}" for n in range(count)],
            "start_local": "2024-03-09 14:00:00", "camera": "Test camera", **extra}


class FakeRunner:
    def __init__(self):
        self.cancelled = []

    def cancel(self, names):
        self.cancelled.append(names)
        return [1]


class FakeLibrary:
    def __init__(self):
        self.calls = []

    def thumbnail(self, asset_id, size):
        self.calls.append((asset_id, size))
        if asset_id.endswith("a05"):
            raise OSError("not there")
        out = io.BytesIO()
        Image.new("RGB", (1440, 1080), (40, 90, 150)).save(out, "WEBP")
        return out.getvalue()


@pytest.fixture
def review(tmp_path):
    work, out = tmp_path / "work", tmp_path / "output"
    work.mkdir()
    out.mkdir()
    panorama = {"verified_type": "panorama", "verified_confidence": 0.9}
    (out / "candidates.json").write_text(json.dumps({"candidates": [
        candidate("P1"),
        candidate("P2", **panorama),
        candidate("P3", **panorama),
        candidate("P4", **panorama),
        candidate("P5", verified_type="not a panorama", verified_confidence=0.2),
        candidate("B1", source="burst"),
    ]}))
    jobs = [{"key": k, "set_id": k.split("-")[0], "confidence": 0.9, "quality": 1} for k in ("P2", "P3-1", "P3-2", "P4")]
    (work / "batch_plan.json").write_text(json.dumps({"jobs": jobs}))
    (work / "P3-1").mkdir()
    (work / "P4").mkdir()
    (work / "P4" / "batch.json").write_text(json.dumps({"status": "stitched"}))
    r = Review(work, out / "candidates.json", library=FakeLibrary())
    r.found.runner = FakeRunner()
    return r


def found_items(review):
    return {i["id"]: i for i in review.items()["items"] if i["kind"] == "found"}


def test_found_sets_stay_until_their_result_is_in(review):
    items = found_items(review)
    assert {k: i["status"] for k, i in items.items()} == {"P1": "found", "P2": "queued", "P3": "stitching"}
    p1 = items["P1"]
    assert p1["key"] == "found:P1" and p1["frames"] == 6 and not p1["checked"]
    assert p1["strip"] == [f"/found/P1/{n}" for n in (0, 2, 3, 5)]
    assert items["P2"]["checked"] and items["P2"]["confidence"] == 0.9


def test_strip_frames_spread_over_the_set():
    assert strip_frames(3) == [0, 1, 2]
    assert strip_frames(10) == [0, 3, 6, 9]


def test_a_found_discard_skips_the_set_and_takes_it_off_the_queue(review):
    out = review.decide_many({"items": [{"set": "P3", "kind": "found", "decision": "discarded"}]})
    review.found.threads[-1].join(5)
    assert review.found.runner.cancelled == [["stitch-P3-1", "stitch-P3-2"]]
    job = {"key": "P3-2", "set_id": "P3"}
    assert batch.left_out(review.work_dir, job)
    assert found_items(review)["P3"]["decision"] == "discarded"
    review.undo({"records": out["records"]})
    assert not batch.left_out(review.work_dir, job)
    assert found_items(review)["P3"]["decision"] is None


def test_only_sets_detect_found_can_be_discarded(review):
    with pytest.raises(KeyError):
        review.decide({"set": "P9", "kind": "found", "decision": "discarded"})
    review.decide({"set": "P1", "kind": "found", "decision": "discarded"})
    assert review.found.runner.cancelled == []


def test_keeping_a_found_set_exports_nothing(review, monkeypatch):
    sent = []
    monkeypatch.setattr(review, "export_later", sent.append)
    review.decide_many({"items": [{"set": "P1", "kind": "found", "decision": "kept"}]})
    assert sent == []


def test_thumbnails_come_from_the_source_once(review):
    path = review.found.thumb_file("P1", "2")
    with Image.open(path) as im:
        assert im.format == "JPEG" and max(im.size) == 320
    assert review.found.thumb_file("P1", "2") == path
    assert review.found.library.calls == [("p1-a02", "thumbnail")]
    for bad in ("6", "x", "5"):
        with pytest.raises(KeyError):
            review.found.thumb_file("P1", bad)


def test_a_set_discarded_while_stitching_comes_back_discarded(review):
    review.decide({"set": "P4", "kind": "found", "decision": "discarded"})
    set_dir = review.work_dir / "P4"
    (set_dir / "previews").mkdir()
    (set_dir / "optimised.pto").write_text("p\n")
    Image.new("RGB", (64, 32)).save(set_dir / "previews" / "cylindrical-level.jpg")
    (set_dir / "batch.json").write_text(json.dumps({"set_id": "P4", "status": "stitched"}))
    item = next(i for i in review.items()["items"] if i["key"] == "stitch:P4")
    assert item["decision"] == "discarded"
    review.decide({"set": "P4", "kind": "stitch", "decision": "kept"})
    item = next(i for i in review.items()["items"] if i["key"] == "stitch:P4")
    assert item["decision"] == "kept"
