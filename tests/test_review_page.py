"""The review page in a real browser: undo brings back the item and the viewer it was decided in,
and a loop's own page saves its decision."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
from conftest import write_frames
from PIL import Image

from fuseshot.__main__ import main

sync_api = pytest.importorskip("playwright.sync_api")


def stitched(work, set_id, colour):
    set_dir = work / set_id
    (set_dir / "previews").mkdir(parents=True)
    (set_dir / "optimised.pto").write_text("p\n")
    Image.new("RGB", (640, 320), colour).save(set_dir / "previews" / "cylindrical-level.jpg")
    (set_dir / "batch.json").write_text(json.dumps({"set_id": set_id, "status": "ok", "error_px": 1.5}))


def loop(work, key):
    loop_dir = work / "actions" / key
    loop_dir.mkdir(parents=True)
    (loop_dir / "action.json").write_text(json.dumps({"frames": 4}))
    frames = [Image.new("RGB", (320, 240), (40 * n, 90, 150)) for n in range(4)]
    frames[0].save(loop_dir / "animation.webp", save_all=True, append_images=frames[1:], duration=120, loop=0)


@pytest.fixture
def page(home):
    work = home / "work"
    stitched(work, "F00001", (90, 120, 150))
    stitched(work, "F00002", (150, 120, 90))
    yield from serve(home)


@pytest.fixture
def loop_page(home):
    loop(home / "work", "F00003")
    yield from serve(home)


@pytest.fixture
def found_page(home, tmp_path):
    photos = write_frames(tmp_path / "photos")
    source = ["--source", f"folder:{photos[0].parent}"]
    main(["--home", str(home), *source, "sync"])
    main(["--home", str(home), *source, "detect"])
    (home / "work").mkdir(exist_ok=True)
    yield from serve(home, source)


def serve(home, before=()):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, FUSESHOT_HOME=str(home))
    proc = subprocess.Popen([sys.executable, "-m", "fuseshot", *before, "review", "--port", str(port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}/"
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        with sync_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except sync_api.Error:
                try:
                    browser = p.chromium.launch(channel="chrome")
                except sync_api.Error as err:
                    pytest.skip(f"no browser for Playwright: {err}")
            tab = browser.new_page()
            tab.goto(base, wait_until="networkidle")
            yield tab, base
            browser.close()
    finally:
        proc.terminate()
        proc.wait(10)


def decisions(base):
    items = json.load(urllib.request.urlopen(base + "api/items"))["items"]
    return {i["id"]: i.get("decision") for i in items}


def wait_for(base, want):
    for _ in range(50):
        if decisions(base) == want:
            return
        time.sleep(0.1)
    assert decisions(base) == want


def test_undo_after_the_last_item_opens_the_viewer_again(page):
    tab, base = page
    tab.locator(".tile").first.click()
    assert tab.locator("#viewer").is_visible()
    first = tab.locator("#vname").text_content()
    second = "F00002" if first == "F00001" else "F00001"
    tab.keyboard.press("k")
    tab.wait_for_function(f"document.getElementById('vname').textContent === '{second}'")
    tab.keyboard.press("d")
    tab.locator("#viewer").wait_for(state="hidden")
    wait_for(base, {first: "kept", second: "discarded"})

    tab.keyboard.press("u")
    tab.locator("#viewer").wait_for(state="visible")
    assert tab.locator("#vname").text_content() == second
    wait_for(base, {first: "kept", second: None})

    tab.keyboard.press("k")
    wait_for(base, {first: "kept", second: "kept"})


def test_loop_page_keeps_with_the_keyboard(loop_page):
    tab, base = loop_page
    tab.goto(base + "action/F00003", wait_until="networkidle")
    assert tab.locator("#loop").evaluate("img => img.naturalWidth") == 320
    assert tab.locator(".v-top svg.i").count() >= 2
    tab.keyboard.press("k")
    tab.wait_for_function("document.getElementById('status').textContent === 'kept'")
    wait_for(base, {"F00003": "kept"})


def test_a_found_set_shows_its_frames_and_can_be_discarded_before_stitching(found_page, home):
    tab, base = found_page
    tile = tab.locator('.tile[data-key^="found:"]')
    assert tile.count() == 1
    assert "Found while scanning" in tab.locator(".section-head").first.text_content()
    tab.wait_for_function("[...document.querySelectorAll('.found-strip img')].every(i => i.naturalWidth > 0)")
    assert tile.locator(".found-strip img").count() == 4
    assert "Found" in tile.locator(".bl").text_content()
    set_id = tile.get_attribute("data-key").split(":")[1]

    plan = {"jobs": [{"key": set_id, "set_id": set_id, "confidence": 0.9}]}
    (home / "work" / "batch_plan.json").write_text(json.dumps(plan))
    tile.locator(".bl.queued").wait_for(timeout=20000)

    tile.click()
    assert tab.locator("#vplaceholder .found-strip img").count() == 4
    assert tab.locator("#vdoedit").is_hidden()
    tab.keyboard.press("d")
    tab.locator("#viewer").wait_for(state="hidden")
    wait_for(base, {set_id: "discarded"})
    tab.keyboard.press("u")
    tab.locator("#viewer").wait_for(state="visible")
    wait_for(base, {set_id: None})
