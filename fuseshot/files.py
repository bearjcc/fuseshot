"""Small file helpers shared by the stitcher, the batch runner and the review page."""

import os
import tempfile
import time
from pathlib import Path

TRIES = 20


def retry_busy(do, tries=TRIES):
    """Run do(), trying again for a moment while Windows says another thread has the file open."""
    for attempt in range(tries):
        try:
            return do()
        except PermissionError:
            if os.name != "nt" or attempt == tries - 1:
                raise
            time.sleep(0.05)


def read_text(path):
    """Read a file another thread may be replacing with write_atomic."""
    return retry_busy(lambda: Path(path).read_text(encoding="utf-8"))


def write_atomic(path, data):
    """Write text or bytes so readers only ever see the old file or the whole new one."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        retry_busy(lambda: os.replace(tmp, path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
