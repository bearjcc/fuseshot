import pytest

from fuseshot import cache, sources


def test_key_from_environment_wins(home, tmp_path, monkeypatch):
    key_file = tmp_path / "key"
    key_file.write_text("from-file\n", encoding="utf-8")
    assert sources.read_key(key_file) == "from-file"
    monkeypatch.setenv("FUSESHOT_IMMICH_KEY", " from-env ")
    assert sources.read_key(key_file) == "from-env"


def test_missing_key_explains_itself(home, tmp_path):
    with pytest.raises(sources.SourceError, match="API Keys"):
        sources.read_key(tmp_path / "nothing")


def test_folder_sync_picks_up_new_and_removed_photos(home, photos):
    source = sources.FolderSource(photos[0].parent)
    db = cache.connect(home / "cache" / "assets.sqlite")
    changed, removed, failed = source.sync(db)
    assert (changed, removed, failed) == (len(photos), 0, 0)
    assert source.sync(db) == (0, 0, 0)
    photos[-1].unlink()
    assert source.sync(db) == (0, 1, 0)
    row = db.execute("SELECT make, model FROM assets LIMIT 1").fetchone()
    assert tuple(row) == ("Example", "Camera")
