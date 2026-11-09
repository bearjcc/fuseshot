"""Start the review page for the Windows desktop build: one window, loopback only."""

import json
import os
import socket
import urllib.error
import urllib.request
import webbrowser

from . import config, log, runtime

LOG = log.get(__name__)
LOCK_NAME = "desktop-session.json"
DEFAULT_PORT = 8765


def lock_path():
    return config.home() / LOCK_NAME


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _server_up(host, port, base_path=""):
    url = f"http://{host}:{port}{base_path or ''}/"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            return resp.status < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def read_session():
    path = lock_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not _pid_alive(data.get("pid")):
        return None
    host = data.get("host", "127.0.0.1")
    if not _server_up(host, data.get("port", DEFAULT_PORT), data.get("base_path", "")):
        return None
    return data


def open_session(data):
    token = data.get("token")
    host = "127.0.0.1" if data.get("host") in ("0.0.0.0", "::") else data.get("host", "127.0.0.1")
    link = f"http://{host}:{data.get('port', DEFAULT_PORT)}{data.get('base_path', '')}/"
    if token:
        link += f"?token={token}"
    webbrowser.open(link)
    LOG.info(f"Fuseshot is already running; opened {link}")


def write_session(host, port, base_path="", token=None):
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "base_path": base_path or "",
        "token": token,
    }
    from .files import write_atomic

    write_atomic(path, json.dumps(payload, indent=1))


def clear_session():
    try:
        lock_path().unlink(missing_ok=True)
    except OSError:
        pass


def pick_port(host, preferred=DEFAULT_PORT):
    for port in [preferred, *range(preferred + 1, preferred + 24)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def prepare_review_args(args):
    """Loopback, a free port, and hand off if another packaged instance is already serving."""
    runtime.seed_environment()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        LOG.warning("Packaged Fuseshot only supports loopback review; using 127.0.0.1")
        args.host = "127.0.0.1"
    existing = read_session()
    if existing:
        open_session(existing)
        raise SystemExit(0)
    args.port = pick_port(args.host, args.port)
    if not args.open:
        args.open = True
    return args


def run_app():
    """Double-click entry: open the review page (settings first when nothing is configured)."""
    from .__main__ import main

    runtime.seed_environment()
    main(["review", "--open"])
