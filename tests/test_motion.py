import json
import struct
import time
from datetime import datetime, timezone

import cv2
import numpy as np
import pytest
from PIL import Image

from conftest import scene
from fuseshot import cache, motion, sources
from fuseshot.review import Review, byte_range

W, H = 640, 480


def clip_frames(kind, count=30):
    """A short clip: a still camera with a ball crossing it, or a camera panning over a still scene."""
    background = scene(width=W * 3, height=H, seed=3)
    frames = []
    for n in range(count):
        if kind == "pan":
            frames.append(background[:, 25 * n:25 * n + W].copy())
            continue
        frame = background[:, :W].copy()
        cv2.circle(frame, (60 + 17 * n, H // 2), 80, (20, 220, 240), -1)
        frames.append(frame)
    return frames


def write_clip(path, kind="ball"):
    tmp = path.with_suffix(".tmp.mp4")
    writer = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"mp4v"), 30, (W, H))
    for frame in clip_frames(kind):
        writer.write(frame)
    writer.release()
    tmp.replace(path)
    return path.read_bytes()


def still_jpeg(path, frame, xmp=b"", when="2024:03:09 14:00:00"):
    """A JPEG with camera EXIF and, when given, an XMP packet after it, as phones write them."""
    exif = Image.Exif()
    exif[0x010F], exif[0x0110], exif[0x0132] = "Google", "Pixel 8", when
    exif.get_ifd(0x8769)[0x9003] = when
    exif.get_ifd(0x8769)[0x9011] = "+13:00"
    Image.fromarray(frame[:, :, ::-1]).save(path, quality=90, exif=exif)
    data = path.read_bytes()
    if xmp:
        packet = b"http://ns.adobe.com/xap/1.0/\x00" + xmp
        segment = b"\xff\xe1" + struct.pack(">H", len(packet) + 2) + packet
        at = 4 + struct.unpack(">H", data[4:6])[0] if data[2:4] == b"\xff\xe1" else 2
        data = data[:at] + segment + data[at:]
    path.write_bytes(data)
    return data


def pixel_xmp(length):
    return (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description GCamera:MotionPhoto="1" '
            b'GCamera:MotionPhotoVersion="1"><Container:Directory><rdf:Seq>'
            b'<rdf:li rdf:parseType="Resource"><Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary"/></rdf:li>'
            b'<rdf:li rdf:parseType="Resource"><Container:Item Item:Mime="video/mp4" Item:Semantic="MotionPhoto" '
            b'Item:Length="' + str(length).encode() + b'" Item:Padding="0"/></rdf:li>'
            b'</rdf:Seq></Container:Directory></rdf:Description></rdf:RDF></x:xmpmeta>')


def set_mov_time(path, moment):
    data = bytearray(path.read_bytes())
    at = data.find(b"mvhd")
    seconds = int((moment - motion.MAC_EPOCH).total_seconds())
    data[at + 8:at + 12] = struct.pack(">I", seconds)
    path.write_bytes(bytes(data))


