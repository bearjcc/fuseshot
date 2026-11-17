"""Small splat helpers."""


from fuseshot import splat


def test_read_json_and_gate():
    assert splat.gate_settings()["min_registered_ratio"] == splat.GATE_RATIO
    assert splat.splat_dir("/work", "O1").name == "O1"


def test_write_json_round_trip(tmp_path):
    path = tmp_path / "x.json"
    splat.write_json(path, {"a": 1})
    assert splat.read_json(path)["a"] == 1
    assert splat.read_json(tmp_path / "missing.json", {}) == {}
