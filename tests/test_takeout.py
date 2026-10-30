"""Google Takeout: sidecar names, dates from sidecars, and copies counted once."""

import json
import os
import shutil
from datetime import datetime, timezone

import pytest
from PIL import Image

from fuseshot import cache, detect, takeout
from fuseshot.__main__ import main

from conftest import write_frames

# Sidecar and photo name pairs from immich-go (AGPL-3.0, Jean-François Casas and
# contributors), adapters/googlePhotos/matchers_test.go: the sidecar, the photo,
# and the first rule that should pair them ("" when they must not pair).
CASES = [
    ('PXL_20211013_220651983.jpg.json', 'PXL_20211013_220651983.jpg', 'fast track'),
    ('PXL_20211013_220651983.jpg.json', 'PXL_20211013_220651958.jpg', ''),
    ('PXL_20220405_090123740.PORTRAIT.jpg.json', 'PXL_20220405_090123740.PORTRAIT-modifié.jpg', 'edited'),
    ('PXL_20220405_090123740.PORTRAIT.jpg.json', 'PXL_20220405_090123741.PORTRAIT-modifié.jpg', ''),
    ('DSC_0100.JPG.json', 'DSC_0100.JPG', 'fast track'),
    ('DSC_0101.JPG(1).json', 'DSC_0101(1).JPG', 'normal'),
    ('DSC_0102.JPG(2).json', 'DSC_0102(1).JPG', ''),
    ('DSC_0103.JPG(1).json', 'DSC_0103.JPG', ''),
    ('DSC_0104.JPG.json', 'DSC_0104(1).JPG', ''),
    ('IMG_2710.HEIC(1).json', 'IMG_2710(1).HEIC', 'normal'),
    ('PXL_20231118_035751175.MP.jpg.json', 'PXL_20231118_035751175.MP.jpg', 'fast track'),
    ('PXL_20230809_203449253.LONG_EXPOSURE-02.ORIGIN.json', 'PXL_20230809_203449253.LONG_EXPOSURE-02.ORIGINA.jpg', 'normal'),
    ('05yqt21kruxwwlhhgrwrdyb6chhwszi9bqmzu16w0 2.jp.json', '05yqt21kruxwwlhhgrwrdyb6chhwszi9bqmzu16w0 2.jpg', 'normal'),
    ('😀😃😄😁😆😅😂🤣🥲☺️😊😇🙂🙃😉😌😍🥰😘😗😙😚😋.json', '😀😃😄😁😆😅😂🤣🥲☺️😊😇🙂🙃😉😌😍🥰😘😗😙😚😋😛.jpg', 'normal'),
    ('Backyard_ceremony_wedding_photography_xxxxxxx_(494).json', 'Backyard_ceremony_wedding_photography_xxxxxxx_m(494).jpg', 'normal'),
    ('Backyard_ceremony_wedding_photography_xxxxxxx_(494).json', 'Backyard_ceremony_wedding_photography_xxxxxxx_m(185).jpg', ''),
    ('original_1d4caa6f-16c6-4c3d-901b-9387de10e528_.json', 'original_1d4caa6f-16c6-4c3d-901b-9387de10e528_P.jpg', 'normal'),
    ('original_1d4caa6f-16c6-4c3d-901b-9387de10e528_.json', 'original_1d4caa6f-16c6-4c3d-901b-9387de10e528_P(1).jpg', 'forgotten duplicate'),
    ('PXL_20210102_221126856.MP~2.jpg.json', 'PXL_20210102_221126856.MP~2.jpg', 'fast track'),
    ('13039_327707840323_537645323_9470255_27214_n.j(1).json', '13039_327707840323_537645323_9470255_27214_n(1).jpg', 'normal'),
    ('20161105_170829.jpg.supplemental-metadata.json', '20161105_170829.jpg', 'normal'),
    ('Screenshot_20231027_123303_Facebook.jpg.supple.json', 'Screenshot_20231027_123303_Facebook.jpg', 'normal'),
    ('Screenshot_20231027_123303_Facebook.jpg.supple(1).json', 'Screenshot_20231027_123303_Facebook(1).jpg', 'normal'),
    ('MVIMG_20191230_232926.jpg.supplemental-metadat.json', 'MVIMG_20191230_232926.jpg', 'normal'),
    ('Screenshot_20200301-161151.png.supplemental-me.json', 'Screenshot_20200301-161151.png', 'normal'),
    ('MVIMG_20200207_134534~2.jpg.supplemental-metad.json', 'MVIMG_20200207_134534~2.jpg', 'normal'),
    ('Scan35.jpg.supplemental-metadata(1).json', 'Scan35(1).jpg', 'normal'),
    ('CLIP0001.AVI.supplemental-metadata(10).json', 'CLIP0001(10).AVI', 'normal'),
    ('IMAG0061.JPG.supplemental-metadata.json', 'IMAG0061-edited.JPG', 'edited'),
    ('IMG-20230325-WA0122~2.jpg.supplemental-metadat.json', 'IMG-20230325-WA0122~2-edited.jpg', 'edited'),
    ('2234089303984509579.supplemental-metadata.json', '2234089303984509579-edited.jpg', 'edited'),
    ('Screenshot_20231027_123303_Facebook.jpg.supple.json', 'Screenshot_20231027_123303_Facebook-edited.jpg', 'edited'),
    ('Screenshot_20231027_123303_Facebook.jpg.supple(1).json', 'Screenshot_20231027_123303_Facebook(1).jpg', 'normal'),
    ('Screenshot_2024-08-31-00-53-48-647_com.snapcha.json', 'Screenshot_2024-08-31-00-53-48-647_com.snapchat.jpg', 'normal'),
    ('IMG_4488.HEIC.supplemental-metadata.json', 'IMG_4488.HEIC', 'normal'),
    ('IMG_4488.HEIC.supplemental-metadata.json', 'IMG_4488.MP4', 'live photo video'),
    ('IMG_4488.HEIC.supplemental-metadata(1).json', 'IMG_4488(1).HEIC', 'normal'),
    ('IMG_4488.HEIC.supplemental-metadata(1).json', 'IMG_4488(1).MP4', 'live photo video'),
    ('IMG_4488.HEIC.supplemental-metadata(2).json', 'IMG_4488(2).HEIC', 'normal'),
    ('IMG_4488.HEIC.supplemental-metadata(2).json', 'IMG_4488(2).MP4', 'live photo video'),
    ('IMG_4488.HEIC.supplemental-metadata(2).json', 'IMG_4488(1).MP4', ''),
    ('IMG_4488.MP4.supplemental-metadata.json', 'IMG_4488.MP4', 'normal'),
    ('PXL_20220615_101533000(1).jpg.supplemental-metadata.json', 'PXL_20220615_101533000(1).MP4', 'live photo video'),
    ('X.jpg.supplemental-metadata.json', 'X-bearbeitet.jpg', 'edited'),
    ('X.jpg.supplemental-metadata.json', 'X-edytowany.jpg', 'edited'),
    ('X.jpg.supplemental-metadata.json', 'X-editado.jpg', 'edited'),
    ('X.jpg.supplemental-metadata.json', 'X-bewerkt.jpg', 'edited'),
    ('X.jpg.supplemental-metadata.json', 'X-編集済み.jpg', 'edited'),
    ('X.jpg.supplemental-metadata.json', 'X-edi.jpg', 'edited'),
]


