"""Guards against tests writing PTO artifacts into the repository root."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_repo_root_has_no_stray_pto_files():
    found = sorted(p.name for p in REPO_ROOT.glob("*.pto"))
    assert not found, f"remove stray PTO files from repo root: {found}"
