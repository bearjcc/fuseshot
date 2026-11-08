"""When to try a failed result again.

A failure records the code that failed (a fingerprint of the modules that do the work)
and the parameters it ran with. The next run tries it again only if either has changed
since, and at most MAX_ATTEMPTS times in all. Retry on the Failures page clears the count
and asks for one more try whatever changed.
"""

import hashlib
import json
from datetime import datetime
from functools import cache
from pathlib import Path

from .files import write_atomic

MAX_ATTEMPTS = 3
PACKAGE = Path(__file__).resolve().parent


@cache
def code_version(*modules):
    """A short fingerprint of the source of these fuseshot modules."""
    h = hashlib.sha1()
    for name in modules:
        h.update((PACKAGE / f"{name}.py").read_bytes())
    return h.hexdigest()[:12]


def record(error, code, params, old=None):
    """The fields to save with a failure, counting attempts on from an earlier failure."""
    attempts = (old or {}).get("attempts", 0) + 1
    return {"error": str(error)[:400], "code": code, "params": params, "attempts": attempts,
            "failed_at": datetime.now().isoformat(timespec="seconds")}


def should_retry(old, code, params):
    """True when a failure is worth another go: asked for by hand, or the code or the
    parameters changed and the attempts are not used up."""
    if not old:
        return True
    if old.get("retry"):
        return True
    if old.get("attempts", 1) >= MAX_ATTEMPTS:
        return False
    return old.get("code") != code or old.get("params") != params


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def request(path):
    """Ask for one more try of the failure saved at path, with a fresh count."""
    found = read(path)
    if found is None:
        raise KeyError(str(path))
    found.update(retry=True, attempts=0)
    write_atomic(path, json.dumps(found, indent=1))
    return found
