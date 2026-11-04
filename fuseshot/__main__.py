"""Command line entry point: fuseshot <command> (or python -m fuseshot <command>)."""

import argparse
import errno
import importlib.util
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, cache, config, jobs, limits, log, sources
from .immich import ImmichError
from .files import write_atomic

LOG = log.get("main")
VISIBILITIES = ["timeline", "archive"]
EXAMPLES = """examples:
  fuseshot init                        choose the photo folders and where results go, once
  fuseshot doctor                      check what is installed and what is missing
  fuseshot run                         sync, detect, verify and stitch previews: the usual pass
  fuseshot run --year 2023             try one year first; a later full run reuses the work
  fuseshot review --open               look through the results and keep or discard them

step by step:
  fuseshot sync                        read photo details from Immich into the local cache
  fuseshot detect                      find sets of photos that look like panoramas, loops or groups
  fuseshot verify                      check those sets for real overlap
  fuseshot batch --plan && fuseshot batch --chunk 0
                                       stitch previews of the sets that passed
  fuseshot --source folder:~/Pictures/Trip sync
                                       use a folder of photos instead of Immich

Everything is kept in the home folder (--home, else FUSESHOT_HOME); see
`fuseshot doctor` for where that is.
"""


def cmd_sync(args):
    args.db.parent.mkdir(parents=True, exist_ok=True)
    found, output = sources.configured(args)
    db = cache.connect(args.db)
    try:
        for source in found:
            if source.kind == "folder":
                sync_folder(db, source, output)
            elif not args.folders_only:
                sync_immich(args, db, source)
    finally:
        db.close()


def sync_folder(db, source, output):
    t0 = time.time()
    changed, removed, failed = source.sync(db, skip=[output] if output else [])
    LOG.info(f"{source.label()}: {changed} new or changed, {removed} gone"
             + (f", {failed} unreadable" if failed else "") + f" ({time.time() - t0:.0f}s)")
    return changed + removed


def sync_immich(args, db, source):
    immich = source.client()
    LOG.info(f"Immich {immich.version()} at {source.url}")
    stats = immich.statistics()
    LOG.info(f"Server reports {stats.get('total')} assets visible to this key")

    updated_after = None if args.full else cache.get_meta(db, "last_sync")
    if updated_after:
        LOG.info(f"Fetching assets updated since {updated_after}")
    started = datetime.now(timezone.utc).isoformat()
    total = 0
    t0 = time.time()
    for visibility in VISIBILITIES:
        for page in immich.iter_assets(visibility, updated_after=updated_after):
            cache.upsert(db, page)
            db.commit()
            total += len(page)
            LOG.debug(f"  {visibility}: {total} assets ({time.time() - t0:.0f}s)")
    cache.set_meta(db, "last_sync", started)
    db.commit()
    count = db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    LOG.info(f"Fetched {total} assets, cache now holds {count}")


def cmd_detect(args):
    from . import detect, report

    db = cache.connect(args.db)
    if not db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]:
        raise RuntimeError("the cache is empty; run `fuseshot sync` first")
    web_url = sources.web_url(args)
    settings = detect.Settings(max_gap=args.max_gap, min_shots=args.min_shots, **date_range(args))
    bursts = []
    candidates, stats = detect.find_candidates(db, settings, bursts)
    args.out.mkdir(parents=True, exist_ok=True)
    report.write_bursts(bursts, stats, args.out, web_url)
    if args.bursts_only:
        LOG.info(f"{len(bursts)} burst-mode runs with {sum(b['count'] for b in bursts)} shots "
              f"written to {args.out / 'bursts.json'}")
        return
    sites = None
    if args.sites.exists():
        sites = json.loads(args.sites.read_text(encoding="utf-8-sig"))
    verified_file = args.out / "verified.json"
    if args.keep_verified and verified_file.exists():
        # Immich set numbers don't move when folder photos change; folder sets may, so they are checked again.
        results = json.loads(verified_file.read_text(encoding="utf-8"))
        report.merge_verified(candidates, {k: v for k, v in results.items() if k.startswith("P")}, web_url)
    report.write_all(candidates, stats, settings, args.out, web_url, db, sites)
    if not getattr(args, "in_run", False):  # run prints the summary once, after verify
        print((args.out / "summary.txt").read_text(encoding="utf-8"))


def date_range(args):
    """since and until (YYYY-MM-DD) from --year, --since and --until."""
    since, until = getattr(args, "since", None), getattr(args, "until", None)
    year = getattr(args, "year", None)
    if year:
        since, until = since or f"{year}-01-01", until or f"{year}-12-31"
    for value in (since, until):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise RuntimeError(f"{value} is not a date; use YYYY-MM-DD, for example 2023-07-01") from None
    return {"since": since, "until": until}


