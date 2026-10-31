"""Variants: each library lives in `<PAPYRUS_ROOT>/<variant>` (default `~/.papyrus/release`)."""

import pytest
from click.testing import CliRunner

from papyrus import config, db
from papyrus.cli import main


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.delenv("PAPYRUS_HOME")  # conftest sets it; these tests resolve the home from the variant
    monkeypatch.setenv("PAPYRUS_ROOT", str(tmp_path / "root"))
    return tmp_path / "root"


def test_home_is_the_variant_under_the_root(root, monkeypatch):
    assert db.variant() == "release"
    assert db.library_home() == root / "release"
    assert db.default_db_path() == root / "release" / "papyrus.sqlite"
    assert config.path() == root / "release" / "config.toml"
    monkeypatch.setenv("PAPYRUS_VARIANT", "personal")
    assert db.library_home() == root / "personal"
    monkeypatch.setenv("PAPYRUS_HOME", str(root / "elsewhere"))  # an explicit home wins
    assert db.library_home() == root / "elsewhere"


def test_bad_variant_id(root, monkeypatch):
    monkeypatch.setenv("PAPYRUS_VARIANT", "../x")
    with pytest.raises(ValueError):
        db.library_home()
    r = CliRunner().invoke(main, ["stats"])
    assert r.exit_code != 0 and "not a variant id" in r.output


def test_cli_creates_release_but_not_an_unknown_variant(root, monkeypatch):
    monkeypatch.setenv("PAPYRUS_VARIANT", "release")  # --variant sets it for the process; restored after the test
    r = CliRunner().invoke(main, ["--variant", "typo", "stats"])
    assert r.exit_code != 0 and "no variant 'typo'" in r.output
    assert not (root / "typo").exists()
    r = CliRunner().invoke(main, ["--variant", "release", "stats"])
    assert r.exit_code == 0, r.output
    assert (root / "release" / "papyrus.sqlite").exists()
    (root / "personal").mkdir()
    r = CliRunner().invoke(main, ["--variant", "personal", "stats"])
    assert r.exit_code == 0, r.output
    assert (root / "personal" / "papyrus.sqlite").exists()


def test_cli_refuses_the_old_layout(root):
    root.mkdir()
    (root / "papyrus.sqlite").write_bytes(b"")
    r = CliRunner().invoke(main, ["stats"])
    assert r.exit_code != 0 and "old layout" in r.output
    assert not (root / "release").exists()
