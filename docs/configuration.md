# Configuration

Fuseshot works without any settings if Immich is at `http://127.0.0.1:2283` and
the outside programs are on `PATH`. Everything below is for when they are not.

## The home folder

Everything Fuseshot keeps lives in one folder: the photo details cache, the lists it
writes, work folders with stitched results, downloaded models and viewer scripts,
the settings file and (if you use one) the Immich key file. It is `--home`, else
`FUSESHOT_HOME`, else the per-user data folder (`~/.local/share/fuseshot`,
`%LOCALAPPDATA%\fuseshot` or `~/Library/Application Support/fuseshot`).
`fuseshot doctor` prints which one is in use.

    cache/         photo details (assets.sqlite), downloaded photos and job logs
    output/        candidates.csv/.json, verified.json and summaries from detect and verify
    work/          one folder per set with its stitch, previews and checks; reviews.json
    models/        downloaded models
    tools/         outside programs you unpacked here, and the review page's viewer scripts
    secrets/       immich_key, if the key is not in the environment
    fuseshot.toml  settings

`work/reviews.json` holds your decisions; it is the one file worth backing up.

## Settings file

`fuseshot.toml` in the home folder, or `--config`. See
[fuseshot.example.toml](../fuseshot.example.toml) for every key with a comment.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `FUSESHOT_HOME` | per-user data folder | home folder |
| `FUSESHOT_IMMICH_URL` | `http://127.0.0.1:2283` | Immich server |
| `FUSESHOT_IMMICH_KEY` | | Immich API key (wins over the key file) |
| `FUSESHOT_WEB_URL` | the server URL | Immich address used in photo links |
| `FUSESHOT_REVIEW_HOST` | `127.0.0.1` | address the review page listens on |
| `FUSESHOT_REVIEW_PORT` | `8765` | its port |
| `FUSESHOT_REVIEW_TOKEN` | | token needed to open the page |
| `FUSESHOT_BASE_PATH` | | path prefix behind a reverse proxy, e.g. `/fuseshot` |
| `FUSESHOT_HUGIN`, `FUSESHOT_COLMAP`, `FUSESHOT_BRUSH` | | where an outside program is |

## Photo sources

Photos can come from Immich, from folders on disk, or both. Folders are only read.
Photo details come from the EXIF in each file (HEIC included; JPEG XL with the `jxl`
extra); a photo with no EXIF date uses the file's modified time.

Sources come from `--source` options if given, else `[[sources]]` in the settings
file, else Immich alone. `--recursive` takes in the folders below the folder source
just before it.

    fuseshot --source immich --source folder:~/Pictures/Camera --recursive sync
    fuseshot --source folder:/mnt/scans sync --folders-only   # rescan folders, skip Immich
    fuseshot watch --detect                                  # rescan folders as photos arrive
    fuseshot export                                          # copy kept folder results again
    fuseshot export --immich                                 # and the Immich ones
    fuseshot export --immich --upload                        # upload kept Immich results instead

`sync` only reads what is new or changed since the last sync, and drops photos
whose files are gone. `sync --full` reads everything again.

`watch` rescans once changes have settled (`--settle 20` seconds) and fully every
hour in case an event was missed (`--rescan-every 60` minutes). Without watchdog
installed it rescans every minute.

Sets with folder photos are numbered F00001, F00002... apart from the Immich sets
(P00001...), so adding folders never renumbers Immich sets.

Kept results from folder sets (the chosen view of a panorama, the merged group
photo, the loop or the splat) are copied to the output folder as
`<date> <set> panorama.jpg` and so on as soon as they are kept.

## Uploading to Immich

    [upload]
    immich = true          # default false: Fuseshot only reads from Immich
    album = "Fuseshot"     # "" for no album

With `immich = true`, `fuseshot review` uploads each kept result from an Immich
set (the chosen panorama view, the merged group photo or the loop) 30 seconds after
it is kept, once it has checked the decision was not undone. `fuseshot export
--immich --upload` uploads every kept one that has not gone up yet, with or
without the setting; `--album` overrides the album for that run.

Each result is sent to `POST /api/assets` as a new asset with an XMP sidecar
holding the first photo's date and GPS position, `CreatorTool` Fuseshot and a
description of what was done, including when edges were AI-filled. The file's SHA-1
goes in the `x-immich-checksum` header, so Immich answers "duplicate" instead of
storing a copy. Fuseshot records each upload (item, SHA-1, asset id) in
`work/uploads.json` and skips anything already there. Nothing is ever updated or
deleted in Immich; if you keep a different view later, it is uploaded as well
and the earlier one stays until you remove it.

Permissions the key needs:

| Mode | Permissions |
| --- | --- |
| Read only (default) | `asset.read`, `asset.statistics`, `asset.view`, `asset.download` |
| Uploads | add `asset.upload` |
| Uploads into an album | add `album.read`, `album.create`, `albumAsset.create` |

Works with Immich 2 and 3 (Immich 2 also gets the `deviceId` fields it requires).

## Outside programs

| Program | Needed for | Get it |
| --- | --- | --- |
| Hugin (nona, cpfind, enblend...) | stitching | [hugin.sourceforge.io](https://hugin.sourceforge.io/download/), or `apt install hugin-tools enblend enfuse` |
| COLMAP 3.12 or newer | camera poses for splats | [GitHub releases](https://github.com/colmap/colmap/releases); distribution packages are often too old |
| Brush | training splats | [GitHub releases](https://github.com/ArthurBrussee/brush/releases) |

Each is looked for at `FUSESHOT_<NAME>`, then `[tools]` in the settings file, then
`tools/` in the home folder (unpack a release zip there), then the usual install
places, then `PATH`. None of them is shipped with the pip package.

## Optional extras

- `fuseshot[fill]`: AI fill for panorama edges. Pulls in PyTorch. For a CUDA build
  on Linux install torch from [pytorch.org](https://pytorch.org/get-started/locally/)
  first. Without a GPU it still works, slowly. With pipx: `pipx inject fuseshot torch`.
- `fuseshot[group]`: group shots, with MediaPipe. It goes in the same venv as
  Fuseshot: both use opencv-contrib-python, so there is one OpenCV. With pipx:
  `pipx inject fuseshot mediapipe`. `group_python` under `[jobs]` can still point
  at another Python if you keep MediaPipe elsewhere. An older install that has
  opencv-python-headless as well shows up in `fuseshot doctor`, with the fix.
- `fuseshot[jxl]`: JPEG XL photos. The plugin is GPL-3.0, so it is not in the
  Windows build or the Docker image.

Models are downloaded the first time they are needed and checked against pinned
hashes. `fuseshot fetch-assets` downloads everything up front, for a machine that
will later be offline.

## Jobs

Stitching chunks, AI fill, group merges and splat training run as jobs. By default
they run one at a time in the background of the review page (`runner = "local"`),
with logs in `cache/jobs/`.

On a machine shared with other GPU work, set `runner = "pueue"` to send them to a
[Pueue](https://github.com/Nukesor/pueue) queue instead. GPU jobs can go through
your own submit command with `gpu_submit`; see the example settings file.

## Logging

Results go to stdout and the log to stderr. `-v` shows more, `-q` only warnings and
errors. The Immich key and review token are never logged; a token you chose is
shown as `<your token>`.
