"""Small batch helpers."""

from fuseshot import batch


def test_job_params_and_folder_mb(tmp_path):
    job = {"type": "single-row rotation", "frames": [1, 2], "stitch_plan": ["rotation"], "extra": 1}
    assert batch.job_params(job) == {"type": "single-row rotation", "frames": [1, 2], "stitch_plan": ["rotation"]}
    f = tmp_path / "data.bin"
    f.write_bytes(b"x" * 2**20)
    assert batch.folder_mb(tmp_path) >= 1.0
