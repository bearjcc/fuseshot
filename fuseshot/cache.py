"""Local SQLite cache of asset metadata, so detection never needs to hit the server."""

import sqlite3
from pathlib import Path

from . import limits

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    type TEXT,
    visibility TEXT,
    file_name TEXT,
    mime TEXT,
    taken_utc TEXT,
    local_time TEXT,
    time_zone TEXT,
    make TEXT,
    model TEXT,
    lens TEXT,
    focal_length REAL,
    f_number REAL,
    exposure_time REAL,
    iso REAL,
    latitude REAL,
    longitude REAL,
    city TEXT,
    state TEXT,
    country TEXT,
    width INTEGER,
    height INTEGER,
    orientation TEXT,
    projection TEXT,
    duplicate_id TEXT,
    live_photo_video_id TEXT,
    people INTEGER,
    is_trashed INTEGER,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_camera_time ON assets (make, model, taken_utc);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

COLUMNS = [
    "id", "type", "visibility", "file_name", "mime", "taken_utc", "local_time",
    "time_zone", "make", "model", "lens", "focal_length", "f_number",
    "exposure_time", "iso", "latitude", "longitude", "city", "state", "country", "width", "height",
    "orientation", "projection", "duplicate_id", "live_photo_video_id",
    "people", "is_trashed", "updated_at",
]


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute(f"PRAGMA cache_size = -{limits.sqlite_cache_kb()}")
    db.executescript(SCHEMA)
    return db


def parse_exposure(value):
    """Immich returns exposure time as text like '1/250' or '0.5'. Return seconds."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            return float(num) / float(den)
        return float(text)
    except (ValueError, ZeroDivisionError):
        return None


def _num(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def asset_row(asset):
    exif = asset.get("exifInfo") or {}
    return (
        asset["id"],
        asset.get("type"),
        asset.get("visibility"),
        asset.get("originalFileName"),
        asset.get("originalMimeType"),
        exif.get("dateTimeOriginal") or asset.get("fileCreatedAt"),
        asset.get("localDateTime"),
        exif.get("timeZone"),
        (exif.get("make") or "").strip() or None,
        (exif.get("model") or "").strip() or None,
        exif.get("lensModel"),
        _num(exif.get("focalLength")),
        _num(exif.get("fNumber")),
        parse_exposure(exif.get("exposureTime")),
        _num(exif.get("iso")),
        _num(exif.get("latitude")),
        _num(exif.get("longitude")),
        exif.get("city"),
        exif.get("state"),
        exif.get("country"),
        exif.get("exifImageWidth") or asset.get("width"),
        exif.get("exifImageHeight") or asset.get("height"),
        exif.get("orientation"),
        exif.get("projectionType"),
        asset.get("duplicateId"),
        asset.get("livePhotoVideoId"),
        len(asset.get("people") or []),
        1 if asset.get("isTrashed") else 0,
        asset.get("updatedAt"),
    )


def upsert(db, assets):
    placeholders = ",".join("?" for _ in COLUMNS)
    db.executemany(
        f"INSERT OR REPLACE INTO assets ({','.join(COLUMNS)}) VALUES ({placeholders})",
        [asset_row(a) for a in assets],
    )


def set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))


def get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default

