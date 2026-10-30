"""Logging setup: messages go to stderr, results to stdout."""

import logging
import sys

LOG = logging.getLogger("fuseshot")


def setup(verbosity=0):
    level = logging.WARNING if verbosity < 0 else logging.DEBUG if verbosity > 0 else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s" if verbosity > 0 else "%(message)s"
    handler.setFormatter(logging.Formatter(fmt, "%H:%M:%S"))
    LOG.handlers[:] = [handler]
    LOG.setLevel(level)
    LOG.propagate = False


def get(name):
    return LOG.getChild(name.rsplit(".", 1)[-1])
