"""installer_smoke.ps1 regression checks (script content only)."""

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "installer_smoke.ps1"


def test_installer_smoke_uses_native_wrapper_and_pid_check():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "lib.ps1" in text
    assert "Invoke-NativeCommand" in text
    assert "/NOCLOSEAPPLICATIONS" in text
    assert "/TASKS=" in text
    assert "session.pid" in text and "proc.Id" in text
