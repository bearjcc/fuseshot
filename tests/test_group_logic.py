"""Group scan logic without MediaPipe models."""

import json

import numpy as np

from fuseshot import group


def test_ids_key_stable():
    cand = {"asset_ids": ["b", "a"]}
    a = group._ids_key(cand)
    b = group._ids_key({"asset_ids": "a b".split()})
    assert len(a) == 16 and a != b


def test_longest_run_follows_links():
    kept = ["a", "b", "c", "d"]
    still = [{"pair": ["a", "b"]}, {"pair": ["c", "d"]}]
    assert group.longest_run(kept, still) == ["a", "b"]


def test_chains_accumulate_homographies():
    frames = ["a", "b", "c"]
    H = np.eye(3).tolist()
    linked = {("a", "b"): {"H": H}, ("b", "c"): {"H": H}}
    chains = group.chains(frames, linked)
    assert len(chains) == 1
    assert "c" in chains[0]


def test_load_and_is_group(tmp_path):
    path = tmp_path / "groups.json"
    path.write_text(json.dumps({"sets": {"G1": {"group": True, "ids": "abc"}}}), encoding="utf-8")
    assert group.load_groups(path)["G1"]["group"]
    assert group.is_group("G1", path)
    assert not group.is_group("G2", path)