@pytest.mark.parametrize("json_name,file_name,rule", CASES)
def test_sidecar_names(json_name, file_name, rule):
    first = next((name for name, matcher in takeout.MATCHERS if matcher(json_name, file_name)), "")
    assert first == rule
    assert (file_name in takeout.pair([json_name, file_name])) == bool(rule)


def sidecar(path, when, **extra):
    data = {"title": path.name, "photoTakenTime": {"timestamp": str(int(when.timestamp()))},
            "googlePhotosOrigin": {"mobileUpload": {"deviceType": "ANDROID_PHONE"}}, **extra}
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def takeout_tree(tmp_path):
    """Photos from 2024 and an album holding the same photos, EXIF stripped as
    Takeout sometimes leaves it, each with its sidecar under one of Google's names."""
    root = tmp_path / "Takeout" / "Google Photos"
    year = root / "Photos from 2024"
    frames = write_frames(tmp_path / "camera")
    start = datetime(2024, 3, 9, 1, 0, 0, tzinfo=timezone.utc)
    names = ["IMG_0000.jpg", "IMG_0001.jpg",
             "a_very_long_file_name_from_a_messaging_app_000002.jpg", "IMG_0003.jpg", "IMG_0004.jpg"]
    year.mkdir(parents=True)
    for n, (frame, name) in enumerate(zip(frames, names)):
        with Image.open(frame) as img:
            img.save(year / name, quality=92)  # no EXIF
        when = start.replace(second=3 * n)
        if n == 1:
            json_name = name + ".supplemental-metadata.json"
        elif n == 2:
            json_name = (name + ".supplemental-metadata")[:46] + ".json"
        else:
            json_name = name + ".json"
        extra = {"geoData": {"latitude": -43.5, "longitude": 172.6}} if n == 0 else {}
        sidecar(year / json_name, when, **extra)
    # Copies Takeout makes: an album folder, a (1) duplicate, and an edited version.
    album = root / "Trip to the coast"
    shutil.copytree(year, album)
    shutil.copy(year / "IMG_0003.jpg", year / "IMG_0003(1).jpg")
    sidecar(year / "IMG_0003.jpg(1).json", start.replace(second=9))
    with Image.open(year / "IMG_0004.jpg") as img:
        img.rotate(1).save(year / "IMG_0004-edited.jpg", quality=90)
    (year / "IMG_0004.MOV").write_bytes(b"not a photo")
    old = datetime(2025, 1, 1).timestamp()
    for path in year.iterdir():
        os.utime(path, (old, old))
    return tmp_path / "Takeout"


