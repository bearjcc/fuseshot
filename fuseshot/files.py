"""Small file helpers shared by the stitcher, the batch runner and the review page."""

import os
import tempfile
from pathlib import Path


def write_atomic(path, data):
    """Write text or bytes so readers only ever see the old file or the whole new one."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
