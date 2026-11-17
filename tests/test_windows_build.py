"""Regression tests for packaging/windows/build.ps1 (no Windows required)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "packaging" / "windows" / "build.ps1"


def test_build_ensures_pyinstaller_without_bare_pip_first():
    text = BUILD.read_text(encoding="utf-8")
    assert "Ensure-PyInstaller" in text
    assert "function Ensure-PyInstaller" in text
    # uv venvs often have no pip module; install must be conditional.
    assert "uv pip install" in text
    assert "ensurepip" in text
    assert "importlib.util.find_spec" in text
    assert "lib.ps1" in text