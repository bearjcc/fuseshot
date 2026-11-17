"""report.write_all and write_bursts."""

import json
import sqlite3

from fuseshot import report


def _candidate(**extra):
    base = {
        "set_id": "S1",
        "start_local": "2024-01-01T10:00:00",
        "camera": "Cam",
        "count": 5,
        "asset_ids": ["a1", "a2"],
        "guessed_type": "single-row rotation",
        "confidence": 0.8,
        "splat_candidate": True,
        "positions": 1,
        "places": "",
        "verified_type": "single-row rotation",
        "verified_confidence": 0.85,
        "motion_model": "rotation",
        "coverage_h": 120,
        "coverage_v": 80,
        "quality": 0.9,
        "verified_splat": False,
    }
    base.update(extra)
    return base


def test_write_all_outputs_files(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    cands = [_candidate(first_link="https://immich/photos/a1", best_link="https://immich/photos/a1"),
             _candidate(set_id="S2", guessed_type="exposure bracket", confidence=0.3,
                        verified_type=None, verified_confidence=None, first_link="https://immich/photos/a1")]
    stats = {"photos": 10}
    report.write_all(cands, stats, {"x": 1}, out, "https://immich/")
    assert (out / "candidates.csv").is_file()
    assert (out / "candidates.json").is_file()
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "S1" in summary and "Candidate sets" in summary


def test_write_bursts(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    bursts = [{"set_id": "B1", "count": 3, "asset_ids": ["x"]}]
    report.write_bursts(bursts, {}, out, "https://immich/")
    data = json.loads((out / "bursts.json").read_text(encoding="utf-8"))
    assert data["stats"]["runs"] == 1


def test_match_sites_finds_gps_candidate(tmp_path):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE assets (local_time TEXT, latitude REAL, longitude REAL, city TEXT, state TEXT)")
    db.execute("INSERT INTO assets VALUES ('2024-01-01 10:00:00', 51.5, -0.1, 'London', 'England')")
    db.commit()
    sites = [{"name": "Home", "lat": 51.5, "lon": -0.1, "radius_km": 1, "place_names": ["London, England"]}]
    cands = [_candidate(lat=51.5, lon=-0.1, first_link="https://immich/photos/a1", best_link="https://immich/photos/a1")]
    results = report.match_sites(db, cands, sites)
    assert results[0]["matches"]
