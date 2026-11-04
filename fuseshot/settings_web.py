"""The settings page: where the photos come from and what to make of them.

The first time the review page opens with nothing set up, it shows this page in place
of the empty grid. The Immich key goes to secrets/immich_key under the home folder,
never into fuseshot.toml, and the page is only ever told whether one is saved.
"""

import os
import shutil
import string
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from . import config, jobs, tools
from .files import write_atomic

FIELDS = {"immich", "folders", "output", "outputs"}
BROWSE_MAX = 400
FORWARDED = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "Forwarded",
             "Tailscale-User-Login")
# What each kind of result needs beyond Fuseshot itself.
OUTPUTS = {
    "panoramas": {"label": "Panoramas", "tools": ("hugin",)},
    "splats": {"label": "Object scans (splats)", "tools": ("colmap", "brush")},
    "loops": {"label": "Action loops, motion photos too", "ffmpeg": True},
    "groups": {"label": "Best-face group shots", "module": ("group", "mediapipe")},
    "wigglegrams": {"label": "Wigglegrams from stereo photos", "ffmpeg": True},
    "composites": {"label": "Action composites"},
    "enhance": {"label": "Enhance candidates (photos restoration could help)"},
    "colourise": {"label": "Suggest colourising black-and-white scans (optional model pack)"},
}


class SetupError(ValueError):
    pass


def local_request(client, headers):
    """A request made on this computer and not passed on by a proxy (Tailscale serve, nginx),
    which would make every visitor look local."""
    return client in ("127.0.0.1", "::1", "::ffff:127.0.0.1") and not any(headers.get(h) for h in FORWARDED)


def key_file():
    return config.path("secrets", "immich_key")


def immich_spec(found=None):
    found = config.settings() if found is None else found
    return next((s for s in found.get("sources") or [] if s.get("type") == "immich"), None)


def key_saved(spec=None):
    if os.environ.get("FUSESHOT_IMMICH_KEY", "").strip():
        return True
    path = Path((spec or {}).get("key_file") or key_file()).expanduser()
    return path.is_file() and bool(path.read_text(encoding="utf-8").strip())


def needs_setup(cli_sources=False):
    """Nothing to scan yet: no sources on the command line or in the settings file, and no key."""
    return not (cli_sources or config.settings().get("sources") or key_saved())


def state(cli_sources=False):
    found = config.settings()
    spec = immich_spec(found)
    folders = [{"path": s.get("path", ""), "recursive": bool(s.get("recursive", False))}
               for s in found.get("sources") or [] if s.get("type") == "folder"]
    url = (spec or {}).get("url") or os.environ.get("FUSESHOT_IMMICH_URL", "")
    from .sources import DEFAULT_OUTPUT

    return {
        "needs_setup": needs_setup(cli_sources),
        "cli_sources": bool(cli_sources),
        "immich": {"on": spec is not None or (not found.get("sources") and key_saved()), "url": url,
                   "key_saved": key_saved(spec)},
        "folders": folders,
        "output": str(found.get("output") or DEFAULT_OUTPUT),
        "outputs": {name: config.wants(name) for name in OUTPUTS},
        "labels": {name: o["label"] for name, o in OUTPUTS.items()},
        "unknown": config.unknown_keys(found),
        "settings_file": str(config.settings_path()),
        "home": str(config.home()),
    }


def output_status():
    """Installed or missing, for each kind of result, with how to add what is missing.
    Slower than state(): it may start the group Python to look for mediapipe."""
    from .doctor import _python_has

    found = {}
    for name, needs in OUTPUTS.items():
        missing, hints = [], []
        for tool in needs.get("tools", ()):
            where = tools.find(tool)
            gaps = [tool] if where is None else tools.missing_parts(tool, where)
            if gaps:
                missing += gaps
                hints.append(f"{tool}: {tools.TOOLS[tool]['get']}")
        if "module" in needs:
            extra, module = needs["module"]
            if not _python_has(jobs.python_for(extra), module):
                missing.append(module)
                hints.append(jobs.extra_hint(extra, module))
        note = None
        if needs.get("ffmpeg") and not shutil.which("ffmpeg"):
            note = "Without ffmpeg there is no MP4, only WebP and GIF: https://ffmpeg.org/download.html"
        found[name] = {"ready": not missing, "missing": missing, "hints": hints, "note": note}
    return found


def check_url(url):
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SetupError("the Immich address starts with http:// or https://, like http://192.168.1.10:2283")
    if parts.username or parts.password:
        raise SetupError("leave the user name and password out of the Immich address; the API key signs in")
    return url.strip().rstrip("/")


def test_immich(body):
    """Sign in to Immich with the key typed in, else the saved one: whose library, and how big."""
    from .immich import Immich, ImmichError

    url = check_url(body.get("url"))
    key = (body.get("key") or "").strip()
    if not key:
        from .sources import SourceError, read_key

        try:
            key = read_key((immich_spec() or {}).get("key_file") or key_file())
        except SourceError:
            raise SetupError("type in an API key to test") from None
    client = Immich(url, key, timeout=10)
    try:
        me = client._request("GET", "/api/users/me", retries=1)
        stats = client._request("GET", "/api/assets/statistics", retries=1)
    except ImmichError as err:
        text = str(err)
        if " 401" in text or " 403" in text:
            text = "Immich did not take that key"
        return {"ok": False, "error": text}
    return {"ok": True, "user": me.get("name") or me.get("email") or "", "assets": stats.get("total"),
            "images": stats.get("images"), "videos": stats.get("videos")}


