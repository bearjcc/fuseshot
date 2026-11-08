"""Stitch previews for many verified sets, one set at a time, in resumable chunks.

The plan lists one job per stitchable sub-set, most likely to work out first, so
sets discarded on the review page while they wait are mostly the doubtful ones.
Discards are read again just before each set starts. Each job records its
result in work/<key>/batch.json, so a chunk that is stopped or paused picks up
at the first job without a result. Creating work/batch.stop ends a chunk at the
next set boundary.

Straightening goes over the same chunks for sets stitched before the optimiser's
levelling was checked: any set it left tipped over or upside down is turned
upright and its preview rendered again. It only needs the kept project and
images, so nothing is fetched.
"""

from collections import Counter
import errno
import json
import os
import shutil
import sqlite3
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import lens, limits, log, retry, tools
from .fetch import fetch_set
from .files import write_atomic
from .review import discarded
from .stitch import (POOR_ERROR, OutOfSpace, StitchError, align, auto_projection, coverage,
                     default_orientation, level, render, repair, studio_background_share, upright)

LOG = log.get(__name__)

STITCH_TYPES = ("bracket", "single-row rotation", "multi-zoom layered", "multi-row grid")
INTERMEDIATES = ("points.pto", "clean.pto", "start.pto", "refine.pto", "refine_clean.pto", "repair_tmp.pto",
                 "resolve_tmp.pto", "level_tmp.pto", "level_best.pto")
DONE = ("stitched", "poor fit", "failed")


def make_plan(verified_file, candidates_file, min_confidence, exclude):
    verified = json.loads(Path(verified_file).read_text(encoding="utf-8"))
    data = json.loads(Path(candidates_file).read_text(encoding="utf-8"))
    candidates = {c["set_id"]: c for c in data["candidates"]}
    jobs = []
    for set_id, result in verified.items():
        if set_id in exclude:
            continue
        subs = result.get("sub_sets") or []
        ids = candidates[set_id]["asset_ids"]
        for number, sub in enumerate(subs, 1):
            if sub["type"] not in STITCH_TYPES or sub["confidence"] < min_confidence:
                continue
            jobs.append({
                "key": set_id if len(subs) == 1 else f"{set_id}-{number}",
                "set_id": set_id,
                "type": sub["type"],
                "frames": sub["frames"],
                "asset_ids": [ids[f - 1] for f in sub["frames"]],
                "stitch_plan": sub.get("stitch_plan") or [],
                "confidence": sub["confidence"],
                "quality": sub.get("quality") or 0,
                "coverage": [sub.get("coverage_h"), sub.get("coverage_v")],
                "taken": candidates[set_id].get("start_local"),
                "camera": candidates[set_id].get("camera"),
            })
    jobs.sort(key=lambda j: (-j["confidence"], -j["quality"], j["key"]))
    return jobs


def left_out(work, job):
    """Whether the review page discarded this job's result, or its whole set while it was found."""
    return job["key"] in discarded(work) or job.get("set_id", job["key"]) in discarded(work, "found")


def save_plan(jobs, plan_file, chunk_size):
    chunks = [[j["key"] for j in jobs[i:i + chunk_size]] for i in range(0, len(jobs), chunk_size)]
    Path(plan_file).parent.mkdir(parents=True, exist_ok=True)
    Path(plan_file).write_text(json.dumps({"chunk_size": chunk_size, "jobs": jobs, "chunks": chunks}, indent=1),
                               encoding="utf-8")
    counts = {}
    for job in jobs:
        counts[job["type"]] = counts.get(job["type"], 0) + 1
    LOG.info(f"{len(jobs)} jobs in {len(chunks)} chunks of up to {chunk_size}: {counts}")


def focal_hints(db_file, cache_dir, asset_ids):
    """35 mm equivalent focal length per asset: the per-frame tag, else focal length times crop factor."""
    cache_dir = Path(cache_dir)
    f35 = json.loads((cache_dir / "focal35.json").read_text(encoding="utf-8")) \
        if (cache_dir / "focal35.json").exists() else {}
    factors = json.loads((cache_dir / "crop_factors.json").read_text(encoding="utf-8")) \
        if (cache_dir / "crop_factors.json").exists() else {}
    db = sqlite3.connect(db_file)
    hints = {}
    for asset_id in asset_ids:
        if f35.get(asset_id):
            hints[asset_id] = float(f35[asset_id])
            continue
        row = db.execute("SELECT make, model, focal_length FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if row and row[2]:
            crop = lens.crop_factor(factors, row[0], row[1], row[2])
            if crop:
                hints[asset_id] = round(float(row[2]) * crop, 1)
    db.close()
    return hints


def folder_mb(path):
    return round(sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file()) / 2**20, 1)


