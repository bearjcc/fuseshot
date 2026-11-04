import json

import cv2
import numpy as np
import pytest
from PIL import Image

from conftest import scene
from fuseshot import cache, sources, stereo
from fuseshot.review import Review

W, H = 1000, 750
NEAR, FAR, LIFT = 30, -6, 3


def views():
    """Two views of a made-up scene: a round thing close by in front of a far wall. The
    close thing moves more between the views than the wall, and the right lens sits a
    little lower."""
    wall, thing = scene(W + 40, H + 10, seed=5), scene(300, 300, seed=11)
    disc = np.zeros((300, 300), np.uint8)
    cv2.circle(disc, (150, 150), 120, 255, -1)

    def view(wall_x, thing_x, y):
        out = wall[y:y + H, wall_x:wall_x + W].copy()
        top, left = H // 2 - 150, thing_x - 150
        patch = out[top:top + 300, left:left + 300]
        patch[disc > 0] = thing[disc > 0]
        return out

    return view(6 + 20, W // 2 + NEAR // 2, 5), view(20, W // 2 - NEAR // 2, 5 + LIFT)


def test_stereo_photos_are_told_from_wide_ones():
    assert stereo.kind("SV_20250101_120000.jpg", "", 7680, 2880) == "sbs"
    assert stereo.kind("IMG_1.jpg", "XREAL", 7680, 2880) == "sbs"
    assert stereo.kind("SV_20250101_120000.jpg", "", 4000, 3000) is None
    assert stereo.kind("PANO_1.jpg", "Google", 7680, 2880) is None
    assert stereo.kind("DSCF0001.MPO", "FUJIFILM", 3584, 2016) == "mpo"


def test_the_views_line_up_on_the_subject():
    left, right = views()
    dx, dy, used = stereo.offsets(left, right, (W / 2, H / 2))
    assert abs(dx - NEAR) <= 2 and abs(dy - LIFT) <= 1 and used >= 5
    wall_dx, _, _ = stereo.offsets(left, right, (40, 40))
    assert abs(wall_dx - FAR) <= 2


def test_an_mpo_and_a_side_by_side_photo_read_as_two_views(tmp_path):
    left, right = views()
    sbs = tmp_path / "SV_20250101_120000.jpg"
    cv2.imwrite(str(sbs), np.hstack([left, right]), [cv2.IMWRITE_JPEG_QUALITY, 95])
    mpo = tmp_path / "DSCF0001.MPO"
    first, second = (Image.fromarray(v[:, :, ::-1]) for v in (left, right))
    first.save(mpo, "MPO", save_all=True, append_images=[second], quality=95)
    for path in (sbs, mpo):
        a, b = stereo.read_pair(path)
        assert a.shape == b.shape == (H, W, 3)
        assert np.abs(a.astype(int) - left).mean() < 8 and np.abs(b.astype(int) - right).mean() < 8


def test_stereo_photos_in_a_folder_become_wigglegrams_on_the_review_page(tmp_path, home, monkeypatch):
    folder = tmp_path / "beam"
    folder.mkdir()
    left, right = views()
    cv2.imwrite(str(folder / "SV_20250101_120000.jpg"), np.hstack([left, right]), [cv2.IMWRITE_JPEG_QUALITY, 95])
    cv2.imwrite(str(folder / "IMG_0001.jpg"), left)
    db_path = home / "cache" / "assets.sqlite"
    db = cache.connect(db_path)
    sources.FolderSource(folder).sync(db)
    db.close()
    monkeypatch.setattr(stereo, "subject", lambda image: (image.shape[1] / 2, image.shape[0] / 2, "salient"))
    work, out = tmp_path / "work", tmp_path / "output"
    assert stereo.build(db_path, sources.Library([], db_path), tmp_path / "cache", work / "actions") == 1
    assert stereo.build(db_path, sources.Library([], db_path), tmp_path / "cache", work / "actions") == 0
    (loop,) = (work / "actions").iterdir()
    info = json.loads((loop / "action.json").read_text(encoding="utf-8"))
    assert info["style"] == "wiggle" and info["frames"] == 2 and info["subject"] == "salient"
    assert abs(info["disparity"] * W - NEAR) <= 2
    frames = [cv2.cvtColor(np.asarray(f.convert("RGB")), cv2.COLOR_RGB2GRAY).astype(int)
              for f in _frames(loop / "animation.webp")]
    h, w = frames[0].shape
    middle = (slice(h // 2 - h // 10, h // 2 + h // 10), slice(w // 2 - w // 10, w // 2 + w // 10))
    assert np.abs(frames[0][middle] - frames[1][middle]).mean() < 12
    out.mkdir()
    (out / "candidates.json").write_text(json.dumps({"candidates": []}), encoding="utf-8")
    review = Review(work, out / "candidates.json", cache_dir=tmp_path / "web")
    (item,) = [i for i in review.items()["items"] if i["kind"] == "action"]
    assert item["style"] == "wiggle" and item["name"] == "SV_20250101_120000.jpg"
    assert item["library"] == "folder" and not item["flags"]
    assert item["taken"] and item["reason"].startswith("stereo photo")


def _frames(path):
    with Image.open(path) as im:
        for n in range(im.n_frames):
            im.seek(n)
            yield im.copy()


@pytest.mark.parametrize("name", ["SV_1.jpg"])
def test_a_photo_that_is_not_side_by_side_fails_cleanly(tmp_path, name):
    path = tmp_path / name
    cv2.imwrite(str(path), scene(600, 900))
    with pytest.raises(ValueError):
        stereo.read_pair(path)


def test_the_subject_is_a_face_else_the_salient_spot(monkeypatch):
    left, _ = views()

    class Finder:
        def detect(self, image):
            return 1, np.array([[100, 80, 40, 40, 0.9], [600, 300, 120, 140, 0.95]], np.float32)

    monkeypatch.setattr(stereo.group, "detector", lambda size: Finder())
    assert stereo.subject(left) == (660.0, 370.0, "face")

    def missing(size):
        raise stereo.assets.AssetError("model yunet is missing")

    monkeypatch.setattr(stereo.group, "detector", missing)
    x, y, how = stereo.subject(left)
    assert how == "salient" and 0 <= x < W and 0 <= y < H
