"""Background library scan from the review server (no terminal needed).

The desktop app starts `fuseshot run` as a child process with the right argv
for a frozen build. Pause touches pipeline.stop; cmd_run checks between steps
and exits cleanly so the user can resume later.
"""

import json
import os
import subprocess
import threading
import time

from . import config, jobs, log, runtime
from .files import write_atomic

LOG = log.get(__name__)
STATE_NAME = "pipeline.json"
STOP_NAME = "pipeline.stop"
LOG_NAME = "run.log"


def state_path():
    return config.home() / "output" / STATE_NAME


def stop_path():
    return config.home() / "output" / STOP_NAME


def log_path():
    return config.home() / "output" / LOG_NAME


def stop_requested():
    return stop_path().is_file() or (config.home() / "work" / "batch.stop").exists()


def clear_stop():
    for path in (stop_path(), config.home() / "work" / "batch.stop"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def touch_stop():
    path = stop_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def read_state():
    path = state_path()
    if not path.is_file():
        return {"state": "idle"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "idle"}
    return data if isinstance(data, dict) else {"state": "idle"}


def write_state(**fields):
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {**read_state(), **fields, "at": time.time()}
    write_atomic(path, json.dumps(data, indent=1).encode("utf-8"))


def mark_paused(where):
    write_state(state="paused", paused_at=where, error=None)


def checkpoint(where):
    """True when the user asked to pause before the next step."""
    if not stop_requested():
        return False
    mark_paused(where)
    LOG.info("Pipeline paused at %s", where)
    return True


_lock = threading.Lock()
_proc = None
_watcher = None


def active():
    global _proc
    if _proc is not None and _proc.poll() is None:
        return True
    data = read_state()
    return data.get("state") in ("running", "starting")


def _watch_process(proc):
    global _proc, _watcher
    code = proc.wait()
    with _lock:
        mine = _proc is proc
        if mine:
            _proc = None
            _watcher = None
    if not mine:
        return
    paused = read_state().get("state") == "paused" or stop_requested()
    if paused:
        write_state(state="paused")
        LOG.info("Pipeline scan paused")
    elif code == 0:
        clear_stop()
        write_state(state="done", error=None)
        LOG.info("Pipeline scan finished")
    else:
        write_state(state="failed", exit_code=code, error=f"scan stopped with exit code {code}")
        LOG.warning("Pipeline scan failed with exit code %s", code)


def start():
    """Start `fuseshot run` when nothing is already going."""
    global _proc, _watcher
    from .settings_web import SetupError, needs_setup

    if active():
        raise SetupError("a scan is already running")
    if needs_setup():
        raise SetupError("save settings first: pick at least one folder or Immich, and an output folder")
    clear_stop()
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    cmd = runtime.fuseshot_argv(["run"])
    write_state(state="starting", stage=None, log=str(log), error=None)
    with _lock:
        with open(log, "ab") as out:
            _proc = subprocess.Popen(
                cmd,
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=jobs.child_env(),
                creationflags=flags,
                start_new_session=os.name != "nt",
            )
        _watcher = threading.Thread(target=_watch_process, args=(_proc,), daemon=True, name="fuseshot-pipeline")
        _watcher.start()
    write_state(state="running")
    return {"started": True, "log": str(log)}


def pause():
    from .settings_web import SetupError

    if not active():
        raise SetupError("no scan is running")
    touch_stop()
    work = config.home() / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "batch.stop").touch()
    return {"paused": True}


def resume():
    from .settings_web import SetupError

    if active():
        raise SetupError("a scan is already running")
    clear_stop()
    return start()


def log_tail(max_bytes=65536):
    path = log_path()
    if not path.is_file():
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def status():
    from .settings_web import needs_setup

    data = read_state()
    run = active()
    prog = None
    progress = config.home() / "output" / "progress.json"
    if progress.is_file():
        try:
            prog = json.loads(progress.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prog = None
    if run:
        data["state"] = "running"
    elif data.get("state") == "running":
        data["state"] = "idle" if data.get("exit_code") in (None, 0) else "failed"
    paused = data.get("state") == "paused" and not run
    return {
        **data,
        "active": run,
        "log": str(log_path()),
        "log_tail": log_tail(12000),
        "progress": prog,
        "frozen": runtime.frozen(),
        "needs_setup": needs_setup(),
        "can_pause": run,
        "can_resume": paused,
        "can_start": not run and not paused and data.get("state") != "running",
    }
