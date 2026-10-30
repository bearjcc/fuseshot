"""Fuseshot: panoramas, splats, action loops and group shots from an Immich library or a folder."""

from PIL import Image

__version__ = "0.1.0"

# Pillow refuses pictures over about 179 megapixels as a guard against hostile
# files. These are the user's own photos, and phone and stitched panoramas pass
# that size often, so allow up to a gigapixel without a warning (two before it refuses).
Image.MAX_IMAGE_PIXELS = 1_000_000_000
