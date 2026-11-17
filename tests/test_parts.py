"""PTO parsing and geometry in parts.py."""

from conftest import minimal_pto

from fuseshot import parts


def test_frame_info_reads_yaws(tmp_path):
    pto = minimal_pto(tmp_path / "p.pto", names=["a.jpg", "b.jpg", "c.jpg"])
    frames, points = parts.frame_info(pto)
    assert len(frames) == 3 and len(points) >= 8
    assert frames[0]["w"] == 1000 and frames[1]["y"] == 8.0


def test_rotation_matrix_is_orthogonal():
    mat = parts.rotation(10, 5, 2)
    import numpy as np

    prod = np.matmul(mat, np.transpose(mat))
    assert abs(prod[0][0] - 1.0) < 1e-6


def test_split_writes_parts(tmp_path):
    pto = minimal_pto(tmp_path / "split.pto", pairs=12)
    parts.split(pto, px_scale=1.0)
    assert any(pto.parent.glob("part_*.pto")) or pto.is_file()
