"""Progress for the long stages: how far along, and an honest time left.

The time left comes from a smoothed rate for this stage alone, so a quick stage
before it (or files that were already done) does not make the estimate look
better than it is. On a terminal it is a bar; in a log (a Pueue task, a
scheduled run) it is a line every half minute. The latest state is also written
to progress.json in the output folder for the review page.
"""

import json
import os
import sys
import time
from pathlib import Path

from . import log

LOG = log.get(__name__)
STATE = None  # where progress.json goes; set by the command line


def duration(seconds):
    if seconds is None:
        return "working it out"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min"
    return f"{seconds // 3600} h {round(seconds % 3600 / 60):02d} min"


class Progress:
    def __init__(self, stage, total, unit="photos", every=30.0, smoothing=0.1):
        self.stage, self.total, self.unit = stage, total, unit
        self.done = 0
        self.every = every
        self.smoothing = smoothing
        self.started = self.mark = time.monotonic()
        self.mark_done = 0
        self.rate = None
        self.bar = None
        self.written = 0.0
        if sys.stderr.isatty() and total and LOG.getEffectiveLevel() <= 20:
            from tqdm import tqdm
            self.bar = tqdm(total=total, desc=stage, unit=f" {unit}", smoothing=smoothing, dynamic_ncols=True,
                            leave=False, mininterval=0.5)

    def __enter__(self):
        if self.bar is not None:
            from tqdm.contrib.logging import logging_redirect_tqdm
            self._redirect = logging_redirect_tqdm(loggers=[log.LOG])
            self._redirect.__enter__()
        return self

    def __exit__(self, *exc):
        if self.bar is not None:
            self.bar.close()
            self._redirect.__exit__(*exc)
        self._save(finished=exc[0] is None)
        return False

    def _tick(self, now):
        span = now - self.mark
        if span <= 0:
            return
        recent = (self.done - self.mark_done) / span
        self.rate = recent if self.rate is None else self.rate + self.smoothing * 3 * (recent - self.rate)
        self.mark, self.mark_done = now, self.done

    def left(self):
        if not self.total or not self.rate:
            return None
        return max(0.0, (self.total - self.done) / self.rate)

    def update(self, n=1):
        self.done += n
        if self.bar is not None:
            self.bar.update(n)
        now = time.monotonic()
        if now - self.mark >= min(5.0, self.every):
            self._tick(now)
        if now - self.written >= self.every:
            self.written = now
            if self.bar is None and self.total and now - self.started >= self.every:
                left = self.left()
                LOG.info(f"  {self.stage}: {self.done:,} of {self.total:,} {self.unit}, "
                         + (f"about {duration(left)} left" if left is not None else "time left not known yet"))
            self._save()

    def _save(self, finished=False):
        if STATE is None:
            return
        state = {"stage": self.stage, "done": self.done, "total": self.total, "unit": self.unit,
                 "seconds_left": None if finished else self.left(), "finished": finished,
                 "elapsed": round(time.monotonic() - self.started, 1), "at": time.time()}
        try:
            path = Path(STATE)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass
