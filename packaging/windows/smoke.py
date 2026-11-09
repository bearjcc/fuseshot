"""Read-only checks for the Windows packaging tree (runs on any OS in CI)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]


def required_files():
    missing = []
    for rel in (
        "README.md",
        "build.ps1",
        "fuseshot.spec",
        "installer.iss",
        "notices/HUGIN.txt",
    ):
        if not (ROOT / rel).is_file():
            missing.append(rel)
    return missing


def spec_mentions_web_bundle():
    text = (ROOT / "fuseshot.spec").read_text(encoding="utf-8")
    return "fuseshot/web" in text and "mediapipe" in text


def installer_per_user():
    text = (ROOT / "installer.iss").read_text(encoding="utf-8")
    return "PrivilegesRequired=lowest" in text and "localappdata" in text


def main():
    miss = required_files()
    if miss:
        raise SystemExit(f"missing packaging files: {miss}")
    if not spec_mentions_web_bundle():
        raise SystemExit("fuseshot.spec should bundle web and exclude mediapipe")
    if not installer_per_user():
        raise SystemExit("installer.iss should be per-user (lowest)")
    if not re.search(r"def frozen", (REPO / "fuseshot/runtime.py").read_text()):
        raise SystemExit("fuseshot.runtime missing")
    print("packaging/windows layout ok")


if __name__ == "__main__":
    main()