def test_takeout_photos_make_one_set(home, takeout_tree):
    main(["--home", str(home), "--source", f"folder:{takeout_tree}", "--recursive", "sync"])
    db = cache.connect(home / "cache" / "assets.sqlite")
    rows = db.execute("SELECT file_name, taken_utc, make, latitude FROM assets ORDER BY file_name").fetchall()
    assert not any(name.endswith(".json") or name.endswith(".MOV") for name, *_ in rows)
    assert len(rows) == 5 * 2 + 2
    times = {name: taken for name, taken, *_ in rows}
    assert times["IMG_0000.jpg"].startswith("2024-03-09T01:00:00")
    assert times["a_very_long_file_name_from_a_messaging_app_000002.jpg"].startswith("2024-03-09T01:00:06")
    assert times["IMG_0004-edited.jpg"] == times["IMG_0004.jpg"]
    assert {make for _, _, make, _ in rows} == {"Google Photos"}
    assert any(lat == -43.5 for *_, lat in rows)

    candidates, stats = detect.find_candidates(db, detect.Settings())
    assert stats["skipped_copy"] == 5 + 1
    assert len(candidates) == 1
    files = [row[0] for row in db.execute(
        f"SELECT file_name FROM assets WHERE id IN ({','.join('?' * len(candidates[0]['asset_ids']))})",
        candidates[0]["asset_ids"])]
    assert len(files) == 5 and "IMG_0004-edited.jpg" not in files


def test_exif_time_wins_over_sidecar(tmp_path):
    from fuseshot.sources import read_photo

    photo = write_frames(tmp_path / "p", count=1)[0]
    sidecar(tmp_path / "p" / "IMG_0000.jpg.json", datetime(2020, 1, 1, tzinfo=timezone.utc))
    asset = read_photo(photo, tmp_path / "p" / "IMG_0000.jpg.json")
    assert asset["localDateTime"].startswith("2024-03-09T14:00:00")


def test_implausible_exif_time_gives_way(tmp_path):
    from fuseshot.sources import read_photo

    folder = tmp_path / "p"
    photo = write_frames(folder, count=1)[0]
    with Image.open(photo) as img:
        exif = img.getexif()
        exif[0x0132] = "1970:01:01 00:00:00"
        exif.get_ifd(0x8769)[0x9003] = "0000:00:00 00:00:00"
        img.save(folder / "zero.jpg", exif=exif)
    sidecar(folder / "zero.jpg.json", datetime(2021, 6, 1, 12, tzinfo=timezone.utc))
    asset = read_photo(folder / "zero.jpg", folder / "zero.jpg.json")
    assert asset["fileCreatedAt"].startswith("2021-06-01T12:00:00")


def strip(src, dest):
    with Image.open(src) as img:
        img.save(dest, quality=92)  # no EXIF


