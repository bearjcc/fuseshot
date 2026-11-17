# Testing guide (contributors)

Fuseshot targets a 1.0 desktop release. Tests use **synthetic scenes** from `tests/conftest.py` (procedural JPEGs with EXIF) and **CC0** samples only where noted (raw tests fetch from raw.pixls.us). No personal libraries in the repo.

## Commands

| Command | When |
| --- | --- |
| `pytest -m "not slow"` | Default fast suite (Linux/macOS/Windows dev install) |
| `pytest` | Full suite including Hugin stitch, browser review, and CC0 raw tests |
| `python tests/smoke.py` | Contributor smoke: CLI scan + review HTTP on a tiny library |
| `pytest -m smoke` | Smoke tests via pytest (includes browser review when Playwright is installed) |
| `pytest --cov=fuseshot --cov-report=term-missing -m "not slow"` | Line coverage on the fast suite |
| `pytest --cov=fuseshot --cov-branch --cov-report=term-missing -m "not slow"` | Line and branch coverage on the fast suite |

Optional dev deps: `uv sync` (uses `dependency-groups.dev`: pytest, pytest-cov, hypothesis, ruff).

Windows installer smoke (real Windows only): `packaging/windows/installer_smoke.ps1` after `build.ps1`.

## Markers

- **`slow`**: Full `fuseshot run` with Hugin, Playwright review page, CC0 raw network tests.
- **`smoke`**: End-to-end checks suitable after packaging changes.
- **`windows_only`**: Reserved for tests that must run on a built installer (documented in release checklist).

## Coverage (fast suite, Nov 2026 pass)

Measured on Linux with `pytest -m "not slow"`.

### Overall

| Metric | Before (main, pre-pass) | After (this pass) | Target |
| --- | ---: | ---: | ---: |
| Line coverage | ~55% | **~67%** | 75% |
| Branch coverage (`--cov-branch`) | ~51% | **~62%** | (informative) |

The 75% line target is not fully met yet; the largest remaining gaps are `group.py` (face merge / MediaPipe), `splat.py` (COLMAP / Brush), and HTML-heavy paths in `review.py`. New tests mock Hugin at `stitch.Runner`, torch at `fill.fill`, and Immich at `immich.Client` boundaries.

### By module (line %, fast suite)

Focus modules from the coverage pass (approximate **before → after** on the same metric):

| Module | Before | After | Notes |
| --- | ---: | ---: | --- |
| `stitch.py` | 16 | **61** | align/render/blend/solve/level with mocked Runner |
| `parts.py` | 11 | **63** | PTO split, subset, layout |
| `group.py` | 17 | **28** | Pure geometry; full `check`/`merge` still needs models |
| `splat.py` | 41 | **42** | `make_plan`, helpers; SFM/train untouched |
| `fill.py` | 0 | **62** | Masks/tiles; torch mocked for `fill()` |
| `edit.py` | 0 | **56** | Missing originals, mocked re-stitch |
| `export.py` | (low) | **89** | `export` / `result_file` |
| `enhance.py` | (low) | **93** | Existing + pack tests |
| `immich.py` | 47 | **72** | Client paging, retry, multipart |
| `settings_web.py` | (high) | **90** | `test_settings_page.py` |
| `review.py` | 49 | **63** | HTTP routes, decide/undo, table builders |
| `blend.py` | 7 | **89** | `plan_layers`, `blend_layers` |
| `batch.py` | 18 | **23** | `make_plan`, `left_out` |
| `doctor.py` | 19 | **71** | `check`, mocked tool probe |
| `fetch.py` | 27 | **67** | `fetch_set` with stub Immich |
| `watch.py` | 0 | **53** | `rescan`, `run` interrupt path |

### Branch coverage

Branch coverage counts whether both outcomes of conditionals were exercised. Run:

```bash
pytest -m "not slow" --cov=fuseshot --cov-branch --cov-report=term-missing
```

Report the **TOTAL** row: `Stmts`, `Miss`, `Branch`, `BrPart`, and `Cover` (combined line/branch figure from pytest-cov). On this pass, branch coverage is about **62%** combined vs ~51% before.

Low branch % often means error paths (Immich 503, stitch OOM fallbacks, optional torch) are only partially hit; failure-injection tests in `test_failure_injection.py` cover several of these.

## What the new fast tests exercise

- **Stitch**: `tests/test_stitch_core.py`, `test_stitch_more.py`, `test_stitch_render_mock.py`, `test_stitch_blend_unit.py`, `test_stitch_level_repair.py`, `test_stitch_exposure_crop.py`, `test_stitch_solve_mock.py`, `test_align_mocked.py`.
- **Group**: `test_group_logic.py`, `test_group_geometry.py`, `test_group_check_fast.py`, `test_group_overlay.py`.
- **Fill**: `test_fill_unit.py` (torch boundary mock).
- **Edit / export**: `test_edit_export.py`.
- **Immich**: `test_immich_client.py`, `test_fetch_mock.py`.
- **Review actions**: `test_review_routes.py`, `test_review_api_more.py`, `test_review_actions_more.py`, `test_review_bulk.py`, `test_review_http_get.py`, `test_review_decide_many.py`.
- **Splat plan**: `test_splat_plan.py`, `test_splat_helpers.py`.
- **Blend**: `test_blend_unit.py`, `test_blend_layers_run.py`.
- **Setup / doctor / watch**: `test_doctor_fetch.py`, `test_watch.py`, `test_watch_run.py`.

## Untested flows (ranked by user impact)

1. **Full panorama stitch on Windows frozen build** (Hugin bundled, paths with spaces) — minttop only.
2. **Review Keep → export at full megapixel cap** — partial; slow `test_end_to_end`.
3. **Gaussian splat plan → COLMAP/Brush** — plan only; execution needs tools.
4. **Group photos / moving with MediaPipe** — headless skip in `test_moving_headless`.
5. **Enhance / colourise with real torch** — optional extra; mocks in fast suite.
6. **Immich live upload** — fake server only unless upload enabled in settings.
7. **Frozen `Fuseshot.exe` argv** — `test_pipeline.py`; installer_e2e on Windows.

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
- `minimal_pto()` / `stitched_set()`: tiny Hugin projects and review-ready set dirs.
- Do not commit copyrighted photos. Raw tests download CC0 once to the pytest cache directory.

## Gaps for manual / minttop

- Clean Windows VM: installer, Unicode user profile, uninstall keeps data.
- GPU: torch fill, mediapipe on production GPU drivers.
- Hugin quality: visual review of stitched output, not just `status == stitched`.
- `packaging/windows/installer_smoke.ps1` full path with silent install + frozen `run`.

When you add tests for a risky flow, prefer **regression tests that fail without the fix** and keep them in the fast suite when runtime is under a few seconds.