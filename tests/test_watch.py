"""Folder watch rescan without starting the loop."""

from fuseshot import watch
from conftest import write_frames


def test_rescan_counts_changes(home, tmp_path):
    photos = write_frames(tmp_path / "lib", count=2)
    from fuseshot.sources import FolderSource

    source = FolderSource(str(photos[0].parent), False, home / "cache" / "assets.sqlite")
    db = home / "cache" / "assets.sqlite"
    watch.rescan(db, [source], [], log=lambda *a: None)
    assert db.is_file()
    moved = watch.rescan(db, [source], [], log=lambda *a: None)
    assert moved == 0
