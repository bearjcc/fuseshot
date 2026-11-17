"""Contributor smoke test: tiny synthetic library through CLI and review HTTP.

Run:  python -m tests.smoke
      pytest tests/smoke.py -m smoke
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import write_frames  # noqa: E402
from fuseshot import config, settings_web, tools
from fuseshot.__main__ import main
from fuseshot.review import Review, make_handler

LIGHT = ("run", "--no-stitch", "--no-loops", "--no-groups")
SET = "F20240309T140000"


def _home() -> Path:
    root = Path(tempfile.mkdtemp(prefix="fuseshot-smoke-"))
    os.environ["FUSESHOT_HOME"] = str(root)
    config.setup(root)
    return root


def cli_scan(home: Path, photo_dir: Path) -> None:
    main(["--home", str(home), "--source", f"folder:{photo_dir}", *LIGHT])
    candidates = json.loads((home / "output" / "candidates.json").read_text(encoding="utf-8"))["candidates"]
    assert candidates, "expected at least one candidate set"
    assert candidates[0]["set_id"] == SET


def review_http(home: Path) -> None:
    review = Review(
        home / "work",
        candidates_file=home / "output" / "candidates.json",
        output_dir=home / "output",
        base_path="",
    )
    review.reconfigure = lambda: None
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(review))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/"
    for path in ("/", "/settings", "/api/items", "/api/settings", "/api/settings/outputs"):
        with urllib.request.urlopen(base + path.lstrip("/"), timeout=10) as resp:
            assert resp.status == 200
    outputs = json.loads(urllib.request.urlopen(base + "api/settings/outputs", timeout=10).read())
    if not tools.find("hugin"):
        assert outputs["panoramas"]["ready"] is False
    if not tools.find("colmap"):
        assert outputs["splats"]["ready"] is False
    server.shutdown()


def optional_stitch(home: Path, photo_dir: Path) -> None:
    if tools.find("hugin") is None:
        return
    main(["--home", str(home), "--source", f"folder:{photo_dir}", "run", "--no-loops", "--no-groups"])
    record = json.loads((home / "work" / SET / "batch.json").read_text(encoding="utf-8"))
    assert record.get("status") == "stitched", record.get("error")


def run_smoke() -> None:
    home = _home()
    photos = write_frames(home / "PHOTOS", count=5)
    cli_scan(home, photos[0].parent)
    review_http(home)
    optional_stitch(home, photos[0].parent)
    found = settings_web.output_status()
    assert "panoramas" in found
    print("smoke ok:", home)


@pytest.mark.smoke
def test_smoke_pytest_entry(home, photos):
    main(["--home", str(home), "--source", f"folder:{photos[0].parent}", *LIGHT])
    assert (home / "output" / "candidates.json").is_file()


def main_entry() -> int:
    try:
        run_smoke()
        return 0
    except Exception as err:
        print(f"smoke failed: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main_entry())
