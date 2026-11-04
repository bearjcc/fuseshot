"""Action composites: every moment of a fixed-camera burst in one still.

The frames of a loop are already lined up on the background, so the median of
them is the scene with the moving subject taken out. Each frame's difference
from that median is the subject at that moment. Frames are picked from first
to last whose subject does not overlap one already placed, and those subjects
are pasted onto the background with a soft edge. Bursts where most of the frame
moves (water, crowds, fireworks) or where the subject never clears its own
earlier place give no composite.
"""

import cv2
import numpy as np

from . import log
from .files import write_atomic

LOG = log.get(__name__)

FILE = "composite.jpg"
MIN_PLACED = 3
MAX_PLACED = 12
MAX_MOVING = 0.30
MIN_AREA = 0.0004
THRESHOLD = 28


def moving_masks(frames, background):
    """For each frame, where it differs from the background, cleaned of speckle."""
    h, w = background.shape[:2]
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (max(3, w // 160) | 1,) * 2)
    out = []
    for frame in frames:
        diff = cv2.absdiff(frame, background).max(axis=2)
        diff = cv2.GaussianBlur(diff, (5, 5), 0)
        mask = (diff > THRESHOLD).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        keep = [n for n in range(1, count) if stats[n, cv2.CC_STAT_AREA] >= MIN_AREA * h * w]
        out.append(np.isin(labels, keep) if keep else np.zeros((h, w), bool))
    return out


def pick(masks):
    """Indexes of frames whose subject does not overlap one picked before, first to last."""
    taken = np.zeros_like(masks[0])
    picked = []
    for n, mask in enumerate(masks):
        if not mask.any() or (mask & taken).sum() > 0.05 * mask.sum():
            continue
        picked.append(n)
        taken |= cv2.dilate(mask.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    if len(picked) > MAX_PLACED:
        picked = [picked[round(i)] for i in np.linspace(0, len(picked) - 1, MAX_PLACED)]
    return picked


def compose(frames):
    """(the composite, details) or (None, why not) for frames lined up on their background."""
    if len(frames) < MIN_PLACED:
        return None, "too few frames"
    background = np.median(np.stack(frames), axis=0).astype(np.uint8)
    masks = moving_masks(frames, background)
    moving = float(np.mean([m.mean() for m in masks]))
    if moving > MAX_MOVING:
        return None, f"{moving:.0%} of the frame moves"
    picked = pick(masks)
    if len(picked) < MIN_PLACED:
        return None, "the subject does not move clear of itself"
    out = background.astype(np.float32)
    for n in picked:
        soft = cv2.GaussianBlur(masks[n].astype(np.float32), (7, 7), 0)[..., None]
        out = out * (1 - soft) + frames[n].astype(np.float32) * soft
    return out.clip(0, 255).astype(np.uint8), {"placed": len(picked), "frames": picked,
                                                "moving": round(moving, 3)}


def write(frames, out_dir, log=LOG.info):
    """composite.jpg in out_dir when the frames make one; returns its details or None."""
    image, found = compose(frames)
    if image is None:
        log(f"{out_dir.name}: no composite, {found}")
        return None
    write_atomic(out_dir / FILE, cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes())
    return {"file": FILE, **found}
