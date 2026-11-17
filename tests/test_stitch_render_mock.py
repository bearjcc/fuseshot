"""stitch.render with Hugin tools mocked at the Runner boundary."""

import json
import re
from pathlib import Path

import numpy as np
from PIL import Image

from fuseshot import stitch
from conftest import minimal_pto


def test_render_writes_preview_jpegs(tmp_path, monkeypatch):
    set_dir = tmp_path / "P1"
    set_dir.mkdir()
    previews = set_dir / "previews"
    previews.mkdir()
    minimal_pto(set_dir / "optimised.pto", pairs=12)

    class MockRunner:
        def __init__(self, log_file):
            self.log_file = Path(log_file)
            self.log_file.write_text("", encoding="utf-8")
            self.steps = []

        def __call__(self, tool, *args, cwd=None, check=True):
            if tool == "pano_modify":
                src = Path(args[-1])
                dest = Path(args[args.index("-o") + 1])
                text = src.read_text(encoding="utf-8")
                joined = " ".join(map(str, args))
                if "--canvas=" in joined:
                    size = joined.split("--canvas=", 1)[1].split()[0]
                    if "x" in size:
                        width, height = size.split("x", 1)
                        text = re.sub(r"\sw\d+", f" w{width}", text, count=1)
                        text = re.sub(r"\sh\d+", f" h{height}", text, count=1)
                dest.write_text(text, encoding="utf-8")
            elif tool == "nona":
                prefix = Path(args[args.index("-o") + 1])
                for layer in range(2):
                    Image.fromarray(np.uint8(np.random.rand(80, 120, 3) * 255)).save(
                        prefix.parent / f"{prefix.name}{layer:04d}.tif"
                    )
            self.steps.append({"tool": tool, "peak_mb": 1})
            return ""

    def fake_blend(run, tmp, work, layers, outer, inner, orientation, fuse):
        flat = Image.new("RGB", (400, 200), (10, 20, 30))
        crop = Image.new("RGB", (380, 180), (10, 20, 30))
        rgba = Image.new("RGBA", (400, 200), (10, 20, 30, 255))
        return flat, crop, rgba, None, "multiblend", 0, 0

    monkeypatch.setattr(stitch, "Runner", MockRunner)
    monkeypatch.setattr(stitch, "blend", fake_blend)
    info = stitch.render(set_dir, "cylindrical", "level", width=400)
    assert info["projection"] == "cylindrical"
    assert (previews / "cylindrical.jpg").is_file()
    assert (previews / "cylindrical_crop.jpg").is_file()
    renders = json.loads((previews / "renders.json").read_text(encoding="utf-8"))
    assert "cylindrical" in renders
