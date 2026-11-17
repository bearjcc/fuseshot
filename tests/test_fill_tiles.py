"""fill.tiles tiling."""

from fuseshot import fill


def test_tiles_cover_image():
    boxes = fill.tiles(2000, 3000)
    assert boxes
    ys = sorted({y0 for y0, _, _, _ in boxes})
    assert ys[0] == 0
    assert max(y1 for _, y1, _, _ in boxes) >= 2000


def test_tiles_small_image_single_pass():
    assert fill.tiles(100, 100) == [(0, 100, 0, 100)]
