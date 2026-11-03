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


def kinds(tmp_path, cands, actions=None, groups=None, types=None):
    work, out = tmp_path / "work", tmp_path / "output"
    work.mkdir()
    out.mkdir()
    (out / "candidates.json").write_text(json.dumps({"candidates": cands}))
    if actions:
        (out / "actions.json").write_text(json.dumps({"sets": actions}))
    if groups:
        (out / "groups.json").write_text(json.dumps({"sets": groups}))
    if types:
        (work / "types.json").write_text(json.dumps(types))
    return Review(work, out / "candidates.json")


def labels(review):
    return {k: (i["type"], i["checked"]) for k, i in found_items(review).items()}


def test_a_found_card_names_the_type_the_classifier_gives(tmp_path):
    review = kinds(tmp_path, [
        candidate("A1", guessed_type="single-row rotation"),
        candidate("G1", guessed_type="single-row rotation"),
        candidate("S1", guessed_type="multi-row grid"),
        candidate("V1", guessed_type="single-row rotation", verified_type="multi-row grid (+ bracket)"),
        candidate("N1", guessed_type="single-row rotation", verified_type="not a panorama"),
    ], actions={"A1": {"action": True}}, groups={"G1": {"group": True}}, types={"S1": {"type": "object scan"}})
    (review.work_dir / "batch_plan.json").write_text(json.dumps({"jobs": [{"key": "V1", "set_id": "V1"}]}))
    assert labels(review) == {"A1": ("action loop", True), "G1": ("group photo", True), "S1": ("splat", True),
                              "V1": ("multi-row grid", True)}
    # A set the overlap check turned down leaves the found list; its name still matches.
    n1 = {c["set_id"]: c for c in review.candidates()}["N1"]
    assert review.found.what("N1", n1, review.actions(), {}) == ("not a panorama", True)


def test_before_the_checks_a_frame_count_guess_is_not_shown_as_a_panorama_type(tmp_path):
    review = kinds(tmp_path, [
        candidate("R1", guessed_type="single-row rotation"),
        candidate("M1", guessed_type="multi-row grid"),
        candidate("B1", guessed_type="exposure bracket"),
        candidate("W1", guessed_type="linear walk"),
    ])
    assert labels(review) == {"R1": ("set", False), "M1": ("set", False),
                              "B1": ("exposure bracket", False), "W1": ("linear walk", False)}


def put_thumbs(review, c, images):
    from fuseshot.found import strip_frames

    for i, img in zip(strip_frames(len(c["asset_ids"])), images):
        asset_id = c["asset_ids"][i]
        path = review.cache_dir / "found" / asset_id[:2] / f"{asset_id}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(img[:, :, ::-1]).save(path, quality=90)


def test_the_card_frames_give_a_cheap_guess_once_their_thumbnails_are_here(tmp_path):
    import cv2

    from conftest import scene

    quick = {"guessed_type": "single-row rotation", "median_gap_s": 0.5, "span_s": 3}
    fixed, turned = candidate("F1", **quick), candidate("T1", **quick)
    review = kinds(tmp_path, [fixed, turned])
    assert labels(review) == {"F1": ("set", False), "T1": ("set", False)}

    wide = scene(width=1200, height=240)
    still = wide[:, :320].copy()
    moved = still.copy()
    cv2.rectangle(moved, (60, 40), (170, 200), (20, 220, 240), -1)  # something crosses the frame
    put_thumbs(review, fixed, [still, moved, still, moved])
    put_thumbs(review, turned, [wide[:, x:x + 320] for x in (0, 120, 240, 360)])  # the camera turned
    assert labels(review) == {"F1": ("action loop", False), "T1": ("panorama", False)}
