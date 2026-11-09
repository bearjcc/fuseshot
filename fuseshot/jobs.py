"""Run slow work (AI fill, group re-merges, stitching) away from the review server.

By default jobs run one at a time on a background thread of the review server.
They can go to a Pueue queue instead (https://github.com/Nukesor/pueue), set in
the [jobs] table of the settings file:

    [jobs]
    runner = "pueue"
    pueue = "pueue"              # the pueue program, if it is not on PATH
    group = "default"
    label_prefix = "fuseshot:"
    # A command to queue GPU jobs with instead, for a wrapper of your own. It
    # gets {name}, {label}, {workdir} and {command}, and must print the Pueue
    # task id on its last line.
    # gpu_submit = "..."
    # Sets `run` keeps queued for stitching at a time when the runner is pueue.
    # stitch_window = 2
    # Pythons that have the fill (torch) and group (mediapipe) extras, if they
    # are not this one.
    # fill_python = "..."
    # group_python = "..."
"""

import itertools
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, log

LOG = log.get(__name__)
def package_root():
    from . import runtime

    return runtime.package_root()


PACKAGE_ROOT = package_root()


def settings():
    return config.section("jobs")


def extra_hint(extra, module):
    """How to add the fill or group extra to this install, for error messages and doctor."""
    setting = f"{extra}_python under [jobs]"
    parts = Path(sys.prefix).parts
    if "pipx" in parts and "venvs" in parts:
        how = f"pipx inject fuseshot {module}"
    elif getattr(sys, "frozen", False):
        how = f"install {module} with pip somewhere else"
    else:
        python = f'"{sys.executable}"' if " " in sys.executable else sys.executable
        how = f"{python} -m pip install {module}"
    return f"{how}, or set {setting} to a Python that has {module}"


def python_for(kind):
    value = settings().get(f"{kind}_python")
    return os.path.expanduser(value) if value else sys.executable


def child_env():
    env = dict(os.environ, PYTHONIOENCODING="utf-8", FUSESHOT_HOME=str(config.home()))
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(package_root()), env.get("PYTHONPATH")]))
    return env


def argv(kind, args):
    if kind in ("fill", "group"):
        return [str(python_for(kind)), "-u", "-m", "fuseshot", *[str(a) for a in args]]
    from .runtime import fuseshot_argv

    return fuseshot_argv(args)


def shell_line(kind, args):
    """The job as one shell line, for queues that run commands through a shell."""
    words = argv(kind, args)
    if os.name == "nt":
        def quote(s):
            return "'" + s.replace("'", "''") + "'"

        return (f"$env:PYTHONPATH={quote(str(PACKAGE_ROOT))}; $env:PYTHONIOENCODING='utf-8'; "
                f"$env:FUSESHOT_HOME={quote(str(config.home()))}; "
                f"& {' '.join(quote(w) for w in words)}; exit $LASTEXITCODE")
    return (f"PYTHONPATH={shlex.quote(str(PACKAGE_ROOT))} PYTHONIOENCODING=utf-8 "
            f"FUSESHOT_HOME={shlex.quote(str(config.home()))} {shlex.join(words)}")


class JobError(RuntimeError):
    pass


class LocalRunner:
    """One worker thread; job state lives in memory and is lost on restart."""

    def __init__(self):
        self.ids = itertools.count(int(time.time()))
        self.jobs = {}
        self.waiting = queue.Queue()
        self.lock = threading.Lock()
        self.thread = None

    def submit(self, kind, name, args, gpu=False):
        task = next(self.ids)
        with self.lock:
            self.jobs[task] = {"label": name, "state": "queued", "kind": kind, "args": args}
            if self.thread is None:
                self.thread = threading.Thread(target=self.work, daemon=True, name="fuseshot-jobs")
                self.thread.start()
        self.waiting.put(task)
        return task

    def work(self):
        while True:
            task = self.waiting.get()
            job = self.jobs[task]
            with self.lock:
                if job["state"] == "cancelled":
                    continue
                job["state"] = "running"
            log_file = config.path("cache", "jobs", f"{job['label']}.log")
            log_file.parent.mkdir(parents=True, exist_ok=True)
            LOG.info("job %s started: %s", task, job["label"])
            try:
                with open(log_file, "w", encoding="utf-8", errors="replace") as out:
                    code = subprocess.run(argv(job["kind"], job["args"]), stdout=out, stderr=subprocess.STDOUT,
                                          env=child_env(), cwd=config.home()).returncode
            except OSError as err:
                LOG.error("job %s could not start: %s", task, err)
                code = -1
            job["state"] = "done" if code == 0 else "failed"
            log_fn = LOG.info if code == 0 else LOG.warning
            log_fn("job %s %s: %s (log in %s)", task, job["state"], job["label"], log_file)

    def status(self, task):
        job = self.jobs.get(int(task))
        return job["state"] if job else "lost"

    def active(self, name):
        for task, job in self.jobs.items():
            if job["label"] == name and job["state"] in ("queued", "running"):
                return task, job["state"]
        return None

    def cancel(self, names):
        """Drop the jobs with these names that have not started; returns their task ids."""
        dropped = []
        with self.lock:
            for task, job in self.jobs.items():
                if job["label"] in names and job["state"] == "queued":
                    job["state"] = "cancelled"
                    dropped.append(task)
        return dropped


