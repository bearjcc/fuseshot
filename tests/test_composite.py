import json

import cv2
import numpy as np

from fuseshot import action, composite, config, export
from fuseshot.review import Review

from conftest import scene

W, H = 640, 360


def burst(count=8, radius=30, step=70, start=50):
    """A fixed-camera burst: a ball rolling left to right over a still scene."""
    background = cv2.resize(scene(1600, 900, seed=3), (W, H), interpolation=cv2.INTER_AREA)
    frames = []
    for n in range(count):
        frame = background.copy()
        cv2.circle(frame, (start + n * step, H // 2), radius, (20, 230, 250), -1)
        frames.append(frame)
    return background, frames


def test_every_clear_moment_of_the_ball_lands_on_the_background():
    background, frames = burst()
    image, found = composite.compose(frames)
    assert image is not None and found["placed"] == 8
    for n in range(8):
        x = 50 + n * 70
        assert tuple(image[H // 2, x]) == (20, 230, 250)
    corner = (slice(0, 60), slice(0, 60))
    assert np.abs(image[corner].astype(int) - background[corner]).mean() < 2


def test_overlapping_moments_are_skipped():
    _, frames = burst(count=8, radius=30, step=25)
    image, found = composite.compose(frames)
    assert image is not None
    assert all(b - a >= 3 for a, b in zip(found["frames"], found["frames"][1:]))


def test_no_composite_when_the_subject_never_clears_itself_or_everything_moves():
    _, frames = burst(count=6, step=4)
    assert composite.compose(frames) == (None, "the subject does not move clear of itself")
    rng = np.random.default_rng(1)
    noisy = [rng.integers(0, 255, (H, W, 3), dtype=np.uint8) for _ in range(5)]
    image, why = composite.compose(noisy)
    assert image is None and "moves" in why


def test_a_loop_keeps_its_composite_through_review_and_export(tmp_path, home):
    _, frames = burst()
    work, out = tmp_path / "work", tmp_path / "output"
    loop = work / "actions" / "burst-1"
    action.loop_from_frames(frames, loop, 100, {"name": "IMG_0001.jpg", "taken": "2025-06-01T10:00:00"})
    info = json.loads((loop / "action.json").read_text(encoding="utf-8"))
    assert (loop / "composite.jpg").exists() and info["composite"]["placed"] >= 3
    out.mkdir()
    (out / "candidates.json").write_text(json.dumps({"candidates": []}), encoding="utf-8")
    review = Review(work, out / "candidates.json", cache_dir=tmp_path / "web")
    (item,) = [i for i in review.items()["items"] if i["kind"] == "action"]
    assert item["composite"].split("?")[0].endswith("/loop/burst-1/composite.jpg")
    assert review.loop_file("burst-1", "composite.jpg") == loop / "composite.jpg"
    item = {**item, "decision": "kept", "library": "folder"}
    review.items = lambda: {"items": [item]}
    names = sorted(p.name for p in export.export(review, tmp_path / "kept"))
    assert [n.rsplit(" ", 1)[1] for n in names] == ["composite.jpg", "loop.webp"]


def test_composites_can_be_turned_off(tmp_path, home):
    (home / "fuseshot.toml").write_text("[outputs]\ncomposites = false\n", encoding="utf-8")
    config.setup(home)
    _, frames = burst()
    loop = tmp_path / "burst"
    (loop).mkdir()
    (loop / "composite.jpg").write_bytes(b"old")
    action.loop_from_frames(frames, loop, 100, {})
    assert not (loop / "composite.jpg").exists()
    assert "composite" not in json.loads((loop / "action.json").read_text(encoding="utf-8"))
