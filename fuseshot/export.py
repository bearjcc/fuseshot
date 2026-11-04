"""Copy kept results into the output folder.

Results made from folder photos are copied as they are kept. Results made
from Immich photos are copied only when asked (export --immich); upload.py can
send them to Immich instead. Nothing is ever written into the photo folders.

Panoramas and group photos are written at the photos' own resolution (as far as
the memory budget and --max-megapixels allow), not at the size the review page
shows, with the first frame's date, camera and GPS position in their EXIF.
A panorama is cut to the largest clean rectangle unless the uncropped view was
picked for it, or crop = false is in the settings file."""

import json
import os
from pathlib import Path

from . import config, log, metadata, stitch

LOG = log.get(__name__)

KIND_WORDS = {"stitch": "panorama", "group": "group", "action": "loop", "splat": "splat"}


def result_file(review, item, decisions, log=LOG.info, full=True):
    """The file a kept item stands for: the view chosen when it was kept,
    the merged group photo, the loop or the splat. With full, panoramas and
    group photos are made at full size first (once; later calls reuse it)."""
    kind, key = item["kind"], item["id"]
    if kind == "group":
        path = review.groups_dir / key / "merged.jpg"
        if full and path.exists():
            from . import group

            try:
                path = group.full_size(path.parent, log=log)
            except Exception as err:
                log(f"{item['key']}: full size merge failed ({err}); using the review size")
    elif kind == "action":
        path = review.loops_dir / key / "animation.webp"
    elif kind == "splat":
        path = review.splats_dir / key / "scene.splat"
    else:
        last = review.last_decision(decisions, key, "stitch")
        crop = last.get("crop") or (not last.get("uncropped") and config.crop_default())
        names = []
        if last.get("projection"):
            name = stitch.preview_name(last["projection"], last.get("orientation") or "level")
            names += [name + ("_fill" if last.get("fill") else "_crop" if crop else ""), name]
        if item.get("preview"):
            names += [item["preview"] + "_crop"] * (crop and not last.get("fill")) + [item["preview"]]
        set_dir = review.work_dir / key
        found = [set_dir / "previews" / f"{n}.jpg" for n in names]
        path = next((f for f in found if f.exists()), None)
        if full and path is not None and not path.stem.endswith("_fill"):
            # an AI fill is made at the review size, so it stays at that size
            name = path.stem.removesuffix("_crop")
            try:
                path = stitch.render_full(set_dir, name, log=log) / path.name
            except Exception as err:
                log(f"{item['key']}: full size render failed ({err}); using the review size")
    return path if path and path.exists() else None


def frames(review, item):
    """The manifest of the photos a result was made from, first frame first."""
    kind, key = item["kind"], item["id"]
    folder = review.groups_dir / key if kind == "group" else review.work_dir / key
    try:
        return json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def finished(review, item, path):
    """The bytes to write out for a result: JPEGs get the first frame's EXIF."""
    return metadata.stamped(path, frames(review, item) if item["kind"] in ("stitch", "group") else [])


def export(review, output, keys=None, log=LOG.info, dry_run=False, libraries=("folder",)):
    """Copy kept results into output. Returns the files written (or that would be)."""
    output = Path(output)
    decisions = review.decisions()
    written = []
    for item in review.items()["items"]:
        if item.get("library") not in libraries or item.get("decision") != "kept" or item["kind"] not in KIND_WORDS:
            continue
        if keys is not None and item["key"] not in keys:
            continue
        source = result_file(review, item, decisions, log=log, full=not dry_run)
        if source is None:
            log(f"{item['key']}: kept, but its result file is missing")
            continue
        day = (item.get("taken") or "")[:10]
        word = "wigglegram" if item.get("style") == "wiggle" else KIND_WORDS[item["kind"]]
        dest = output / f"{day} {item['id']} {word}{source.suffix}".strip()
        if dry_run:
            written.append(dest)
            log(f"{item['key']}: would copy to {dest}")
            continue
        data = finished(review, item, source)
        if dest.exists() and dest.stat().st_size == len(data) and dest.stat().st_mtime >= source.stat().st_mtime:
            continue
        output.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name("." + dest.name + ".part")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        written.append(dest)
        log(f"{item['key']}: {dest}")
    return written
