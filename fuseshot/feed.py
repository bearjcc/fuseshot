"""Stitch the plan through the job runner, one set per job, a few jobs ahead.

Used by `run` when [jobs] runner is pueue. Only a short window of sets is queued
at a time, so the queue is not filled with hours of work, and a set discarded on
the review page while it waits is either never queued or taken off the queue
before it starts.
"""

import json
import time
from collections import deque
from pathlib import Path

from . import log
from .batch import DONE, left_out, load_record

LOG = log.get(__name__)
WINDOW = 2
POLL_S = 5.0


def job_name(key):
    return f"stitch-{key}"


def child_options(args):
    """The options a queued `batch` needs to find the same photos, cache and files as this run."""
    words = ["--db", args.db, "--url", args.url]
    if args.key_file:
        words += ["--key-file", args.key_file]
    for spec in getattr(args, "sources", None) or []:
        if spec["type"] == "immich":
            words += ["--source", "immich"]
        else:
            words += ["--source", f"folder:{spec['path']}"]
            words += ["--recursive"] if spec.get("recursive") else []
    return words


def batch_args(options, key, plan_file, work, candidates):
    return [*options, "batch", "--sets", key, "--plan-file", plan_file, "--work", work, "--candidates", candidates]


def finished(work, job):
    record = load_record(work, job["key"])
    return bool(record and record.get("status") in DONE)


class Feeder:
    def __init__(self, runner, plan_file, work, make_args, window=WINDOW, on_done=None, poll_s=POLL_S):
        self.runner, self.work, self.make_args = runner, Path(work), make_args
        self.window, self.poll_s = max(1, int(window)), poll_s
        self.on_done = on_done or (lambda: None)
        plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))
        self.waiting = deque(plan["jobs"])
        self.active = {}  # task id -> job

    def stopped(self):
        return (self.work / "batch.stop").exists()

    def fill(self):
        while self.waiting and len(self.active) < self.window and not self.stopped():
            job = self.waiting.popleft()
            if left_out(self.work, job) or finished(self.work, job):
                self.on_done()
                continue
            task = self.runner.submit("stitch", job_name(job["key"]), self.make_args(job["key"]))
            LOG.info(f"{job['key']}: {job['type']}, {len(job['frames'])} frames, queued as task {task}")
            self.active[task] = job

    def drop_discarded(self):
        names = {job_name(j["key"]) for j in self.active.values() if left_out(self.work, j)}
        for task in self.runner.cancel(names) if names else []:
            LOG.info(f"{self.active[task]['key']}: discarded on the review page, taken off the queue")

    def collect(self):
        for task, job in list(self.active.items()):
            state = self.runner.status(task)
            if state in ("queued", "running"):
                continue
            del self.active[task]
            self.on_done()
            if state == "failed":
                LOG.warning(f"{job['key']}: the job failed; its log is in Pueue (pueue log {task})")

    def run(self):
        self.fill()
        while self.active:
            time.sleep(self.poll_s)
            self.drop_discarded()
            self.collect()
            self.fill()
        if self.stopped() and self.waiting:
            LOG.info(f"stop file found, {len(self.waiting)} sets left for next time")
