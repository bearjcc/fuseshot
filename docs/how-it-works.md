# How it works

The detail behind each step, for when a result looks wrong or you want to tune it.

## How sets are found

1. Photos are sorted by camera and time. Scanners, screenshots and photos with no
   camera make are skipped.
2. Copies of one moment (RAW+JPEG, phone `+1`, `.jxl` and edited copies) count as
   one shot.
3. A set ends when the gap between shots is longer than `--max-gap` or GPS jumps
   more than 250 m.
4. Sets of three or more are kept, except burst mode (median gap under 0.6 s, a
   span under 8 s, steady exposure and focal length) and small sets that are nearly
   all faces. Burst-mode runs go to `output/bursts.json` instead, named after their
   start time (`B20180311T135900`) so the `P` numbers stay put, and the action scan
   checks them. `detect --bursts-only` rewrites just that file.

Most cameras only record whole seconds, so the frames of a burst often share one
timestamp and the gap comes out as zero; that is still burst mode. Immich stacks
do not hide anything here: metadata search returns stacked assets either way, and
its totals match the server's asset statistics.

Each set gets a guessed type:

| Type | Clue |
| --- | --- |
| exposure bracket | shutter speed cycles in groups of 3/5/7/9 at fixed aperture and ISO |
| linear walk | GPS moves 30 m or more in a fairly straight line |
| multi-zoom layered | two or more focal lengths with several shots each |
| sphere or large multi-row | 30 or more positions |
| multi-row grid | 12 to 29 positions |
| single-row rotation | everything else |

Bracketed sets with several positions are reported as e.g. `multi-row grid (bracketed x3)`.

The confidence score (0 to 1) rises for a steady shooting rhythm, consistent exposure
and focal length, no faces and a tight GPS cluster, and falls for three-shot sets,
long pauses, mixed settings, faces and long sets. Sets of 25 or more positions shot
over at least 45 seconds around one spot are also flagged as possible Gaussian splat
sources.

## Output

`output/candidates.csv` has one row per set: set id, start time (local time where the
photo was taken), camera, shot count, focal range, exposure spread, GPS spread, guessed type, confidence, a link to the
first photo and all asset ids. `candidates.json` has the same data plus run
settings. `summary.txt` has the counts, the most confident sets and, if a sites file
is present, the sets found at each known place.

## Checking overlap

    fuseshot verify                       # all sets; rerun to continue
    fuseshot verify --stop-at 19:00       # stop measuring at that local time
    fuseshot verify --classify-only       # reuse cached matches and focal lengths
    fuseshot verify --sets P00012 P00034

`verify` downloads each set's preview images (about 1440 px; read only) one set at
a time, scales them to 1024 px and matches SIFT features on the CPU (two threads):
every pair for sets of up to 30 frames, and for larger sets the four neighbours of
each frame plus the eight likeliest partners from a quick coarse pass. Previews are kept in `cache/thumbs/` (about 1 GB for the whole
library) and matches in `cache/verify/`, so an interrupted run picks up where it
stopped and classification can be rerun without Immich. Immich does not keep the
35 mm equivalent focal length, so `verify` requests the first 128 KB of each original
(an HTTP range, read only) and keeps just that number in `cache/focal35.json`. It
includes the phone's digital zoom, which the lens focal length does not. For files
without the tag, one original per camera and focal length gives a crop factor
(`cache/crop_factors.json`). Originals are not kept. Memory use stays under about
100 MB.

For each pair that overlaps, the matches are fitted with a homography and with a
pure camera rotation at the set's field of view (from EXIF, refined within 20%).
A pair is:

| Pair | Test |
| --- | --- |
| same view | almost no shift |
| rotation | rotation fits nearly as well as the homography |
| zoom | rotation fits after allowing for a focal length change |
| planar | homography fits but rotation does not: camera moved over a flat or distant subject |
| parallax | neither fits well: camera moved around a 3D subject |

Pairs whose matches cover only a small part of one frame (a moving person or car)
are ignored. Frames are grouped into connected sub-sets; frames that do not overlap
anything are listed as junk. Each sub-set gets a motion model and a type:

