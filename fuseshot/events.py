"""Changes to the review list, pushed to open pages as server-sent events.

One thread works out the list again when something under the watched folders changes (or a
decision is saved, or now and then anyway), compares it with the last one and keeps the
difference as a numbered event. Each open page has a stream that waits for new events and
writes them out. Pages that fall too far behind are told to load the whole list again.
"""

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

LOG = logging.getLogger(__name__)
KEEP = 50
PING = 25.0


class Changes:
    def __init__(self, compute, watch=(), ignore=(), every=10.0, gap=2.0):
        self.compute = compute
        self.watch = [Path(p) for p in watch if p and Path(p).is_dir()]
        self.ignore = [str(Path(p)) for p in ignore if p]
        self.every, self.gap = every, gap
        self.cond = threading.Condition()
        self.dirty = threading.Event()
        self.version = 0
        self.events = deque(maxlen=KEEP)
        self.state = None
        self.running = None
        self.listeners = 0
        self.observer = None

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="review-changes").start()
        self.observer = self.observe()
        return self

    def observe(self):
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            return None
        changes = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if not any(str(event.src_path).startswith(p) for p in changes.ignore):
                    changes.touch()

        observer = Observer()
        observer.daemon = True
        try:
            for path in self.watch:
                observer.schedule(Handler(), str(path), recursive=True)
            observer.start()
        except OSError as err:
            LOG.info("not watching for changes (%s), checking every %.0f s", err, self.every)
            return None
        return observer

    def touch(self):
        self.dirty.set()

    def loop(self):
        while True:
            self.dirty.wait(self.every)
            self.dirty.clear()
            if self.listeners:
                self.recompute()
            time.sleep(self.gap)

    def recompute(self):
        try:
            data = self.compute()
        except Exception:
            LOG.exception("could not work out the review list")
            return None
        new = {i["key"]: json.dumps(i, sort_keys=True) for i in data["items"]}
        old, self.state = self.state, new
        running, self.running = self.running, data.get("running")
        if old is None:
            return None
        changed = [json.loads(v) for k, v in new.items() if old.get(k) != v]
        removed = [k for k in old if k not in new]
        if not changed and not removed and running == self.running:
            return None
        return self.publish({"changed": changed, "removed": removed, "running": self.running})

    def publish(self, payload):
        with self.cond:
            self.version += 1
            self.events.append((self.version, payload))
            self.cond.notify_all()
            return self.version

    def wait(self, since, timeout=PING):
        """(version, events after since, whether since is too old to catch up from)."""
        with self.cond:
            self.cond.wait_for(lambda: self.version > since, timeout)
            news = [(v, p) for v, p in self.events if v > since]
            behind = bool(news) and news[0][0] > since + 1
            return self.version, news, behind

    def stream(self, write):
        """Write events to one page until it goes away."""
        with self.cond:
            self.listeners += 1
            since = self.version
        self.touch()
        try:
            write(f"retry: 3000\nevent: hello\ndata: {json.dumps({'version': since})}\n\n")
            while True:
                since, news, behind = self.wait(since)
                if behind:
                    write("event: reset\ndata: {}\n\n")
                for version, payload in [] if behind else news:
                    write(f"id: {version}\nevent: items\ndata: {json.dumps(payload)}\n\n")
                if not news:
                    write(": ping\n\n")
        except (OSError, ValueError):
            pass
        finally:
            with self.cond:
                self.listeners -= 1