def test_multi_part_export_extracted_side_by_side(home, tmp_path):
    """Two zips unpacked into their own folders, as Windows "Extract All" does: a
    sidecar in the other part from its photo, album copies, a (1) duplicate, an
    edited copy, a truncated name, and Google's own non-sidecar JSON files."""
    frames = write_frames(tmp_path / "camera")
    start = datetime(2024, 3, 9, 1, 0, 0, tzinfo=timezone.utc)
    parts = [tmp_path / "Downloads" / f"takeout-20261030T010203Z-00{n}" / "Takeout" / "Google Photos" for n in (1, 2)]
    year = [p / "Photos from 2024" for p in parts]
    for folder in year:
        folder.mkdir(parents=True)
    names = ["PXL_20240309_010000000.jpg", "IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg", "IMG_0004.jpg"]
    where = [0, 0, 0, 1, 1]  # which part each photo is in
    for n, (frame, name) in enumerate(zip(frames, names)):
        shutil.copy(frame, year[where[n]] / name)
        sidecar(year[1 if n == 1 else where[n]] / f"{name}.supplemental-metadata.json", start.replace(second=3 * n))
    # Saved from a chat: no EXIF, so the time has to come from the sidecar, which for
    # the first is in the other part and for the second is cut short at 46 characters.
    saved = "IMG-20240501-WA0001.jpg"
    long_name = "IMG_20240502_140006_shared_in_the_family_group_chat.jpg"
    strip(frames[0], year[0] / saved)
    strip(frames[1], year[0] / long_name)
    sidecar(year[1] / f"{saved}.supplemental-metadata.json", datetime(2024, 5, 1, 9, tzinfo=timezone.utc))
    sidecar(year[0] / ((long_name + ".supplemental-metadata")[:46] + ".json"),
            datetime(2024, 5, 2, 2, 0, 6, tzinfo=timezone.utc))
    shutil.copy(year[1] / "IMG_0003.jpg", year[1] / "IMG_0003(1).jpg")
    sidecar(year[1] / "IMG_0003.jpg.supplemental-metadata(1).json", start.replace(second=9))
    with Image.open(year[1] / "IMG_0004.jpg") as img:
        img.rotate(1).save(year[1] / "IMG_0004-edited.jpg", quality=90, exif=img.getexif())
        img.rotate(2).save(year[1] / "IMG_0004-bearbeitet.jpg", quality=90, exif=img.getexif())
    album = parts[1] / "Trip to the coast"
    album.mkdir()
    for n in (0, 3):
        shutil.copy(year[where[n]] / names[n], album / names[n])
    (album / "metadata.json").write_text(json.dumps({"title": "Trip to the coast"}), encoding="utf-8")
    (parts[0] / "print-subscriptions.json").write_text("{}", encoding="utf-8")
    (parts[0].parent / "archive_browser.html").write_text("<html></html>", encoding="utf-8")

    main(["--home", str(home), "--source", f"folder:{tmp_path / 'Downloads'}", "--recursive", "sync"])
    db = cache.connect(home / "cache" / "assets.sqlite")
    rows = db.execute("SELECT file_name, taken_utc FROM assets WHERE is_trashed = 0").fetchall()
    times = {}
    for name, taken in rows:
        times.setdefault(name, set()).add(taken[:19])
    assert len(rows) == 5 + 2 + 1 + 2 + 2
    assert times[saved] == {"2024-05-01T09:00:00"}
    assert times[long_name] == {"2024-05-02T02:00:06"}
    assert times["IMG_0003(1).jpg"] == times["IMG_0003.jpg"]

    candidates, stats = detect.find_candidates(db, detect.Settings())
    assert stats["skipped_copy"] == 2 + 1
    assert len(candidates) == 1
    files = sorted(row[0] for row in db.execute(
        f"SELECT file_name FROM assets WHERE id IN ({','.join('?' * len(candidates[0]['asset_ids']))})",
        candidates[0]["asset_ids"]))
    assert [n.replace("(1)", "") for n in files] == sorted(names)


