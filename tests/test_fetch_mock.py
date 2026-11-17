"""fetch_set with a stub Immich client."""

import json

from PIL import Image

from fuseshot import fetch


class StubImmich:
    def asset_info(self, asset_id):
        return {"originalFileName": f"{asset_id}.jpg", "id": asset_id}

    def download_original(self, asset_id, dest):
        Image.new("RGB", (32, 24), (1, 2, 3)).save(dest, quality=90)
        return dest.stat().st_size

    def thumbnail(self, asset_id, size):
        return b"\xff\xd8\xff"


def test_fetch_set_builds_manifest(tmp_path):
    work = tmp_path / "work"
    candidate = {"set_id": "F1", "asset_ids": ["id1", "id2"]}
    fetch.fetch_set(StubImmich(), candidate, work)
    manifest = json.loads((work / "F1" / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest) == 2
