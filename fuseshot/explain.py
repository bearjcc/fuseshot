"""Why a run found nothing, and what to try next."""

import json
import os
from pathlib import Path

from . import log

LOG = log.get(__name__)
LOOK_AT = 5000  # folder entries looked at when guessing whether photos sit in subfolders


def photos_below(folder, photo_types):
    """How many photos the first few subfolders hold (a quick look, not a count)."""
    seen = found = 0
    for root, dirs, files in os.walk(folder):
        if root == str(folder):
            continue
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            seen += 1
            if Path(name).suffix.lower() in photo_types:
                found += 1
        if seen > LOOK_AT or found > 50:
            break
    return found


def no_photos(sources):
    """Lines for a sync that found no photos at all."""
    from .sources import PHOTO_TYPES

    lines = []
    for source in sources:
        if source.kind != "folder":
            continue
        if not Path(source.path).is_dir():
            lines.append(f"{source.path} does not exist or is not a folder.")
        elif not source.recursive and photos_below(source.path, PHOTO_TYPES):
            lines.append(f"{source.path} has no photos itself, but its subfolders do. Only the folder itself "
                         f"is read unless asked: add --recursive after folder:{source.path}, "
                         "or run `fuseshot init` to set it up once.")
        else:
            lines.append(f"No photos in {source.path}" + ("" if source.recursive else " (subfolders not read)")
                         + ". Fuseshot reads JPEG, HEIC, PNG, TIFF, WebP and camera raw files.")
    return lines


def nothing_to_stitch(candidates_file, plan_file, min_shots):
    """Lines for a run that read photos but found no set worth stitching."""
    data = json.loads(Path(candidates_file).read_text(encoding="utf-8"))
    stats, sets = data.get("stats", {}), data.get("candidates", [])
    images = stats.get("images", 0)
    lines = []
    if not images:
        return ["None of the photos fall in the dates asked for (--year, --since, --until)."]
    usable = images - stats.get("skipped_no_camera", 0) - stats.get("skipped_screenshot", 0) \
        - stats.get("skipped_bad_time", 0)
    if stats.get("skipped_no_camera", 0) > images / 2:
        lines.append(f"{stats['skipped_no_camera']:,} of {images:,} photos have no camera details or time "
                     "in them (EXIF). Scans, screenshots and pictures saved from chat apps usually don't, "
                     "and Fuseshot needs the time each photo was taken to find a sequence.")
    if usable < min_shots:
        lines.append(f"Only {usable:,} usable photos; a panorama needs at least {min_shots} taken one "
                     "after another.")
    elif not sets:
        lines.append(f"{usable:,} photos were looked at, but no {min_shots} or more were taken by one camera "
                     "within a few seconds of each other, which is how a panorama or burst is shot.")
    else:
        plan = json.loads(Path(plan_file).read_text(encoding="utf-8")) if Path(plan_file).exists() else {}
        if not plan.get("jobs"):
            lines.append(f"{len(sets):,} sequences were found, but none overlap the way a panorama does. "
                         "They may be bursts, brackets or group shots; those show on the review page. "
                         "To try weaker matches: fuseshot run --min-confidence 0.3")
    if stats.get("skipped_copy"):
        lines.append(f"({stats['skipped_copy']:,} copies of the same photo were counted once.)")
    return lines


def report(lines):
    if not lines:
        return
    LOG.warning("Nothing to stitch was found. Why:")
    for line in lines:
        LOG.warning("  " + line)
