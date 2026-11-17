# Testing guide (contributors)

Fuseshot targets a 1.0 desktop release. Tests use **synthetic scenes** from `tests/conftest.py` (procedural JPEGs with EXIF) and **CC0** samples only where noted (raw tests fetch from raw.pixls.us). No personal libraries in the repo.

## Commands

| Command | When |
| --- | --- |
| `pytest -m "not slow"` | Default fast suite (Linux/macOS/Windows dev install) |
| `pytest` | Full suite including Hugin stitch, browser review, and CC0 raw tests |
| `python tests/smoke.py` | Contributor smoke: CLI scan + review HTTP on a tiny library |
| `pytest -m smoke` | Smoke tests via pytest (includes browser review when Playwright is installed) |
| `pytest --cov=fuseshot --cov-report=term-missing -m "not slow"` | Coverage on the fast suite |

Optional dev deps: `uv sync` (uses `dependency-groups.dev`: pytest, pytest-cov, hypothesis, ruff).

Windows installer smoke (real Windows only): `packaging/windows/installer_smoke.ps1` after `build.ps1`.

## Markers

- **`slow`**: Full `fuseshot run` with Hugin, Playwright review page, CC0 raw network tests.
- **`smoke`**: End-to-end checks suitable after packaging changes.
- **`windows_only`**: Reserved for tests that must run on a built installer (documented in release checklist).

## Coverage map (line coverage, fast suite)

Measured with `pytest --cov=fuseshot -m "not slow"` on Linux (Nov 2026). Overall **~55%** line coverage before this pass; the same metric after adding failure-injection, fuzz, pipeline, and moving-headless tests is **~55%** (more lines exercised in `moving`, `pipeline`, `settings_web`; large stitch/group/splat modules unchanged).

### By module (approximate % covered, fast suite)

| Module | % | Risk if untested |
| --- | ---: | --- |
| `stitch.py` | 16 | Panorama align, Hugin CLI, crop, export sizes |
| `parts.py` | 11 | PTO parsing, lens lines |
| `group.py` | 17 | Best-face merges, MediaPipe |
| `splat.py` | 41 | COLMAP/Brush orchestration |
| `fill.py` | 0 | AI border fill (torch) |
| `edit.py` | 0 | Re-stitch jobs from review |
| `watch.py` | 0 | Filesystem watch helper |
| `doctor.py` | 19 | First-run capability checks |
| `fetch.py` | 27 | Asset download CLI |
| `review.py` | 49 | HTTP API, Keep/Discard/Edit, auth |
| `desktop.py` | 51 | Single instance, loopback |
| `pipeline.py` | 75 | Settings Start/Pause/Resume |
| `immich.py` | 47 | API client, paging |
| `sources.py` | 83 | Folder/Immich sync, unreadable files |
| `verify.py` | 79 | Overlap, motion routing |
| `detect.py` | 75 | Set detection |
| `takeout.py` | 94 | Sidecar pairing |
| `upload.py` | 92 | Upload approval gate |

### Untested flows (ranked by user impact)

1. **Full panorama stitch on Windows frozen build** (Hugin bundled, paths with spaces) — minttop only.
2. **Review Keep → export with EXIF** at full megapixel cap — partially covered in slow `test_end_to_end`; needs frozen UI path.
3. **Pipeline pause mid-stitch → resume** — unit tests mock subprocess; needs Windows UI smoke.
4. **Gaussian splat plan → COLMAP/Brush** — no CI tools; doctor output tested, not execution.
5. **Group photos / moving / loops with MediaPipe** — skipped when EGL/GPU libs missing; headless skip regression in `test_moving_headless`.
6. **Enhance / colourise with torch** — optional extra; pack checksum tested.
7. **Immich upload** — fake server tests; live upload stays off unless `[upload] immich = true` and approval in review.
8. **Frozen `Fuseshot.exe` argv** — `tests/test_pipeline.py`, `tests/test_runtime` paths; installer_e2e on Windows.

## What is already well covered

- Settings page HTTP, Immich key handling, scan concurrency (`test_settings_page.py`).
- Decisions Keep/Discard/Edit/retry (`test_decisions.py`, `test_retry.py`).
- Takeout sidecar pairing (`test_takeout.py`).
- Motion/wiggle/composite/unit paths (`test_motion.py`, `test_stereo.py`, `test_composite.py`).
- Upload gating and fake Immich (`test_upload.py`).
- Bad input, corrupt files, permissions (`test_bad_input.py`).
- Conservative moving-subject routing (`test_moving.py`).

## Failure-injection tests

See `tests/test_failure_injection.py`: pipeline pause, corrupt JPEG, Immich 401/timeout, port busy, second desktop instance, pack checksum failure, disk full message.

## Property / fuzz tests

`tests/test_fuzz_properties.py` uses [Hypothesis](https://hypothesis.readthedocs.io/) (MPL 2.0). It stress-tests crop maths, takeout pairing, lens header parsing, and config unknown keys.

## Synthetic fixtures

- `write_frames()` / `scene()`: wide procedural scene, shared EXIF pattern, no external files.
- Do not commit copyrighted photos. Raw tests download CC0 once to the pytest cache directory.

## Gaps for manual / minttop

- Clean Windows VM: installer, Unicode user profile, uninstall keeps data.
- GPU: torch fill, mediapipe on production GPU drivers.
- Hugin quality: visual review of stitched output, not just `status == stitched`.
- `packaging/windows/installer_smoke.ps1` full path with silent install + frozen `run`.

When you add tests for a risky flow, prefer **regression tests that fail without the fix** and keep them in the fast suite when runtime is under a few seconds.