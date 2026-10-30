import hashlib

import pytest

from fuseshot import assets


@pytest.fixture
def fake_model(home, tmp_path, monkeypatch):
    data = b"not really a model" * 100
    source = tmp_path / "model.bin"
    source.write_bytes(data)
    spec = {"file": "fake.bin", "url": source.as_uri(), "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data), "licence": "MIT", "for": "fill"}
    monkeypatch.setitem(assets.MODELS, "fake", spec)
    return spec


def test_model_is_downloaded_and_checked(fake_model):
    assert assets.check_model("fake")[0] == "missing"
    path = assets.model("fake")
    assert path.read_bytes().startswith(b"not really")
    assert assets.check_model("fake")[0] == "ok"


def test_model_with_wrong_hash_is_thrown_away(fake_model):
    fake_model["sha256"] = "0" * 64
    with pytest.raises(assets.AssetError):
        assets.download_model("fake")
    assert not assets.model_path("fake").exists()
    assert list(assets.models_dir().iterdir()) == []


def test_model_override_from_settings(home, fake_model, tmp_path):
    own = tmp_path / "mine.bin"
    own.write_bytes(b"x")
    (home / "fuseshot.toml").write_text(f"[models]\nfake = '{own}'\n", encoding="utf-8")
    from fuseshot import config

    config.setup(home)
    assert assets.model("fake", fetch=False) == own


def test_every_pinned_asset_has_a_hash():
    for spec in assets.MODELS.values():
        assert len(spec["sha256"]) == 64 and spec["url"].startswith("https://")
    for spec in assets.VIEWER.values():
        assert spec["tarball"].startswith("https://registry.npmjs.org/")