def clean_up(set_dir):
    """Keep only what re-rendering needs: the optimised projects and the images they point at."""
    set_dir = Path(set_dir)
    used = set()
    for pto in [set_dir / "optimised.pto", set_dir / "full.pto", *set_dir.glob("part_*.pto")]:
        if not pto.exists():
            continue
        text = pto.read_text(encoding="utf-8", errors="replace")
        used |= {(set_dir / line.split(' n"')[-1].rstrip('"')).resolve() for line in text.splitlines()
                 if line.startswith("i ") and ' n"' in line}
    for folder in ("originals", "images"):
        for f in (set_dir / folder).glob("*"):
            if f.resolve() not in used:
                f.unlink()
    for name in INTERMEDIATES:
        (set_dir / name).unlink(missing_ok=True)
    for f in set_dir.glob("refine_*.pto"):
        f.unlink()
    shutil.rmtree(set_dir / "render_tmp", ignore_errors=True)


def render_preview(set_dir, record, fuse):
    """Render the default preview for an aligned set and note it in the record."""
    hfov, vfov = coverage(set_dir)
    projection = auto_projection(hfov, vfov)
    orientation = default_orientation(set_dir / "optimised.pto")
    info = render(set_dir, projection, orientation, width=3000, fuse=fuse)
    name = projection if orientation == "level" else f"{projection}-{orientation}"
    record.update({"coverage": [hfov, vfov], "projection": projection, "orientation": orientation,
                   "preview": info["size"], "crop": info["crop_size"], "crop_rect": info["crop_rect"], "fused_stacks": info["fused_stacks"],
                   "preview_file": f"previews/{name}.jpg"})
    return info


def check_studio(set_dir, record):
    if record["status"] != "stitched":
        return
    share = studio_background_share(set_dir / record["preview_file"])
    if share >= 0.45:
        record["status"] = "poor fit"
        record["poor_fit"] = True
        record["poor_fit_reason"] = "looks like a studio/green-screen subject, not scenery"
        record["studio_share"] = round(share, 2)


def save_record(set_dir, record):
    write_atomic(Path(set_dir) / "batch.json", json.dumps(record, indent=1))


def straighten_job(set_dir, record):
    """Turn a finished set upright if the optimiser left it tipped over, then render it again.

    Safe to repeat: the project is only rewritten when it is off, and a preview
    that went missing part way through is rendered on the next run."""
    set_dir = Path(set_dir)
    turned = upright(set_dir)
    previews = set_dir / "previews"
    if turned:
        kept = previews / "before-upright"
        n = 2
        while kept.exists() and any(kept.iterdir()):
            kept = previews / f"before-upright-{n}"
            n += 1
        kept.mkdir(parents=True, exist_ok=True)
        for f in previews.glob("*"):
            if f.is_file():
                os.replace(f, kept / f.name)
        if record.get("upright"):
            record.setdefault("upright_history", []).append(
                {"upright": record["upright"], "before": record.get("before_upright")})
        record["upright"] = turned
        record["before_upright"] = {k: record.get(k) for k in ("coverage", "projection", "orientation", "preview_file")}
        record["before_upright"]["kept"] = kept.relative_to(set_dir).as_posix()
        save_record(set_dir, record)
    t0 = time.time()
    if turned or not (set_dir / record.get("preview_file", "previews/none.jpg")).exists():
        info = render_preview(set_dir, record, record.get("type") == "bracket")
        record["seconds"]["rerender"] = round(time.time() - t0, 1)
        record["rerender_peak_mb"] = info["peak_mb"]
    record.setdefault("upright", None)
    record["upright_checked"] = datetime.now().isoformat(timespec="seconds")
    save_record(set_dir, record)
    return turned


def part_key(key, number):
    return f"{key}-part{number}"


