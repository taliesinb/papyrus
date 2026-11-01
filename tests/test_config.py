import subprocess

import pytest
from click.testing import CliRunner

import papyrus.llm as llm
import papyrus.llm.classify as classify
from papyrus import config
from papyrus.cli import main
from papyrus.llm import lmstudio
from papyrus.llm.base import BackendUnavailable

from test_ingest import PAPER, FakeBackend, _lib


def _write(text: str) -> None:
    config.path().parent.mkdir(parents=True, exist_ok=True)
    config.path().write_text(text)


def test_path_defaults_to_library_home_and_can_be_overridden(tmp_path, monkeypatch):
    assert config.path() == tmp_path / "papyrus-home" / "config.toml"
    monkeypatch.setenv("PAPYRUS_CONFIG", str(tmp_path / "elsewhere.toml"))
    assert config.path() == tmp_path / "elsewhere.toml"


def test_precedence_cli_env_file_default(monkeypatch):
    assert config.setting("backend") == ("auto", "default")
    assert config.lmstudio_autostart() == (False, "PAPYRUS_LMSTUDIO_AUTOSTART")  # conftest turns it off
    _write('backend = "lmstudio:qwen3.5-9b-mlx"\nclassifier = "none"\n[lmstudio]\nautostart = false\n')
    assert config.setting("backend") == ("lmstudio:qwen3.5-9b-mlx", str(config.path()))
    assert config.classifier() == "none"
    monkeypatch.delenv("PAPYRUS_LMSTUDIO_AUTOSTART")
    assert config.lmstudio_autostart() == (False, str(config.path()))
    monkeypatch.setenv("PAPYRUS_BACKEND", "apple")
    assert config.setting("backend") == ("apple", "PAPYRUS_BACKEND")
    assert config.setting("backend", "lmstudio") == ("lmstudio", "command line")


def test_template_parses_to_the_defaults(monkeypatch):
    monkeypatch.delenv("PAPYRUS_LMSTUDIO_AUTOSTART")
    p, written = config.init()
    assert written and p.exists()
    assert config.init() == (p, False)  # never overwrites
    assert config.backend() == "auto" and config.classifier() == "auto"
    assert config.lmstudio_autostart() == (True, str(p))


@pytest.mark.parametrize("text, msg", [
    ("backend = ", "config.toml"),
    ("backend = 3", "backend must be a non-empty string"),
    ("[lmstudio]\nautostart = 'yes'", "autostart must be true or false"),
])
def test_bad_config_is_reported(monkeypatch, text, msg):
    monkeypatch.delenv("PAPYRUS_LMSTUDIO_AUTOSTART")
    _write(text)
    with pytest.raises(config.ConfigError, match=msg):
        config.backend()
        config.lmstudio_autostart()


def test_ensure_server_starts_lms_once_and_only_locally(monkeypatch):
    up = iter([False, False, True])
    monkeypatch.setattr(lmstudio, "server_up", lambda url, timeout=2.0: next(up))
    monkeypatch.setattr(lmstudio, "lms_path", lambda: "/x/lms")
    monkeypatch.setattr(lmstudio, "_start_attempted", set())
    monkeypatch.setattr(lmstudio.time, "sleep", lambda s: None)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0, "Success!", ""))
    assert lmstudio.ensure_server("http://localhost:1234/v1", autostart=True)
    assert calls == [["/x/lms", "server", "start", "--port", "1234"]]

    monkeypatch.setattr(lmstudio, "server_up", lambda url, timeout=2.0: False)
    assert not lmstudio.ensure_server("http://localhost:1234/v1", autostart=True)  # tried once already
    assert not lmstudio.ensure_server("http://gpu-box:1234/v1", autostart=True)  # not ours to start
    assert not lmstudio.ensure_server("http://localhost:5555/v1", autostart=False)
    assert len(calls) == 1
    lmstudio.ensure_server("http://127.0.0.1:5555/v1", autostart=True, wait_s=0)
    assert calls[-1][-1] == "5555"