| Motion | Rule | Types |
| --- | --- | --- |
| rotation | most moving pairs fit a rotation | single-row rotation, multi-row grid, sphere/360, multi-zoom layered, exposure bracket |
| planar | camera moved, little parallax | linear walk/translation |
| parallax | camera moved, strong parallax | linear walk/translation, object scan/walk-around |
| mixed | real rotation and real parallax pairs of the same subject | mixed motion |

Rotation sets get horizontal and vertical coverage in degrees from the solved camera
directions. Sets with little extra coverage, matches that do not follow the shooting
order (fireworks, moving subjects) or no three overlapping frames are marked not a
panorama. A walk is also rejected when people are the subject, when the camera
switches between portrait and landscape, or when frames turn more than 12 degrees
in the image plane (snapshots of a board game, a pet, a table). Walks have no
geometric test behind them, so they rank below verified rotations of the same size. Object scans and mixed sets with many well connected positions are flagged
as splat candidates.

Results go to `output/verified.json` and are merged into `candidates.csv`/`.json`
(verified type, confidence, motion model, stitch plan, coverage, rows, sub-sets,
junk frames, quality and a link to the best frame). `summary.txt` gets the verified
breakdown, the sets whose type changed from the guess, splat candidates and the best
panoramas.

## Stitching

