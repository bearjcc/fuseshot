import json

import cv2
import numpy as np
from conftest import scene
from PIL import Image

from fuseshot import cache, config, enhance, review, sources


def photo(seed=3):
    return scene(1600, 1200, seed)


def jpeg_bytes(img, quality):
    ok, data = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return data.tobytes()


def test_a_clean_sharp_photo_needs_nothing():
    m = enhance.measure(photo(), jpeg_bytes(photo(), 95), (4000, 3000))
    gain, chain, reasons = enhance.score(m)
    assert gain < enhance.MIN_GAIN and chain == [] and reasons == []


def test_jpeg_quality_comes_from_the_header():
    img = photo()
    assert abs(enhance.jpeg_quality(jpeg_bytes(img, 40)[:4096]) - 40) <= 2
    assert abs(enhance.jpeg_quality(jpeg_bytes(img, 90)[:4096]) - 90) <= 2
    assert enhance.jpeg_quality(b"not a jpeg") is None


def test_each_problem_suggests_its_step():
    img = photo()
    blurry = cv2.GaussianBlur(img, (0, 0), 4)
    noisy = np.clip(img + np.random.default_rng(1).normal(0, 12, img.shape), 0, 255).astype(np.uint8)
    cases = {
        "nafnet": enhance.measure(blurry, None, (4000, 3000)),
        "scunet": enhance.measure(noisy, None, (4000, 3000)),
        "fbcnn": enhance.measure(img, jpeg_bytes(img, 30), (4000, 3000)),
        "realesrgan": enhance.measure(img, None, (800, 600)),
    }
    for step, m in cases.items():
        gain, chain, reasons = enhance.score(m)
        assert step in chain, (step, m)
        assert gain >= enhance.MIN_GAIN and reasons


def old_scan():
    grey = cv2.cvtColor(cv2.GaussianBlur(photo(5), (0, 0), 1), cv2.COLOR_BGR2GRAY)
    sepia = cv2.merge([grey * 0.75, grey * 0.9, grey * 1.0]).clip(0, 255).astype(np.uint8)
    rng = np.random.default_rng(2)
    for _ in range(400):
        x, y = int(rng.integers(0, 1600)), int(rng.integers(0, 1200))
        cv2.circle(sepia, (x, y), 1, (250, 250, 250), -1)
    return sepia


def test_a_faded_scan_with_dust_gets_restoration_steps():
    m = enhance.measure(old_scan(), None, (1600, 1200))
    assert m["look"] == "sepia"
    gain, chain, reasons = enhance.score(m, camera=False)
    assert chain[0] == "lama" and "ddcolor" in chain and chain[-1] == "realesrgan"
    assert any("scan" in r for r in reasons)
    mono = enhance.measure(cv2.cvtColor(cv2.cvtColor(photo(), cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR))
    assert mono["look"] == "mono"


def test_scan_is_resumable_and_ranks_biggest_gain_first(home, tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    img = cv2.resize(photo(), (2800, 2100))
    Image.fromarray(img[..., ::-1]).save(folder / "fine.jpg", quality=95)
    Image.fromarray(cv2.resize(img, (640, 480))[..., ::-1]).save(folder / "small_bad.jpg", quality=25)
    Image.fromarray(cv2.GaussianBlur(img, (0, 0), 3)[..., ::-1]).save(folder / "soft.jpg", quality=90)
    db_path = home / "cache" / "assets.sqlite"
    db = cache.connect(db_path)
    sources.FolderSource(folder).sync(db)
    db.close()
    lib = sources.Library([sources.FolderSource(folder, db_path=db_path)], db_path)
    out = home / "work" / "enhance"
    first = enhance.scan(db_path, lib, out, limit=2)
    assert first["scored"] == 2
    found = enhance.scan(db_path, lib, out)
    assert found["scored"] == 3 and found["fine"] == 1
    names = [r["file_name"] for r in found["candidates"]]
    assert names == ["small_bad.jpg", "soft.jpg"]
    gains = [r["gain"] for r in found["candidates"]]
    assert gains == sorted(gains, reverse=True)
    assert len((out / "scores.jsonl").read_text().splitlines()) == 3
    assert enhance.thumb_path(out, found["candidates"][0]["id"]).exists()
    assert json.loads((out / "candidates.json").read_text())["candidates"][0]["chain"]


def test_rank_keeps_only_the_best(tmp_path):
    scores = tmp_path / "scores.jsonl"
    scores.write_text("".join(json.dumps({"v": enhance.VERSION, "id": str(i), "gain": float(i),
                                          "chain": [], "reasons": []}) + "\n" for i in range(100)))
    found = enhance.rank(scores, tmp_path / "c.json", keep=5)
    assert [r["id"] for r in found["candidates"]] == ["99", "98", "97", "96", "95"]


def test_enhance_is_off_until_turned_on(home):
    assert not config.wants("enhance") and config.wants("panoramas")
    (home / "fuseshot.toml").write_text("[outputs]\nenhance = true\n")
    config.reload()
    assert config.wants("enhance")


def test_the_review_page_lists_candidates(home):
    out = home / "work" / "enhance"
    out.mkdir(parents=True)
    (out / "candidates.json").write_text(json.dumps({"scored": 4, "fine": 3, "candidates": [
        {"id": "abc-1", "file_name": "<b>.jpg", "taken": "2020-01-02T00:00:00", "gain": 61.2,
         "chain": ["fbcnn", "realesrgan"], "reasons": ["JPEG saved at quality 30"]}]}))
    r = review.Review(home / "work")
    page = r.enhance_page()
    assert "61" in page and "JPEG saved at quality 30" in page and "fbcnn &gt; realesrgan" in page
    assert "<b>.jpg" not in page and "turned off" in page
    assert "id=\"enhancelink\"" in r.app_page() and "hidden" in r.app_page().split('id="enhancelink"')[1][:80]
