import json

import cv2
import numpy as np
from PIL import Image

from conftest import scene
from fuseshot import rig, splat

W, H = 480, 360


def pair(seed):
    wall = scene(W + 30, H, seed=seed)
    return wall[:, 30:30 + W].copy(), wall[:, :W].copy()


def sbs_photo(path, seed=3):
    left, right = pair(seed)
    Image.fromarray(np.hstack([left, right])[:, :, ::-1]).save(path, "JPEG", quality=95)


def stereo_set(tmp_path, monkeypatch, count=3, plain=()):
    manifest = []
    for n in range(1, count + 1):
        path = tmp_path / ("IMG_%04d.jpg" % n if n in plain else "SV_2025010%d_120000.jpg" % n)
        if n in plain:
            cv2.imwrite(str(path), scene(W, H, seed=n))
        else:
            sbs_photo(path, seed=n)
        manifest.append({"index": n, "image": str(path), "asset_id": f"a{n}", "make": None, "f35": 24})
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps({"candidates": [{"set_id": "P00001"}]}), encoding="utf-8")
    monkeypatch.setattr(splat, "manifest_for", lambda *a: manifest)
    return splat.prepare(tmp_path / "work", "P00001", candidates, None)


def test_the_rig_puts_the_right_lens_a_baseline_to_the_side(tmp_path):
    (config,) = json.loads(rig.config_file(tmp_path / "rig.json").read_text())
    left, right = config["cameras"]
    assert left == {"image_prefix": "left/", "ref_sensor": True}
    assert right["cam_from_rig_rotation"] == [1.0, 0.0, 0.0, 0.0]
    assert right["cam_from_rig_translation"] == [-rig.BASELINE_M, 0.0, 0.0]


def test_only_stereo_photos_give_two_views(tmp_path):
    sbs_photo(tmp_path / "SV_1.jpg")
    cv2.imwrite(str(tmp_path / "IMG_1.jpg"), np.hstack(pair(3)))
    left, right = rig.views(tmp_path / "SV_1.jpg")
    assert left.shape == right.shape == (H, W, 3)
    assert rig.views(tmp_path / "IMG_1.jpg") is None


def test_a_set_of_stereo_photos_is_prepared_as_a_rig(tmp_path, monkeypatch):
    info = stereo_set(tmp_path, monkeypatch)
    images = tmp_path / "work" / "splats" / "P00001" / "images"
    assert info["rig"] == {"baseline_m": rig.BASELINE_M}
    assert [f["file"] for f in info["frames"]] == ["left/001.jpg", "left/002.jpg", "left/003.jpg"]
    assert all((images / f["pair"]).exists() for f in info["frames"])
    with Image.open(images / "right/002.jpg") as im:
        assert im.size == (W, H) and im.getexif().get_ifd(0x8769)[0xA405] == 24


def test_a_mixed_set_uses_the_left_view_of_its_stereo_photos(tmp_path, monkeypatch):
    info = stereo_set(tmp_path, monkeypatch, plain=(2,))
    assert "rig" not in info
    assert [(f["file"], tuple(f["size"])) for f in info["frames"]] == [
        ("001.jpg", (W, H)), ("002.jpg", (W, H)), ("003.jpg", (W, H))]


def test_sfm_on_a_rig_ties_the_pairs_and_keeps_the_baseline(tmp_path, monkeypatch):
    stereo_set(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(splat, "colmap", lambda args, log: calls.append([str(a) for a in args]) or "")
    result = splat.sfm(tmp_path / "work", "P00001", gpu=False, threads=1)
    names = [c[0] for c in calls]
    assert names[:3] == ["feature_extractor", "rig_configurator", "exhaustive_matcher"]
    extract, configure, mapper = calls[0], calls[1], calls[names.index("mapper")]
    assert "--ImageReader.single_camera_per_folder" in extract and "--ImageReader.single_camera" not in extract
    assert configure[configure.index("--rig_config_path") + 1].endswith("rig_config.json")
    assert mapper[mapper.index("--Mapper.ba_refine_sensor_from_rig") + 1] == "0"
    assert result["status"] == "failed" and result["images"] == 6


def sbs_video(path, count=90, fps=30):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (640, 180))
    wall = scene(400, 180, seed=2)
    for n in range(count):
        view = np.roll(wall, n, axis=1)[:, :320]
        writer.write(np.hstack([view, np.roll(wall, n + 6, axis=1)[:, :320]]))
    writer.release()


def test_a_stereo_video_becomes_a_planned_set_of_pairs(tmp_path):
    video = tmp_path / "SV_20250101_120000.avi"
    sbs_video(video)
    work = tmp_path / "work"
    info = splat.from_stereo_video(work, video)
    assert info["set_id"] == "SV-SV_20250101_120000" and info["rig"]
    assert len(info["frames"]) == 12 and info["frames"][1]["at_s"] == 0.27
    images = work / "splats" / info["set_id"] / "images"
    assert len(list((images / "right").glob("*.jpg"))) == 12
    with Image.open(images / "left" / "000.jpg") as im:
        assert im.size == (320, 180)
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps({"candidates": []}), encoding="utf-8")
    (row,) = splat.make_plan(candidates, tmp_path / "cache", work_dir=work)["sets"]
    assert row["kind"] == "stereo video" and row["usable"] == 12 and not row["skip"]


def test_long_stereo_videos_are_capped(tmp_path):
    video = tmp_path / "clip.avi"
    sbs_video(video, count=300)
    frames = rig.video_pairs(video, tmp_path / "images", 1600, cap=20)
    assert len(frames) == 20 and frames[-1]["at_s"] > 9


def test_holdout_renders_are_matched_inside_the_rig_folders(tmp_path):
    images, evals = tmp_path / "images", tmp_path / "eval"
    for folder, colour in (("left", 40), ("right", 200)):
        (images / folder).mkdir(parents=True)
        (evals / folder).mkdir(parents=True)
        Image.new("RGB", (32, 24), (colour,) * 3).save(images / folder / "000.jpg", quality=100)
        Image.new("RGB", (32, 24), (colour,) * 3).save(evals / folder / "000.png")
    quality = splat.holdout_quality(evals, images)
    assert [v["frame"] for v in quality["holdout_views"]] == ["000.jpg", "000.jpg"]
    assert quality["quality"] == "good"
