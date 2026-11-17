"""Splat planning without COLMAP."""

import json

import numpy as np
from PIL import Image

from fuseshot import splat


def test_set_kind_from_candidate():
    assert splat.set_kind({"verified_type": "object scan"}) == "object scan"
    assert splat.set_kind({"verified_type": "linear walk along a path"}) == "walk"
    assert splat.set_kind({"type": "single-row rotation"}) is None


def test_make_plan_marks_eligible(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "O1").mkdir()
    for index, colour in enumerate([(10, 10, 10), (20, 20, 20), (30, 30, 30)]):
        path = cache / "O1" / f"t{index}.jpg"
        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        frame[:, :] = colour
        Image.fromarray(frame).save(path)
    candidates = tmp_path / "candidates.json"
    candidates.write_text(
        json.dumps({"candidates": [{
            "set_id": "O1",
            "verified_type": "object scan",
            "asset_ids": ["a", "b", "c", "d"],
            "library": "folder",
        }]}),
        encoding="utf-8",
    )
    (tmp_path / "verified.json").write_text(json.dumps({"O1": {"pair_kinds": {"parallax": 10}}}), encoding="utf-8")
    plan = splat.make_plan(candidates, cache, min_parallax=1)
    assert plan["sets"]
    assert plan["sets"][0]["set_id"] == "O1"