@pytest.fixture
def library(tmp_path, home):
    """A folder of synthetic phone photos: a Pixel motion photo (new and old XMP), a Samsung one,
    a Live Photo pair, a motion photo that pans, and a plain JPEG."""
    folder = tmp_path / "phone"
    folder.mkdir()
    ball, pan = write_clip(tmp_path / "ball.mp4"), write_clip(tmp_path / "pan.mp4", "pan")
    first = clip_frames("ball", 1)[0]
    still_jpeg(folder / "PXL_20240309_010000000.MP.jpg", first, pixel_xmp(len(ball)))
    (folder / "PXL_20240309_010000000.MP.jpg").write_bytes((folder / "PXL_20240309_010000000.MP.jpg").read_bytes() + ball)
    old = b'<x:xmpmeta><rdf:Description GCamera:MicroVideo="1" GCamera:MicroVideoVersion="1" ' \
          b'GCamera:MicroVideoOffset="' + str(len(ball)).encode() + b'"/></x:xmpmeta>'
    still_jpeg(folder / "MVIMG_20190101_120000.jpg", first, old, "2019:01:01 12:00:00")
    (folder / "MVIMG_20190101_120000.jpg").write_bytes((folder / "MVIMG_20190101_120000.jpg").read_bytes() + ball)
    still_jpeg(folder / "20240310_090000.jpg", first, when="2024:03:10 09:00:00")
    samsung = folder / "20240310_090000.jpg"
    samsung.write_bytes(samsung.read_bytes() + b"MotionPhoto_Data" + ball + b"\x00\x00\x00\x00SEFH trailer")
    still_jpeg(folder / "IMG_0042.JPG", first, when="2024:03:11 08:00:00")
    (folder / "IMG_0042.MOV").write_bytes(ball)
    still_jpeg(folder / "PXL_20240312_010000000.MP.jpg", clip_frames("pan", 1)[0], pixel_xmp(len(pan)), "2024:03:12 14:00:00")
    (folder / "PXL_20240312_010000000.MP.jpg").write_bytes((folder / "PXL_20240312_010000000.MP.jpg").read_bytes() + pan)
    still_jpeg(folder / "IMG_0050.jpg", first, when="2024:03:13 08:00:00")
    db_path = home / "cache" / "assets.sqlite"
    db = cache.connect(db_path)
    sources.FolderSource(folder).sync(db)
    db.close()
    ids = {name: asset_id for asset_id, name in
           cache.connect(db_path).execute("SELECT id, file_name FROM assets").fetchall()}
    return {"folder": folder, "db": db_path, "lib": sources.Library([], db_path), "ids": ids, "ball": ball}


def test_motion_photos_are_found_by_their_xmp_and_marks(library):
    folder = library["folder"]
    for name in ("PXL_20240309_010000000.MP.jpg", "MVIMG_20190101_120000.jpg", "20240310_090000.jpg"):
        start, end = motion.embedded_range(folder / name)
        assert (folder / name).read_bytes()[start:end] == library["ball"], name
    assert motion.embedded_range(folder / "IMG_0050.jpg") is None
    assert motion.embedded_range(folder / "IMG_0042.JPG") is None


def test_live_photos_pair_by_name_and_time(library, tmp_path):
    folder = library["folder"]
    videos = motion.folder_videos(folder)
    taken = "2024-03-10T19:00:00Z"
    assert motion.companion(folder / "IMG_0042.JPG", videos, taken) == folder / "IMG_0042.MOV"
    set_mov_time(folder / "IMG_0042.MOV", datetime(2024, 3, 10, 19, 0, 1, tzinfo=timezone.utc))
    assert motion.mov_time(folder / "IMG_0042.MOV") == datetime(2024, 3, 10, 19, 0, 1, tzinfo=timezone.utc)
    assert motion.companion(folder / "IMG_0042.JPG", videos, taken) == folder / "IMG_0042.MOV"
    assert motion.companion(folder / "IMG_0042.JPG", videos, "2024-03-10T21:00:00Z") is None
    assert motion.companion(folder / "IMG_0050.jpg", videos, taken) is None


def test_scan_offers_the_still_camera_clips_and_reads_each_jpeg_once(library, tmp_path, monkeypatch):
    out = tmp_path / "output" / "motion.json"
    clips = motion.scan(library["db"], library["lib"], tmp_path / "cache", out)
    by_name = {c["file_name"]: c for c in clips.values()}
    assert set(by_name) == {"PXL_20240309_010000000.MP.jpg", "MVIMG_20190101_120000.jpg", "20240310_090000.jpg",
                            "IMG_0042.JPG", "PXL_20240312_010000000.MP.jpg"}
    assert by_name["IMG_0042.JPG"]["clip"]["kind"] == "pair"
    assert by_name["20240310_090000.jpg"]["clip"]["kind"] == "embedded"
    for name in ("PXL_20240309_010000000.MP.jpg", "MVIMG_20190101_120000.jpg", "20240310_090000.jpg", "IMG_0042.JPG"):
        assert by_name[name]["action"], (name, by_name[name])
    assert not by_name["PXL_20240312_010000000.MP.jpg"]["action"]
    assert by_name["PXL_20240312_010000000.MP.jpg"]["reason"] == "the camera moved"

    def no_reads(path):
        raise AssertionError(f"read {path} again")

    monkeypatch.setattr(motion, "embedded_range", no_reads)
    monkeypatch.setattr(motion, "check", no_reads)
    assert motion.scan(library["db"], library["lib"], tmp_path / "cache", out) == clips


