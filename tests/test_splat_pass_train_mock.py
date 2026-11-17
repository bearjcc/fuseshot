"""splat SfM pass gate and brush train with subprocess mocked."""

import json
from pathlib import Path

import numpy as np
from PIL import Image

from fuseshot import splat


def test_sfm_passes_gate_and_writes_view(tmp_path, monkeypatch):
    work = tmp_path / "work"
    out = splat.splat_dir(work, "O2")
    out.mkdir(parents=True)
    img_dir = out / "images"
    img_dir.mkdir()
    for n in range(10):
        Image.new("RGB", (160, 120), (n * 20, 30, 40)).save(img_dir / f"{n:03d}.jpg")
    (out / "frames.json").write_text(json.dumps({
        "frames": [{"index": n, "file": f"images/{n:03d}.jpg", "size": [160, 120], "f35": 26, "asset_id": f"a{n}"}
                   for n in range(10)],
    }), encoding="utf-8")

    def fake_colmap(args, log):
        if "model_analyzer" in args:
            return (
                "Cameras: 1\nRegistered images: 10\nPoints: 100\n"
                "Mean track length: 3.0\nMean reprojection error: 1.0\n"
            )
        if "--output_path" in args:
            out_path = Path(args[args.index("--output_path") + 1])
            if "sparse" in str(out_path):
                model = out_path / "0"
                model.mkdir(parents=True, exist_ok=True)
                (model / "cameras.txt").write_text("1 PINHOLE 160 120 100 100 80 60\n", encoding="ascii")
                (model / "images.txt").write_text(
                    "1 1 0 0 0 0 0 0 1 000.jpg\n0 0 -1\n2 1 0 0 0 0 0 0 1 001.jpg\n0 0 -1\n",
                    encoding="ascii",
                )
                (model / "points3D.txt").write_text("1 0 0 2 0 0 0\n", encoding="ascii")
            elif "dataset" in str(out_path):
                sparse = out_path / "sparse"
                sparse.mkdir(parents=True, exist_ok=True)
                (sparse / "cameras.bin").write_bytes(b"\x00")
            elif args[args.index("--output_type") + 1] == "TXT":
                text = out_path
                text.mkdir(parents=True, exist_ok=True)
                (text / "cameras.txt").write_text("1 PINHOLE 160 120 100 100 80 60\n", encoding="ascii")
                (text / "images.txt").write_text(
                    "1 1 0 0 0 0 0 0 1 000.jpg\n0 0 -1\n", encoding="ascii"
                )
                (text / "points3D.txt").write_text("1 0 0 2 0 0 0\n", encoding="ascii")
        return ""

    monkeypatch.setattr(splat, "colmap", fake_colmap)
    monkeypatch.setattr(splat.tools, "require", lambda name: Path("/bin/true"))
    monkeypatch.setattr(splat.limits, "gpu", lambda: None)
    monkeypatch.setattr(splat.limits, "colmap_cuda", lambda exe: False)
    result = splat.sfm(work, "O2", gpu=False, threads=1)
    assert result["status"] == "passed"
    assert result.get("view")


def test_train_completes_with_fake_brush(tmp_path, monkeypatch):
    work = tmp_path / "work"
    out = splat.splat_dir(work, "T1")
    out.mkdir(parents=True)
    dataset = out / "dataset"
    dataset.mkdir()
    (dataset / "images").mkdir()
    (out / "sfm.json").write_text(json.dumps({"status": "passed", "reason": "ok"}), encoding="utf-8")
    train_dir = out / "train"
    train_dir.mkdir()
    ply = train_dir / "export_30.ply"
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        "element vertex 1\nproperty float x\nproperty float y\nproperty float z\n"
        "end_header\n"
    )
    body = np.zeros(12, dtype=np.float32).tobytes()
    ply.write_bytes(header.encode("ascii") + body)

    monkeypatch.setattr(splat.tools, "require", lambda name: Path("/bin/true"))
    monkeypatch.setattr(splat, "run_brush", lambda *a, **k: (0, False))
    def fake_ply_to_splat(p, dest):
        dest.write_bytes(b"splat")
        return {"gaussians": 1, "splat_mb": 0.1}

    monkeypatch.setattr(splat, "ply_to_splat", fake_ply_to_splat)
    monkeypatch.setattr(splat, "holdout_quality", lambda *a, **k: {})
    rec = splat.train(work, "T1", steps=30)
    assert rec and rec["steps"] == 30
    assert (out / "scene.splat").is_file()
    assert (out / "splat.json").is_file()
