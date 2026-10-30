# Contributing

Bug reports with a sample set are the most useful thing. If a set was missed or
stitched badly, the set's `work/<set>/` folder and the photos' EXIF (not the photos
themselves, unless you are happy to share them) usually show why.

## Development

    git clone https://github.com/bearjcc/fuseshot
    cd fuseshot
    uv sync                 # or: python -m venv .venv && pip install -e . pytest ruff
    uv run pytest
    uv run ruff check .

The end-to-end test draws its own photos, so no sample library is needed. The
stitching part of it runs only when Hugin is installed.
The review page test needs Playwright and a browser:
`uv run --with playwright playwright install chromium`, then
`uv run --with playwright pytest`.

Keep changes small and focused, with a test where it makes sense. Fuseshot must stay
read-only towards source folders, and towards Immich apart from the opt-in upload of
kept results, which only adds; anything that writes outside
the home folder needs a very good reason.

By sending a pull request you agree to license your change under the AGPL-3.0 or
later, like the rest of the project.
