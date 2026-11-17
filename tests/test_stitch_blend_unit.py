"""stitch.blend with mocked enblend."""


import numpy as np
from PIL import Image

from fuseshot import stitch
from conftest import minimal_pto


def test_blend_single_layer_crops(tmp_path):
    work = minimal_pto(tmp_path / "work.pto")
    layer = tmp_path / "layer0000.tif"
    Image.fromarray(np.uint8(np.random.rand(100, 120, 4) * 255), "RGBA").save(layer)
    outer = (0, 120, 0, 100)
    inner = (5, 115, 5, 95)

    class Run:
        steps = []

        def __call__(self, tool, *args, **kwargs):
            self.steps.append(tool)

    flat, cropped, rgba, seams, blender, dropped, stacks = stitch.blend(
        Run(), tmp_path, work, [layer], outer, inner, "level", False
    )
    assert blender == "single"
    assert flat.size == (120, 100)
    assert cropped.width <= flat.width
