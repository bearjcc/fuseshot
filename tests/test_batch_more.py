"""batch helpers: focal hints, cleanup, studio check, straighten."""

import json
import sqlite3

import cv2
import numpy as np

from fuseshot import batch
from conftest import stitched_set


def test_focal_hints_from_cache_and_db(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "focal35.json").write_text(json.dumps({"a1": 26}), encoding="utf-8")
    db = tmp_path / "db.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE assets (id TEXT, make TEXT, model TEXT, focal_length REAL)")
    conn.execute("INSERT INTO assets VALUES ('b2', 'Make', 'Model', 50)")
    conn.commit()
    conn.close()
    hints = batch.focal_hints(db, cache, ["a1", "b2"])
    assert hints == {"a1": 26.0}


def test_left_out_respects_review_discards(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    reviews = work / "reviews.json"
    reviews.write_text(json.dumps({
        "K1": [{"kind": "stitch", "decision": "discarded"}],
        "S9": [{"kind": "found", "decision": "discarded"}],
    }), encoding="utf-8")
    assert batch.left_out(work, {"key": "K1", "set_id": "S1"})
    assert batch.left_out(work, {"key": "P2", "set_id": "S9"})


def test_clean_up_keeps_linked_images(tmp_path):
    set_dir = stitched_set(tmp_path / "work", "C1")
    originals = set_dir / "originals"
    keep = originals / "keep.jpg"
    drop = originals / "drop.jpg"
    cv2.imwrite(str(keep), np.zeros((10, 10, 3), np.uint8))
    cv2.imwrite(str(drop), np.zeros((10, 10, 3), np.uint8))
    pto = set_dir / "optimised.pto"
    pto.write_text('i w123 h123 f0 n"originals/keep.jpg"\n', encoding="utf-8")
    (set_dir / "points.pto").write_text("x", encoding="utf-8")
    batch.clean_up(set_dir)
    assert keep.exists() and not drop.exists()
    assert not (set_dir / "points.pto").exists()


def test_check_studio_flags_green_screen(tmp_path, monkeypatch):
    set_dir = stitched_set(tmp_path / "work", "S1")
    previews = set_dir / "previews"
    previews.mkdir(exist_ok=True)
    green = np.zeros((100, 100, 3), np.uint8)
    green[:] = (0, 200, 0)
    cv2.imwrite(str(previews / "cylindrical.jpg"), green)
    record = {"status": "stitched", "preview_file": "previews/cylindrical.jpg"}
    monkeypatch.setattr(batch, "studio_background_share", lambda p: 0.9)
    batch.check_studio(set_dir, record)
    assert record["status"] == "poor fit" and record["poor_fit"]


def test_straighten_when_upright_changes_preview(tmp_path, monkeypatch):
    set_dir = stitched_set(tmp_path / "work", "S2")
    record = {
        "status": "stitched",
        "type": "single-row rotation",
        "preview_file": "previews/cylindrical.jpg",
        "seconds": {},
    }
    monkeypatch.setattr(batch, "upright", lambda d: {"roll": 1.0})
    monkeypatch.setattr(batch, "render_preview", lambda d, r, fuse: {"peak_mb": 1, "size": [10, 10]})
    turned = batch.straighten_job(set_dir, record)
    assert turned and record.get("upright_checked")
