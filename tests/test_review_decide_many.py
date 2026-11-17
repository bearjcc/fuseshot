"""review.decide_many batches keeps."""

import json

from fuseshot.review import Review
from conftest import stitched_set


def test_decide_many_records_all(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    stitched_set(work, "P1")
    stitched_set(work, "P2")
    page = Review(work)
    exported = []
    monkeypatch.setattr(page, "export_later", lambda keys: exported.extend(keys))
    rows = page.decide_many({
        "items": [
            {"set": "P1", "decision": "kept", "projection": "cylindrical", "orientation": "level"},
            {"set": "P2", "decision": "discarded"},
        ],
    })
    assert len(rows["records"]) == 2
    data = json.loads((work / "reviews.json").read_text(encoding="utf-8"))
    assert len(data) == 2
    assert exported == ["stitch:P1"]
