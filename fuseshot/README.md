# fuseshot

The package. `__main__.py` holds the command line; each step has its own module.

- `sources.py`, `immich.py`, `cache.py`, `watch.py`: reading photos and their details
- `detect.py`, `report.py`, `verify.py`: finding sets and checking their overlap
- `fetch.py`, `lens.py`, `stitch.py`, `parts.py`, `blend.py`, `batch.py`, `fill.py`:
  panoramas
- `group.py`, `action.py`, `splat.py`: group shots, loops, splats
- `review.py`, `export.py`, `upload.py` and `web/`: the review page, copying results
  out and the opt-in upload to Immich
- `config.py`, `assets.py`, `tools.py`, `jobs.py`, `doctor.py`, `log.py`: settings,
  downloads, outside programs, background jobs, logging
