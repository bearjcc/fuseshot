"""Camera raws and Pixel file names.

The raw files are public samples from raw.pixls.us (CC0), fetched the first
time the tests run and kept outside the repository; without a network the
tests that need them are skipped."""

import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from fuseshot import action, cache, detect, fetch, raw, sources
from fuseshot.__main__ import main

from conftest import write_frames

SAMPLES = {
    "a7m3.ARW": ("Sony/ILCE-7M3/_DSC0009.ARW", "46ef96914aab9b3c1e56d5544b29c52b7052cf57"),
    "PXL_20240415_103400204.RAW-02.ORIGINAL.dng": (
        "Google/Pixel%208%20Pro/PXL_20240415_103400204.RAW-02.ORIGINAL.dng",
        "0d1bb1bb751a50d4efe375b6c8bf3d4bc2920aa4"),
}


def sample(name):
    folder = Path(os.environ.get("FUSESHOT_TEST_SAMPLES") or Path.home() / ".cache" / "fuseshot-test-samples")
    path = folder / name
    where, sha1 = SAMPLES[name]
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(f"https://raw.pixls.us/data/{where}", timeout=60) as response:
                data = response.read()
        except OSError as err:
            pytest.skip(f"raw sample not available: {err}")
        if hashlib.sha1(data).hexdigest() != sha1:
            pytest.skip("raw sample changed upstream")
        part = path.with_suffix(".part")
        part.write_bytes(data)
        part.replace(path)
    return path


@pytest.fixture(scope="module")
def arw():
    return sample("a7m3.ARW")


@pytest.fixture(scope="module")
def dng():
    return sample("PXL_20240415_103400204.RAW-02.ORIGINAL.dng")


def test_sony_raw_details(arw):
    asset = sources.read_photo(arw)
    info = asset["exifInfo"]
    assert (info["make"], info["model"]) == ("SONY", "ILCE-7M3")
    assert asset["originalMimeType"] == "image/arw"
    assert (asset["width"], asset["height"]) == (6024, 4024)
    assert info["focalLength"] == 32
    assert info["timeZone"] == "UTC+1"


def test_pixel_dng_details(dng):
    asset = sources.read_photo(dng)
    info = asset["exifInfo"]
    assert info["make"] == "Google" and "Pixel 8 Pro" in info["model"]
    assert asset["originalMimeType"] == "image/dng"
    assert asset["localDateTime"][19:23] == ".621"


@pytest.mark.parametrize("name", ["arw", "dng"])
def test_raw_preview_is_the_embedded_jpeg(name, request):
    path = request.getfixturevalue(name)
    data = sources.preview_bytes(path, side=1024)
    with Image.open(__import__("io").BytesIO(data)) as img:
        assert img.format == "JPEG" and max(img.size) == 1024


def test_raw_is_developed_for_stitching(arw, tmp_path):
    out = tmp_path / "frame.jpg"
    fetch.to_jpeg(arw, out)
    with Image.open(out) as img:
        assert img.size == (6024, 4024)
        assert img.getexif().get_ifd(0x8769).get(0x920A) == 32
    assert not list(tmp_path.glob("*.part"))


def frame_exif(when_utc, zone_hours=13):
    local = when_utc + timedelta(hours=zone_hours)
    exif = Image.Exif()
    exif[0x010F], exif[0x0110] = "Google", "Pixel 8 Pro"
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = local.strftime("%Y:%m:%d %H:%M:%S")
    sub[0x9291] = f"{local.microsecond // 1000:03d}"
    sub[0x9011] = f"+{zone_hours:02d}:00"
    sub[0x920A], sub[0xA405] = 6.9, 24
    return exif


