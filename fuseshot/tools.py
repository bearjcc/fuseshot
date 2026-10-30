"""Find the outside programs Fuseshot runs: Hugin, COLMAP and Brush.

Each is looked for, in order, at the FUSESHOT_<NAME> environment variable, the
[tools] table of the settings file, the tools folder in the home folder, the
usual install places and finally PATH. A setting may name the program itself
or the folder holding it. None of them is shipped with Fuseshot.
"""

import os
import shutil
from pathlib import Path

from . import config

EXE = ".exe" if os.name == "nt" else ""
TOOLS = {
    "hugin": {
        "program": "nona",
        "needed": ("nona", "cpfind", "cpclean", "autooptimiser", "pano_modify", "pto_var", "checkpto",
                   "enblend", "enfuse"),
        "under_home": ("hugin/*/bin", "hugin/bin", "hugin*/bin"),
        "installed": (r"%ProgramW6432%\Hugin\bin", r"%ProgramFiles%\Hugin\bin", r"%ProgramFiles(x86)%\Hugin\bin",
                      r"C:\Program Files\Hugin\bin", "/usr/bin", "/usr/local/bin"),
        "for": "stitching panoramas",
        "get": "https://hugin.sourceforge.io/download/ (Linux: apt install hugin-tools enblend enfuse)",
    },
    "colmap": {
        "program": "colmap",
        "needed": ("colmap",),
        "under_home": ("colmap*/bin", "colmap*"),
        "installed": (r"%ProgramFiles%\COLMAP\bin", r"%ProgramFiles%\COLMAP", r"C:\Program Files\COLMAP\bin",
                      "/usr/local/bin", "/usr/bin"),
        "for": "camera poses for splats",
        "get": "https://github.com/colmap/colmap/releases (3.12 or newer; distribution packages are often older)",
    },
    "brush": {
        "program": "brush_app",
        "needed": ("brush_app",),
        "under_home": ("brush*",),
        "installed": (),
        "for": "training splats",
        "get": "https://github.com/ArthurBrussee/brush/releases",
    },
}

# Hugin's tools on Windows are not long-path aware, and Windows will not start a
# program in a folder whose path passes 260 characters even when long paths are
# turned on. Leave room for the file names inside the work folder.
WIN_TOOL_DIR_MAX = 200


class ToolMissing(RuntimeError):
    pass


def _program_in(folder, program):
    path = Path(folder) / (program + EXE)
    return path if path.is_file() else None


def _given(value, program):
    path = Path(value).expanduser()
    return path if path.is_file() else _program_in(path, program)


def find(name):
    """Full path of the tool's main program, or None."""
    tool = TOOLS[name]
    program = tool["program"]
    for value in (os.environ.get(f"FUSESHOT_{name.upper()}"), config.section("tools").get(name)):
        if value:
            return _given(value, program)
    tools_dir = config.path("tools")
    for pattern in tool["under_home"]:
        for folder in sorted(tools_dir.glob(pattern)) if tools_dir.is_dir() else ():
            found = _program_in(folder, program)
            if found:
                return found
    for folder in tool["installed"]:
        if "%" in folder:
            if os.name != "nt":
                continue
            folder = os.path.expandvars(folder)
            if "%" in folder:  # the variable is not set
                continue
        found = _program_in(folder, program)
        if found:
            return found
    found = shutil.which(program)
    return Path(found) if found else None


def require(name):
    found = find(name)
    if found is None:
        tool = TOOLS[name]
        raise ToolMissing(f"{name} is needed for {tool['for']} but was not found. Get it from {tool['get']}, "
                          f"then put it on PATH, set FUSESHOT_{name.upper()} or add {name} = '...' under "
                          f"[tools] in {config.settings_path()}")
    return found


def missing_parts(name, found):
    """Programs the tool needs that are not next to its main program."""
    folder = found.parent
    return [p for p in TOOLS[name]["needed"] if not _program_in(folder, p) and not shutil.which(p)]
