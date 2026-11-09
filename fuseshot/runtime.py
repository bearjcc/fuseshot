"""Paths and defaults when Fuseshot runs from a packaged Windows build (PyInstaller)."""

import os
import sys
from pathlib import Path


def frozen():
    return bool(getattr(sys, "frozen", False))


def install_dir():
    """Folder holding Fuseshot.exe and bundled tools (not the per-user data folder)."""
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundle_dir():
    """Unpackaged Python code and static assets inside a frozen build."""
    if frozen():
        return Path(getattr(sys, "_MEIPASS", install_dir()))
    return Path(__file__).resolve().parent.parent


def package_root():
    """Root on PYTHONPATH for child Python jobs."""
    if frozen():
        return bundle_dir()
    return Path(__file__).resolve().parent.parent


def packaged_tool_bins():
    """Redistributable tool folders shipped next to the executable (Hugin only today)."""
    root = install_dir() / "tools"
    if not root.is_dir():
        return []
    found = []
    for pattern in ("hugin/bin", "hugin/*/bin"):
        for folder in sorted(root.glob(pattern)):
            if folder.is_dir():
                found.append(folder)
    return found


def fuseshot_argv(args, home=None):
    """Command line to run fuseshot subcommands (dev install or frozen exe)."""
    from . import config

    home = str(home or config.home())
    prefix = ["--home", home]
    tail = [str(a) for a in args]
    if frozen():
        return [sys.executable, *prefix, *tail]
    return [sys.executable, "-u", "-m", "fuseshot", *prefix, *tail]


def seed_environment():
    """Tell child processes and finders where the packaged tree lives."""
    if not frozen():
        return
    home = os.environ.get("FUSESHOT_HOME")
    if home:
        os.environ.setdefault("FUSESHOT_HOME", home)
    bindir = packaged_tool_bins()
    if bindir:
        os.environ.setdefault("FUSESHOT_HUGIN", str(bindir[0]))
    os.environ.setdefault("FUSESHOT_REVIEW_HOST", "127.0.0.1")