def date_options(p):
    p.add_argument("--year", type=int, help="only photos taken in this year, to try Fuseshot on part of the library")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="only photos taken on or after this day")
    p.add_argument("--until", metavar="YYYY-MM-DD", help="only photos taken on or before this day")


def cmd_verify(args):
    from . import lens, report, verify

    web_url = sources.web_url(args)
    data = json.loads(args.candidates.read_text(encoding="utf-8"))
    candidates = data["candidates"]
    chosen = candidates
    if args.sets:
        wanted = set(args.sets)
        chosen = [c for c in candidates if c["set_id"] in wanted]
    if args.limit:
        chosen = chosen[:args.limit]
    cache_dir = args.db.parent
    db = cache.connect(args.db)
    if not args.classify_only:
        immich = sources.library(args)
        lens.update_crop_factors(immich, db, chosen, cache_dir)
        ordered = sorted(chosen, key=lambda c: len(c["asset_ids"]))
        run = verify.measure_all(immich, ordered, cache_dir, stop_at=args.stop_at,
                                 log=LOG.info)
        LOG.info(f"Measured {run['measured']} sets in {run['seconds'] / 60:.1f} min, peak {run['peak_rss_mb']} MB")
        if args.measure_only:
            return
    factors_file = cache_dir / "crop_factors.json"
    factors = json.loads(factors_file.read_text(encoding="utf-8")) if factors_file.exists() else {}
    focal35 = lens.update_frame_focals(sources.library(args), chosen, cache_dir,
                                       log=LOG.info)
    results = verify.classify_all(chosen, cache_dir, db, factors, focal35)
    LOG.info(f"Classified {len(results)} of {len(chosen)} sets")
    out = args.candidates.parent
    previous = {}
    if (out / "verified.json").exists():
        previous = json.loads((out / "verified.json").read_text(encoding="utf-8"))
    previous.update(results)
    write_atomic(out / "verified.json", json.dumps(previous, indent=1))
    report.merge_verified(candidates, previous, web_url)
    sites = json.loads(args.sites.read_text(encoding="utf-8-sig")) if args.sites.exists() else None
    report.write_all(candidates, data["stats"], data["settings"], out, web_url, db, sites)
    print((out / "summary.txt").read_text(encoding="utf-8"))


def cmd_fetch(args):
    from .fetch import fetch_set, load_candidate

    immich = sources.library(args)
    for set_id in args.set_ids:
        fetch_set(immich, load_candidate(args.candidates, set_id), args.work)


def set_folder(args, set_id):
    folder = args.work / set_id
    if not folder.is_dir():
        raise RuntimeError(f"no set {set_id} in {args.work}; `fuseshot fetch {set_id}` downloads it, "
                           "and `fuseshot batch --status` lists the stitched ones")
    return folder


def cmd_align(args):
    from .stitch import align

    model, plan = args.model, None
    overrides_file = args.work / "overrides.json"
    if model is None and overrides_file.exists():
        model = json.loads(overrides_file.read_text(encoding="utf-8")).get(args.set_id, {}).get("model")
    verified_file = args.candidates.parent / "verified.json"
    if verified_file.exists():
        plan = json.loads(verified_file.read_text(encoding="utf-8")).get(args.set_id, {}).get("stitch_plan")
    layout = align(set_folder(args, args.set_id), celeste=args.celeste, multirow=args.multirow,
                   model=model or "auto", plan=plan, threads=args.threads or limits.align_threads())
    keys = ("images", "overlapping_pairs", "sequential_neighbours_overlapping", "sequential_gaps",
            "connected_groups", "yaw_span", "pitch_span", "mean_error_px", "model", "align_seconds")
    for key in keys:
        print(f"{key}: {layout.get(key)}")


def cmd_render(args):
    from .stitch import render

    for projection in args.projections:
        info = render(set_folder(args, args.set_id), projection, args.orientation, width=args.width)
        print(json.dumps(info))


def cmd_batch(args):
    from . import batch

    if args.plan:
        plan = batch.make_plan(args.candidates.parent / "verified.json", args.candidates, args.min_confidence,
                               set(args.exclude))
        batch.save_plan(plan, args.plan_file, args.chunk_size)
        return
    if args.status:
        batch.print_status(args.plan_file, args.work)
        return
    if args.repair:
        batch.repair_chunk(args.plan_file, args.work, sets=args.sets, recheck=args.recheck)
        return
    if args.straighten:
        batch.straighten_chunk(args.plan_file, args.chunk, args.work, sets=args.sets, recheck=args.recheck)
        return
    batch.run_chunk(sources.library(args), args.plan_file, args.chunk, args.work, args.db,
                    retry_failed=args.retry_failed, max_mb=args.max_mb, sets=args.sets)


