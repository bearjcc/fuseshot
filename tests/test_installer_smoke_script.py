"""installer_smoke.ps1 regression checks (script content only)."""

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "installer_smoke.ps1"


def test_installer_smoke_uses_native_wrapper_and_pid_check():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "lib.ps1" in text
    assert "Invoke-NativeCommand" in text
    assert "/NOCLOSEAPPLICATIONS" in text
    assert "/TASKS=" in text
    assert "session.pid" in text and "proc.Id" in text
    assert "fixtures\\panorama_sample" in text or "fixtures/panorama_sample" in text
    assert "--source" in text and "folder:" in text
    assert "--output-folder" in text
    assert re.search(
        r'\$candidates\s*=\s*Join-Path\s+\$DataHome\s+"output\\candidates\.json"',
        text,
    ), "candidates.json must be under FUSESHOT_HOME/output, not --output-folder"
    assert 'Join-Path $OutDir "candidates.json"' not in text