"""watch.run exits cleanly on KeyboardInterrupt."""


from fuseshot import watch
from conftest import write_frames


def test_watch_run_one_pass(tmp_path, monkeypatch):
    photos = write_frames(tmp_path / "lib", count=2)
    from fuseshot.sources import FolderSource

    db = tmp_path / "assets.sqlite"
    source = FolderSource(str(photos[0].parent), False, db)

    calls = []

    def fake_sleep(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(watch.time, "sleep", fake_sleep)
    monkeypatch.setattr(watch, "observe", lambda *a, **k: None)
    watch.run(db, [source], None, rescan_every=0, after=lambda: calls.append(1))
    assert calls == [1]