def test_zip_parts_unpacked_later_are_picked_up(home, tmp_path):
    """Takeout zips unpacked one at a time between runs: the photos in a later part are
    read, and its sidecars put the right dates on photos read in an earlier run, so
    the set they make is found then."""
    from fuseshot.sources import FolderSource

    frames = write_frames(tmp_path / "camera")
    start = datetime(2024, 3, 9, 1, 0, 0, tzinfo=timezone.utc)
    downloads = tmp_path / "Downloads"
    year = [downloads / f"takeout-20261030T010203Z-00{n}" / "Takeout" / "Google Photos" / "Photos from 2024"
            for n in (1, 2)]
    names = [f"IMG_000{n}.jpg" for n in range(5)]
    first = year[0]
    first.mkdir(parents=True)
    for n in range(3):
        strip(frames[n], first / names[n])  # no EXIF: only a sidecar knows when
        os.utime(first / names[n], (datetime(2025, 1, 1 + 7 * n).timestamp(),) * 2)
    sidecar(first / f"{names[0]}.supplemental-metadata.json", start)

    db_file = home / "cache" / "assets.sqlite"
    sync = ["--home", str(home), "--source", f"folder:{downloads}", "--recursive", "sync"]
    main(sync)
    db = cache.connect(db_file)

    def times():
        return {name: taken[:19] for name, taken in db.execute(
            "SELECT file_name, taken_utc FROM assets WHERE is_trashed = 0")}

    assert times()["IMG_0000.jpg"] == "2024-03-09T01:00:00"
    assert not times()["IMG_0001.jpg"].startswith("2024-03-09")
    assert detect.find_candidates(db, detect.Settings())[0] == []

    # The second zip, unpacked an hour later: the sidecars of two photos already read,
    # and two more photos with theirs.
    year[1].mkdir(parents=True)
    for n in (1, 2):
        sidecar(year[1] / f"{names[n]}.supplemental-metadata.json", start.replace(second=3 * n))
    for n in (3, 4):
        strip(frames[n], year[1] / names[n])
        sidecar(year[1] / f"{names[n]}.supplemental-metadata.json", start.replace(second=3 * n))
    main(sync)
    db = cache.connect(db_file)
    assert times() == {name: f"2024-03-09T01:00:{3 * n:02d}" for n, name in enumerate(names)}
    candidates, _ = detect.find_candidates(db, detect.Settings())
    assert len(candidates) == 1 and len(candidates[0]["asset_ids"]) == 5

    # Nothing new: nothing is read again.
    assert FolderSource(downloads, recursive=True).sync(db)[0] == 0


def test_kept_file_gets_the_place_from_the_sidecar(home, takeout_tree):
    """A Takeout photo's GPS position is often only in its .json. The details read
    again for a set (and written into the kept file) have to include it."""
    from fuseshot import metadata, sources

    main(["--home", str(home), "--source", f"folder:{takeout_tree}", "--recursive", "sync"])
    db = cache.connect(home / "cache" / "assets.sqlite")
    asset_id = db.execute("SELECT id FROM assets WHERE latitude = -43.5").fetchone()[0]
    source = sources.FolderSource(takeout_tree, recursive=True, db_path=home / "cache" / "assets.sqlite")
    info = source.asset_info(asset_id)
    assert info["exifInfo"]["latitude"] == -43.5
    tags = metadata.frame_tags({"latitude": info["exifInfo"]["latitude"], "longitude": info["exifInfo"]["longitude"]})
    assert tags["gps"][1] == "S" and tags["gps"][3] == "E"


def test_the_copy_with_its_json_is_the_one_used(home, takeout_tree):
    """An album copy without a .json must not stand in for the year folder copy
    that has one, or the set loses the place and time Google kept there."""
    album = takeout_tree / "Google Photos" / "Trip to the coast"
    for path in album.glob("*.json"):
        path.unlink()
    main(["--home", str(home), "--source", f"folder:{takeout_tree}", "--recursive", "sync"])
    db = cache.connect(home / "cache" / "assets.sqlite")
    stats = {"images": 0, "skipped_no_camera": 0, "skipped_screenshot": 0, "skipped_bad_time": 0}
    used = {shot.id for shot in detect.load_shots(db, stats)}
    folders = {row[0] for row in db.execute(
        f"SELECT folder FROM source_files WHERE id IN ({','.join('?' * len(used))})", sorted(used))}
    assert not any(folder.endswith("Trip to the coast") for folder in folders)
    assert any(shot_lat == -43.5 for shot_lat, in db.execute(
        f"SELECT latitude FROM assets WHERE id IN ({','.join('?' * len(used))})", sorted(used)))
