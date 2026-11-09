# Windows desktop build (private test installer)

This folder builds an unsigned **per-user** Fuseshot desktop app for Windows x64.
The result is meant for clean-machine testing before any public release. **Building
here on Linux only validates scripts and Python layout; it does not prove the
installer works on Windows.**

## What you get

- **Fuseshot.exe** (PyInstaller one-folder build): bundled CPython and core Python
  dependencies (numpy, OpenCV, Pillow, and the rest of the default wheel). No
  personal settings, photo libraries, API keys or downloaded models are included.
- Optional **Hugin** tools under `tools\hugin\bin` when you supply a Hugin tree at
  build time (see below). The installer copies the GPL notices in `notices\`.
- **Inno Setup** produces `Fuseshot-Setup-test.exe` (unsigned). SmartScreen and
  Defender will warn until the file is signed with a real certificate.

## Requirements (build machine: Windows 10/11 x64)

- Python 3.11+ with `uv` or `pip`
- `pyinstaller` (installed by `build.ps1`)
- [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`ISCC.exe` on PATH)
- Optional: installed Hugin 64-bit, or an unpacked Hugin zip

## Build

From the repository root in PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\packaging\windows\build.ps1
```

Artifacts land in `packaging\windows\dist\`:

| File | Meaning |
| --- | --- |
| `Fuseshot\` | Raw application folder (can be zipped for portable test) |
| `Fuseshot-Setup-test.exe` | Per-user installer (no admin) |
| `SHA256SUMS.txt` | Checksums of the above |

### Bundling Hugin

Hugin is **not** downloaded automatically (licence and size). Before building:

```powershell
$env:HUGIN_ROOT = 'C:\Program Files\Hugin'
.\packaging\windows\build.ps1
```

Or copy a portable tree to `packaging\windows\stage\hugin` and pass `-HuginStage`.

Redistributing Hugin inside the installer must comply with the GPL. Ship
`notices\HUGIN.txt` and offer source as Hugin's licence requires.

## End-user install (unsigned test build)

1. Run `Fuseshot-Setup-test.exe`. Windows may say the publisher is unknown; that is
   expected without code signing.
2. Start **Fuseshot** from the Start menu. The local review page opens in your
   browser on `http://127.0.0.1` (port chosen automatically if 8765 is busy).
3. First-run setup is the **Settings** page: Immich and/or folder sources, output
   folder, and which result types to make. Use **Start** on the Scan card to run
   sync through stitching without a terminal. Optional models (faces, fill, splats)
   are downloaded later from settings with licence text and checksum verification.

Data lives under `%LOCALAPPDATA%\fuseshot` by default and is kept across upgrades.
Uninstall removes the program files; data is left unless you delete the folder.

## Not self-contained (by design)

| Feature | On a typical test install |
| --- | --- |
| Panoramas | Works when Hugin was bundled or found in settings |
| Gaussian splats | Needs COLMAP and Brush installed separately (or paths in settings) |
| Group photos / moving subject scan | Needs `mediapipe` in a Python with the group extra, or download models only where ONNX applies |
| AI fill / enhance | Needs optional torch models from settings; GPU is optional (CPU fallback where implemented) |
| CUDA | Never bundled; use NVIDIA drivers + install torch yourself if you want GPU jobs |

## Clean-install validation (still required on Windows)

Run through this on a VM or PC **without** Python or Hugin on PATH:

- Install from `Fuseshot-Setup-test.exe` (paths with spaces and non-ASCII username).
  Silent install: quote `/DIR=` when the path contains spaces, for example
  `Fuseshot-Setup-test.exe /DIR="{localappdata}\Programs\My Fuseshot" /SILENT`.
- First-run settings: folder source only, then one small `fuseshot run --year …` from doctor.
- Start menu twice: second launch should focus the existing browser tab, not a second server.
- Upgrade install over an older test build: `fuseshot.toml` in `%LOCALAPPDATA%\fuseshot` unchanged.
- Uninstaller removes `Program Files` / local app copy; launch again only after reinstall.

Report results back to the packaging issue; Linux CI cannot substitute for this list.
