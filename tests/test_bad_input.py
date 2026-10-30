"""Damaged files, folders that cannot be opened and full disks give a plain message, not a crash."""

import errno
import os

import pytest

from fuseshot import stitch
from fuseshot.__main__ import explain, main
from fuseshot.sources import SourceError, preview_bytes

from conftest import write_frames


def test_damaged_files_are_named_and_skipped(home, tmp_path, capfd):
    photos = write_frames(tmp_path / "photos", count=3)
    folder = photos[0].parent
    (folder / "empty.jpg").write_bytes(b"")
    (folder / "noise.jpg").write_bytes(os.urandom(4000))
    (folder / "not-a-raw.ARW").write_text("hello", encoding="utf-8")
    main(["--home", str(home), "--source", f"folder:{folder}", "sync"])
    err = capfd.readouterr()
    text = err.out + err.err
    assert "empty.jpg: empty file" in text
    assert "noise.jpg" in text and "not-a-raw.ARW" in text
    assert "3 unreadable" in text


def test_cut_short_photo_names_itself(tmp_path):
    photo = write_frames(tmp_path / "photos", count=1)[0]
    short = tmp_path / "short.jpg"
    short.write_bytes(photo.read_bytes()[:20000])
    with pytest.raises(SourceError, match="short.jpg could not be read, it may be damaged or cut short"):
        preview_bytes(short, 500)


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs a folder this user cannot open")
def test_folder_that_cannot_be_opened_is_reported(home, tmp_path, capfd):
    photos = write_frames(tmp_path / "photos" / "open", count=2)
    locked = photos[0].parent.parent / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        main(["--home", str(home), "--source", f"folder:{tmp_path / 'photos'}", "--recursive", "sync"])
    finally:
        locked.chmod(0o755)
    err = capfd.readouterr()
    assert f"could not open {locked}" in err.out + err.err


def test_full_disk_and_long_path_messages():
    full = explain(OSError(errno.ENOSPC, "No space left on device", "/data/out/pano.jpg"))
    assert full.startswith("the disk is full (writing /data/out/pano.jpg)")
    assert "run the same command again" in full
    long = OSError(errno.ENOENT, "The filename or extension is too long")
    long.winerror = 206
    assert "too long for Windows" in explain(long)
    assert "LongPathsEnabled" in explain(OSError(errno.ENAMETOOLONG, "File name too long", "x" * 300))


@pytest.mark.skipif(os.name == "nt", reason="uses a shell script as the tool")
def test_tool_that_runs_out_of_room_is_tried_again_later(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "nona").write_text("#!/bin/sh\necho 'TIFFAppendToStrip: Write error at scanline 261.'\nexit 134\n")
    (bin_dir / "nona").chmod(0o755)
    monkeypatch.setattr(stitch.tools, "require", lambda name: bin_dir / "nona")
    run = stitch.Runner(tmp_path / "render.log")
    with pytest.raises(stitch.OutOfSpace, match="could not write its result"):
        run("nona", "-o", "out")


def test_big_panoramas_are_read(tmp_path):
    """A 340 megapixel phone panorama is past Pillow's own limit but is a photo."""
    from PIL import Image

    from fuseshot import sources
    path = tmp_path / "PANO_20241106_155730.jpg"
    Image.new("RGB", (8, 8)).save(path)
    data = bytearray(path.read_bytes())
    sof = data.index(b"\xff\xc0")
    data[sof + 5:sof + 9] = (12000).to_bytes(2, "big") + (28300).to_bytes(2, "big")
    path.write_bytes(bytes(data))
    with Image.open(path) as img:
        assert img.size == (28300, 12000)
    asset = sources.read_photo(path)
    assert (asset["width"], asset["height"]) == (28300, 12000)


@pytest.mark.parametrize("name,expected", [
    ("IMG_20240108_080218_268.jpg", "2024-01-08 08:02:18.268000"),
    ("IMG20240420181421.heic", "2024-04-20 18:14:21"),
    ("20240629_161840.jpg", "2024-06-29 16:18:40"),
    ("PANO_20241110_104217.jpg", "2024-11-10 10:42:17"),
    ("Screenshot_2024-05-01-10-00-00.png", "2024-05-01 10:00:00"),
    ("DSC00001.JPG", None),
    ("IMG_1234.jpg", None),
    ("photo_20241399_250000.jpg", None),
])
def test_time_in_a_file_name(name, expected):
    from fuseshot import sources
    found = sources.name_time(name)
    assert (str(found) if found else None) == expected


def test_saved_time_does_not_make_a_set(tmp_path, home):
    """A phone writes its own panoramas with only the time they were saved. Three
    made days apart but saved together must not look like one sweep."""
    from PIL import Image

    from fuseshot import cache, detect, sources
    from fuseshot.__main__ import main
    folder = tmp_path / "phone"
    folder.mkdir()
    for n, name in enumerate(["PANO_20241110_104217.jpg", "PANO_20241108_135236.jpg", "PANO_20241110_105313.jpg"]):
        exif = Image.Exif()
        exif[0x010F], exif[0x0110], exif[0x0132] = "OnePlus", "OnePlus 12R", f"2024:11:08 13:47:5{n}"
        Image.new("RGB", (64, 32)).save(folder / name, exif=exif)
    asset = sources.read_photo(folder / "PANO_20241110_104217.jpg")
    assert asset["localDateTime"].startswith("2024-11-10T10:42:17")
    main(["--home", str(home), "--source", f"folder:{folder}", "sync"])
    db = cache.connect(home / "cache" / "assets.sqlite")
    candidates, _ = detect.find_candidates(db, detect.Settings(), [])
    assert candidates == []


def test_long_work_folder_on_windows_is_named():
    from fuseshot import doctor, stitch

    deep = "C:\\Users\\someone\\" + "\\".join(["a-long-folder-name"] * 12)
    with pytest.raises(stitch.StitchError, match="too long for Hugin"):
        stitch.check_tool_dir(deep, windows=True)
    stitch.check_tool_dir(deep, windows=False)
    stitch.check_tool_dir("C:\\fs\\work\\F20240101T120000", windows=True)
    assert "would pass" in doctor.home_too_long(deep, windows=True)
    assert doctor.home_too_long("C:\\fs", windows=True) is None
    assert doctor.home_too_long(deep, windows=False) is None
