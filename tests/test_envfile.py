import os

from papyrus import envfile


def test_the_env_file_fills_only_what_the_environment_lacks(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PAPYRUS_ROOT", str(tmp_path))
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.delenv("PAPYRUS_MAILTO", raising=False)
    monkeypatch.setenv("S2_API_KEY", "from-the-shell")
    p = tmp_path / "env"
    p.write_text("# keys\nexport OPENALEX_API_KEY='abc123'\nS2_API_KEY=from-the-file\nPAPYRUS_MAILTO = me@example.org\n"
                 "not a line\n")
    p.chmod(0o600)
    assert sorted(envfile.load()) == ["OPENALEX_API_KEY", "PAPYRUS_MAILTO"]
    assert os.environ["OPENALEX_API_KEY"] == "abc123" and os.environ["PAPYRUS_MAILTO"] == "me@example.org"
    assert os.environ["S2_API_KEY"] == "from-the-shell"  # the environment wins
    assert capsys.readouterr().err == ""
    p.chmod(0o644)
    monkeypatch.delenv("OPENALEX_API_KEY")
    envfile.load()
    err = capsys.readouterr().err
    assert "chmod 600" in err and "abc123" not in err  # warns, never prints a value


def test_no_env_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPYRUS_ROOT", str(tmp_path))
    assert envfile.load() == []
