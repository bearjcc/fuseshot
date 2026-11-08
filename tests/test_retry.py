"""Failures are tried again when the code or the parameters change, up to a cap."""

import json

from fuseshot import retry, review


def test_a_failure_waits_for_a_change_and_stops_at_the_cap():
    first = retry.record("boom", "abc", {"frames": 3})
    assert first["attempts"] == 1
    assert not retry.should_retry(first, "abc", {"frames": 3})
    assert retry.should_retry(first, "def", {"frames": 3})
    assert retry.should_retry(first, "abc", {"frames": 4})
    third = retry.record("boom", "ghi", {"frames": 3}, retry.record("boom", "def", {"frames": 3}, first))
    assert third["attempts"] == retry.MAX_ATTEMPTS
    assert not retry.should_retry(third, "new", {"frames": 3})
    assert retry.should_retry(None, "abc", {})


def test_code_version_follows_the_source():
    assert retry.code_version("batch", "stitch") == retry.code_version("batch", "stitch")
    assert retry.code_version("batch") != retry.code_version("stitch")


def test_retry_on_the_failures_page_asks_for_one_more_try(home):
    work = home / "work"
    (work / "P00001").mkdir(parents=True)
    capped = {"status": "failed", **retry.record("no overlap", "x", {}), "attempts": 3}
    (work / "P00001" / "batch.json").write_text(json.dumps(capped))
    (work / "actions" / "P00002-1").mkdir(parents=True)
    (work / "actions" / "P00002-1" / "failed.json").write_text(json.dumps({"error": "old style"}))
    r = review.Review(work)
    found = {f["key"]: f for f in r.failures()}
    assert found["P00001"]["capped"] and found["P00002-1"]["attempts"] == 1
    assert "no more automatic tries" in r.failures_page() and "Retry" in r.failures_page()
    r.retry_failure({"kind": "stitch", "key": "P00001"})
    saved = json.loads((work / "P00001" / "batch.json").read_text())
    assert saved["retry"] and saved["attempts"] == 0
    assert retry.should_retry(saved, "x", {})
    import pytest

    with pytest.raises(KeyError):
        r.retry_failure({"kind": "stitch", "key": "../x"})
