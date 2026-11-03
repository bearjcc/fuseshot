"""The clean rectangle is worked out once and cut from a render of any size."""

import pytest

from fuseshot.stitch import crop_pixels, crop_rect

PREVIEW = (0, 3000, 0, 812)  # uncropped panorama on the preview canvas: left, right, top, bottom
CLEAN = (37, 2961, 55, 760)  # what Hugin found for it


def shape(box):
    left, right, top, bottom = box
    return (right - left) / (bottom - top)


def test_the_stored_rectangle_is_shares_of_the_uncropped_panorama():
    rect = crop_rect(PREVIEW, CLEAN)
    assert all(0 <= x <= 1 for x in rect)
    assert crop_pixels(rect, PREVIEW) == CLEAN


@pytest.mark.parametrize("scale", [0.37, 1.9, 4.37, 7.2])
def test_capped_and_full_size_renders_are_cut_to_the_same_shape(scale):
    rect = crop_rect(PREVIEW, CLEAN)
    # a larger canvas, with the panorama a little off its corner as Hugin leaves it
    outer = (3, 3 + round(3000 * scale), 1, 1 + round(812 * scale))
    box = crop_pixels(rect, outer)
    # within a pixel of rounding on each side
    height = box[3] - box[2]
    assert abs(shape(box) - shape(CLEAN)) <= shape(CLEAN) * 2 / min(height, 705)
    assert outer[0] <= box[0] < box[1] <= outer[1] and outer[2] <= box[2] < box[3] <= outer[3]
