"""splat.sfm GPU fallback and mapper error text."""

import json
from pathlib import Path

from PIL import Image

from fuseshot import splat


def test_sfm_falls_back_to_cpu_after_gpu_colmap_error(tmp_path, monkeypatch):
    work = tmp_path / "work"
    out = splat.splat_dir(work, "G1")
    out.mkdir(parents=True)
    img_dir = out / "images"
    img_dir.mkdir()
    for n in range(8):
        Image.new("RGB", (160, 120), (30, 30, 30)).save(img_dir / f"{n:03d}.jpg")
    (out / "frames.json").write_text(json.dumps({
        "frames": [{"index": n, "file": f"images/{n:03d}.jpg", "size": [160, 120], "f35": 26, "asset_id": f"a{n}"}
                   for n in range(8)],
    }), encoding="utf-8")
    calls = []

    def fake_colmap(args, log):
        calls.append(int("--FeatureExtraction.use_gpu" in args and args[args.index("--FeatureExtraction.use_gpu") + 1]))
        if "feature_extractor" in args and calls[-1] == 1:
            raise splat.SplatError("gpu broke")
        if "model_analyzer" in args:
            return "Cameras: 1\nRegistered images: 0\nPoints: 0\nMean reprojection error: 9.0\n"
        if "--output_path" in args:
            Path(args[args.index("--output_path") + 1]).mkdir(parents=True, exist_ok=True)
        return ""

    monkeypatch.setattr(splat, "colmap", fake_colmap)
    monkeypatch.setattr(splat.tools, "require", lambda name: Path("/bin/true"))
    monkeypatch.setattr(splat.limits, "gpu", lambda: object())
    monkeypatch.setattr(splat.limits, "colmap_cuda", lambda exe: True)
    result = splat.sfm(work, "G1", gpu=True, threads=1)
    assert result["status"] == "failed"
    assert calls[0] == 1 and 0 in calls