def test_ensure_server_follows_config(monkeypatch):
    monkeypatch.setattr(lmstudio, "server_up", lambda url, timeout=2.0: False)
    monkeypatch.setattr(lmstudio, "lms_path", lambda: "/x/lms")
    monkeypatch.setattr(lmstudio, "_start_attempted", set())
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: pytest.fail("must not start the server"))
    assert not lmstudio.ensure_server("http://localhost:1234/v1")  # conftest: PAPYRUS_LMSTUDIO_AUTOSTART=0
    monkeypatch.delenv("PAPYRUS_LMSTUDIO_AUTOSTART")
    _write("[lmstudio]\nautostart = false\n")
    assert not lmstudio.ensure_server("http://localhost:1234/v1")


# --- classify/extract pick their models from the config; no classifier, no classifying ---------

def _no_classifier(spec):
    raise BackendUnavailable("LM Studio not reachable")


def test_classify_refuses_without_classifier_unless_none(tmp_path, monkeypatch):
    _lib(tmp_path, "paper.pdf", "A Study of Things. Jane Doe. Abstract We study things in great detail.")
    specs = []
    monkeypatch.setattr(llm, "get_backend", lambda spec: specs.append(spec) or FakeBackend(PAPER))
    monkeypatch.setattr(classify, "get_classifier", _no_classifier)
    runner, db_arg = CliRunner(), ["--db", str(tmp_path / "t.sqlite")]

    r = runner.invoke(main, [*db_arg, "classify"])
    assert r.exit_code == 1 and "no classifier" in r.output and "--classifier none" in r.output

    _write('backend = "lmstudio:qwen3.5-9b-mlx"\nclassifier = "none"\n')
    r = runner.invoke(main, [*db_arg, "classify"])
    assert r.exit_code == 0 and "classifying 1 files with" in r.output and "no classifier" in r.output, r.output
    r = runner.invoke(main, [*db_arg, "extract"])
    assert r.exit_code == 0, r.output
    assert "extracting the metadata of 1 files with fake:m" in r.output and specs[-1] == "lmstudio:qwen3.5-9b-mlx"

    r = runner.invoke(main, [*db_arg, "extract", "--all", "--backend", "apple"])
    assert r.exit_code == 0 and specs[-1] == "apple"  # the command line beats the file


def test_config_command(tmp_path):
    runner, db_arg = CliRunner(), ["--db", str(tmp_path / "t.sqlite")]
    r = runner.invoke(main, [*db_arg, "config"])
    assert r.exit_code == 0 and "not present; using defaults" in r.output
    assert "backend            auto" in r.output and "(default)" in r.output
    r = runner.invoke(main, [*db_arg, "config", "--init"])
    assert r.exit_code == 0 and "wrote" in r.output and config.path().exists()
    _write("classifier = 7\n")
    r = runner.invoke(main, [*db_arg, "config"])
    assert r.exit_code == 1 and "classifier must be a non-empty string" in r.output


def test_the_old_decider_names_still_set_the_classifier(tmp_path, monkeypatch):
    """The classifier was called the decider until 2026-11-01: `decider = …` in config.toml and
    PAPYRUS_DECIDER keep working (the new names win)."""
    from papyrus import config

    cfg = tmp_path / "c.toml"
    cfg.write_text('decider = "none"\n')
    monkeypatch.setenv("PAPYRUS_CONFIG", str(cfg))
    monkeypatch.delenv("PAPYRUS_CLASSIFIER", raising=False)
    monkeypatch.delenv("PAPYRUS_DECIDER", raising=False)
    assert config.classifier() == "none"
    monkeypatch.setenv("PAPYRUS_DECIDER", "lmstudio:x")
    assert config.classifier() == "lmstudio:x"
    monkeypatch.setenv("PAPYRUS_CLASSIFIER", "lmstudio:y")
    assert config.classifier() == "lmstudio:y"


def test_watch_background_priority_can_be_turned_off(tmp_path, monkeypatch):
    from papyrus import config

    cfg = tmp_path / "c.toml"
    monkeypatch.setenv("PAPYRUS_CONFIG", str(cfg))
    cfg.write_text("[watch]\ndirs = ['/tmp']\n")
    assert config.watch().background is True
    cfg.write_text("[watch]\nbackground = false\n")
    assert config.watch().background is False
    cfg.write_text("[watch]\nbackground = 'no'\n")
    with pytest.raises(config.ConfigError):
        config.watch()