def make_parts(work, key, record, repaired):
    """Give every extra part a set folder of its own (key-part2, ...) and render it.

    The part's frames are copied in, so the folder stands alone; folders of
    parts that no longer exist are removed."""
    work = Path(work)
    set_dir = work / key
    wanted = set()
    manifest = json.loads((set_dir / "manifest.json").read_text(encoding="utf-8"))
    by_name = {Path(m["image"]).name: m for m in manifest}
    for part in (repaired or {}).get("parts", []):
        number = int(part["file"].split("_")[1].split(".")[0])
        pkey = part_key(key, number)
        wanted.add(pkey)
        pdir = work / pkey
        (pdir / "originals").mkdir(parents=True, exist_ok=True)
        lines = (set_dir / part["file"]).read_text(encoding="utf-8", errors="replace").splitlines()
        entries = []
        for index, line in enumerate(lines):
            if line.startswith("i ") and ' n"' in line:
                source = Path(line.split(' n"')[-1].rstrip('"'))
                if not source.is_absolute():
                    source = (set_dir / source).resolve()
                target = pdir / "originals" / source.name
                if not target.exists():
                    shutil.copyfile(source, target)
                lines[index] = line.split(' n"')[0] + f' n"{target.resolve()}"'
                entry = dict(by_name.get(source.name, {"file": source.name}))
                entry["image"] = entry["original"] = str(target.resolve())
                entries.append(entry)
        write_atomic(pdir / "optimised.pto", "\n".join(lines) + "\n")
        write_atomic(pdir / "manifest.json", json.dumps(entries, indent=2))
        upright(pdir)
        frames = len(entries)
        px_scale = max(max(m.get("width") or 0, m.get("height") or 0) for m in entries) / 1000 or 1.0
        error = part.get("error_px")
        poor = error is None or error > POOR_ERROR * px_scale
        prec = {"key": pkey, "set_id": record.get("set_id"), "type": record.get("type"), "part_of": key,
                "part": number, "frames": frames, "source_frames": part["frames"],
                "started": datetime.now().isoformat(timespec="seconds"), "seconds": {},
                "model": (repaired or {}).get("model"), "poor_fit": poor, "error_px": error,
                "status": "poor fit" if poor else "stitched"}
        t0 = time.time()
        render_preview(pdir, prec, record.get("type") == "bracket")
        prec["seconds"]["render"] = round(time.time() - t0, 1)
        check_studio(pdir, prec)
        prec["upright_checked"] = prec["finished"] = datetime.now().isoformat(timespec="seconds")
        prec["disk_mb"] = folder_mb(pdir)
        save_record(pdir, prec)
    for old in work.glob(f"{key}-part*"):
        if old.name in wanted or not (old / "batch.json").exists():
            continue
        if json.loads((old / "batch.json").read_text(encoding="utf-8")).get("part_of") == key:
            shutil.rmtree(old, ignore_errors=True)
    return sorted(wanted)


def repair_job(work, key, record):
    """Drop frames that are not held in place, split off separate pieces, level the row, render again.

    Leaves a set whose frames all belong untouched (apart from noting the
    check). The previews it replaces are kept in previews/before-repair."""
    work = Path(work)
    set_dir = work / key
    repaired = repair(set_dir, model=record.get("model") if record.get("model") in ("rotation", "mosaic") else None)
    tilt = level(set_dir)
    record["repair_checked"] = datetime.now().isoformat(timespec="seconds")
    if not repaired and not tilt and not record.get("repair"):
        save_record(set_dir, record)
        return None
    previews = set_dir / "previews"
    kept = previews / "before-repair"
    if not kept.exists() or not any(kept.iterdir()):
        kept.mkdir(parents=True, exist_ok=True)
        for f in previews.glob("*"):
            if f.is_file():
                os.replace(f, kept / f.name)
    else:
        for f in previews.glob("*"):
            if f.is_file():
                f.unlink()
    if "before_repair" not in record:
        record["before_repair"] = {k: record.get(k) for k in
                                   ("coverage", "projection", "orientation", "preview_file", "error_px", "status")}
        record["before_repair"]["kept"] = kept.relative_to(set_dir).as_posix()
    turned = upright(set_dir)
    record["repair"] = repaired
    record["levelled"] = tilt
    if turned:
        record["upright"] = turned
    if repaired:
        manifest = json.loads((set_dir / "manifest.json").read_text(encoding="utf-8"))
        used = [manifest[k - 1] for k in repaired["kept"] if k - 1 < len(manifest)] or manifest
        px_scale = max(max(m.get("width") or 0, m.get("height") or 0) for m in used) / 1000 or 1.0
        error = repaired["error_px"]
        poor = error is None or error > POOR_ERROR * px_scale or len(repaired["kept"]) < 0.6 * record["frames"]
        record.update({"error_px": error, "poor_fit": poor, "frames_used": len(repaired["kept"])})
        if record.get("status") in ("stitched", "poor fit"):
            record["status"] = "poor fit" if poor else "stitched"
            record.pop("poor_fit_reason", None)
    t0 = time.time()
    render_preview(set_dir, record, record.get("type") == "bracket")
    record["seconds"]["repair"] = round(time.time() - t0, 1)
    check_studio(set_dir, record)
    record["parts"] = make_parts(work, key, record, repaired)
    save_record(set_dir, record)
    return record


