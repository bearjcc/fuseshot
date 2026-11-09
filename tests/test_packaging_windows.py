import subprocess
import sys
from pathlib import Path


def test_packaging_smoke_script():
    script = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "smoke.py"
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr or out.stdout


def test_packaging_docs_mention_unsigned():
    readme = (Path(__file__).resolve().parents[1] / "packaging" / "windows" / "README.md").read_text()
    assert "unsigned" in readme.lower()
    assert "clean-install" in readme.lower() or "Clean-install" in readme
