"""A made-up panorama from start to finish: folder, cache, detect, overlap check, and stitching if Hugin is here."""

import json

import pytest

from fuseshot import tools
from fuseshot.__main__ import main

SET = "F20240309T140000"  # folder sets are named after the moment they start


def run(home, photos, *args):
    main(["--home", str(home), "--source", f"folder:{photos[0].parent}", *args])


def test_turning_camera_is_found_and_checked(home, photos):
    run(home, photos, "run", "--no-stitch")
    candidates = json.loads((home / "output" / "candidates.json").read_text(encoding="utf-8"))["candidates"]
    assert [c["set_id"] for c in candidates] == [SET]
    verified = json.loads((home / "output" / "verified.json").read_text(encoding="utf-8"))
    sub = verified[SET]["sub_sets"][0]
    assert sub["type"] == "single-row rotation"
    assert sub["frames_count"] == len(photos)


def test_photos_are_left_alone(home, photos):
    before = {p: p.stat().st_mtime_ns for p in photos}
    run(home, photos, "run", "--no-stitch")
    assert {p: p.stat().st_mtime_ns for p in photos} == before
    assert sorted(photos[0].parent.iterdir()) == sorted(photos)


def test_dry_run_does_nothing(home, photos):
    run(home, photos, "run", "--dry-run")
    assert not (home / "cache" / "assets.sqlite").exists()


@pytest.mark.skipif(tools.find("hugin") is None, reason="Hugin is not installed")
def test_stitched_preview(home, photos):
    run(home, photos, "run")
    record = json.loads((home / "work" / SET / "batch.json").read_text(encoding="utf-8"))
    assert record["status"] == "stitched", record.get("error")


def test_missing_hugin_fails_before_marking_sets(home, photos, monkeypatch):
    monkeypatch.setenv("FUSESHOT_HUGIN", str(home / "no-hugin"))
    with pytest.raises(SystemExit):
        run(home, photos, "run")
    assert not (home / "work" / SET / "batch.json").exists()


@pytest.mark.skipif(tools.find("hugin") is None, reason="Hugin is not installed")
def test_edit_makes_a_new_version(home, photos):
    run(home, photos, "run")
    run(home, photos, "edit", SET, "--projection", "equirectangular", "--model", "mosaic")
    set_dir = home / "work" / SET
    done = json.loads((set_dir / "edit" / "done.json").read_text(encoding="utf-8"))
    assert done["state"] == "done", done.get("error")
    record = json.loads((set_dir / "batch.json").read_text(encoding="utf-8"))
    assert record["preview_file"] == "previews/equirectangular.jpg" or record["preview_file"].startswith("previews/equirectangular")
    assert record["model"] == "mosaic"


def test_one_year_first_then_everything(home, photos):
    run(home, photos, "run", "--no-stitch", "--year", "2023")
    candidates = json.loads((home / "output" / "candidates.json").read_text(encoding="utf-8"))["candidates"]
    assert candidates == []
    run(home, photos, "run", "--no-stitch", "--since", "2024-03-09", "--until", "2024-03-09")
    candidates = json.loads((home / "output" / "candidates.json").read_text(encoding="utf-8"))["candidates"]
    assert [c["set_id"] for c in candidates] == [SET]


def test_bad_date_is_explained(home, photos, capsys):
    with pytest.raises(SystemExit):
        run(home, photos, "run", "--no-stitch", "--since", "9 March")
    assert "YYYY-MM-DD" in capsys.readouterr().err


@pytest.mark.skipif(tools.find("hugin") is None, reason="Hugin is not installed")
def test_kept_panorama_is_full_size_with_the_first_frames_exif(home, photos, monkeypatch):
    from PIL import Image

    from fuseshot import limits
    from fuseshot.export import export
    from fuseshot.review import Review

    run(home, photos, "run")
    page = Review(home / "work", candidates_file=home / "output" / "candidates.json")
    monkeypatch.setattr(page, "export_later", lambda keys: None)
    page.decide({"set": SET, "decision": "kept"})
    out = home / "kept"
    from fuseshot import progress

    monkeypatch.setattr(progress, "STATE", home / "output" / "progress.json")
    lines = []
    [written] = export(page, out, log=lines.append)
    assert any("at full size, which can take several minutes" in line for line in lines)
    assert any("photos remapped (2 of 4)" in line for line in lines)
    shown = json.loads((home / "output" / "progress.json").read_text(encoding="utf-8"))
    assert (shown["stage"], shown["done"], shown["finished"]) == (f"full-size render of {SET}", 4, True)
    assert written.name.endswith("panorama.jpg")
    full_crop = next((home / "work" / SET / "full").glob("*_crop.jpg"))
    with Image.open(written) as img, Image.open(full_crop) as crop:
        assert img.size == crop.size  # cut to fit unless the whole canvas was picked
    record = json.loads((home / "work" / SET / "batch.json").read_text(encoding="utf-8"))
    preview = Image.open(home / "work" / SET / record["preview_file"]).size
    with Image.open(written) as img:
        size, exif = img.size, img.getexif()
    assert size != preview and preview[0] == 3000  # the review copy is 3000 px; the kept one is not
    assert exif.get_ifd(0x8769)[0x9003] == "2024:03:09 14:00:00"
    assert (exif[0x010F], exif[0x0110]) == ("Example", "Camera")

    # a cap makes it smaller, and the same picture
    monkeypatch.setattr(limits, "MAX_MEGAPIXELS", 1)
    full = home / "work" / SET / "full"
    for f in full.glob("*.jpg"):
        f.unlink()
    written.unlink()
    [written] = export(page, out)
    with Image.open(written) as img:
        assert img.width * img.height <= 1_010_000  # nona rounds the canvas to whole pixels
        assert abs(img.width / img.height - size[0] / size[1]) < 0.02