def test_pixel_burst_with_raw_copies_is_one_burst(home, tmp_path):
    """A burst as a Pixel leaves it in a Takeout: each frame is a cover JPEG and a
    second copy of the raw (a JPEG with no EXIF when stored at saver quality), each
    with a sidecar. This is made up to the real naming pattern: no public sample of a
    real Pixel burst was found."""
    folder = tmp_path / "Takeout" / "Google Photos" / "Photos from 2024"
    frames = write_frames(tmp_path / "scene", count=6, step_degrees=1)
    folder.mkdir(parents=True)
    start = datetime(2024, 3, 9, 1, 0, 0, 120000, tzinfo=timezone.utc)
    for n, frame in enumerate(frames):
        when = start + timedelta(milliseconds=250 * n)
        stamp = when.strftime("%Y%m%d_%H%M%S") + f"{when.microsecond // 1000:03d}"
        cover = f"PXL_{stamp}.RAW-01.MP.COVER.jpg"
        original = f"PXL_{stamp}.RAW-02.ORIGINAL.jpg"
        with Image.open(frame) as img:
            img.save(folder / cover, quality=90, exif=frame_exif(when))
            img.save(folder / original, quality=90)
        for name in (cover, original):
            json_name = (name + ".supplemental-metadata")[:46] + ".json"
            (folder / json_name).write_text(json.dumps(
                {"title": name, "photoTakenTime": {"timestamp": str(int(when.timestamp()))}}), encoding="utf-8")

    main(["--home", str(home), "--source", f"folder:{tmp_path / 'Takeout'}", "--recursive", "sync"])
    db = cache.connect(home / "cache" / "assets.sqlite")
    makes = {row[0] for row in db.execute("SELECT make FROM assets")}
    assert makes == {"Google"}
    bursts = []
    candidates, stats = detect.find_candidates(db, detect.Settings(), bursts)
    assert candidates == []
    assert len(bursts) == 1
    burst = bursts[0]
    assert len(burst["asset_ids"]) == 6 and len(burst["paired_ids"]) == 6
    names = [row[0] for row in db.execute(
        f"SELECT file_name FROM assets WHERE id IN ({','.join('?' * 6)})", burst["asset_ids"])]
    assert all(".COVER." in n for n in names)

    main(["--home", str(home), "--source", f"folder:{tmp_path / 'Takeout'}", "--recursive", "detect"])
    found = action.load_sets(home / "output" / "candidates.json")
    assert [s["set_id"] for s in found] == [burst["set_id"]]  # the group check looks at it


@pytest.mark.parametrize("name,index", [
    ("PXL_20240309_010000120.RAW-01.MP.COVER.jpg", None),
    ("PXL_20240309_010000120.RAW-02.ORIGINAL.dng", None),
    ("PXL_20240309_010000120.BURST-03.jpg", "03"),
    ("00001IMG_00001_BURST20190101120000123.jpg", "00001"),
    ("IMG_20231207_101605_BURST002.jpg", "002"),
    ("IMG_1234.jpg", None),
])
def test_burst_frame_numbers(name, index):
    assert detect.burst_index(name) == index


def test_raw_types_are_photos():
    assert raw.is_raw("x.ARW") and raw.is_raw("y.dng") and not raw.is_raw("z.jpg")
    assert ".arw" in sources.PHOTO_TYPES and ".json" not in sources.PHOTO_TYPES


def test_raw_and_camera_jpeg_count_as_one_shot(arw, home, tmp_path):
    """A camera saving RAW+JPEG writes both files with one name and one time.
    No public sample comes with the camera's own JPEG, so the JPEG here is the
    one embedded in the real ARW, given the ARW's metadata with exiftool. Three
    copies at one second apart make a set of three camera positions."""
    exiftool = shutil.which("exiftool")
    if not exiftool:
        pytest.skip("exiftool not installed")
    folder = tmp_path / "card"
    folder.mkdir()
    for n in (1, 2, 3):
        raw_file = folder / f"DSC0000{n}.ARW"
        shutil.copyfile(arw, raw_file)
        subprocess.run([exiftool, "-q", "-overwrite_original", f"-DateTimeOriginal=2024:05:01 10:00:0{n}",
                        str(raw_file)], check=True)
        jpeg = folder / f"DSC0000{n}.JPG"
        jpeg.write_bytes(subprocess.run([exiftool, "-b", "-PreviewImage", str(raw_file)],
                                        check=True, capture_output=True).stdout)
        subprocess.run([exiftool, "-q", "-overwrite_original", "-tagsfromfile", str(raw_file), "-all:all",
                        "-Orientation=", str(jpeg)], check=True)

    main(["--home", str(home), "--source", f"folder:{folder}", "sync"])
    db = cache.connect(home / "cache" / "assets.sqlite")
    names = dict(db.execute("SELECT id, file_name FROM assets"))
    assert len(names) == 6
    candidates, _ = detect.find_candidates(db, detect.Settings(), [])
    assert len(candidates) == 1
    found = candidates[0]
    assert sorted(names[i] for i in found["asset_ids"]) == ["DSC00001.JPG", "DSC00002.JPG", "DSC00003.JPG"]
    assert sorted(names[i] for i in found["paired_ids"]) == ["DSC00001.ARW", "DSC00002.ARW", "DSC00003.ARW"]