def cmd_action(args):
    from . import action

    if args.scan:
        action.scan(args.candidates, args.cache, args.actions, args.plan_file,
                    immich=sources.library(args))
        return
    if args.motion:
        from . import motion

        motion.scan(args.db, sources.library(args), args.cache, args.actions.with_name("motion.json"))
        return
    if args.wiggle:
        from . import stereo

        stereo.build(args.db, sources.library(args), args.cache / "stereo", args.work / "actions")
        return
    action.build(sources.library(args), args.actions, args.cache, args.work, args.candidates,
                 keys=args.sets, chunk=args.chunk, chunk_size=args.chunk_size,
                 stop_file=args.work / "batch.stop")


def peak_mb():
    try:
        import psutil
        info = psutil.Process().memory_info()
        return round(getattr(info, "peak_wset", info.rss) / 2**20)
    except ImportError:
        return "?"


def cmd_enhance_scan(args):
    from . import enhance

    found = enhance.scan(args.db, sources.library(args), args.work / "enhance", limit=args.limit,
                         stop_file=args.work / "batch.stop")
    for row in found["candidates"][:10]:
        LOG.info(f"  {row['gain']:5.1f}  {row['id']}  {', '.join(row['reasons'])}")


def cmd_group(args):
    from . import group
    from .action import load_sets

    if args.scan:
        group.scan(args.candidates, args.cache, args.groups, immich=sources.library(args))
        return
    sets = args.sets
    if args.all:
        found = group.load_groups(args.groups)
        from .review import discarded

        sets = sorted(k for k, v in found.items() if v.get("group") and k not in discarded(args.work, "group"))
    by_id = {c["set_id"]: c for c in load_sets(args.candidates)}
    immich = sources.library(args)
    failures = 0
    if getattr(args, "in_run", False):
        sets = [k for k in sets or [] if not (args.work / "groups" / k / "group.json").exists()]
    for set_id in sets or []:
        if set_id not in by_id:
            LOG.error(f"{set_id}: not a known set")
            failures += 1
            continue
        started = time.time()
        try:
            group.merge(immich, by_id[set_id], args.groups, args.work)
            LOG.info(f"{set_id}: done in {time.time() - started:.0f} s, peak memory {peak_mb()} MB")
        except Exception as err:
            failures += 1
            LOG.error(f"{set_id}: {err}")
    if failures:
        raise RuntimeError(f"{failures} set(s) failed")


def cmd_splat(args):
    from . import splat

    splat.run(args, lambda: sources.library(args))


def cmd_fill(args):
    from . import fill

    info = fill.fill(args.work / args.set, args.name, device=args.device, seams=args.seams, force=args.force)
    print(json.dumps(info, indent=1))


def cmd_edit(args):
    from . import edit

    plan = None
    verified_file = args.candidates.parent / "verified.json"
    if args.model and verified_file.exists():
        plan = json.loads(verified_file.read_text(encoding="utf-8")).get(args.set_id.split("-")[0], {}).get("stitch_plan")
    info = edit.run(args.work, args.set_id, args.projection, args.orientation, model=args.model, fill=args.fill,
                    plan=plan, plan_file=args.plan_file, library=lambda: sources.library(args), db_file=args.db,
                    queued=args.queued)
    print(json.dumps(info, indent=1))


def cmd_review(args):
    from .review import serve

    from . import upload

    found, output = sources.configured(args)
    upload_to = album = None
    if upload.enabled():
        immich = next((s for s in found if s.kind == "immich"), None)
        if immich is None:
            raise RuntimeError("[upload] immich = true, but no Immich source is set up")
        upload_to, album = immich.client(), upload.settings()["album"]
    serve(args.work, args.host, args.port, candidates_file=args.candidates, base_path=args.base_path,
          cache_dir=args.cache, redirect_from=args.redirect_from, output_dir=output, token=args.token,
          open_browser=args.open, upload_to=upload_to, album=album, library=sources.Library(found, args.db),
          cli_sources=bool(getattr(args, "sources", None)), reconfigure=lambda: sources.library(args))


def cmd_watch(args):
    from . import watch

    found, output = sources.configured(args)
    after = None
    if args.detect:
        args.max_gap, args.min_shots, args.bursts_only, args.keep_verified = 30.0, 3, False, True
        after = lambda: cmd_detect(args)
    watch.run(args.db, found, output, settle=args.settle, rescan_every=args.rescan_every * 60, after=after)


def cmd_export(args):
    from .export import export
    from .review import Review

    found, output = sources.configured(args)
    if args.upload and not args.immich:
        raise RuntimeError("--upload works with --immich: export --immich --upload")
    review = Review(args.work, args.candidates)
    if args.upload:
        from . import upload

        immich = next((s for s in found if s.kind == "immich"), None)
        if immich is None:
            raise RuntimeError("--upload needs an Immich source")
        album = upload.settings()["album"] if args.album is None else (args.album.strip() or None)
        sent = upload.Uploader(review, immich.client(), album, log=print).run(dry_run=args.dry_run)
        print(f"{len(sent)} result(s) {'would be ' if args.dry_run else ''}uploaded to Immich")
        return
    libraries = ("folder", "immich") if args.immich else ("folder",)
    written = export(review, output, dry_run=args.dry_run, libraries=libraries)
    kept = sum(1 for item in review.items()["items"]
               if item.get("library") in libraries and item.get("decision") == "kept" and item["kind"] != "found")
    there = f" ({kept - len(written)} already there)" if kept > len(written) else ""
    print(f"{len(written)} file(s) {'would be ' if args.dry_run else ''}copied to {output}{there}")


