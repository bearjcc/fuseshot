"""Brush train path with subprocess mocked."""

import json

from fuseshot import splat


def test_train_skips_without_sfm(tmp_path):
    work = tmp_path / "work"
    out = splat.splat_dir(work, "S1")
    out.mkdir(parents=True)
    with open(out / "sfm.json", "w", encoding="utf-8") as fh:
        json.dump({"status": "rejected", "reason": "test"}, fh)
    assert splat.train(work, "S1", steps=10, chunk=10) is None
