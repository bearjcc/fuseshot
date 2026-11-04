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
        "fbcnn": enhance.measure(blurry, jpeg_bytes(blurry, 8), (4000, 3000), jpeg_bytes(blurry, 8)),
        "realesrgan": enhance.measure(img, None, (320, 240)),
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
    assert chain == ["lama"] and "ddcolor" not in chain
    gain, chain, reasons = enhance.score(m, camera=False, colourise=True)
    assert chain == ["lama", "ddcolor"] and any("scan" in r for r in reasons)
    mono = enhance.measure(cv2.cvtColor(cv2.cvtColor(photo(), cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR))
    assert mono["look"] == "mono"


def test_scan_is_resumable_and_ranks_biggest_gain_first(home, tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    img = cv2.resize(photo(), (2800, 2100))
    Image.fromarray(img[..., ::-1]).save(folder / "fine.jpg", quality=95)
    Image.fromarray(cv2.resize(img, (320, 240))[..., ::-1]).save(folder / "small_bad.jpg", quality=10)
    Image.fromarray(cv2.GaussianBlur(img, (0, 0), 8)[..., ::-1]).save(folder / "soft.jpg", quality=90)
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


def test_blocking_must_show_not_just_a_low_quality_setting():
    img = photo()
    flat = cv2.GaussianBlur(img, (0, 0), 6)
    assert enhance.original_blockiness(jpeg_bytes(img, 95)) < 1.1
    assert enhance.original_blockiness(jpeg_bytes(flat, 10)) > enhance.BLOCKY
    busy = enhance.measure(img, jpeg_bytes(img, 60), (4000, 3000), jpeg_bytes(img, 60))
    assert busy["jpeg_quality"] < 75 and "fbcnn" not in enhance.score(busy)[1]


def test_size_counts_only_when_small_and_grows_smoothly():
    base = enhance.measure(photo(), None, (4000, 3000))
    gains = [enhance.score({**base, "long_side": side})[0] for side in (2400, 1000, 900, 700, 500, 300)]
    assert gains[0] == gains[1] == 0
    assert gains == sorted(gains) and gains[-1] - gains[-2] < 25


def test_black_and_white_alone_is_not_a_candidate():
    clean = cv2.GaussianBlur(photo(), (0, 0), 3)
    mono = cv2.cvtColor(cv2.cvtColor(clean, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    m = {**enhance.measure(mono, None, (4000, 3000)), "laplacian": 300}
    assert enhance.score(m, camera=False) == (0.0, [], [])
    assert enhance.score(m, camera=False, colourise=True)[1] == ["ddcolor"]


def test_chains_stay_short_and_rank_rescores_from_measurements(tmp_path):
    m = {"laplacian": 5, "edges": 0.1, "noise": 9, "blockiness": 2, "look": "mono", "specks": 0.02,
         "long_side": 400, "camera": False}
    gain, chain, _ = enhance.score(m, camera=False, colourise=True)
    assert len(chain) == enhance.MAX_STEPS and gain <= 100
    scores = tmp_path / "scores.jsonl"
    scores.write_text(json.dumps({"v": enhance.VERSION, "id": "a", "gain": 0, "chain": [], "reasons": [],
                                  "measures": m}) + "\n")
    found = enhance.rank(scores, tmp_path / "c.json", colourise=False)
    assert found["candidates"][0]["gain"] > 50 and "ddcolor" not in found["candidates"][0]["chain"]


def test_screenshots_and_drawings_are_left_out(tmp_path):
    m = {"laplacian": 300, "edges": 0.1, "noise": 0.2, "blockiness": None, "look": "mono", "specks": 0.02,
         "long_side": 400, "camera": False}
    rows = [{"v": enhance.VERSION, "id": name, "file_name": name, "gain": 0, "chain": [], "reasons": [],
             "measures": {**m, "camera": camera}} for name, camera in
            (("Screenshot.png", False), ("scan.jpg", False), ("phone.png", True), ("phone.jpg", True))]
    scores = tmp_path / "scores.jsonl"
    scores.write_text("".join(json.dumps(r) + "\n" for r in rows))
    found = enhance.rank(scores, tmp_path / "c.json", colourise=False)
    assert sorted(r["id"] for r in found["candidates"]) == ["phone.jpg", "scan.jpg"]


def text_page(lines=18):
    page = np.full((900, 1200, 3), 245, np.uint8)
    for i in range(lines):
        cv2.putText(page, f"It was an insurance scam, line {i}", (30, 45 + i * 46), cv2.FONT_HERSHEY_SIMPLEX,
                    1, (15, 15, 15), 2)
    return page


def score_table():
    table = np.full((1200, 800, 3), 255, np.uint8)
    for i in range(30):
        cv2.line(table, (0, i * 40), (800, i * 40), (0, 0, 0), 1)
        cv2.putText(table, f"P{i}  Player {i}   {i * 3}", (10, i * 40 + 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 1)
    return table


def measured(img, camera):
    m = enhance.measure(img, size=img.shape[1::-1])
    return {**m, "camera": camera}


def test_text_tables_and_memes_are_graphics_and_photos_are_not():
    meme = photo().copy()
    meme[-260:] = 255
    for i in range(4):
        cv2.putText(meme, f"when the caption {i}", (20, meme.shape[0] - 220 + i * 55), cv2.FONT_HERSHEY_SIMPLEX,
                    1.2, (0, 0, 0), 3)
    for img in (text_page(), score_table(), meme):
        assert enhance.graphic("x.jpg", True, measured(img, True))
    grey_print = cv2.GaussianBlur(text_page(), (0, 0), 0.6)
    grey_print = np.clip(grey_print.astype(np.int16) - 60 + np.random.default_rng(1).normal(0, 4, grey_print.shape),
                         0, 255).astype(np.uint8)
    assert enhance.graphic("x.jpg", False, measured(grey_print, False))
    noisy = np.clip(photo().astype(np.int16) + np.random.default_rng(2).normal(0, 6, photo().shape), 0, 255)
    assert not enhance.graphic("x.jpg", False, measured(noisy.astype(np.uint8), False))


def test_a_soft_text_image_is_not_suggested_for_dust_removal(tmp_path):
    soft = cv2.GaussianBlur(text_page(), (0, 0), 1.0)
    row = {"v": enhance.VERSION, "id": "t", "file_name": "t.jpg", "gain": 0, "chain": [], "reasons": [],
           "measures": {**measured(soft, False), "specks": 0.02, "long_side": 500}}
    enhance.rescore(row)
    assert row["gain"] == 0 and row["chain"] == []


def test_a_camera_photo_is_never_a_graphic_on_looks_alone(tmp_path):
    soft_sign = {**measured(cv2.GaussianBlur(text_page(), (0, 0), 1.0), True), "specks": 0.02, "long_side": 500}
    rows = [{"v": enhance.VERSION, "id": name, "file_name": name, "gain": 0, "chain": [], "reasons": [],
             "measures": soft_sign} for name in ("sign.jpg", "saved.jpg", "Screenshot.png")]
    scores = tmp_path / "scores.jsonl"
    scores.write_text("".join(json.dumps(r) + "\n" for r in rows))
    found = enhance.rank(scores, tmp_path / "c.json", min_gain=1, colourise=False,
                         shots={"sign.jpg", "Screenshot.png"})
    assert [r["id"] for r in found["candidates"]] == ["sign.jpg"]
    assert "lama" not in found["candidates"][0]["chain"]
