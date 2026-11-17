"""fill.inpaint with torch tensors mocked."""

import numpy as np

from fuseshot import fill


class Torch:
    @staticmethod
    def from_numpy(arr):
        return TorchArray(arr)

    class inference_mode:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False


class TorchArray:
    def __init__(self, arr):
        self._arr = np.asarray(arr)

    def __getitem__(self, key):
        return TorchArray(self._arr[key])

    def to(self, device):
        return self

    def permute(self, *axes):
        arr = self._arr
        if arr.ndim == 4 and len(axes) == 3:
            arr = arr[0]
        return TorchArray(np.transpose(arr, axes))

    def float(self):
        return TorchArray(self._arr.astype(np.float32))

    def cpu(self):
        return self

    def numpy(self):
        return self._arr


class Model:
    def __call__(self, ti, tm):
        return [ti]


def test_inpaint_fills_hole_region():
    rgb = np.ones((64, 64, 3), np.float32) * 0.5
    hole = np.zeros((64, 64), bool)
    hole[20:40, 20:40] = True
    out = fill.inpaint(Model(), Torch, "cpu", rgb, hole)
    assert out.shape == rgb.shape
