"""Optional model packs: nothing is fetched until the licence is agreed to."""

import json

import numpy as np
import pytest

from fuseshot import assets, packs, restore


def test_packs_are_off_until_agreed_and_never_fetched_otherwise(home, monkeypatch):
    fetched = []
    monkeypatch.setattr(assets, "download_model", lambda name: fetched.append(name))
    found = packs.status()
    assert set(found) == {"face", "colourise"} and not any(p["installed"] for p in found.values())
    assert all(p["link"].startswith("https://") and p["plain"] for p in found.values())
    with pytest.raises(ValueError):
        packs.install("face", agree=False)
    with pytest.raises(ValueError):
        packs.install("face", agree="yes")
    assert fetched == []
    assert assets.MODELS["codeformer"]["for"] not in ("viewer", "group", "fill")
    assert restore.usable(["fbcnn", "ddcolor", "codeformer"]) == ["fbcnn"]


def test_agreeing_downloads_and_records_the_licence(home, monkeypatch):
    def fake_download(name):
        path = assets.models_dir() / assets.MODELS[name]["file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"weights")

    monkeypatch.setattr(assets, "download_model", fake_download)
    done = packs.install("colourise", agree=True)
    assert done["installed"] and not packs.installed("face")
    record = json.loads(packs.agreement_file("colourise").read_text())
    assert record["licence"] == "Apache License 2.0" and record["agreed"]
    assert restore.usable(["realesrgan", "ddcolor"]) == ["ddcolor", "realesrgan"]


def test_a_face_pass_is_added_once(tmp_path):
    folder = restore.results_dir(tmp_path) / "a"
    folder.mkdir(parents=True)
    (folder / "job.json").write_text(json.dumps({"id": "a", "chain": ["nafnet"], "original": "o.jpg"}))
    assert restore.add_face_pass(tmp_path, "a")
    assert json.loads((folder / "job.json").read_text())["chain"] == ["nafnet", "codeformer"]
    assert not restore.add_face_pass(tmp_path, "a")


def test_face_boxes_stay_inside_the_picture(monkeypatch):
    img = np.zeros((300, 400, 3), np.uint8)
    monkeypatch.setattr(restore, "faces", lambda bgr: [(350, 250, 60, 60), (10, 10, 10, 10)])
    boxes = restore.face_boxes(img)
    assert len(boxes) == 1
    x, y, side = boxes[0]
    assert x >= 0 and y >= 0 and x + side <= 400 and y + side <= 300
    mask = restore.feather(64)
    assert mask[32, 32] == 1 and mask[0, 0] == 0
