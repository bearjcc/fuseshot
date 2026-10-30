"""EXIF put into finished results, full size limits, and the group merge scaled up."""

import io

import numpy as np
from PIL import Image

from fuseshot import group, limits, metadata


def jpeg(exif=None, size=(320, 200)):
    buf = io.BytesIO()
    pixels = np.random.default_rng(1).integers(0, 255, (size[1], size[0], 3), np.uint8)
    Image.fromarray(pixels).save(buf, "JPEG", quality=90, exif=exif.tobytes() if exif else b"")
    return buf.getvalue()


def test_exif_is_swapped_in_without_touching_the_picture():
    old = Image.Exif()
    old[0x0110] = "Old"
    data = jpeg(old)
    tags = {"make": "SONY", "model": "ILCE-7RM3A", "sub": {0x9003: "2025:02:15 17:05:59", 0x9011: "+13:00"},
            "gps": {}}
    out = metadata.with_exif(data, metadata.exif_block(tags, (320, 200)))
    a, b = Image.open(io.BytesIO(data)), Image.open(io.BytesIO(out))
    assert np.array_equal(np.asarray(a), np.asarray(b))
    exif = b.getexif()
    assert (exif[0x010F], exif[0x0110]) == ("SONY", "ILCE-7RM3A")
    assert exif.get_ifd(0x8769)[0x9003] == "2025:02:15 17:05:59"
    assert exif.get_ifd(0x8769)[0x9011] == "+13:00"
    assert out.count(b"Exif\x00\x00") == 1


def test_first_frame_tags_come_from_the_file(tmp_path):
    exif = Image.Exif()
    exif[0x010F], exif[0x0110] = "Google", "Pixel 8 Pro"
    exif.get_ifd(0x8769)[0x9003] = "2024:12:25 09:30:00"
    gps = exif.get_ifd(0x8825)
    gps.update({1: "S", 2: (36.0, 50.0, 30.0), 3: "E", 4: (174.0, 45.0, 50.0)})
    first = tmp_path / "PXL_1.jpg"
    first.write_bytes(jpeg(exif))
    tags = metadata.frame_tags({"original": str(first), "taken": "2000-01-01T00:00:00.000Z"})
    result = tmp_path / "result.jpg"
    result.write_bytes(jpeg())
    out = Image.open(io.BytesIO(metadata.stamped(result, [{"original": str(first)}]))).getexif()
    assert tags["sub"][0x9003] == "2024:12:25 09:30:00"
    assert out[0x0110] == "Pixel 8 Pro"
    lat = out.get_ifd(0x8825)
    assert lat[1] == "S" and float(lat[2][0]) == 36.0 and float(lat[4][1]) == 45.0


def test_library_details_fill_in_when_the_file_has_none(tmp_path):
    bare = tmp_path / "copy.jpg"
    bare.write_bytes(jpeg())
    entry = {"original": str(bare), "taken": "2023-07-01T08:15:00.000Z", "make": "SONY", "model": "ILCE-7M4",
             "latitude": -41.2865, "longitude": 174.7762}
    out = Image.open(io.BytesIO(metadata.stamped(bare, [entry]))).getexif()
    assert out.get_ifd(0x8769)[0x9003] == "2023:07:01 08:15:00"
    assert out[0x010F] == "SONY"
    gps = out.get_ifd(0x8825)
    degrees = float(gps[2][0]) + float(gps[2][1]) / 60 + float(gps[2][2]) / 3600
    assert gps[1] == "S" and abs(degrees - 41.2865) < 1e-3


def test_non_jpegs_are_left_as_they_are(tmp_path):
    loop = tmp_path / "animation.webp"
    loop.write_bytes(b"RIFF....WEBP")
    assert metadata.stamped(loop, [{"make": "X"}]) == b"RIFF....WEBP"


def test_full_size_fits_memory_cap_and_jpeg(monkeypatch):
    monkeypatch.setattr(limits, "MEMORY_MB", 64 * 1024)
    monkeypatch.setattr(limits, "MAX_MEGAPIXELS", None)
    monkeypatch.delenv("FUSESHOT_MAX_MEGAPIXELS", raising=False)
    assert limits.full_scale(12000, 4000) == 1.0
    assert limits.full_scale(200000, 4000) == limits.JPEG_MAX_SIDE / 200000
    monkeypatch.setattr(limits, "MEMORY_MB", 1024)
    assert limits.full_scale(12000, 4000) ** 2 * 12000 * 4000 * limits.FULL_BYTES_PER_PX <= 1024 * 1048576 * 1.001
    monkeypatch.setattr(limits, "MEMORY_MB", 64 * 1024)
    monkeypatch.setattr(limits, "MAX_MEGAPIXELS", 12)
    assert abs(limits.full_scale(12000, 4000) ** 2 * 48 - 12) < 0.01


def test_group_analysis_scales_with_the_frames():
    H = np.array([[1.01, 0.02, 12.0], [-0.01, 0.99, -7.0], [1e-6, 2e-6, 1.0]])
    analysis = {"size": [3000, 2000], "homographies": [list(H.ravel()), None],
                "faces": [{"frame": 0, "box": [100, 200, 50, 60], "points": [110, 220] * 5, "centre": [125, 230]}]}
    big = group.scaled(analysis, 2.5)
    assert big["size"] == [7500, 5000]
    assert big["faces"][0]["box"] == [250, 500, 125, 150]
    point = np.array([400.0, 300.0, 1.0])
    small = H @ point
    large = np.reshape(big["homographies"][0], (3, 3)) @ (point * [2.5, 2.5, 1])
    assert np.allclose(small[:2] / small[2] * 2.5, large[:2] / large[2])
    assert big["homographies"][1] is None
