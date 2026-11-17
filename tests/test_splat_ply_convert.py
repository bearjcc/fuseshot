"""splat.read_ply errors and status()."""

import json

import pytest

from fuseshot import splat


def test_read_ply_rejects_ascii(tmp_path):
    ply = tmp_path / "a.ply"
    ply.write_text("ply\nformat ascii 1.0\nelement vertex 0\nend_header\n", encoding="ascii")
    with pytest.raises(splat.SplatError, match="unsupported"):
        splat.read_ply(ply)


def test_status_prints_rows(tmp_path, capsys):
    work = tmp_path / "work"
    out = splat.splat_dir(work, "S1")
    out.mkdir(parents=True)
    (out / "sfm.json").write_text(json.dumps({"status": "passed", "reason": "ok"}), encoding="utf-8")
    (out / "splat.json").write_text(json.dumps({"steps": 30, "train_seconds": 60, "splat_mb": 1.2}), encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"sets": [{"set_id": "S1", "kind": "walk", "usable": 8, "score": 3, "skip": ""}]}),
                    encoding="utf-8")
    splat.status(work, plan)
    assert "S1" in capsys.readouterr().out
