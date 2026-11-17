"""splat.sfm gate and prepare boundaries with COLMAP mocked."""

import json
from pathlib import Path

import pytest
from PIL import Image

from fuseshot import splat


def test_sfm_requires_prepare(tmp_path):
    work = tmp_path / "work"
    (splat.splat_dir(work, "O1")).mkdir(parents=True)
    with pytest.raises(splat.SplatError, match="prepare"):
        splat.sfm(work, "O1")


def test_sfm_records_gate_failure(tmp_path, monkeypatch):
    work = tmp_path / "work"
    out = splat.splat_dir(work, "O1")
    out.mkdir(parents=True)
    img_dir = out / "images"
    img_dir.mkdir()
    for n in range(8):
        Image.new("RGB", (160, 120), (n * 10, 20, 30)).save(img_dir / f"{n:03d}.jpg")
    (out / "frames.json").write_text(json.dumps({
        "frames": [{"index": n, "file": f"images/{n:03d}.jpg", "size": [160, 120], "f35": 26, "asset_id": f"a{n}"}
                   for n in range(8)],
    }), encoding="utf-8")

    def fake_colmap(args, log):
        if "model_analyzer" in args:
            return (
                "Cameras: 1\nRegistered images: 2\nPoints: 10\n"
                "Mean track length: 2.0\nMean reprojection error: 5.0\n"
            )
        if "--output_path" in args:
            model = Path(args[args.index("--output_path") + 1]) / "0"
            model.mkdir(parents=True, exist_ok=True)
            (model / "cameras.txt").write_text("1 PINHOLE 160 120 100 100 80 60\n", encoding="ascii")
            (model / "images.txt").write_text(
                "1 1 0 0 0 0 0 0 0 1 i.jpg\n0 0 -1\n", encoding="ascii"
            )
            (model / "points3D.txt").write_text("1 0 0 1 0 0 0\n", encoding="ascii")
        return ""

    monkeypatch.setattr(splat, "colmap", fake_colmap)
    monkeypatch.setattr(splat.tools, "require", lambda name: Path("/bin/true"))
    monkeypatch.setattr(splat.limits, "gpu", lambda: None)
    monkeypatch.setattr(splat.limits, "colmap_cuda", lambda exe: False)
    result = splat.sfm(work, "O1", gpu=False, threads=1)
    assert result["status"] == "failed"
    assert "registered" in result["reason"].lower() or "reprojection" in result["reason"].lower()
    assert (out / "sfm.json").is_file()