def roots():
    if os.name == "nt":
        return [f"{d}:\\" for d in string.ascii_uppercase if os.path.isdir(f"{d}:\\")]
    found = ["/", str(Path.home())]
    return found + [p for p in ("/media", "/mnt", "/srv") if os.path.isdir(p)]


def browse(body):
    """The folders in a folder, for picking a source; files are never listed."""
    where = (body.get("path") or "").strip()
    if not where:
        return {"path": "", "parent": None, "folders": [], "roots": roots()}
    path = Path(os.path.expanduser(where))
    if not path.is_absolute():
        raise SetupError("give the whole path to the folder, starting from a drive or /")
    path = Path(os.path.abspath(path))
    if not path.is_dir():
        raise SetupError(f"there is no folder at {path}")
    names = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith((".", "$")):
                    continue
                try:
                    if entry.is_dir():
                        names.append(entry.name)
                except OSError:
                    continue
    except OSError as err:
        raise SetupError(f"cannot look inside {path}: {err.strerror or err}") from None
    names.sort(key=str.lower)
    parent = str(path.parent) if path.parent != path else None
    return {"path": str(path), "parent": parent, "folders": names[:BROWSE_MAX],
            "more": max(0, len(names) - BROWSE_MAX), "roots": roots()}


def check(body):
    """The settings asked for, checked: (immich or None, key or None, folders, output, outputs)."""
    unknown = set(body) - FIELDS
    if unknown:
        raise SetupError(f"unknown settings: {', '.join(sorted(unknown))}")
    immich = body.get("immich") or {}
    on = bool(immich.get("on"))
    url = check_url(immich.get("url")) if on else None
    key = (immich.get("key") or "").strip() or None
    if key and (len(key) > 200 or any(c.isspace() for c in key)):
        raise SetupError("that does not look like an Immich API key")
    if on and not key and not key_saved(immich_spec()):
        raise SetupError("Immich needs an API key: make one in Immich under Account Settings > API Keys")
    folders = []
    for folder in body.get("folders") or []:
        path = Path(os.path.expanduser(str(folder.get("path") or "").strip()))
        if not path.is_absolute() or not path.is_dir():
            raise SetupError(f"there is no folder at {folder.get('path')!r}")
        folders.append({"type": "folder", "path": str(path), "recursive": bool(folder.get("recursive"))})
    if not on and not folders:
        raise SetupError("pick at least one place to scan: Immich or a folder")
    output = str(body.get("output") or "").strip()
    if output:
        output = os.path.abspath(os.path.expanduser(output))
        from .sources import same_or_inside

        for folder in folders:
            if same_or_inside(output, folder["path"]):
                raise SetupError(f"the output folder would sit among the originals in {folder['path']}")
    outputs = body.get("outputs") or {}
    if set(outputs) - set(OUTPUTS):
        raise SetupError(f"unknown results: {', '.join(sorted(set(outputs) - set(OUTPUTS)))}")
    return url if on else None, key, folders, output, {k: bool(v) for k, v in outputs.items()}


def save(body, cli_sources=False):
    """Write fuseshot.toml (keeping its comments and anything else in it) and the key file."""
    import tomlkit

    url, key, folders, output, outputs = check(body)
    path = config.settings_path()
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    doc = tomlkit.parse(text) if text else tomlkit.document()
    old = doc.get("sources") or []
    sources = tomlkit.aot()
    kept = next((t for t in old if t.get("type") == "immich"), None) if url else None
    if url:
        table = kept if kept is not None else tomlkit.table()
        table["type"], table["url"] = "immich", url
        sources.append(table)
    for folder in folders:
        table = tomlkit.table()
        table.update(folder)
        sources.append(table)
    doc["sources"] = sources
    if output:
        doc["output"] = output
    if outputs:
        table = doc.get("outputs") or tomlkit.table()
        for name in OUTPUTS:
            if name in outputs:
                table[name] = outputs[name]
        doc["outputs"] = table
    if key:
        target = Path((kept or {}).get("key_file") or key_file()).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(target, key.encode("utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if text:
        write_atomic(path.with_name(path.name + ".bak"), text.encode("utf-8"))
    write_atomic(path, tomlkit.dumps(doc).encode("utf-8"))
    config.reload()
    return state(cli_sources)


_scan = None


def start_scan(running=False):
    """fuseshot run in the background, logging to output/run.log. running: whether the
    review page already sees a run going (from its progress.json)."""
    global _scan
    if running or (_scan is not None and _scan.poll() is None):
        raise SetupError("a scan is already running")
    log = config.home() / "output" / "run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with open(log, "ab") as out:
        _scan = subprocess.Popen([sys.executable, "-m", "fuseshot", "--home", str(config.home()), "run"],
                         stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=jobs.child_env(),
                         creationflags=flags, start_new_session=os.name != "nt")
    return {"started": True, "log": str(log)}
