import pytest


@pytest.fixture(autouse=True)
def _isolated_library(tmp_path, monkeypatch):
    """Keep every test's managed store, config and HTTP cache out of the user's ~/.papyrus/<variant>,
    ignore the user's model settings, and never start a real LM Studio server."""
    monkeypatch.setenv("PAPYRUS_HOME", str(tmp_path / "papyrus-home"))
    monkeypatch.setenv("PAPYRUS_HTTP_CACHE", "0")
    monkeypatch.setenv("PAPYRUS_LMSTUDIO_AUTOSTART", "0")
    for var in ("PAPYRUS_CONFIG", "PAPYRUS_BACKEND", "PAPYRUS_DECIDER", "PAPYRUS_VARIANT", "PAPYRUS_ROOT"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def no_enrich_network(monkeypatch):
    """Enrichment finds nothing, without asking any API (for tests that run the watcher, which
    enriches what it promotes)."""
    from papyrus.enrich import match

    monkeypatch.setattr(match, "resolve", lambda hint, **kw: [])
