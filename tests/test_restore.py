"""Restoring candidates: tiles, the difference gate, resuming, and the blind A/B answers."""

import json

import cv2
import numpy as np
import pytest
from conftest import scene

from fuseshot import enhance_review, restore, review


def test_tiles_cover_the_picture_and_join_without_seams():
    img = np.random.default_rng(0).random((700, 1100, 3)).astype(np.float32)
    out = restore.tiled(lambda t: t, img, tile=256, overlap=32)
    assert np.allclose(out, img, atol=1e-5)
    up = restore.tiled(lambda t: cv2.resize(t, (t.shape[1] * 2, t.shape[0] * 2), interpolation=cv2.INTER_NEAREST),
                       img, scale=2, tile=256, overlap=32)
    assert up.shape == (1400, 2200, 3) and np.allclose(up[::2, ::2], img, atol=1e-5)


def test_the_gate_lets_through_only_a_visible_change():
    img = scene(1200, 900, 3)
    assert restore.ssim_difference(img, img) < 1e-6
    tiny = np.clip(img.astype(np.int16) + 1, 0, 255).astype(np.uint8)
    assert restore.ssim_difference(img, tiny) < restore.MIN_DIFFERENCE
    soft = cv2.GaussianBlur(img, (0, 0), 3)
    assert restore.ssim_difference(soft, img) > restore.MIN_DIFFERENCE


def make_job(root, asset_id, chain, gain=80):
    folder = restore.results_dir(root) / asset_id
    folder.mkdir(parents=True)
    cv2.imwrite(str(folder / "original.jpg"), cv2.GaussianBlur(scene(640, 480, 5), (0, 0), 2.5))
    (folder / "job.json").write_text(json.dumps({"id": asset_id, "chain": chain, "gain": gain,
                                                 "original": "original.jpg", "file_name": "IMG_1.jpg",
                                                 "taken": "2021-05-06T10:00:00"}))
    return folder


def test_steps_resume_from_the_last_saved_one(tmp_path):
    folder = make_job(tmp_path, "a1", ["fbcnn", "nafnet"])
    job = restore.pending(folder)
    assert restore.latest(folder, job["chain"], 1).name == "original.jpg"
    sharp = scene(640, 480, 5)
    cv2.imwrite(str(restore.step_file(folder, 0, "fbcnn")), sharp)
    assert restore.latest(folder, job["chain"], 2).name == "step0-fbcnn.png"
    result = restore.finish(folder, job, 1.0)
    assert result["differs"] and (folder / "view-enhanced.jpg").exists()
    assert restore.pending(folder) is None
    job["chain"] = ["fbcnn", "nafnet", "realesrgan"]
    (folder / "job.json").write_text(json.dumps(job))
    assert restore.pending(folder) is not None


def test_dust_mask_finds_specks_not_edges():
    img = np.full((200, 200, 3), 128, np.uint8)
    cv2.rectangle(img, (0, 0), (100, 200), (40, 40, 40), -1)
    img[50:52, 150:152] = img[120:122, 160:162] = 255
    mask = restore.dust_mask(img)
    assert mask[50, 150] and mask[120, 160] and not mask[100:, 95:105].any()


def test_prepare_copies_originals_and_orders_the_chain(tmp_path):
    class Library:
        def download_original(self, asset_id, dest):
            dest.write_bytes(b"jpeg")

    chosen = restore.prepare(tmp_path, Library(), [
        {"id": "a", "gain": 90, "chain": ["realesrgan", "fbcnn"], "file_name": "a.JPG"},
        {"id": "b", "gain": 80, "chain": []}, {"id": "c", "gain": 70, "chain": ["lama"]}], top=1)
    assert [c["id"] for c in chosen] == ["a"]
    job = json.loads((restore.results_dir(tmp_path) / "a" / "job.json").read_text())
    assert job["chain"] == ["fbcnn", "realesrgan"] and job["original"] == "original.jpg"


def test_answers_are_choice_records_and_keep_never_touches_the_original(tmp_path):
    folder = make_job(tmp_path, "a1", ["nafnet"], gain=90)
    cv2.imwrite(str(restore.step_file(folder, 0, "nafnet")), scene(640, 480, 5))
    restore.finish(folder, restore.pending(folder), 1.0)
    same = make_job(tmp_path, "b2", ["nafnet"], gain=95)
    cv2.imwrite(str(restore.step_file(same, 0, "nafnet")), cv2.imread(str(same / "original.jpg")))
    restore.finish(same, restore.pending(same), 1.0)
    assert [r["id"] for r in enhance_review.queue(tmp_path)] == ["a1"]

    class Client:
        stacks, uploads = [], []

        def major_version(self):
            return 3

        def upload(self, data, filename, created_at, **kw):
            self.uploads.append(filename)
            return {"id": "new-1"}

        def create_stack(self, ids):
            self.stacks.append(ids)

    before = (folder / "original.jpg").read_bytes()
    row = enhance_review.record(tmp_path, "a1", "enhanced", "right")
    assert {"type", "id", "item", "direction", "value", "label", "note", "ts", "file"} <= set(row)
    assert enhance_review.keep(tmp_path, "a1", Client()) == {"asset_id": "new-1"}
    assert Client.stacks == [["new-1", "a1"]] and Client.uploads == ["2021-05-06 IMG_1 enhanced.jpg"]
    saved = enhance_review.keep(tmp_path, "a1", None)
    assert saved["file"].endswith("2021-05-06 IMG_1 enhanced.jpg")
    assert (folder / "original.jpg").read_bytes() == before
    assert enhance_review.queue(tmp_path) == []


def test_the_review_page_saves_a_blind_answer_and_only_exports_without_uploads(home):
    r = review.Review(home / "work")
    folder = make_job(r.enhance_dir(), "a1", ["nafnet"])
    cv2.imwrite(str(restore.step_file(folder, 0, "nafnet")), scene(640, 480, 5))
    restore.finish(folder, restore.pending(folder), 1.0)
    assert r.enhance_queue()["items"][0]["id"] == "a1"
    assert "compare.js" in r.compare_page()
    assert r.enhance_result_file("a1", "view-enhanced.jpg").exists()
    with pytest.raises(KeyError):
        r.enhance_result_file("a1", "../job.json")
    with pytest.raises(ValueError):
        r.enhance_decide({"id": "a1", "pick": "both", "enhanced_side": "left"})
    answer = r.enhance_decide({"id": "a1", "pick": "enhanced", "enhanced_side": "left"})
    assert answer["sent"]["file"].startswith(str(r.enhance_dir() / "kept"))
    assert enhance_review.decisions(r.enhance_dir())["a1"]["enhanced_side"] == "left"
