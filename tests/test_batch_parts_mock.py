"""batch.make_parts copies frames into part folders."""

import json

import cv2
import numpy as np

from fuseshot import batch
from conftest import stitched_set


def test_make_parts_creates_part_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "upright", lambda d: None)
    monkeypatch.setattr(batch, "render_preview", lambda d, r, fuse: {"peak_mb": 1})
    monkeypatch.setattr(batch, "check_studio", lambda *a, **k: None)
    work = tmp_path / "work"
    set_dir = stitched_set(work, "M1")
    img = set_dir / "originals" / "01_a.jpg"
    img.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(img), np.zeros((10, 10, 3), np.uint8))
    (set_dir / "manifest.json").write_text(json.dumps([{"image": str(img), "asset_id": "a"}]), encoding="utf-8")
    record = {"type": "single-row rotation"}
    part_pto = set_dir / "part_02.pto"
    part_pto.write_text(f'i w10 h10 f0 n"{img.resolve()}"\n', encoding="utf-8")
    repaired = {"parts": [{"frames": [1], "file": "part_02.pto", "error_px": 1.0}]}
    parts = batch.make_parts(work, "M1", record, repaired)
    assert "M1-part2" in parts
    assert (work / "M1-part2" / "originals" / img.name).is_file()
