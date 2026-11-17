"""splat.prepare with fetch/manifest mocked."""

import json
from pathlib import Path

import numpy as np
from PIL import Image

from fuseshot import splat


def test_prepare_writes_frames_json(tmp_path, monkeypatch):
    work = tmp_path / "work"
    set_dir = work / "P1"
    img = set_dir / "originals" / "01.jpg"
    img.parent.mkdir(parents=True)
    Image.fromarray(np.full((120, 160, 3), 90, np.uint8)).save(img)
    cand = tmp_path / "candidates.json"
    cand.write_text(json.dumps({"candidates": [{"set_id": "P1", "asset_ids": ["a1"]}]}), encoding="utf-8")

    monkeypatch.setattr("fuseshot.fetch.load_candidate", lambda c, s: {
        "set_id": "P1", "junk_frames": [], "asset_ids": ["a1"],
    })
    monkeypatch.setattr(splat, "manifest_for", lambda *a, **k: [{
        "index": 1, "image": str(img), "asset_id": "a1", "f35": 26,
    }])
    monkeypatch.setattr(splat, "resolve", lambda work, p: Path(p))
    monkeypatch.setattr("fuseshot.rig.views", lambda *a, **k: None)
    info = splat.prepare(work, "P1", cand, lambda: None)
    assert info["frames"] and (splat.splat_dir(work, "P1") / "frames.json").is_file()
