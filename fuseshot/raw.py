"""Camera raw files (Sony ARW, Pixel and other DNGs, and the rest LibRaw reads).

Detection and thumbnails use the JPEG preview the camera embeds in every raw
file, which is quick. A raw is only fully decoded when it is a frame in a
stitch or a group merge with no JPEG beside it, one file at a time, and the
decoded pixels are freed before the next one.
"""

import io
import warnings
from pathlib import Path

RAW_TYPES = {
    ".arw": "image/arw", ".srf": "image/arw", ".sr2": "image/arw", ".dng": "image/dng",
    ".cr2": "image/cr2", ".cr3": "image/cr3", ".nef": "image/nef", ".nrw": "image/nef",
    ".orf": "image/orf", ".rw2": "image/rw2", ".raf": "image/raf", ".pef": "image/pef",
}
HEAD = 1 << 18
# EXIF tags worth carrying into a decoded frame: Hugin reads the focal length from them.
KEEP = (0x010F, 0x0110, 0x0132)
KEEP_SUB = (0x9003, 0x9004, 0x9010, 0x9011, 0x9291, 0x829A, 0x829D, 0x8827, 0x920A, 0xA405, 0xA434, 0xA433)
TURN = {3: 180, 6: 270, 8: 90}


def is_raw(path):
    return Path(path).suffix.lower() in RAW_TYPES


def exif(path):
    """The raw's EXIF, read from the TIFF structure at the start of the file."""
    from PIL import Image

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            with Image.open(path) as img:
                data = img.getexif()
                return data, dict(data.get_ifd(0x8769)), dict(data.get_ifd(0x8825))
        except Exception:
            with open(path, "rb") as f:
                data = Image.Exif()
                data.load(f.read(HEAD))
        return data, dict(data.get_ifd(0x8769)), dict(data.get_ifd(0x8825))


def size(path):
    """Width and height of the decoded image, after the camera's rotation."""
    import rawpy

    with rawpy.imread(str(path)) as raw:
        width, height, flip = raw.sizes.width, raw.sizes.height, raw.sizes.flip
    return (height, width) if flip in (5, 6) else (width, height)


def preview(path):
    """The embedded preview as an upright Pillow image."""
    import rawpy
    from PIL import Image

    with rawpy.imread(str(path)) as raw:
        thumb = raw.extract_thumb()
        if thumb.format == rawpy.ThumbFormat.JPEG:
            img = Image.open(io.BytesIO(bytes(thumb.data)))
            img.load()
        else:
            img = Image.fromarray(thumb.data)
    orientation = exif(path)[0].get(0x0112, 1)
    if TURN.get(orientation):
        img = img.rotate(TURN[orientation], expand=True)
    return img.convert("RGB")


def to_jpeg(path, dest, quality=95):
    """Decode the raw at full size with the camera's white balance and write a JPEG
    that keeps the EXIF a stitcher needs."""
    import rawpy
    from PIL import Image

    with rawpy.imread(str(path)) as raw:
        rgb = raw.postprocess(use_camera_wb=True, output_bps=8)
    img = Image.fromarray(rgb)
    del rgb
    source, sub, _ = exif(path)
    out = Image.Exif()
    for tag in KEEP:
        if source.get(tag) is not None:
            out[tag] = source[tag]
    out[0x0112] = 1
    ifd = out.get_ifd(0x8769)
    for tag in KEEP_SUB:
        if sub.get(tag) is not None:
            ifd[tag] = sub[tag]
    tmp = Path(dest).with_suffix(".part")
    img.save(tmp, "JPEG", quality=quality, exif=out.tobytes(), subsampling=0)
    img.close()
    tmp.replace(dest)
