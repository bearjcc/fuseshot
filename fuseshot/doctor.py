"""`fuseshot doctor`: what is installed, what is missing and where to get it."""

import importlib.util
import os
import platform
import subprocess
import sys

from . import __version__, assets, config, formats, jobs, limits, sources, tools

NEEDED, OPTIONAL = "needed", "optional"


def _python_has(python, module):
    """Whether another Python (the fill or group one from the settings) can import module."""
    if os.path.abspath(python) == os.path.abspath(sys.executable):
        return importlib.util.find_spec(module) is not None
    try:
        done = subprocess.run([python, "-c", f"import {module}"], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _opencv_packages():
    """The OpenCV packages installed here; more than one means they share, and fight over, cv2."""
    from importlib import metadata

    found = []
    for name in ("opencv-python", "opencv-python-headless", "opencv-contrib-python",
                 "opencv-contrib-python-headless"):
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        found.append(name)
    return found


def _torch_device(python):
    code = "import torch; print('cuda' if torch.cuda.is_available() else 'cpu')"
    try:
        done = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def home_too_long(home, windows=os.name == "nt"):
    """Work folders sit two levels under home; Hugin on Windows fails in long ones."""
    longest = len(os.path.abspath(str(home))) + len("/work/F20240101T120000_00")
    if windows and longest > tools.WIN_TOOL_DIR_MAX:
        return (f"{home} is long enough that its work folders would pass {tools.WIN_TOOL_DIR_MAX} characters, "
                "where Hugin on Windows stops working")
    return None


def check(args=None):
    """Everything doctor reports; args are the command line, for --source and --output-folder."""
    checks = []

    def add(name, state, detail, need=OPTIONAL, fix=None):
        if state == "missing" and need == NEEDED:
            state = "problem"
        checks.append({"name": name, "state": state, "detail": detail, "need": need, "fix": fix})

    problem = config.home_problem()
    add("home folder", "problem" if problem else "ok", problem or "can be written to", need=NEEDED)
    long_home = home_too_long(config.home())
    if long_home:
        add("home folder path", "problem", long_home, fix="use a shorter home folder, for example --home D:\\fs")
    settings_file = config.settings_path()
    unknown = config.unknown_keys()
    if unknown:
        add("settings", "problem", f"{settings_file} has settings Fuseshot does not know: {', '.join(unknown)}",
            fix=f"check the spelling; the known ones are {', '.join(config.KNOWN_KEYS)}")
    else:
        add("settings", "ok", str(settings_file) if settings_file.exists() else f"none yet ({settings_file})")

    for name, tool in tools.TOOLS.items():
        found = tools.find(name)
        if found is None:
            add(name, "missing", f"not found; used for {tool['for']}", fix=tool["get"],
                need=NEEDED if name == "hugin" else OPTIONAL)
            continue
        gaps = tools.missing_parts(name, found)
        if gaps:
            add(name, "problem", f"{found.parent}, but without {', '.join(gaps)}", fix=tool["get"])
        else:
            add(name, "ok", str(found.parent))

    add("viewer", "ok" if assets.viewer_ready() else "missing",
        str(assets.viewer_dir()) if assets.viewer_ready() else "downloaded on first use of the review page",
        fix="fuseshot fetch-assets viewer")

    for name, spec in assets.MODELS.items():
        state, path = assets.check_model(name)
        detail = {"ok": str(path), "custom": f"{path} (from the settings, not checked)",
                  "missing": f"downloaded on first use ({spec['size'] / 1e6:.1f} MB)",
                  "mismatch": f"{path} does not match the pinned checksum and will not be used"}[state]
        add(f"model {name}", "ok" if state in ("ok", "custom") else state, detail,
            fix=f"fuseshot fetch-assets {spec['for']}")

    card = limits.gpu()
    add("graphics card", "ok",
        f"{card['name']}, {card['vram_mb'] / 1024:.0f} GB: used for AI fill and COLMAP when their builds can"
        if card else "no NVIDIA card found; everything runs on the CPU")
    add("CPU and memory", "ok", f"{limits.cores()} cores, {limits.memory_mb() / 1024:.0f} GB; using "
        f"{limits.workers()} threads and up to {limits.budget_mb():,} MB (change with --workers and --memory-mb)")

    fill_python = jobs.python_for("fill")
    device = _torch_device(fill_python) if _python_has(fill_python, "torch") else None
    cuda_hint = ("pip install torch --index-url https://download.pytorch.org/whl/cu126 "
                 "(the CUDA build, for the graphics card)")
    add("AI fill (torch)", "ok" if device else "missing",
        f"runs on the {'GPU' if device == 'cuda' else 'CPU, slowly'} with {fill_python}"
        + (f"; for the graphics card: {cuda_hint}" if card and device == "cpu" else "") if device
        else f"torch not found in {fill_python}",
        fix=cuda_hint if card and device != "cuda" else jobs.extra_hint("fill", "torch"))

    group_python = jobs.python_for("group")
    has_mp = _python_has(group_python, "mediapipe")
    add("group merge (mediapipe)", "ok" if has_mp else "missing",
        f"with {group_python}" if has_mp else f"mediapipe not found in {group_python}",
        fix=jobs.extra_hint("group", "mediapipe"))

    opencv = _opencv_packages()
    if len(opencv) > 1:
        extra = [name for name in opencv if name != "opencv-contrib-python"]
        add("OpenCV", "problem", f"{', '.join(opencv)} are all installed and write over each other's cv2",
            need=NEEDED, fix=f"pip uninstall -y {' '.join(extra)}, then "
                             "pip install --force-reinstall opencv-contrib-python")

    heif = formats.heif_ready()
    add("HEIC photos", "ok" if heif else "missing", "readable" if heif else "not readable without pi-heif",
        fix="pip install pi-heif")
    has = importlib.util.find_spec("watchdog") is not None
    add("watchdog", "ok" if has else "missing", "fuseshot watch reacts to new photos at once" if has
        else "fuseshot watch falls back to rescanning on a timer", fix="pip install watchdog")

    try:
        given = _Args() if args is None else args
        chosen = getattr(given, "sources", None) or config.settings().get("sources")
        configured, output = sources.configured(given)
        has_key = False
        for source in configured:
            if source.kind == "immich":
                has_key = bool(os.environ.get("FUSESHOT_IMMICH_KEY")) or source.key_file.is_file()
                if not chosen and not has_key:
                    add("photos", "problem", "no photo folders set up yet", need=NEEDED,
                        fix="fuseshot init (or name one on each run with --source folder:PATH --recursive); "
                            "to read an Immich server instead, set FUSESHOT_IMMICH_KEY or write the key to "
                            + str(source.key_file))
                    continue
                add("Immich key", "ok" if has_key else "missing",
                    f"set ({'FUSESHOT_IMMICH_KEY' if os.environ.get('FUSESHOT_IMMICH_KEY') else source.key_file})"
                    if has_key else f"not set; needed to read {source.url}",
                    need=NEEDED, fix="set FUSESHOT_IMMICH_KEY or write the key to " + str(source.key_file))
            else:
                add(f"folder {source.path}", "ok" if source.path.is_dir() else "problem",
                    "found" if source.path.is_dir() else "does not exist", need=NEEDED)
        if output:
            add("output folder", "ok", str(output))
        from . import upload

        wanted = upload.settings()
        if wanted["immich"]:
            has_immich = any(source.kind == "immich" for source in configured)
            add("Immich uploads", "ok" if has_immich else "problem",
                ("on; kept Immich results become new photos"
                 + (f" in the album {wanted['album']!r}" if wanted["album"] else "")
                 + "; the key needs asset.upload" + (", album.read, album.create and albumAsset.create"
                                                     if wanted["album"] else ""))
                if has_immich else "on, but there is no Immich source", need=NEEDED)
        elif any(source.kind == "immich" for source in configured) and (chosen or has_key):
            add("Immich uploads", "ok", "off; Fuseshot only reads from Immich")
    except sources.SourceError as err:
        add("sources", "problem", str(err), need=NEEDED)

    try:
        runner = jobs.settings().get("runner", "local")
        if runner not in ("local", "pueue"):
            raise jobs.JobError(f"[jobs] runner must be local or pueue, not {runner!r}")
        add("job runner", "ok", runner)
    except jobs.JobError as err:
        add("job runner", "problem", str(err), need=NEEDED)

    return {"version": __version__, "python": sys.version.split()[0], "platform": platform.platform(),
            "home": str(config.home()), "checks": checks}


class _Args:
    """The parts of the command line sources.configured reads, left at their defaults."""
    sources = None
    output_folder = None
    key_file = None
    url = os.environ.get("FUSESHOT_IMMICH_URL", "http://127.0.0.1:2283")

    @property
    def db(self):
        return config.path("cache", "assets.sqlite")


def describe(found):
    marks = {"ok": "ok  ", "missing": "--  ", "problem": "!!  ", "mismatch": "!!  "}
    lines = [f"fuseshot {found['version']} on Python {found['python']}, {found['platform']}",
             f"home: {found['home']}", ""]
    width = max(len(c["name"]) for c in found["checks"])
    for c in found["checks"]:
        lines.append(f"{marks.get(c['state'], '?   ')}{c['name']:<{width}}  {c['detail']}")
        if c["state"] != "ok" and c["fix"]:
            lines.append(f"    {'':<{width}}  fix: {c['fix']}")
    lines += ["", "ok = ready, -- = optional and missing, !! = needs attention"]
    return "\n".join(lines)
