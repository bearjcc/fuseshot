# PyInstaller spec for Fuseshot desktop (Windows x64).
# Run via packaging/windows/build.ps1 on Windows.

import os
from pathlib import Path

root = Path(SPECPATH).resolve().parents[1]

a = Analysis(
    [str(Path(SPECPATH) / "entry.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / "fuseshot" / "web"), "fuseshot/web"),
        (str(root / "LICENSE"), "."),
        (str(root / "THIRD_PARTY.md"), "."),
    ],
    hiddenimports=[
        "fuseshot",
        "fuseshot.desktop",
        "fuseshot.runtime",
        "tomllib",
        "pi_heif",
        "rawpy",
        "cv2",
        "numpy",
        "PIL",
        "platformdirs",
        "watchdog",
        "tqdm",
        "tomlkit",
    ],
    excludes=[
        "torch",
        "torchvision",
        "mediapipe",
        "playwright",
        "pytest",
        "ruff",
        "spandrel",
        "spandrel_extra_arches",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Fuseshot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Fuseshot",
)
