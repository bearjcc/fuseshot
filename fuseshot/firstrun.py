"""fuseshot init: ask for the photo folders and the output folder once, and write fuseshot.toml."""

import os
import shutil
import sys
from pathlib import Path

from . import config, log
from .files import write_atomic
from .sources import DEFAULT_OUTPUT, same_or_inside

LOG = log.get(__name__)


def _quote(text):
    return "'" + text + "'" if "'" not in text and "\n" not in text else '"' + text.replace("\\", "\\\\").replace(
        '"', '\\"') + '"'


def _value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {_value(v)}" for k, v in value.items()) + "}"
    return _quote(str(value))


def to_toml(settings):
    """TOML for the plain settings Fuseshot uses: values, tables and lists of tables."""
    lines, tables = [], []
    for key, value in settings.items():
        if isinstance(value, dict) or (isinstance(value, list) and value and all(isinstance(v, dict) for v in value)):
            tables.append((key, value))
        else:
            lines.append(f"{key} = {_value(value)}")
    for key, value in tables:
        for item in value if isinstance(value, list) else [value]:
            lines += ["", f"[[{key}]]" if isinstance(value, list) else f"[{key}]"]
            lines += [f"{k} = {_value(v)}" for k, v in item.items()]
    return "\n".join(lines).lstrip("\n") + "\n"


def pick_folder(title):
    """A folder chooser window, where there is a screen to show it on; else None."""
    if os.name != "nt" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return None
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        return None
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(title=title, mustexist=True)
        root.destroy()
    except tkinter.TclError:
        return None
    return chosen or None


def ask(prompt):
    try:
        return input(prompt).strip().strip('"')
    except EOFError:
        return ""


def ask_folders():
    folders = []
    print("Which folders hold the photos? Every folder below each one is read too.")
    while True:
        first = not folders
        answer = ask("Photo folder (type a path, or press Enter to browse): " if first else
                     "Another folder (type a path, b to browse, or press Enter when done): ")
        if not answer and not first:
            return folders
        if not answer or answer.lower() == "b":
            answer = pick_folder("Folder with photos")
            if not answer:
                print("No folder window here; type the path instead.")
                continue
            print(f"  {answer}")
        path = Path(answer).expanduser()
        if not path.is_dir():
            print(f"  {path} is not a folder.")
            continue
        folders.append(str(path.resolve()))


def check(folders, output):
    for folder in folders:
        if not Path(folder).is_dir():
            raise SystemExit(f"{folder} is not a folder")
        if same_or_inside(output, Path(folder)):
            raise SystemExit(f"the output folder {output} is inside the photo folder {folder}; "
                             "pick one outside it so results never mix with the originals")


def run(args):
    path = config.settings_path()
    old = config.load(path) if path.exists() else {}
    if old.get("sources") and not args.force and not args.folders:
        print(f"{path} already lists photo folders; run `fuseshot init --force` to choose again.")
        return
    folders = [str(Path(f).expanduser().resolve()) for f in args.folders]
    interactive = not folders and sys.stdin.isatty()
    if interactive:
        folders = ask_folders()
    if not folders:
        raise SystemExit("no photo folders given; try: fuseshot init PATH [PATH ...]")
    output = args.output_folder or old.get("output")
    if interactive and not args.output_folder:
        default = Path(output or DEFAULT_OUTPUT).expanduser()
        answer = ask(f"Where should kept results go? (Enter for {default}, b to browse): ")
        if answer.lower() == "b":
            answer = pick_folder("Folder for Fuseshot results") or ""
        output = answer or str(default)
    output = Path(os.path.abspath(os.path.expanduser(str(output or DEFAULT_OUTPUT))))
    check(folders, output)
    settings = {k: v for k, v in old.items() if k not in ("output", "sources")}
    settings = {"output": str(output), "sources": [{"type": "folder", "path": f, "recursive": True}
                                                  for f in folders], **settings}
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    write_atomic(path, "# Written by fuseshot init; safe to edit.\n" + to_toml(settings))
    print(f"Saved {path}")
    for f in folders:
        print(f"  photos: {f} and every folder below it")
    print(f"  results: {output}")
    print("Next: `fuseshot doctor` to check Hugin, then `fuseshot run` (try `fuseshot run --year 2023` first "
          "for a quick look at one year).")
