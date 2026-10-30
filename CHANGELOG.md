# Changelog

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