def undo_repair(set_dir):
    """Put back the project and previews a failed repair replaced."""
    set_dir = Path(set_dir)
    if (set_dir / "full.pto").exists():
        os.replace(set_dir / "full.pto", set_dir / "optimised.pto")
    for f in set_dir.glob("part_*.pto"):
        f.unlink()
    kept = set_dir / "previews" / "before-repair"
    if kept.exists() and not any(f.is_file() for f in (set_dir / "previews").glob("*.jpg")):
        for f in kept.glob("*"):
            os.replace(f, set_dir / "previews" / f.name)
        kept.rmdir()


def repair_chunk(plan_file, work, sets=None, recheck=False):
    plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))
    keys = sets if sets else [j["key"] for j in plan["jobs"]]
    stop_file = Path(work) / "batch.stop"
    skipped = discarded(work)
    t0 = time.time()
    fixed = 0
    for n, key in enumerate(keys, 1):
        if stop_file.exists():
            LOG.info(f"stop file found, ending before {key}")
            break
        if key in skipped:
            LOG.info(f"[{n}/{len(keys)}] {key}: discarded on the review page, left alone")
            continue
        set_dir = Path(work) / key
        record = load_record(work, key)
        if not record or record.get("status") == "failed" or not (set_dir / "optimised.pto").exists():
            continue
        if record.get("repair_checked") and not recheck:
            continue
        try:
            done = repair_job(work, key, record)
        except (StitchError, OSError, ValueError, KeyError, RuntimeError) as err:
            shutil.rmtree(set_dir / "render_tmp", ignore_errors=True)
            undo_repair(set_dir)
            LOG.info(f"[{n}/{len(keys)}] {key}: failed, left as it was ({str(err)[:200]})")
            continue
        if done:
            fixed += 1
            info = done.get("repair") or {}
            LOG.info(f"[{n}/{len(keys)}] {key}: kept {len(info.get('kept', [])) or done['frames']}/{done['frames']}, "
                  f"left out {sorted(info.get('left_out', {}), key=int)}, parts {done.get('parts')}, "
                  f"levelled {done.get('levelled')}, {done.get('error_px')} px, {done['status']}")
    LOG.info(f"{fixed} sets repaired; done in {(time.time() - t0) / 60:.1f} min")


def straighten_chunk(plan_file, chunk, work, sets=None, recheck=False):
    plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))
    keys = sets if sets else plan["chunks"][chunk]
    stop_file = Path(work) / "batch.stop"
    skipped = discarded(work)
    t0 = time.time()
    for n, key in enumerate(keys, 1):
        if stop_file.exists():
            LOG.info(f"stop file found, ending before {key}")
            break
        if key in skipped:
            LOG.info(f"[{n}/{len(keys)}] {key}: discarded on the review page, left alone")
            continue
        set_dir = Path(work) / key
        record = load_record(work, key)
        if not record or record.get("status") == "failed" or not (set_dir / "optimised.pto").exists():
            continue
        if record.get("upright_checked") and not recheck:
            continue
        try:
            turned = straighten_job(set_dir, record)
        except (StitchError, OSError, ValueError, KeyError, RuntimeError) as err:
            shutil.rmtree(set_dir / "render_tmp", ignore_errors=True)
            undo_repair(set_dir)
            LOG.info(f"[{n}/{len(keys)}] {key}: failed, left as it was ({str(err)[:200]})")
            continue
        if turned:
            LOG.info(f"[{n}/{len(keys)}] {key}: turned {turned}, now {record['projection']} {record['coverage']}")
        else:
            LOG.info(f"[{n}/{len(keys)}] {key}: already upright")
    LOG.info(f"chunk done in {(time.time() - t0) / 60:.1f} min")


def job_params(job):
    return {k: job.get(k) for k in ("type", "frames", "stitch_plan")}


