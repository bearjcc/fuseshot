"""Keep folder sources in the cache up to date while photos are added, changed or removed.

Uses watchdog when it is installed and falls back to rescanning on a timer when it is not.
A full rescan also runs every so often in case the file system missed an event."""

import threading
import time

from . import cache, log
from .sources import SourceError

LOG = log.get(__name__)

POLL_SECONDS = 60


def rescan(db_path, folders, skip, log=LOG.info):
    db = cache.connect(db_path)
    try:
        moved = 0
        for source in folders:
            changed, removed, failed = source.sync(db, skip, log=log)
            if changed or removed or failed:
                log(f"{source.label()}: {changed} new or changed, {removed} gone"
                    + (f", {failed} unreadable" if failed else ""))
            moved += changed + removed
        return moved
    finally:
        db.close()


def observe(folders, skip, touched):
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        return None

    class Handler(FileSystemEventHandler):
        def __init__(self, source):
            self.source = source

        def on_any_event(self, event):
            if event.event_type in ("opened", "closed_no_write"):
                return
            if event.is_directory:
                if self.source.recursive and event.event_type != "modified":
                    touched()
                return
            paths = [event.src_path, getattr(event, "dest_path", "") or ""]
            if any(p and self.source.wants(p, skip) for p in paths):
                touched()

    observer = Observer()
    for source in folders:
        observer.schedule(Handler(source), str(source.path), recursive=source.recursive)
    observer.start()
    return observer


def run(db_path, found, output, settle=20.0, rescan_every=3600.0, after=None, log=LOG.info):
    folders = [s for s in found if s.kind == "folder"]
    if not folders:
        raise SourceError("there are no folder sources to watch; add --source folder:PATH or a folder in the settings file")
    skip = [output] if output else []

    def update():
        if rescan(db_path, folders, skip, log) and after:
            after()

    log(f"Reading {len(folders)} folder(s)")
    update()
    last = [0.0]
    pending = threading.Event()

    def touched():
        last[0] = time.monotonic()
        pending.set()

    observer = observe(folders, skip, touched)
    if observer:
        log("Watching " + ", ".join(s.label() for s in folders) + " (Ctrl+C to stop)")
    else:
        log(f"watchdog is not installed, so the folders are rescanned every {POLL_SECONDS} s")
        rescan_every = POLL_SECONDS
    next_full = time.monotonic() + rescan_every if rescan_every > 0 else None
    try:
        while True:
            time.sleep(1)
            now = time.monotonic()
            if pending.is_set() and now - last[0] >= settle:
                pending.clear()
                update()
                next_full = now + rescan_every if next_full else None
            elif next_full and now >= next_full:
                update()
                next_full = time.monotonic() + rescan_every
    except KeyboardInterrupt:
        pass
    finally:
        if observer:
            observer.stop()
            observer.join()
