"""Stitch helpers and layout logic without running Hugin."""



from fuseshot import stitch
from conftest import minimal_pto, write_frames


def test_write_project_and_control_points(tmp_path):
    photos = write_frames(tmp_path / "p", count=3)
    manifest = [{"image": str(p), "f35_hint": 26} for p in photos]
    pto = tmp_path / "start.pto"
    lenses = stitch.write_project(manifest, pto)
    assert len(lenses) == 1
    text = pto.read_text(encoding="utf-8")
    assert text.count("\ni ") == 3
    pairs = stitch.control_points(pto)
    assert pairs == {}
    for item in manifest:
        assert "hfov" in item and item["f35"] is not None


def test_components_and_largest_group(tmp_path):
    pto = minimal_pto(tmp_path / "g.pto", names=["a.jpg", "b.jpg", "c.jpg", "d.jpg"], pairs=10)
    pairs = stitch.control_points(pto)
    groups = stitch.components(4, pairs, min_points=8)
    assert len(groups[0]) >= 2
    assert stitch.largest_group(4, pto, min_points=8) == len(groups[0])


def test_describe_layout_rotation(tmp_path):
    pto = minimal_pto(tmp_path / "rot.pto", pairs=10)
    manifest = [{"file": f"f{i}.jpg", "image": f"f{i}.jpg"} for i in range(3)]
    params = stitch.image_params(pto)
    layout = stitch.describe_layout(manifest, stitch.control_points(pto), params, min_points=4)
    assert layout["images"] == 3 and layout["yaw_span"] >= 0


def test_choose_model_prefers_coverage():
    scores = {
        "rotation": {"largest_group": 2, "error_px": 3.0},
        "mosaic": {"largest_group": 4, "error_px": 4.0},
    }
    model, reason = stitch.choose_model(scores, prefer="rotation")
    assert model == "mosaic" and "connects" in reason
    scores["rotation"]["largest_group"] = 5
    model, _ = stitch.choose_model(scores, prefer="rotation")
    assert model == "rotation"


def test_pano_line_and_preview_name(tmp_path):
    pto = minimal_pto(tmp_path / "p.pto")
    line = stitch.pano_line(pto)
    assert line["w"] == 3000 and abs(line["v"] - 90.0) < 0.01
    assert stitch.preview_name("cylindrical", "level") == "cylindrical"
    assert stitch.preview_name("cylindrical", "vertical") == "cylindrical-vertical"


def test_auto_projection_picks_rectilinear():
    assert stitch.auto_projection(30, 20) == "rectilinear"
    assert stitch.auto_projection(120, 80) == "cylindrical"


def test_mean_error_parses_checkpto():
    assert stitch.mean_error("Mean error : 2.75 pixels\n") == 2.75
    assert stitch.mean_error("no numbers") is None


def test_absolute_paths_makes_images_absolute(tmp_path):
    rel = tmp_path / "rel.pto"
    rel.write_text('p f2 w10 h10 v10\ni w10 h10 f0 v50 n"photos/a.jpg"\n', encoding="utf-8")
    stitch.absolute_paths(rel, base=tmp_path)
    assert str((tmp_path / "photos/a.jpg").resolve()) in rel.read_text()


def test_read_exif_fov_from_synthetic_jpeg(tmp_path):
    photo = write_frames(tmp_path / "p", count=1)[0]
    w, h, f35, hfov = stitch.read_exif_fov(photo, f35_hint=26)
    assert w > 0 and abs(f35 - 26) <= 12 and hfov > 0