def test_a_kept_clip_becomes_a_loop_and_nothing_is_encoded_before(library, tmp_path):
    work, out = tmp_path / "work", tmp_path / "output"
    work.mkdir()
    clips = motion.scan(library["db"], library["lib"], tmp_path / "cache", out / "motion.json")
    (out / "candidates.json").write_text(json.dumps({"candidates": []}), encoding="utf-8")
    review = Review(work, out / "candidates.json", cache_dir=tmp_path / "web", library=library["lib"])
    items = {i["id"]: i for i in review.items()["items"] if i["kind"] == "action"}
    offered = {k for k, c in clips.items() if c["action"]}
    assert set(items) == offered
    key = next(k for k, c in clips.items() if c["file_name"] == "PXL_20240309_010000000.MP.jpg")
    item = items[key]
    assert item["clip"].endswith(f"/clip/{key}/video") and item["href"] is None
    assert item["taken"] == "2024-03-09 14:00:00" and item["type"] == "action"
    assert item["name"] == "PXL_20240309_010000000.MP.jpg"
    assert review.clip_file(key).read_bytes() == library["ball"]
    assert Image.open(review.clip_still(key, "thumb")).size[0] <= 600
    assert review.item_detail("action", key)["detected"] == "action"
    assert not (work / "actions").exists()

    review.decide({"set": key, "kind": "action", "decision": "kept"})
    loop = work / "actions" / key
    for _ in range(600):
        if (loop / "action.json").exists() and key not in review.making:
            break
        time.sleep(0.1)
    info = json.loads((loop / "action.json").read_text(encoding="utf-8"))
    assert info["asset_ids"] == [clips[key]["asset_id"]] and info["clip"] == "embedded"
    assert info["frames"] >= 10 and 50 <= info["delay_ms"] <= 100
    assert (loop / "animation.webp").exists() and (loop / "poster.jpg").exists()
    after = [i for i in review.items()["items"] if i["id"] == key]
    assert len(after) == 1 and "clip" not in after[0] and after[0]["decision"] == "kept"
    assert after[0]["taken"].startswith("2024-03-09")


def test_video_ranges():
    assert byte_range("bytes=0-", 100) == (0, 100)
    assert byte_range("bytes=10-19", 100) == (10, 20)
    assert byte_range("bytes=-30", 100) == (70, 100)
    assert byte_range("bytes=90-500", 100) == (90, 100)
    assert byte_range("bytes=200-", 100) is None
    assert byte_range(None, 100) is None


def test_the_check_tells_a_still_camera_from_a_pan():
    assert motion.check(clip_frames("ball")[::6])["action"]
    assert not motion.check(clip_frames("pan")[::6])["action"]
    still = clip_frames("ball", 1) * 6
    assert motion.check([np.copy(f) for f in still])["reason"] == "nothing moves"


def test_one_step_that_does_not_line_up_is_skipped(monkeypatch):
    frames = clip_frames("ball")[::6]
    real = motion.line_up

    def failing(steps):
        calls = []

        def line_up(first, second):
            calls.append(1)
            return None if len(calls) in steps else real(first, second)
        return line_up

    monkeypatch.setattr(motion, "line_up", failing({2}))
    assert motion.check(frames)["action"]
    monkeypatch.setattr(motion, "line_up", failing({2, 3}))
    assert motion.check(frames)["reason"] == "frames do not line up"
