# Third-party software

Fuseshot itself is AGPL-3.0-or-later (see `LICENSE`). This file lists what it builds on and
how each piece reaches you. Very little of it is in this repository. Most of it is downloaded
when you install or first use Fuseshot.

## In this repository

| What | Where | Licence |
| --- | --- | --- |
| Material Design Icons 7.4.47 (path data for a few icons) | `fuseshot/web/icons.js` | Apache-2.0, © Pictogrammers |

## Rules ported from other projects

| What | Where | Licence |
| --- | --- | --- |
| How Google Takeout names sidecar files, from immich-go (`adapters/googlePhotos/matchers.go` and its tests) | `fuseshot/takeout.py`, `tests/test_takeout.py` | AGPL-3.0, © Jean-François Casas and contributors |

## Python packages (installed by pip, not shipped here)

| Package | Licence | Needed for |
| --- | --- | --- |
| numpy | BSD-3-Clause | everything |
| opencv-contrib-python | Apache-2.0 (wheels bundle FFmpeg and Qt under LGPL) | everything |
| Pillow | MIT-CMU | everything |
| pi-heif | BSD-3-Clause (wheels bundle libheif and libde265, LGPL-3.0) | HEIC photos |
| platformdirs | MIT | everything |
| psutil | BSD-3-Clause | everything |
| rawpy | MIT (wheels bundle LibRaw 0.22, LGPL-2.1 or CDDL-1.0) | camera raw files |
| tomlkit | MIT | the settings page, which keeps the comments in fuseshot.toml |
| tqdm | MPL-2.0 and MIT | progress bars |
| watchdog | Apache-2.0 | `fuseshot watch` |
| torch (extra `fill`) | BSD-3-Clause | `fuseshot fill` |
| mediapipe (extra `group`) | Apache-2.0 | `fuseshot group` |
| pillow-jxl-plugin (extra `jxl`) | GPL-3.0 | JPEG XL photos |

pillow-jxl-plugin is GPL-3.0, which can be combined with AGPL-3.0. It stays an optional extra
and is not included in the Windows desktop build scripts or the Docker image.

## Windows desktop packaging (`packaging/windows/`)

The test installer bundles CPython and the default Python dependencies listed above.
It does **not** bundle torch, MediaPipe, COLMAP, Brush, viewer assets, or any model
files. Optional Hugin binaries may be copied in at build time when the builder
supplies them; see `packaging/windows/notices/HUGIN.txt` for GPL obligations.

## Downloaded on first use, checked against a pinned SHA-256

`fuseshot fetch-assets` (or the review page, the first time it starts) downloads these into
the Fuseshot home folder. Nothing is downloaded from anywhere else, and a file that does not
match its hash is thrown away.

| What | Version | Licence | Source |
| --- | --- | --- | --- |
| three.js | 0.186.1 | MIT | npm `three` |
| Spark (Gaussian splat renderer) | 2.4.0 | MIT | npm `@sparkjsdev/spark` |
| Photo Sphere Viewer | 5.15.1 | MIT | npm `@photo-sphere-viewer/core` |
| YuNet face detector | 2023mar | MIT | OpenCV Zoo |
| SFace face recogniser | 2021dec | Apache-2.0 | OpenCV Zoo |
| MediaPipe face landmarker | float16/1 | Apache-2.0 | Google |
| Big LaMa inpainting model | Sanster release | Apache-2.0 | github.com/Sanster/models |

The SFace model card notes that its training data (MS-Celeb-1M derived) has its own terms.
Fuseshot uses it only to tell apart the people in your own photos, on your own machine.

## Separate programs

Fuseshot runs these as separate programs. It finds them on `PATH`, through the `[tools]`
section of `fuseshot.toml`, or through `FUSESHOT_HUGIN` and friends. `fuseshot doctor` says
which are missing and where to get them.

| Program | Licence | Needed for |
| --- | --- | --- |
| Hugin (pto_gen, cpfind, autooptimiser, nona, ...) | GPL-2.0-or-later | stitching |
| enblend / enfuse | GPL-2.0-or-later | stitching |
| COLMAP 3.12 or newer | BSD-3-Clause | splats |
| Brush | Apache-2.0 | splats |

The Docker image installs Hugin and enblend from Debian's packages. Their source is available
from Debian (`apt-get source hugin enblend-enfuse`), and their copyright files are kept in the
image under `/usr/share/doc`. The image does not include COLMAP or Brush, and the Windows build
bundles none of these programs.