def run_job(immich, job, work, db_file, old=None):
    set_dir = Path(work) / job["key"]
    record = {"key": job["key"], "set_id": job["set_id"], "type": job["type"], "frames": len(job["frames"]),
              "started": datetime.now().isoformat(timespec="seconds"), "seconds": {}}
    t0 = time.time()
    try:
        hints = focal_hints(db_file, Path(db_file).parent, job["asset_ids"])
        fetch_set(immich, {"set_id": job["key"], "asset_ids": job["asset_ids"]}, work, f35_hints=hints)
        record["download_mb"] = folder_mb(set_dir / "originals")
        record["seconds"]["fetch"] = round(time.time() - t0, 1)
        t1 = time.time()
        plan = job["stitch_plan"] or (["rotation", "mosaic"] if job["type"] == "bracket" else [])
        layout = align(set_dir, model="auto", plan=plan, threads=limits.align_threads())
        record["seconds"]["align"] = round(time.time() - t1, 1)
        model = layout["model"]
        chosen = model["tried"][model["chosen"]]
        record.update({"model": model["chosen"], "model_reason": model["reason"], "poor_fit": model["poor_fit"],
                       "error_px": chosen["error_px"], "largest_group": chosen["largest_group"],
                       "groups": layout["connected_groups"]})
        record["upright"] = layout.get("upright")
        record["upright_checked"] = datetime.now().isoformat(timespec="seconds")
        t2 = time.time()
        info = render_preview(set_dir, record, job["type"] == "bracket")
        record["seconds"]["render"] = round(time.time() - t2, 1)
        record["peak_mb"] = max([s.get("peak_mb", 0) for s in layout.get("steps", [])] + [info["peak_mb"]])
        record["status"] = "poor fit" if model["poor_fit"] else "stitched"
        check_studio(set_dir, record)
        if layout.get("repair"):
            record["repair"] = layout["repair"]
            record["frames_used"] = len(layout["repair"]["kept"])
            record["parts"] = make_parts(work, job["key"], record, layout["repair"])
        clean_up(set_dir)
    except (StitchError, OSError, ValueError, KeyError, RuntimeError) as err:
        full = isinstance(err, OutOfSpace) or (isinstance(err, OSError) and err.errno == errno.ENOSPC)
        record["status"] = "no space" if full else "failed"  # "no space" is tried again next run
        record.update(retry.record(err, retry.code_version("batch", "stitch"), job_params(job),
                                   old if old and old.get("status") == "failed" else None))
        record["trace"] = traceback.format_exc(limit=3)[-800:]
        shutil.rmtree(set_dir / "render_tmp", ignore_errors=True)
    record["seconds"]["total"] = round(time.time() - t0, 1)
    record["disk_mb"] = folder_mb(set_dir) if set_dir.exists() else 0
    record["finished"] = datetime.now().isoformat(timespec="seconds")
    set_dir.mkdir(parents=True, exist_ok=True)
    save_record(set_dir, record)
    return record


def load_record(work, key):
    path = Path(work) / key / "batch.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def run_chunk(immich, plan_file, chunk, work, db_file, retry_failed=False, max_mb=None, sets=None):
    plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))
    jobs = {j["key"]: j for j in plan["jobs"]}
    keys = sets if sets else plan["chunks"][chunk]
    os.environ["FUSESHOT_MAX_MB"] = str(max_mb or limits.tool_mb())
    tools.require("hugin")  # before any set is marked failed for want of it
    stop_file = Path(work) / "batch.stop"
    t0 = time.time()
    earlier = Counter()
    for n, key in enumerate(keys, 1):
        if stop_file.exists():
            LOG.info(f"stop file found, ending before {key}")
            break
        job = jobs[key]
        if left_out(work, job):
            earlier["discarded on the review page"] += 1
            continue
        old = load_record(work, key)
        again = old and old.get("status") == "failed" and (
            retry_failed or retry.should_retry(old, retry.code_version("batch", "stitch"), job_params(job)))
        if old and old.get("status") in DONE and not again:
            earlier[f"already {old['status']}"] += 1
            continue
        LOG.info(f"[{n}/{len(keys)}] {key}: {job['type']}, {len(job['frames'])} frames")
        record = run_job(immich, job, work, db_file, old=old)
        note = record.get("error") or f"{record.get('model')} {record.get('error_px')} px, {record.get('projection')}"
        LOG.info(f"  {record['status']} in {record['seconds']['total']:.0f}s ({note})")
        if record["status"] == "no space":
            LOG.warning("Stopping here: the disk is full. Free some space and run again to carry on.")
            break
    if earlier:
        LOG.info("left alone: " + ", ".join(f"{count} {why}" for why, count in sorted(earlier.items())))
    LOG.info(f"chunk done in {(time.time() - t0) / 60:.1f} min")


def print_status(plan_file, work):
    plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))
    counts, times, disk = {}, [], 0.0
    for job in plan["jobs"]:
        record = load_record(work, job["key"])
        status = record["status"] if record else "pending"
        counts[status] = counts.get(status, 0) + 1
        if record:
            disk += record.get("disk_mb") or 0
            if status != "failed":
                times.append(record["seconds"]["total"])
    print(json.dumps(counts))
    if times:
        times.sort()
        print(f"per set: median {times[len(times) // 2]:.0f}s, max {times[-1]:.0f}s; disk {disk / 1024:.2f} GB")
