import os
from datetime import datetime, timedelta

import numpy as np
import pytest
from PIL import Image

from fuseshot import config


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fresh Fuseshot home folder with no settings and no Immich."""
    for name in list(os.environ):
        if name.startswith("FUSESHOT_"):
            monkeypatch.delenv(name)
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setenv("FUSESHOT_HOME", str(folder))
    config.setup(folder)
    yield folder
    config._home = None
    config._settings = None


def scene(width=4200, height=1000, seed=7):
    """A wide made-up scene with plenty of corners for feature matching."""
    rng = np.random.default_rng(seed)
    img = np.zeros((height, width, 3), np.uint8)
    img[:] = (90, 120, 150)
    import cv2

    for _ in range(1400):
        x, y = int(rng.integers(0, width)), int(rng.integers(0, height))
        colour = tuple(int(c) for c in rng.integers(0, 255, 3))
        if rng.random() < 0.5:
            cv2.rectangle(img, (x, y), (x + int(rng.integers(8, 70)), y + int(rng.integers(8, 70))), colour, -1)
        else:
            cv2.circle(img, (x, y), int(rng.integers(4, 35)), colour, -1)
    return img


def write_frames(folder, count=5, step_degrees=25, start=datetime(2024, 3, 9, 14, 0, 0)):
    """Frames of a camera turning on the spot, a few seconds apart, as a phone would leave them."""
    import cv2

    folder.mkdir(parents=True, exist_ok=True)
    frame_w, frame_h, focal = 1000, 750, 1000.0
    pano = scene(width=int(np.radians(step_degrees * (count - 1) + 80) * focal), height=1100)
    u, v = np.meshgrid(np.arange(frame_w, dtype=np.float32) - frame_w / 2,
                       np.arange(frame_h, dtype=np.float32) - frame_h / 2)
    paths = []
    for n in range(count):
        yaw = np.radians(20 + step_degrees * n)
        x = u * np.cos(yaw) + focal * np.sin(yaw)
        z = -u * np.sin(yaw) + focal * np.cos(yaw)
        map_x = (np.arctan2(x, z) * focal).astype(np.float32)
        map_y = (v * focal / np.hypot(x, z) + pano.shape[0] / 2).astype(np.float32)
        crop = cv2.remap(pano, map_x, map_y, cv2.INTER_LINEAR)
        img = Image.fromarray(crop[:, :, ::-1])
        exif = Image.Exif()
        exif[0x010F] = "Example"
        exif[0x0110] = "Camera"
        when = (start + timedelta(seconds=3 * n)).strftime("%Y:%m:%d %H:%M:%S")
        exif[0x0132] = when
        sub = exif.get_ifd(0x8769)
        sub[0x9003] = when
        sub[0x920A] = 4.0
        sub[0xA405] = 36  # 35 mm equivalent of the 53 degree view a 1000 px focal length gives
        path = folder / f"IMG_{n:04d}.jpg"
        img.save(path, quality=92, exif=exif)
        paths.append(path)
    return paths


@pytest.fixture
def photos(tmp_path):
    return write_frames(tmp_path / "photos")