Needs the Hugin command line tools (cpfind, autooptimiser, nona, enblend); see
[configuration](configuration.md#outside-programs) for how they are found. On Windows
without admin rights the Hugin MSI can be unpacked into the home folder with
`msiexec /a Hugin-<version>.msi /qn TARGETDIR=<home>\tools\hugin`.

    fuseshot fetch P00012                    # originals into work/P00012
    fuseshot align P00012                    # control points and optimisation
    fuseshot render P00012 cylindrical equirectangular --orientation centred
    fuseshot review                          # http://127.0.0.1:8765/

`fetch` downloads the originals of a candidate set (read only) and converts HEIC to
JPEG, since Hugin cannot read HEIC. `align` writes `optimised.pto` and `layout.json`
(overlapping pairs, gaps, connected groups, mean control point error). If the second
optimisation pass makes the error worse, the first solution is kept. `--threads`
sets cpfind threads; each one needs about 1.5 GB of RAM.

`align` picks the camera model itself. Control points are found once, then the set
is optimised both as a rotation about one point (normal panoramas) and as a mosaic
(camera position free, for flat subjects shot while moving). It keeps the model that
connects more images; if both connect the same images it keeps rotation unless
mosaic has clearly lower error (under 60% of it), or the overlap check saw camera
movement and mosaic fits at least as well. The choice, the reason and both scores
are in `layout.json` under `model`. `--model rotation` or `--model mosaic` forces
one; otherwise an override set on the review page is used.

Straightening (`autooptimiser -l`) looks for the plane the image horizons share.
When the images barely turn, a few frames or a narrow sweep, that plane is not
defined and the set can end up pointing at the zenith or nadir with every frame
rolled: it renders upside down or on its side and claims a full 360 by 180 degrees.
`align` checks for this and turns such a set back so its average view is on the
horizon and the frames stand the way most of them agree. A wide set is only turned
over when it is upside down as a whole. The correction is in `layout.json` under
`upright`.

Before that, `align` checks which frames the control points really hold
(`parts.py`). cpfind links almost any two frames with a stray point or two, and
the optimiser then puts a frame that shares nothing real with the rest wherever
it likes: across the middle of the panorama, or smeared over half the sphere
when it is a sweep panorama the camera made itself, treated as one wide-angle
frame. A frame is kept only when at least four points join it to the rest, its
own points fit the solution (not four times worse than the typical frame and
over 20 px per 1000 px of frame) and it is not a strip much longer than the
other frames. The largest joined group is optimised again on its own and becomes
the panorama; any other group of three or more frames becomes a set of its own,
`<set>-part2` and so on, with its frames copied in. Frames left out are listed
with the reason in `layout.json` under `repair`, and the project with every
frame is kept as `full.pto`. An orphan frame is never rendered. After that the
set is levelled: the up direction is the one every frame's horizontal edge is
most nearly square to (the same idea as Hugin's straighten), and the set is
turned so it points straight up.

The way the frames stand is a vote over their rolls. Albums often hold both the
sideways original of a portrait shot and a copy turned upright by hand or by its
EXIF orientation; both land in the same place, so the vote splits. Cameras save
landscape, so when a set mixes portrait and landscape frames the portrait ones
decide. Otherwise the larger group decides, unless a group a quarter or half turn
away has clearly more sky above it and ground below (`basis` in `upright` says
which rule won). A set whose frames are all sideways the same way, with no turned
copies, cannot be told from its rolls.

Layered sets (multi-zoom, duplicates) are blended by `blend.py` when enblend
cannot place a seam: exact duplicate frames are dropped, layers go down from the
widest to the most detailed so sharper frames win, each layer is matched in
exposure and colour to what is under it with a smooth gain map, and the edges are
joined with a feathered multi-band blend. `render.log` and `renders.json` say
which blender ran. Each render also keeps `<name>_cover.png`, the canvas area the
photos cover.

`render` writes 3000 px wide previews to `work/<set>/previews/`, each as the full
outline and as `_crop.jpg` cut to the largest filled rectangle. Projections:
rectilinear, cylindrical, equirectangular, stereographic, transverse-mercator,
panini. Orientation `level` keeps the horizon, `centred` points the view at the
middle of the set (use it for sets shot well above or below the horizon).

When a result is kept, the export renders it again at full size in
`work/<set>/full/`. It starts from the preview's own `.pto`, so the geometry,
crop and blending match what was reviewed, and only the canvas is scaled up.
The size is the native one unless it would not fit the memory budget (about 32
bytes a pixel while blending) or `max_megapixels`, and never more than 65500 px
a side. The export uses `_crop.jpg` unless the uncropped view was picked
when the panorama was kept, or `crop = false` is in the settings file; AI
fill only when it was picked. A finished render is reused until the preview changes. Group photos
are merged again at the size of the middle frame with the same base frame and
face choices; if a face no longer passes the checks at full size, the log says
so and that person keeps the base frame. Exported JPEGs get the first frame's
EXIF date, time offset, camera make and model and GPS, written in without
re-encoding the picture.

### AI fill

    fuseshot fill <set> --name <preview> [--device cpu] [--seams]

An alternative to cropping: paints the uncovered border with big-lama (LaMa
inpainting, downloaded on first use, run with torch from the `fill` extra). It
works in overlapping tiles at up to 1536 px and is scaled back up; real pixels are
only replaced inside the empty area grown by two pixels (and the seam bands with
`--seams`). It writes `<name>_fill.jpg`, `<name>_fillshow.jpg` with the invented
pixels tinted, and `<name>_fill_mask.png`, and records `ai_filled` with the model,
share, memory and time under `fill` in `renders.json` and `batch.json` so an
upload can label it. The model is loaded for the job and released at the end.
The review page queues one fill per view as its own job
([jobs](configuration.md#jobs); `work/<set>/fill_queue.json`); nothing is filled in bulk. A fill is refused
when more than half the picture is empty, since it would invent most of it (that
usually means a frame is misplaced); `--force` fills anyway.

`review` serves a page on your own machine, laid out like Immich. The front page is
one grid of everything to review: stitched sets, groups, action loops and trained
splats, in justified rows. The sidebar picks the queue (to review, kept, discarded,
being edited when an edit is running, everything) with a count of how many have a decision, the
chips filter by kind, the search box matches set ids, types, cameras and places,
and the queue order puts the sets the checks flagged (not rendered, poor fit, high
control point error, frames left out, loose parts, a doubtful type) first, worst
first. Click a tile's circle to select it (shift-click for a range, long press on a
phone), then keep or discard the whole selection from the bar that replaces the
top bar.

Opening a tile, or Review one by one, shows it full screen with the Info panel:
what the checks found, a note saved with the decision, the date, frames, stitching
model, error, coverage, camera and place, and the choices for the view, crop and
AI fill (with a toggle that shows the filled areas). Equirectangular previews open
in a panorama viewer, and the strip below shows every frame with the ones left out
greyed.

Each result gets one of three answers:

- **Keep** accepts it: it is copied to the output folder, and uploaded when uploads
  are on (see [Uploading to Immich](configuration.md#uploading-to-immich)), about 30
  seconds later so there is time to undo.
- **Discard** hides it from the queue. It is never exported or uploaded, later runs
  leave the set alone, and it stays under Discarded, where it can be kept after all.
- **Edit** opens the Edit section of the Info panel: the set type (saved to
  `work/types.json`), the stitching model (auto, rotation, mosaic; saved to
  `work/overrides.json`), the projection, orientation, crop and AI fill. Make the
  new version queues `fuseshot edit` as a job ([jobs](configuration.md#jobs)), so a
  new alignment or a full render runs on the job runner and not in the page. While
  it runs the result is under Being edited; when it is done it is back in To review,
  and `B` shows the version from before the edit (kept in `work/<set>/edit/`). Loops,
  group shots and splats are edited on their own pages.

Skip leaves a result for later. Deciding moves on to the next one at once and can be
undone. Decisions are saved to `work/reviews.json` (`"kind"` is `action`, `group`
or `splat` for those). Files saved by older versions, with approve, reject and needs
work, still read: approve is keep, reject is discard, and needs work is back in To
review with its note. The first start of `fuseshot review` after the update rewrites
the file in the new names and leaves the old one beside it as `reviews.json.bak`.
Stop it with Ctrl+C.

| Key | Does |
| --- | --- |
| `K`, `D` | keep, discard (and go on; on the grid, for the selection) |
| `E` | edit (for loops, group shots and splats, their own page) |
| space | skip, decide later |
| `U` or Ctrl+Z | undo the last decision |
| left, right | previous, next |
| `I`, `O`, `V`, `Z` | Info panel, original frames, view as a sphere, zoom |
| `B` | tap to compare with the version before an edit or a repair, hold to peek |
| `X`, Ctrl+A, shift-click | select one, all shown, a range |
| Enter, `/`, `?`, Esc | review one by one, search, all shortcuts, close |

On a phone, swipe right to keep, left to discard, up to edit and down to skip; pinch to zoom and double tap to zoom in or out. The old tables are still at `/tables`,
with the per-set, loop, group and splat pages.

Previews are shown as scaled copies (kept in `cache/web/`); click one to zoom and drag around the
full-size image. `--host`, `--port` and `--base-path` (or `FUSESHOT_REVIEW_HOST`, `FUSESHOT_REVIEW_PORT`
and `FUSESHOT_BASE_PATH`) change where it listens. To serve it behind a reverse proxy under a path:

    fuseshot review --base-path /fuseshot

`--redirect-from /old` sends links under an old base path on to the same page under the
new one (301).

## Batch previews

    fuseshot batch --plan          # pick sets, write work/batch_plan.json
    fuseshot batch --chunk 0       # stitch one chunk of the plan
    fuseshot batch --status        # counts, time per set, disk used

`--plan` takes the high confidence (0.75 and up) single-row, multi-zoom, multi-row
and bracket sets from `verified.json`, leaves out walks, object scans, mixed sets
and anything in `--exclude`, and splits them into chunks of 20. Each set is fetched,
aligned with the automatic model choice, and rendered once at 3000 px in a
projection picked from its coverage: rectilinear up to 100 degrees wide,
cylindrical up to 200, equirectangular beyond that or when taller than 110.
Brackets are fused per camera position with enfuse before blending.

Results go to `work/<set>/batch.json` with status `stitched`, `poor fit` (images
left unconnected or high error) or `failed` (with the error). Finished sets are
skipped, so a chunk can be stopped and started again; `--retry-failed` runs failed
ones again. Creating `work/batch.stop` stops a running chunk after the current set.
Once a set is done only the projects, the previews and the converted images the
project uses are kept. `--max-mb` stops any tool that goes over that much memory.

`--straighten` applies the upright check to sets that were already stitched and
renders the ones it turns again; the old previews move to `previews/before-upright/`
(or `before-upright-2` and so on, never over an earlier one) and the record keeps
the old projection and coverage under `before_upright`. Sets already checked are
skipped unless `--recheck` is given.

`--repair` applies the orphan check and the levelling to sets that were already
stitched (`--sets` for some of them): it leaves out loose frames, optimises
what is left again, makes the part sets, levels the row and renders again. The
old previews move to `previews/before-repair/` and the record keeps the old
values under `before_repair`. Sets already checked are skipped unless
`--recheck` is given.

On a shared machine each chunk can go through a job queue, one job per chunk.

## Set types

Each set has a type that decides what is made from it:

| Type | Made into |
| --- | --- |
| panorama | a stitched panorama |
| object scan | a Gaussian splat |
| group | a best-shot merge |
| action | a stabilised loop |
| not | nothing |

The type comes from `verify` and the group and action checks below. One picked on the review
page is saved to `work/types.json` and wins.

## Group shots

    fuseshot group --scan          # write output/groups.json
    fuseshot group --all           # merge every group set
    fuseshot group P00903

A group shot is a few frames of a posed group taken from one spot, where somebody
is always blinking or looking away. The scan runs before the action check and,
like it, reuses the cached matches and previews. A set is a group when:

1. it is not an exposure bracket and shots are less than 10 seconds apart on
   median, over at most 2 minutes;
2. it has a run of consecutive still shots (background homography moving the
   corners by less than 15%) covering at least half the set, and at least three
   shots when there are three or more;
3. at least 60% of the run shows two or more faces (YuNet), as two people posing
   is already a group shot;
4. at least 70% of the faces stay put after lining up the background, so candid
   sets where people move about (opening presents) stay action;
5. the camera does not pan: chained together, the still run's frame centres
   drift less than a quarter of the frame width and cover less than 1.25 frames,
   and no stretch of the set covers 1.5 frames or more. Panoramas with people in
   them stay panoramas.

The merge fetches the originals of the still run into `work/groups/<set>/`, finds
faces with YuNet, follows each person across frames with SFace embeddings, and
scores every face on open eyes and smile (MediaPipe Face Landmarker blendshapes),
head pose, sharpness and exposure. The best base frame is kept and, for each
person whose best face beats the base by a margin, the donor face is lined up by
the background homography and a five-point fit and blended in with Poisson
cloning, keeping neighbours' heads out of the patch. A swap is skipped when the
donor is softer, the face turned or changed size, the landmarks do not fit or the
edge of the patch does not match. The outputs are `merged.jpg`, `base.jpg`,
`overlay.jpg` (which frame each face came from), face crops and `group.json`
(`composite: true`, method, base frame and source frame per person). On the
review page a click on a face picks another frame for it and queues a re-merge.

The models (YuNet and SFace from the OpenCV model zoo, MediaPipe's face
landmarker) are downloaded on first use and checked against pinned hashes. MediaPipe
installs into the same venv as Fuseshot (`fuseshot[group]`); both use
opencv-contrib-python. Group sets are left out of action loops and splats.

## Action shots

    fuseshot action --scan         # write output/actions.json
    fuseshot action --chunk 0      # make loops for one chunk of 20
    fuseshot action --sets P00023

An action shot is a quick burst from a camera held still while the subject moves:
people posing, a handstand, a gift being opened. The scan reuses the cached `verify`
matches (or matches the cached previews itself when a pair was not measured) and
calls a set, a burst-mode run or a stitch job action when:

1. consecutive shots are less than 5 seconds apart on median and the burst lasts
   at most 2 minutes, with the same exposure (brackets are not action);
2. the background lines up between neighbours with a homography that moves the
   frame corners by less than 10% of the frame, with enough inliers;
3. after lining up, between 4% (median) and 60% of the frame still differs, so
   something moved, and the camera has not drifted more than 25% over the burst
   (that is a slow pan, which belongs to the panorama side);
4. at least three such shots cover 60% or more of the set.

Copies of one moment (identical framing and content) count once. Burst-mode runs
from `output/bursts.json` have no cached `verify` matches, so the scan fetches grey
copies of their Immich previews into `cache/thumbs/` (read only) and matches those.

Loops are made from the Immich previews (read only, kept in `cache/colour/`).
Each frame is registered to the middle one on its background with ORB features
and RANSAC, cropped to the area every frame covers, and written to
`work/actions/<set>/` as `animation.webp` and `animation.mp4` (H.264, about 3 s,
looping), plus `animation.gif` when that stays under 4 MB, `poster.jpg` and
`action.json`. Sets that already have a loop are skipped, and `work/batch.stop`
stops a chunk after the current set.

### Motion photos and Live Photos

    fuseshot action --motion       # write output/motion.json

A Pixel `.MP.jpg` or a Samsung motion photo carries a short MP4 after the JPEG
image, and an Apple Live Photo is a HEIC or JPEG with a MOV of the same name. The
pairing is cheap: Immich already links the clip to the photo; in a folder library
the MOV must have the photo's name and, when both carry a time, start within 3
seconds of it, and for a JPEG only the first 128 KB are read for the XMP that says
where the MP4 starts (`GCamera:MicroVideoOffset`, or a `Container:Item` with the
`MotionPhoto` semantic; older Samsung files are found by their `MotionPhoto_Data`
mark). What was found in each JPEG is kept in the cache, so unchanged files are not
read again.

For each clip six frames are compared with the same test as bursts: the
background moves the corners by less than 10% between samples and less than 25%
over the clip, and between 4% and 60% of the frame moves. Those stills show up on
the review page as loops to keep. Until one is kept the page plays the clip
itself, cut out of the JPEG or copied from the MOV without encoding it again;
keeping it makes the loop files above from about 12 frames a second of the clip
(at most 36 frames), registered and cropped the same way.

### Wigglegrams

    fuseshot action --wiggle           # make one for each stereo photo

Stereo photos are MPO files (two JPEGs in one, as Fujifilm W3 and Nintendo 3DS
write them) and side-by-side JPEGs from an XREAL Beam Pro (`SV_` names or XREAL
as the make, 8:3 or 32:9). The two views are lined up on the subject: a face if
OpenCV finds one, else the most salient spot (spectral residual saliency), else
the middle. ORB matches between the views give the vertical offset from all of
them and the sideways one from those near the subject, so the subject stays still
and the rest rocks around it. The two frames are cropped to what both views show
and written like a loop (`work/actions/wiggle-<asset>/`, 140 ms a frame). `run`
makes them when wigglegrams are on in `[outputs]`; a photo that already has one,
or that failed (`failed.json`), is skipped.

### Action composites

A burst from a camera that stayed still can also become one still with every
moment of the subject in it (a stroboscopic or chronophotograph). When a loop is
made, the median of its lined-up frames is the scene with the subject taken out;
each frame's difference from it, cleaned of speckle, is where the subject was.
Frames are picked from first to last whose subject does not overlap one already
placed (at most 12), and those are pasted onto the background with a soft edge
as `composite.jpg` next to the loop. No composite is made when fewer than three
moments come out clear of each other, or when more than 30% of the frame moves
(water, crowds). The viewer's composite button (C) switches between the loop and
the composite; keeping the loop exports both. `composites = false` in
`[outputs]` turns them off.

### Moving subjects

A splat needs the scene to hold still. `fuseshot moving` runs an object detector
(EfficientDet-Lite2, Apache-2.0, through MediaPipe) over each set's thumbnails.
A person, animal or vehicle must cover at least 2% of the frame in at least 40% of
the shots and must shift enough between neighbouring frames; presence alone is not
enough (statues and static displays stay eligible). Boats are left out, since moored
boats hold still. Marked sets are skipped by the splat plan and made into loops from
all their frames. Insects and other subjects the detector does not know can be marked
by hand with `fuseshot moving --mark SET` (and `--unmark`). Overrides from the
review page live in `work/moving_overrides.json`. Scan results go to
`output/moving.json`; the full run does this step when mediapipe is installed.

## Gaussian splats

    fuseshot splat --plan              # rank the sets, write work/splat_plan.json
    fuseshot splat P00045 --sfm        # pose one set with COLMAP (CPU)
    fuseshot splat P00045 --train      # SfM if needed, then train with Brush
    fuseshot splat --status

The plan takes object scans first, then mixed-motion sets and walks with at least
4 parallax pairs, and leaves out anything with fewer than 8 usable frames. Within a
kind sets are ranked on usable frames, sequential overlap, the share of parallax
pairs and how sharp the cached thumbnails are.

For each set the full-size originals already fetched for stitching are reused (or
fetched read only), junk frames dropped, turned upright and scaled to 1600 px on the
long side in `work/splats/<set>/images/`. COLMAP extracts SIFT features with one
shared SIMPLE_RADIAL camera seeded from the 35 mm focal length, matches every pair
and runs the incremental mapper. The largest model has to pass a gate before any
GPU time is spent on it: at least 60% of the frames and at least 8 frames
registered, with a mean reprojection error of 1.5 px or less. The result, pass or
fail with the reason, is in `sfm.json` and on the review page. Passing models are
undistorted into `dataset/` for training.

### Stereo sets

    fuseshot splat --stereo-video SV_20250601_153000.mp4 --train

When every photo in a set is a stereo photo (an MPO, or a side-by-side shot from
an XREAL Beam Pro), both views go in: the left ones in `images/left/`, the right
ones in `images/right/` under the same names. COLMAP gets one camera per folder,
and `rig_configurator` ties each pair into one frame with the right lens 50 mm to
the side of the left (`rig_config.json`). The mapper keeps that offset fixed, so
the model is roughly in metres when the baseline is right. A set where only some
photos are stereo uses their left view, like any other photo. A side-by-side
video becomes its own set (`SV-<name>`): about 4 frames a second, at most 60
pairs, posed the same way. It joins the plan as a stereo video, after object
scans.

Brush trains 7000 steps by default with at most 2 million splats at 1600 px, and
writes a checkpoint every 1000 steps to `train/`. A stopped run picks up from the
last checkpoint. The finished model is converted to `scene.splat` (32 bytes a splat,
the faintest dropped, at most 1.5 million kept) for the page; `splat.json` has the
time and size.

Training needs a GPU and takes a while, so on a shared machine it is best sent to a
queue such as [Pueue](https://github.com/Nukesor/pueue), one job per set. A lock file
per set keeps SfM and training for one set from running at once.

The review page lists the plan under Splats, with the gate result, and opens each
trained set in a viewer under `/splat/<set>` built on Spark and three.js. Those are
downloaded once from npm, checked, and served from the home folder, so the page loads
nothing from a CDN. Decisions are saved to
`work/reviews.json` with kind `splat`.

The same COLMAP poses are the planned fallback for panoramas that fit neither the
rotation nor the mosaic model well: with the cameras known, each
frame can be reprojected onto a fitted plane or cylinder, or a trained splat
rendered from one viewpoint with a wide lens. Not built yet.

COLMAP 3.12 or newer and Brush come from their GitHub releases; `fuseshot doctor` says
whether they were found.

## Failures

A set that fails to stitch, or a loop or wigglegram that fails, is saved as failed along
with a fingerprint of the code that ran and the parameters it ran with. Later runs leave it
alone until one of those changes (a new version of Fuseshot, or a different frame count or
stitch plan), and then try it again, at most 3 times in all. Failures in the sidebar lists
them with their errors; Retry asks for one more try on the next run and starts the count
again. `fuseshot batch --retry-failed` still retries every failed set.

## Enhance candidates

`fuseshot enhance-scan` looks for photos that restoration could help. It is off until
`enhance = true` is set under `[outputs]` (or the toggle in Settings), and it only reads:
it fetches each photo's preview (about 1440 px) and, for JPEGs, the first 64 KB of the
original, and changes nothing in Immich or the folders.

Each photo gets a few cheap CPU checks. A problem counts only when a fix would show:

| Check | How | Counts when | Suggested step |
|---|---|---|---|
| Dust and scratches | small, sharp light or dark marks from a morphological top-hat | black-and-white or faded photos with marks on over 0.3% of the picture | inpaint (LaMa) |
| JPEG blocking | for originals saved below quality 85, the jump across 8×8 block edges against the jump inside blocks, on the original | over 1.6 times | JPEG artefact removal (FBCNN) |
| Noise | Immerkær's fast noise estimate on the flatter parts of the preview, scaled to the original's size | over 2 grey levels | denoise (SCUNet) |
| Softness | variance of the Laplacian, skipped for scenes with few edges | under 60 | deblur (NAFNet) |
| Low resolution | the original's long side | under 1000 px, growing steadily to the most at 300 px | upscale (Real-ESRGAN) |
| Black and white | mean chroma and hue in Lab, and no camera details | only with `colourise = true` under `[outputs]`, since DDColor comes in the optional model pack | colourise (DDColor) |

Each problem has a severity from 0 to 1 and a weight for how much its fix shows. Only
the three biggest count, and only those worth at least 10 points; the expected gain is
`100 × (1 − Π(1 − weight × severity))` over them. Photos under 50 are taken as fine.

Graphics are left out too, since restoration does not help them: PNG, GIF and WebP files
(cameras save JPEG or raw), and pictures that look like text, memes, tables or drawings.
For those the preview is cut into 8×8 tiles, and a picture counts as a graphic when at
least 25% of the tiles are one flat colour (standard deviation under 1.5 grey levels) and
at least 3% of its pixels sit on a crisp edge (Sobel magnitude over 200), or, with no camera
details, when it is black and white with crisp edges on at least 2% (printed text on a
grey page). A JPEG whose EXIF names a camera and an exposure or focal length is never a
graphic on its looks alone; if it looks like a sign or a page, only the dust step is dropped.
Scores and measurements are appended to `work/enhance/scores.jsonl` one photo at a time, so
a scan can be stopped (`work/batch.stop`) and run again, and it skips photos already
scored. The ranking is worked out again from the measurements each time, so turning
colourise on or off needs no new scan. The best 500 go to `work/enhance/candidates.json`,
biggest gain first, with the suggested chain and the reasons. `--limit N` scores only the
newest N photos.

The review page lists them under Enhance candidates in the sidebar. No face restorer is
suggested by default (none has a permissive licence), and no no-reference quality model is used: the usual ones (pyiqa with MUSIQ or CLIP-IQA
weights) are under non-commercial licences.

### Running the chains

`fuseshot enhance-run --top 20` copies the originals of the best 20 candidates into
`work/enhance/results/<id>/`, fetches the models it needs (checked against their SHA-256),
and queues one GPU job, `fuseshot enhance-apply`. The job needs the `enhance` extra (torch
and spandrel) and reads and writes only those folders. It runs one model at a time over
every photo that needs it, in the order dust, JPEG, noise, blur, upscale, and saves each step
as it finishes, so a stopped job carries on where it left off. Large photos are cut into
512 px tiles with feathered overlaps. Upscaling stops at 3000 px on the long side. Peak GPU
memory for each model is written to `work/enhance/results/vram.json`.

| Step | Model | Licence |
|---|---|---|
| Dust and scratches | big-lama (TorchScript, as for AI fill), on the marks the scan found | Apache-2.0 |
| JPEG blocking | FBCNN colour | Apache-2.0 |
| Noise | SCUNet real PSNR | Apache-2.0 |
| Softness | NAFNet GoPro width 64 | MIT |
| Low resolution | Real-ESRGAN x4plus | BSD-3-Clause |

Each result gets a difference score, 1 − SSIM on grey copies at most 1024 px long. Results
under 0.02 look the same as the original and are not shown.

### Optional model packs

Two models are not part of Fuseshot and are never fetched on their own. Settings (and
the first-run setup) lists them under Optional models with an Install button each. The
button shows the licence in plain words with a link to the full text; only after Agree is
the file downloaded into the models folder and checked against its SHA-256, and the
agreement is saved beside it as `<pack>.agreed.json`.

| Pack | Model | Licence | Used for |
|---|---|---|---|
| Face repair | CodeFormer | S-Lab License 1.0 (non-commercial) | redrawing faces after Face looks wrong |
| Colourising | DDColor (ModelScope weights) | Apache-2.0 | black-and-white scans, with `colourise = true` |

Both load through `spandrel_extra_arches`, which has to be added to the Python that runs
the GPU jobs (`fill_python` under `[jobs]`); Settings says how when it is missing. Steps
from a pack that is not installed are left out of the chain. Face repair finds faces with
YuNet, redraws each one at 512 px and blends it back with a soft round edge.

### Comparing

Compare restored photos (from the candidates page) shows each result beside its original
with the sides chosen at random, biggest expected gain first. Keep A (1), Keep B (2) or Face
looks wrong (F). The answers go to `work/enhance/decisions.jsonl`, one line each, in the
record shape Choice uses (`type`, `id`, `item`, `direction`, `value`, `label`, `note`, `ts`,
`file`, plus `enhanced_side`); the latest line for a photo wins. Face looks wrong keeps the
original; with Face repair installed it also queues the result again with the face pass at
the end, and the new result comes back to Compare when it is done. Keeping the enhanced photo never changes the original: with uploads on it is
added to Immich as a new asset and stacked with the original, on top; with uploads off it is
copied to `work/enhance/kept/`.
