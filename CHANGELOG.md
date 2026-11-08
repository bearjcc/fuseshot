# Changelog

## Unreleased

- `fuseshot enhance-scan` ranks photos that restoration could help (blur,
  noise, JPEG quality, low resolution, faded scans, dust), with a suggested chain
  of steps and the reasons, listed read-only on the review page. Off by default.
  Blocking is measured on the original, size counts only under 1000 px, and
  black-and-white photos count only with the optional `colourise` output.
  Screenshots, memes, tables and other text images are left out.
- `fuseshot enhance-run` restores the best candidates on the GPU (LaMa, FBCNN,
  SCUNet, NAFNet, Real-ESRGAN through spandrel), resumably and in tiles, and the
  review page compares each result blind against its original. Kept results are
  stacked with the original in Immich when uploads are on.
- A setup page the first time the review page opens with nothing set up, and
  Settings in the sidebar after that: Immich address and key (with a connection
  test), photo folders (with a folder browser), the output folder, and which
  kinds of results to make, each with whether its tools are installed. Then a
  button to start the first scan. `[outputs]` in `fuseshot.toml` turns kinds off.
- Wigglegrams of stereo photos (MPO files and side-by-side JPEGs from an XREAL
  Beam Pro), lined up on the subject and reviewed like loops.
- Action composites: a burst from a still camera also becomes one still with
  each clear moment of the subject on the background, shown beside the loop
  and exported with it.
- Splats from stereo photos and side-by-side stereo video: both views, posed by
  COLMAP as a two-camera rig with a fixed 50 mm baseline.

## 0.1.0

First public release.

- Finds shot sets in Immich or folders from metadata, and checks them by matching
  features between previews.
- Stitches panoramas with Hugin in several projections, with crop or AI fill for the
  edges.
- Group shots with everyone's best face, action loops, and experimental Gaussian
  splats with COLMAP and Brush.
- A review page to keep, discard or edit each result (edits make a new version
  as a background job); `export` copies kept results to a folder.
- Optional upload of kept results to Immich as new photos, with date, place and
  a description (AI-filled edges are marked), into an album if you like. Off by
  default; never changes or deletes existing photos.
- `fuseshot run` for the usual pass (find, check, stitch, loops), `fuseshot doctor`
  to see what is missing.
- pip package, Docker image and a Windows build.
