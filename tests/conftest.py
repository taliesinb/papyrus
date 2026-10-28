import pytest


@pytest.fixture(autouse=True)
def _isolated_library(tmp_path, monkeypatch):
    """Keep every test's managed store, config and HTTP cache out of the user's ~/.papyrus,
    ignore the user's model settings, and never start a real LM Studio server."""
    monkeypatch.setenv("PAPYRUS_HOME", str(tmp_path / "papyrus-home"))
    monkeypatch.setenv("PAPYRUS_HTTP_CACHE", "0")
    monkeypatch.setenv("PAPYRUS_LMSTUDIO_AUTOSTART", "0")
    for var in ("PAPYRUS_CONFIG", "PAPYRUS_BACKEND", "PAPYRUS_DECIDER"):
        monkeypatch.delenv(var, raising=False)
