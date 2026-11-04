"""Stitching through a job queue: a few sets ahead, and discards taken off the queue."""

import json
from types import SimpleNamespace

from fuseshot import feed


class FakeRunner:
    """Each poll finishes the oldest running task and starts the next queued one."""

    def __init__(self):
        self.tasks = {}
        self.submitted = []
        self.cancelled = []

    def submit(self, kind, name, args, gpu=False):
        task = len(self.submitted) + 1
        self.submitted.append(name)
        self.tasks[task] = "running" if not any(s == "running" for s in self.tasks.values()) else "queued"
        return task

    def status(self, task):
        return self.tasks.get(task)

    def cancel(self, names):
        dropped = [t for t, s in self.tasks.items() if s == "queued" and self.submitted[t - 1] in names]
        for task in dropped:
            del self.tasks[task]
        self.cancelled += dropped
        return dropped

    def step(self):
        running = [t for t, s in self.tasks.items() if s == "running"]
        if running:
            self.tasks[running[0]] = "done"
        queued = [t for t, s in self.tasks.items() if s == "queued"]
        if queued:
            self.tasks[queued[0]] = "running"


def plan(tmp_path, keys):
    path = tmp_path / "plan.json"
    jobs = [{"key": k, "set_id": k.split("-")[0], "type": "bracket", "frames": [1, 2]} for k in keys]
    path.write_text(json.dumps({"jobs": jobs, "chunks": [keys]}))
    return path


def discard_found(work, set_id):
    path = work / "reviews.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data[set_id] = [{"kind": "found", "decision": "discarded", "at": "2026-11-04T00:00:00+00:00"}]
    path.write_text(json.dumps(data))


def test_feeder_keeps_a_short_window_and_drops_discarded_sets(tmp_path):
    runner = FakeRunner()
    discard_found(tmp_path, "P5")
    done, polls = [], []

    def poll(seconds):
        # Stands in for the wait between polls, so the test owns every step.
        polls.append(seconds)
        if len(polls) == 1:  # P2 is waiting on the queue when its set is discarded
            discard_found(tmp_path, "P2")
        else:
            runner.step()

    feeder = feed.Feeder(runner, plan(tmp_path, ["P1", "P2", "P3", "P4", "P5", "P6"]), tmp_path,
                         lambda key: ["batch", "--sets", key], window=2, on_done=lambda: done.append(1),
                         poll_s=0, sleep=poll)
    feeder.fill()
    assert runner.submitted == ["stitch-P1", "stitch-P2"]
    feeder.run()
    assert runner.cancelled == [2]
    assert runner.submitted == ["stitch-P1", "stitch-P2", "stitch-P3", "stitch-P4", "stitch-P6"]
    assert len(done) == 6
    assert all(p == 0 for p in polls)


def test_the_feeder_waits_with_the_sleep_it_was_given(tmp_path, monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(feed.time, "sleep", lambda seconds: (_ for _ in ()).throw(AssertionError("real sleep")))
    waits = []
    feeder = feed.Feeder(runner, plan(tmp_path, ["P1"]), tmp_path, lambda key: [], poll_s=7,
                         sleep=lambda seconds: (waits.append(seconds), runner.step()))
    feeder.run()
    assert waits == [7] and runner.submitted == ["stitch-P1"]


def test_feeder_leaves_finished_sets_and_stops_on_the_stop_file(tmp_path):
    runner = FakeRunner()
    (tmp_path / "P1").mkdir()
    (tmp_path / "P1" / "batch.json").write_text(json.dumps({"status": "stitched"}))
    (tmp_path / "batch.stop").write_text("")
    feeder = feed.Feeder(runner, plan(tmp_path, ["P1", "P2"]), tmp_path, lambda key: [], poll_s=0,
                         sleep=lambda seconds: runner.step())
    feeder.run()
    assert runner.submitted == []
    (tmp_path / "batch.stop").unlink()
    feeder.run()
    assert runner.submitted == ["stitch-P2"]


def test_queued_batch_gets_the_same_sources_and_files(tmp_path):
    args = SimpleNamespace(db=tmp_path / "a.sqlite", url="http://immich:2283", key_file=None,
                           sources=[{"type": "immich"}, {"type": "folder", "path": "/photos", "recursive": True}])
    words = feed.batch_args(feed.child_options(args), "P1-2", "plan.json", "work", "c.json")
    assert words[:4] == ["--db", tmp_path / "a.sqlite", "--url", "http://immich:2283"]
    assert words[4:9] == ["--source", "immich", "--source", "folder:/photos", "--recursive"]
    assert words[9:12] == ["batch", "--sets", "P1-2"]
