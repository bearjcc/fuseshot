"""Property tests for parsers and path handling (Hypothesis, MPL-2.0)."""

import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fuseshot import config, lens, takeout
from fuseshot.stitch import crop_pixels, crop_rect

PREVIEW = (0, 3000, 0, 812)
CLEAN = (37, 2961, 55, 760)


@given(st.floats(min_value=0.5, max_value=8.0))
@settings(max_examples=40)
def test_crop_shares_stay_in_unit_interval(scale):
    outer = (3, 3 + round(3000 * scale), 1, 1 + round(812 * scale))
    rect = crop_rect(PREVIEW, CLEAN)
    box = crop_pixels(rect, outer)
    assert outer[0] <= box[0] < box[1] <= outer[1]


@given(st.sampled_from(["PXL_20211013_220651983.jpg.json", "DSC_0100.JPG.json", "foo.json"]))
def test_takeout_pair_never_crashes(sidecar):
    photo = sidecar.removesuffix(".json")
    rules = takeout.pair([sidecar, photo])
    assert isinstance(rules, dict)


@given(st.binary(min_size=4, max_size=128))
def test_lens_header_parser_tolerates_noise(blob):
    try:
        lens.focal35_from_head(blob)
    except (ValueError, TypeError, IndexError):
        pass


def test_config_unknown_output_reported(home):
    path = home / "fuseshot.toml"
    path.write_text("not_a_top_level_key = true\n", encoding="utf-8")
    config.reload()
    assert "not_a_top_level_key" in config.unknown_keys()


@pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")
@given(st.integers(min_value=1, max_value=40))
def test_windows_long_path_hint(extra):
    from fuseshot.__main__ import explain

    err = OSError(2, "too long", "x" * (200 + extra))
    err.winerror = 206
    assert "too long" in explain(err).lower()