def cmd_doctor(args):
    from . import doctor

    found = doctor.check(args)
    if args.json:
        print(json.dumps(found, indent=1))
    else:
        print(doctor.describe(found))
    if args.strict and any(item["state"] in ("problem", "mismatch") for item in found["checks"]):
        raise SystemExit(1)


def cmd_fetch_assets(args):
    from . import assets

    groups = args.only or ["viewer", "group", "fill"]
    assets.fetch_all(groups)
    print(f"downloaded and checked: {', '.join(groups)}")


def cmd_init(args):
    from . import firstrun

    firstrun.run(args)


def stitch_queued(args, bar):
    """Stitch the plan as one queued job per set; args are those of the `batch --plan` step."""
    from . import feed

    options = feed.child_options(args)

    def make_args(key):
        return feed.batch_args(options, key, args.plan_file, args.work, args.candidates)

    window = jobs.settings().get("stitch_window", feed.WINDOW)
    feed.Feeder(jobs.runner(), args.plan_file, args.work, make_args, window, on_done=bar.update).run()


def cmd_run(args):
    """The usual pass over a library: sync, detect, verify, then stitch previews of what checked out."""
    def step(name, *extra):
        step_args = args.parsers[name].parse_args(list(extra))
        for key, value in vars(args).items():
            if not hasattr(step_args, key):
                setattr(step_args, key, value)
        step_args.command = name
        fill_defaults(step_args)
        step_args.in_run = True
        LOG.info(f"== {name} {' '.join(extra)}".rstrip())
        if not args.dry_run:
            step_args.func(step_args)
        return step_args

    from . import tools

    found, output = sources.configured(args)
    LOG.info(f"Home folder: {config.home()}")
    for source in found:
        LOG.info(f"Source: {source.label() if source.kind == 'folder' else 'Immich at ' + source.url}")
    LOG.info(f"Kept folder results go to: {output}")
    off = [name for name in config.OUTPUTS if not config.wants(name)]
    if off:
        LOG.info(f"Turned off under [outputs] in the settings: {', '.join(off)}")
    args.no_stitch = args.no_stitch or not config.wants("panoramas")
    args.no_loops = args.no_loops or not config.wants("loops")
    args.no_groups = args.no_groups or not config.wants("groups")
    if not args.no_stitch:
        hugin = tools.find("hugin")
        if hugin is None and not args.dry_run:
            tools.require("hugin")
        LOG.info(f"Stitching with: {hugin.parent if hugin else 'Hugin, not found yet'}")
    if args.dry_run:
        LOG.info("Dry run, so nothing below is done:")
    else:
        from . import progress

        progress.STATE = config.home() / "output" / "progress.json"
    synced = step("sync")
    from . import explain

    if not args.dry_run and not cache.connect(synced.db).execute("SELECT COUNT(*) FROM assets").fetchone()[0]:
        explain.report(explain.no_photos(found) or ["No photos were found in the sources."])
        return
    dates = [f"--{k}={v}" for k, v in date_range(args).items() if v]
    detected = step("detect", "--keep-verified", *dates)
    step("verify")
    if not args.no_stitch:
        planned = step("batch", "--plan", "--min-confidence", str(args.min_confidence))
        plan = planned.plan_file
        chunks = [] if args.dry_run else json.loads(plan.read_text(encoding="utf-8"))["chunks"]
        if not args.dry_run and not chunks:
            explain.report(explain.nothing_to_stitch(detected.out / "candidates.json", plan, detected.min_shots))
        if args.dry_run:
            LOG.info("== batch --chunk N, for each chunk of the plan")
        from .progress import Progress

        with Progress("stitching", sum(len(c) for c in chunks), unit="sets") as bar:
            if chunks and jobs.settings().get("runner") == "pueue":
                LOG.info("== batch --sets KEY, one Pueue task per set")
                stitch_queued(planned, bar)
            else:
                for number, chunk in enumerate(chunks):
                    step("batch", "--chunk", str(number))
                    bar.update(len(chunk))
    if not args.no_loops:
        step("action", "--scan")
        step("action")
        step("action", "--motion")
    if config.wants("wigglegrams"):
        step("action", "--wiggle")
    if not args.no_groups:
        if args.dry_run or importlib.util.find_spec("mediapipe"):
            step("group", "--scan")
            step("group", "--all")
        else:
            from .jobs import extra_hint

            LOG.info("Group photos skipped: " + extra_hint("group", "mediapipe"))
    if not args.dry_run:
        LOG.info("Done. Open the review page with `fuseshot review --open` to look through the results.")


