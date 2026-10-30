# tests

Run with `pytest`. `conftest.py` draws synthetic photos (a random scene seen by a
turning camera), so the end-to-end test needs no real library. Tests that need Hugin
skip when it is missing. `test_upload.py` runs its own stand-in Immich server, so no
real Immich is ever touched.
`test_review_page.py` drives the review page in a browser with Playwright and
skips when Playwright is not installed (`uv run --with playwright pytest`).
