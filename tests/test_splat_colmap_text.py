"""Splat COLMAP text-model helpers without COLMAP."""


import numpy as np

from fuseshot import splat


def test_rotation_quaternion_to_matrix():
    q = (1.0, 0.0, 0.0, 0.0)
    r = splat.rotation(q)
    assert np.allclose(r, np.eye(3))


def test_read_text_model_and_starting_view(tmp_path):
    model = tmp_path / "sparse" / "0"
    model.mkdir(parents=True)
    (model / "cameras.txt").write_text(
        "#\n1 SIMPLE_RADIAL 640 480 400 320 240 0\n", encoding="ascii"
    )
    (model / "images.txt").write_text(
        "#\n1 1 0 0 0 0 0 0 1 001.jpg\n0 0 -1\n", encoding="ascii"
    )
    (model / "points3D.txt").write_text(
        "#\n1 0.1 0.2 1.0 0 0 0\n2 0.2 0.3 1.1 0 0 0\n", encoding="ascii"
    )
    cams, images, pts = splat.read_text_model(model)
    assert len(cams) == 1 and len(images) == 1 and pts.shape[0] == 2
    view = splat.starting_view(model)
    assert view and "fov" in view and view["fov"] > 0


def test_exports_and_ply_complete(tmp_path):
    ply = tmp_path / "export_0001.ply"
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        "element vertex 2\nproperty float x\nproperty float y\nproperty float z\n"
        "end_header\n"
    )
    body = np.zeros(2 * 3 * 4, dtype=np.uint8).tobytes()
    ply.write_bytes(header.encode("ascii") + body)
    found = splat.exports(tmp_path)
    assert found and found[0][0] == 1
    assert splat.ply_complete(ply)
    assert not splat.ply_complete(tmp_path / "missing.ply")