def fill_defaults(args):
    """Paths left unset on the command line, under the home folder."""
    home = config.home()
    out = home / "output"
    defaults = {
        "db": home / "cache" / "assets.sqlite",
        "out": out,
        "work": home / "work",
        "candidates": out / "candidates.json",
        "actions": out / "actions.json",
        "groups": out / "groups.json",
        "plan_file": home / "work" / "batch_plan.json",
        "sites": home / "sites.json",
        "cache": home / "cache" / ("web" if args.command == "review" else ""),
    }
    for name, value in defaults.items():
        if hasattr(args, name) and getattr(args, name) is None:
            setattr(args, name, Path(os.path.normpath(value)))


def port(value):
    number = int(value)
    if not 0 < number < 65536:
        raise argparse.ArgumentTypeError(f"{value} is not a port number (1 to 65535)")
    return number


OPTION_HELP = {
    "work": "the work folder (default: work in the home folder)",
    "candidates": "the list of candidate sets (default: output/candidates.json in the home folder)",
    "cache": "the preview cache (default: cache in the home folder)",
    "plan_file": "the stitch plan (default: work/batch_plan.json in the home folder)",
    "actions": "the list of action shots (default: output/actions.json in the home folder)",
    "groups": "the list of group photos (default: output/groups.json in the home folder)",
    "out": "where the lists are written (default: output in the home folder)",
    "chunk_size": "sets per chunk (default: %(default)s)",
    "threads": "threads to use (default: %(default)s)",
    "limit": "only the first N sets",
    "width": "width of the preview in pixels (default: %(default)s)",
    "retry_failed": "try the sets that failed before again",
    "full": "read everything again, not only what changed",
    "min_shots": "fewest shots that make a set (default: %(default)s)",
    "min_confidence": "leave weaker sets out of the plan (default: %(default)s)",
    "orientation": "level the horizon, centre the view or keep it upright (default: %(default)s)",
}


def describe_options(parsers):
    """Give the options that have no help of their own the usual wording."""
    for parser in parsers.values():
        for action in parser._actions:
            if action.help is None and action.dest in OPTION_HELP:
                action.help = OPTION_HELP[action.dest]


FIRST_STEP = {"candidates.json": "fuseshot detect", "verified.json": "fuseshot verify",
              "bursts.json": "fuseshot detect", "actions.json": "fuseshot action --scan",
              "groups.json": "fuseshot group --scan"}


