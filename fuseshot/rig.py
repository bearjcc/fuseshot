"""Stereo pairs for splats: both views of each shot, posed by COLMAP as a two-camera rig.

A stereo photo is two photos taken at the same moment a fixed distance apart, so
a set of them gives COLMAP twice the views and a known baseline between each
pair. The views go into images/left/ and images/right/ under the same file name,
COLMAP gets one camera per folder, and rig_configurator ties each pair into one
frame with the right lens BASELINE_M to the side of the left. The mapper keeps
that offset fixed, so the model comes out roughly in metres.

Side-by-side stereo video (the XREAL Beam Pro's 32:9 clips) becomes a set the same
way: frames are taken about FPS times a second, at most MAX_PAIRS of them.
"""

import json
from pathlib import Path

import cv2

from . import log, stereo
from .files import write_atomic

LOG = log.get(__name__)

BASELINE_M = 0.05
FPS = 4
MAX_PAIRS = 60
FOLDERS = ("left", "right")
VIDEO_RATIOS = (32 / 9, 8 / 3)


def views(path, make=None):
    """(left, right) for a stereo photo, else None."""
    from PIL import Image

    path = Path(path)
    with Image.open(path) as im:
        w, h = im.size
        frames = getattr(im, "n_frames", 1)
        make = make or im.getexif().get(0x010F)
    if frames < 2 and not stereo.kind(path.name, make, w, h):
        return None
    try:
        return stereo.read_pair(path)
    except ValueError:
        return None


def write_pair(left, right, images, name, long_side, exif=None):
    """images/left/name and images/right/name, at most long_side on the long edge."""
    out = []
    for folder, view in zip(FOLDERS, (left, right)):
        scale = min(1.0, long_side / max(view.shape[:2]))
        if scale < 1:
            view = cv2.resize(view, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        target = Path(images) / folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        ok, data = cv2.imencode(".jpg", view, [cv2.IMWRITE_JPEG_QUALITY, 93])
        write_atomic(target, data.tobytes() if exif is None else with_exif(data.tobytes(), exif))
        out.append((f"{folder}/{name}", (view.shape[1], view.shape[0])))
    return out


def with_exif(jpeg, exif):
    import io

    from PIL import Image

    buf = io.BytesIO()
    with Image.open(io.BytesIO(jpeg)) as im:
        im.save(buf, "JPEG", quality=93, exif=exif)
    return buf.getvalue()


def is_sbs_video(width, height, name=""):
    ratio = width / max(1, height)
    return any(abs(ratio - r) < 0.03 for r in VIDEO_RATIOS) or Path(name).name.upper().startswith(
        stereo.STEREO_PREFIXES)


def video_pairs(video, images, long_side, fps=FPS, cap=MAX_PAIRS):
    """Split frames of a side-by-side video into images/left and images/right; the frames written."""
    clip = cv2.VideoCapture(str(video))
    if not clip.isOpened():
        raise ValueError(f"cannot read {Path(video).name}")
    try:
        w, h = clip.get(cv2.CAP_PROP_FRAME_WIDTH), clip.get(cv2.CAP_PROP_FRAME_HEIGHT)
        if not is_sbs_video(w, h, str(video)):
            raise ValueError(f"{Path(video).name} is not side by side ({w:.0f}x{h:.0f})")
        rate = clip.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(clip.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        every = max(1, round(rate / fps))
        if total and total // every > cap:
            every = -(-total // cap)
        frames, n = [], 0
        while len(frames) < cap:
            ok, image = clip.read()
            if not ok:
                break
            if n % every == 0:
                left, right = stereo.split(image)
                name = f"{len(frames):03d}.jpg"
                (_, size), _ = write_pair(left, right, images, name, long_side)
                frames.append({"index": len(frames), "file": f"left/{name}", "pair": f"right/{name}",
                               "size": size, "at_s": round(n / rate, 2)})
            n += 1
        return frames
    finally:
        clip.release()


def config_file(path, baseline=BASELINE_M):
    """rig_config.json: the left lens is the reference, the right one baseline metres to its right."""
    rig = [{"cameras": [
        {"image_prefix": "left/", "ref_sensor": True},
        {"image_prefix": "right/", "cam_from_rig_rotation": [1.0, 0.0, 0.0, 0.0],
         "cam_from_rig_translation": [-baseline, 0.0, 0.0]},
    ]}]
    write_atomic(Path(path), json.dumps(rig, indent=2).encode())
    return path