class PueueRunner:
    def __init__(self, conf):
        self.pueue = conf.get("pueue") or "pueue"
        self.group = conf.get("group") or "default"
        self.prefix = conf.get("label_prefix", "fuseshot:")
        self.gpu_submit = conf.get("gpu_submit")

    def submit(self, kind, name, args, gpu=False):
        command = shell_line(kind, args)
        workdir = str(config.home())
        if gpu and self.gpu_submit:
            quoted = "'" + command.replace("'", "''") + "'" if os.name == "nt" else shlex.quote(command)
            line = self.gpu_submit.format(name=name, label=self.prefix + name, workdir=workdir, command=quoted)
            shell = (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", line]
                     if os.name == "nt" else ["sh", "-c", line])
        else:
            shell = [self.pueue, "add", "-p", "-g", self.group, "-l", self.prefix + name, "-w", workdir,
                     "--", command]
        try:
            done = subprocess.run(shell, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as err:
            raise JobError(f"could not queue: {err}") from None
        lines = done.stdout.strip().splitlines()
        if done.returncode != 0 or not lines or not lines[-1].strip().isdigit():
            raise JobError("could not queue: " + (done.stderr or done.stdout).strip()[-300:])
        return int(lines[-1])

    def tasks(self):
        try:
            done = subprocess.run([self.pueue, "status", "--json"], capture_output=True, text=True, timeout=20)
            return json.loads(done.stdout).get("tasks", {})
        except (OSError, ValueError, subprocess.SubprocessError):
            return {}

    @staticmethod
    def waiting(task):
        """Queued or stashed: Pueue has not started it."""
        status = task.get("status")
        return (next(iter(status)) if isinstance(status, dict) else status) in ("Queued", "Stashed")

    @staticmethod
    def state(task):
        status = task.get("status")
        name = next(iter(status)) if isinstance(status, dict) else status
        if name == "Done":
            result = status["Done"].get("result") if isinstance(status, dict) else None
            return "done" if result == "Success" else "failed"
        return {"Queued": "queued", "Stashed": "queued", "Locked": "queued", "Paused": "queued",
                "Running": "running"}.get(name, "queued")

    def status(self, task):
        found = self.tasks().get(str(task))
        return self.state(found) if found else None

    def active(self, name):
        for key, task in self.tasks().items():
            if task.get("label") == self.prefix + name and self.state(task) in ("queued", "running"):
                return int(key), self.state(task)
        return None

    def cancel(self, names):
        """Remove the tasks with these names that Pueue has not started; returns their task ids."""
        labels = {self.prefix + name for name in names}
        dropped = [int(k) for k, t in self.tasks().items() if t.get("label") in labels and self.waiting(t)]
        if not dropped:
            return []
        try:
            done = subprocess.run([self.pueue, "remove", *map(str, dropped)], capture_output=True, text=True,
                                  timeout=20)
        except (OSError, subprocess.SubprocessError) as err:
            LOG.warning("could not take tasks %s off the queue: %s", dropped, err)
            return []
        if done.returncode != 0:
            LOG.warning("pueue remove %s: %s", dropped, (done.stderr or done.stdout).strip()[-300:])
        left = self.tasks()
        return [task for task in dropped if str(task) not in left]


_runner = None
_runner_lock = threading.Lock()


def runner():
    global _runner
    with _runner_lock:
        if _runner is None:
            conf = settings()
            kind = conf.get("runner", "local")
            if kind not in ("local", "pueue"):
                raise JobError(f"[jobs] runner must be local or pueue, not {kind!r}")
            _runner = PueueRunner(conf) if kind == "pueue" else LocalRunner()
        return _runner