def explain(err):
    """The error as one line, saying which step makes a list file that is not there yet."""
    if isinstance(err, FileNotFoundError) and err.filename:
        name = Path(err.filename).name
        if name in FIRST_STEP:
            return (f"{err.filename} is not there yet; `{FIRST_STEP[name]}` makes it "
                    "(or `fuseshot run` does the usual steps in one go)")
    where = f" (writing {err.filename})" if getattr(err, "filename", None) else ""
    if (isinstance(err, OSError) and err.errno == errno.ENOSPC) or "disk is full" in str(err):
        return (f"the disk is full{where}. Free some space, or point the home or results folder at "
                "another disk, then run the same command again; finished work is kept")
    if isinstance(err, OSError) and (err.errno == errno.ENAMETOOLONG or getattr(err, "winerror", None) == 206):
        return (f"a path is too long for Windows{where}. Turn on long paths (the LongPathsEnabled "
                "setting) or use a shorter home or results folder")
    return str(err)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fuseshot", description="Panoramas, splats, action loops and group "
                                     "shots from the photo sets already in an Immich library or a folder.",
                                     epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"fuseshot {__version__}")
    parser.add_argument("--home", type=Path, help="folder for the cache, results and settings "
                        "(default: FUSESHOT_HOME, else the per-user data folder)")
    parser.add_argument("--config", type=Path, help="settings file (default: fuseshot.toml in the home folder)")
    parser.add_argument("-v", "--verbose", action="store_const", const=1, default=0, dest="verbosity",
                        help="more detail in the log")
    parser.add_argument("-q", "--quiet", action="store_const", const=-1, dest="verbosity",
                        help="only warnings and errors")
    parser.add_argument("--url", default=os.environ.get("FUSESHOT_IMMICH_URL", "http://127.0.0.1:2283"),
                        help="Immich server URL (default: FUSESHOT_IMMICH_URL, else %(default)s)")
    parser.add_argument("--key-file", type=Path,
                        help="file holding the Immich API key (default: FUSESHOT_IMMICH_KEY, "
                             "else secrets/immich_key in the home folder)")
    parser.add_argument("--db", type=Path, help="the photo details cache (default: cache/assets.sqlite)")
    parser.add_argument("--workers", type=int, metavar="N",
                        help="threads to use (default: from the cores and free memory; FUSESHOT_WORKERS)")
    parser.add_argument("--memory-mb", type=int, metavar="MB",
                        help="memory Fuseshot may use (default: half the memory or what is free, "
                             "whichever is less; FUSESHOT_MEMORY_MB)")
    parser.add_argument("--max-megapixels", type=int, metavar="MP",
                        help="largest kept result to make (default: as large as the photos and the "
                             "memory allow; FUSESHOT_MAX_MEGAPIXELS or max_megapixels in the settings file)")
    sources.add_options(parser)
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    web_url = dict(default=os.environ.get("FUSESHOT_WEB_URL"),
                   help="Immich address for links in the output, if people reach it at another address than --url")
    sites = dict(type=Path, help="optional list of known panorama places to check against (default: sites.json)")

    p = sub.add_parser("init", help="choose the photo folders and where results go, once")
    p.add_argument("folders", nargs="*", help="photo folders (asked for when left out)")
    p.add_argument("--output-folder", type=Path, default=argparse.SUPPRESS,
                   help="where kept results go (asked for when left out; default: Pictures/Fuseshot)")
    p.add_argument("--force", action="store_true", help="choose again even if folders are already set")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("run", help="sync, detect, verify and stitch previews in one go; safe to rerun")
    p.add_argument("--min-confidence", type=float, default=0.75,
                   help="leave weaker sets for later (default: %(default)s)")
    p.add_argument("--no-stitch", action="store_true", help="check the sets but stitch nothing")
    p.add_argument("--no-loops", action="store_true", help="skip looking for action shots to loop")
    p.add_argument("--no-groups", action="store_true", help="skip group photos (best face of each person)")
    date_options(p)
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="show the sources, folders and steps, and run nothing")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("review", help="the review page, to keep, discard or edit the results")
    p.add_argument("--work", type=Path)
    p.add_argument("--host", default=os.environ.get("FUSESHOT_REVIEW_HOST", "127.0.0.1"),
                   help="address to listen on (default %(default)s, this computer only)")
    p.add_argument("--port", type=port, default=int(os.environ.get("FUSESHOT_REVIEW_PORT", "8765")),
                   help="port to listen on (default: FUSESHOT_REVIEW_PORT, else 8765)")
    p.add_argument("--token", default=os.environ.get("FUSESHOT_REVIEW_TOKEN"),
                   help="require this token to open the page (default: FUSESHOT_REVIEW_TOKEN; one is made up "
                        "when listening on anything but this computer)")
    p.add_argument("--base-path", default=os.environ.get("FUSESHOT_BASE_PATH", ""),
                   help="serve under this path, for example /fuseshot behind a reverse proxy")
    p.add_argument("--redirect-from", action="append", default=[], metavar="PATH",
                   help="old base path to send on (301) to the base path")
    p.add_argument("--cache", type=Path, help="where scaled copies of the previews are kept")
    p.add_argument("--candidates", type=Path)
    p.add_argument("--open", action="store_true", help="open the page in the browser")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("export", help="copy kept results into the output folder")
    p.add_argument("--immich", action="store_true",
                   help="results made from Immich photos too")
    p.add_argument("--upload", action="store_true",
                   help="with --immich: upload kept Immich results to Immich as new photos instead of "
                        "copying them (the key needs asset.upload)")
    p.add_argument("--album", help="Immich album for uploads (default: [upload] album, else Fuseshot; "
                                   "an empty value means none)")
    p.add_argument("--work", type=Path)
    p.add_argument("--candidates", type=Path)
    p.add_argument("-n", "--dry-run", action="store_true", help="list what would be copied, copy nothing")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("doctor", help="check the tools, models and settings Fuseshot needs")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--strict", action="store_true", help="exit 1 if anything needed is missing")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("fetch-assets", help="download and check the viewer libraries and models ahead of time")
    p.add_argument("only", nargs="*", choices=["viewer", "group", "fill"], help="just these (default: all)")
    p.set_defaults(func=cmd_fetch_assets)

    p = sub.add_parser("sync", help="copy photo details from the sources into the local cache")
    p.add_argument("--full", action="store_true", help="refetch everything from Immich, not just changes")
    p.add_argument("--folders-only", action="store_true",
                   help="rescan the folder sources and leave Immich alone")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("watch", help="keep the folder sources in the cache up to date as photos arrive")
    p.add_argument("--settle", type=float, default=20.0,
                   help="seconds without changes before rescanning (default %(default)s)")
    p.add_argument("--rescan-every", type=float, default=60.0, metavar="MINUTES",
                   help="full rescan this often in case a change was missed; 0 turns it off")
    p.add_argument("--detect", action="store_true",
                   help="run detect after each change, keeping the overlap check results already made")
    p.add_argument("--out", type=Path)
    p.add_argument("--web-url", **web_url)
    p.add_argument("--sites", **sites)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("detect", help="find candidate panorama sets in the cache")
    p.add_argument("--max-gap", type=float, default=30.0, help="seconds allowed between shots in a set")
    p.add_argument("--min-shots", type=int, default=3)
    p.add_argument("--out", type=Path, help="where the lists are written (default: output)")
    p.add_argument("--web-url", **web_url)
    p.add_argument("--sites", **sites)
    date_options(p)
    p.add_argument("--bursts-only", action="store_true",
                   help="only write bursts.json, leaving candidates.json and its set numbers alone")
    p.add_argument("--keep-verified", action="store_true",
                   help="carry the overlap check results in verified.json over to the Immich sets")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("verify", help="check candidate sets for real overlap using previews")
    p.add_argument("--sets", nargs="+", help="only these set ids")
    p.add_argument("--limit", type=int)
    p.add_argument("--measure-only", action="store_true", help="match features, skip classification")
    p.add_argument("--classify-only", action="store_true", help="reuse cached matches, no Immich access")
    p.add_argument("--stop-at", help="local time HH:MM to stop measuring; rerun to continue")
    p.add_argument("--candidates", type=Path)
    p.add_argument("--web-url", **web_url)
    p.add_argument("--sites", **sites)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("fetch", help="download the originals of candidate sets for stitching")
    p.add_argument("set_ids", nargs="+")
    p.add_argument("--candidates", type=Path)
    p.add_argument("--work", type=Path)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("align", help="find control points and optimise a downloaded set")
    p.add_argument("set_id")
    p.add_argument("--work", type=Path)
    p.add_argument("--celeste", action="store_true", help="ignore control points in clouds")
    p.add_argument("--multirow", action="store_true", help="cpfind multirow matching instead of all pairs")
    p.add_argument("--model", choices=["auto", "rotation", "mosaic"],
                   help="camera model; default is the review page override, else auto (tries both)")
    p.add_argument("--candidates", type=Path)
    p.add_argument("--threads", type=int, help="cpfind threads (default: from the cores and memory)")
    p.set_defaults(func=cmd_align)

    p = sub.add_parser("render", help="render previews of an aligned set")
    p.add_argument("set_id")
    p.add_argument("projections", nargs="+", help="cylindrical, rectilinear, equirectangular, ...")
    p.add_argument("--orientation", default="level", choices=["level", "centred", "vertical"])
    p.add_argument("--width", type=int, default=3000)
    p.add_argument("--work", type=Path)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("batch", help="stitch previews for many verified sets, in resumable chunks")
    p.add_argument("--plan", action="store_true", help="write the ordered job list and its chunks")
    p.add_argument("--status", action="store_true", help="summarise finished, failed and pending jobs")
    p.add_argument("--chunk", type=int, help="run one chunk of the plan (0-based)")
    p.add_argument("--sets", nargs="+", help="only these job keys from the plan")
    p.add_argument("--chunk-size", type=int, default=20)
    p.add_argument("--min-confidence", type=float, default=0.75)
    p.add_argument("--exclude", nargs="*", default=[], help="set ids to leave out of the plan")
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--straighten", action="store_true",
                   help="turn finished sets upright where the optimiser tipped them over, and render them again")
    p.add_argument("--repair", action="store_true",
                   help="leave out frames the control points do not hold, split off separate pieces, level, re-render")
    p.add_argument("--recheck", action="store_true",
                   help="with --straighten or --repair, check sets that were checked before")
    p.add_argument("--max-mb", type=int, help="stop a tool that grows past this many MB (default: --memory-mb)")
    p.add_argument("--plan-file", type=Path)
    p.add_argument("--candidates", type=Path)
    p.add_argument("--work", type=Path)
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("action", help="find action shots and make stabilised loops of them")
    p.add_argument("--scan", action="store_true", help="classify every candidate set and stitch job")
    p.add_argument("--motion", action="store_true",
                   help="check motion photos and Live Photos for a still camera with something moving")
    p.add_argument("--wiggle", action="store_true",
                   help="make wigglegrams of stereo photos (side-by-side SV_ photos and MPO files)")
    p.add_argument("--chunk", type=int, help="make loops for one chunk of the action sets (0-based)")
    p.add_argument("--sets", nargs="+", help="make loops for these set ids")
    p.add_argument("--chunk-size", type=int, default=20)
    p.add_argument("--actions", type=Path)
    p.add_argument("--candidates", type=Path)
    p.add_argument("--plan-file", type=Path)
    p.add_argument("--cache", type=Path)
    p.add_argument("--work", type=Path)
    p.set_defaults(func=cmd_action)

    p = sub.add_parser("enhance-scan", help="rank photos that restoration could help (read-only; nothing is changed)")
    p.add_argument("--limit", type=int, help="only the newest this many photos (default: all)")
    p.add_argument("--work", type=Path)
    p.set_defaults(func=cmd_enhance_scan)

    p = sub.add_parser("group", help="find group photo sets and merge the best face of each person")
    p.add_argument("sets", nargs="*", help="set ids to merge (or re-merge after picking faces by hand)")
    p.add_argument("--scan", action="store_true", help="check every candidate set and burst for group photos")
    p.add_argument("--all", action="store_true", help="merge every set the scan marked as a group")
    p.add_argument("--groups", type=Path)
    p.add_argument("--candidates", type=Path)
    p.add_argument("--cache", type=Path)
    p.add_argument("--work", type=Path)
    p.set_defaults(func=cmd_group)

    p = sub.add_parser("splat", help="Gaussian splats from object scans: COLMAP poses, then Brush training")
    p.add_argument("sets", nargs="*", help="set ids to work on")
    p.add_argument("--plan", action="store_true", help="rank the object scan, walk and mixed sets")
    p.add_argument("--status", action="store_true", help="show how far each planned set has got")
    p.add_argument("--prepare", action="store_true", help="redo the downscaled frames")
    p.add_argument("--sfm", action="store_true", help="find camera poses and apply the confidence gate")
    p.add_argument("--train", action="store_true", help="train a splat (runs the earlier steps if needed)")
    p.add_argument("--convert", action="store_true", help="write scene.splat from the latest export")
    p.add_argument("--stereo-video", type=Path, metavar="VIDEO",
                   help="make a set from a side-by-side stereo video (both views, posed as a rig)")
    p.add_argument("--force", action="store_true", help="redo SfM even if it has a result")
    p.add_argument("--gpu", action="store_const", const=True, default=None,
                   help="COLMAP on the GPU (default: when an NVIDIA card and a CUDA build of COLMAP are found)")
    p.add_argument("--cpu", action="store_const", const=False, dest="gpu", help="COLMAP on the CPU only")
    p.add_argument("--threads", type=int, help="COLMAP threads (default: --workers)")
    p.add_argument("--steps", type=int, default=7000, help="training steps")
    p.add_argument("--chunk", type=int,
                   help="train at most this many more steps, then stop (resume with the same command)")
    p.add_argument("--candidates", type=Path)
    p.add_argument("--cache", type=Path)
    p.add_argument("--work", type=Path)
    p.set_defaults(func=cmd_splat)

    p = sub.add_parser("fill", help="paint the empty border of a preview with local AI inpainting (LaMa)")
    p.add_argument("set", help="set key, for example P00041")
    p.add_argument("--name", required=True, help="preview name, for example rectilinear")
    p.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto",
                   help="where to run the model (default: the GPU if torch can see one)")
    p.add_argument("--seams", action="store_true", help="also repaint the bands where layers were faded together")
    p.add_argument("--force", action="store_true", help="fill even when most of the picture is empty")
    p.add_argument("--work", type=Path)
    p.set_defaults(func=cmd_fill)

    p = sub.add_parser("edit", help="make a new version of a stitched result (the review page's Edit queues this)")
    p.add_argument("set_id")
    p.add_argument("--projection", required=True, help="cylindrical, rectilinear, equirectangular, ...")
    p.add_argument("--orientation", default="level", choices=["level", "centred", "vertical"])
    p.add_argument("--model", choices=["auto", "rotation", "mosaic"], help="align again with this camera model first")
    p.add_argument("--fill", action="store_true", help="queue an AI fill of the new version")
    p.add_argument("--queued", help=argparse.SUPPRESS)
    p.add_argument("--candidates", type=Path)
    p.add_argument("--plan-file", type=Path)
    p.add_argument("--work", type=Path)
    p.set_defaults(func=cmd_edit)

    if argv is None and getattr(sys, "frozen", False) and len(sys.argv) == 1:
        argv = ["review", "--open"]  # double-clicked: show the review page
    describe_options(sub.choices)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # keep printed results in step with the log
    args = parser.parse_args(argv)
    args.parsers = sub.choices
    log.setup(args.verbosity)
    limits.WORKERS, limits.MEMORY_MB = args.workers, args.memory_mb
    limits.MAX_MEGAPIXELS = args.max_megapixels
    try:
        config.setup(args.home, args.config)
        unknown = config.unknown_keys()
        if unknown and args.command != "doctor":
            LOG.warning(f"Warning: the settings file has settings Fuseshot does not know, so they do nothing: "
                        f"{', '.join(unknown)} (the known ones are {', '.join(config.KNOWN_KEYS)})")
        problem = config.home_problem()
        if problem and args.command != "doctor":
            raise config.SettingsError(problem)
        fill_defaults(args)
        args.func(args)
    except (ImmichError, RuntimeError, OSError, sqlite3.Error) as err:
        LOG.debug("details", exc_info=True)
        LOG.error(f"Error: {explain(err)}")
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
