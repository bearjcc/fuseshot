"""group.merge with MediaPipe-free mocks."""

import json

import numpy as np
import pytest

from fuseshot import group


def test_merge_reuses_cached_analysis(tmp_path, monkeypatch):
    work = tmp_path / "work"
    out = work / "groups" / "G1"
    out.mkdir(parents=True)
    manifest = [{"image": str(tmp_path / "x.jpg"), "asset_id": "a1"}]
    (tmp_path / "x.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (out / "analysis.json").write_text(json.dumps({
        "size": [10, 10], "faces": [], "tracks": [], "homographies": [np.eye(3).tolist()],
    }), encoding="utf-8")
    monkeypatch.setattr(group, "load_frames", lambda m, size=group.WORK_SIZE: [np.zeros((100, 100, 3), np.uint8)])
    monkeypatch.setattr(group, "compose", lambda *a, **k: {"set_id": "G1", "people": [], "swapped": 0})
    cand = {"set_id": "G1", "asset_ids": ["a1"]}
    rec = group.merge(None, cand, tmp_path / "groups.json", work, log=lambda *a: None)
    assert rec["set_id"] == "G1"


def test_merge_raises_without_mediapipe_when_no_analysis(tmp_path, monkeypatch):
    work = tmp_path / "work"
    out = work / "groups" / "G2"
    out.mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps([{"image": "x", "asset_id": "a"}]), encoding="utf-8")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    with pytest.raises(RuntimeError, match="mediapipe"):
        group.merge(None, {"set_id": "G2", "asset_ids": ["a"]}, tmp_path / "g.json", work)
