"""group.track identity clustering."""

import numpy as np

from fuseshot import group


def test_track_groups_faces_across_frames():
    faces = []
    for frame in range(4):
        faces.append({
            "frame": frame,
            "box": [50 + frame, 40, 30, 35],
            "centre": [65.0 + frame * 5, 55.0],
            "feature": np.ones(8, dtype=np.float32) * (1 + frame * 0.01),
        })
    tracks = group.track(faces, 4)
    assert tracks and len(tracks[0]["faces"]) >= 2
