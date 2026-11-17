# Fuseshot 1.0 release checklist (maintainers)

This is a manual gate before tagging 1.0. No automated release or PyPI publish is defined here.

## All platforms (developer checkout)

1. `uv sync` (or `pip install -e ".[group]"` with dev deps).
2. `ruff check fuseshot tests`
3. Fast tests: `pytest -m "not slow" -q` — expect **0 failures**.
4. Coverage sanity: `pytest -m "not slow" --cov=fuseshot --cov-report=term` — note total % in the release notes.
5. Smoke: `python tests/smoke.py` — prints `smoke ok: …`.
6. Full tests when possible: `pytest -q` (needs **Hugin** on Linux, **Playwright** chromium, optional network for CC0 raw tests).

## Linux

- [ ] Hugin tools on PATH; `test_stitched_preview` and slow export test pass.
- [ ] Headless scan: `pytest tests/test_moving_headless.py` (no EGL crash on `run --no-stitch`).

## Windows (minttop or clean VM)

- [ ] `packaging/windows/build.ps1` on **PowerShell 5.1** without NativeCommandError from PyInstaller INFO lines.
- [ ] Installer size and bundled Hugin match expectations.
- [ ] `packaging/windows/installer_smoke.ps1` on built `Fuseshot-Setup-test.exe` (silent `/DIR="…"` with spaces).
- [ ] Start menu: single instance, loopback port conflict, settings auth unchanged.
- [ ] Settings: Save → Scan Start/Pause/Resume through panorama on sample folder (no terminal).
- [ ] Uninstall removes app; `%LOCALAPPDATA%\fuseshot` data kept unless user deletes it.

## Capabilities honesty

- [ ] Settings outputs list splats/groups/enhance as **not ready** until COLMAP/Brush/mediapipe/torch are present (not merely model weights).
- [ ] Live Immich upload remains off unless explicitly enabled in settings and approved per item in review.

## Known not required for 1.0 tag

- PyPI publish, GitHub release assets, code signing (SmartScreen unsigned test build is OK for private beta).
- New CI workflows beyond existing `test.yml`.

## Sign-off

Record:

- Git commit SHA of `main`
- Fast test count and smoke path
- Windows installer SHA-256 from `packaging/windows/dist/SHA256SUMS.txt`
- Open limitations from `docs/testing.md` still verified on hardware you care about