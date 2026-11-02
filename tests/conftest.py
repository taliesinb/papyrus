import pytest


@pytest.fixture(autouse=True)
def _isolated_library(tmp_path, monkeypatch):
    """Keep every test's managed store, config and web services store out of the user's ~/.papyrus,
    ignore the user's model settings, and never start a real LM Studio server."""
    monkeypatch.setenv("PAPYRUS_HOME", str(tmp_path / "papyrus-home"))
    monkeypatch.setenv("PAPYRUS_ROOT", str(tmp_path / "papyrus-root"))  # not the real ~/.papyrus/env (keys)
    monkeypatch.setenv("PAPYRUS_HTTP_CACHE", "0")
    monkeypatch.setenv("PAPYRUS_LMSTUDIO_AUTOSTART", "0")
    monkeypatch.setenv("PAPYRUS_SCREEN", "0")  # the screen's embedding model is LM Studio's too
    # (not PAPYRUS_ROOT, set above: deleting it made the CLI read the real ~/.papyrus/env) and no API keys:
    # without S2_API_KEY Semantic Scholar is not asked (candidates.default_sources)
    # The web services store (services.py: every request, rate limits and pauses) follows PAPYRUS_ROOT,
    # so each test has its own: a refusal in one test doesn't pause the service in the next.
    for var in ("PAPYRUS_CONFIG", "PAPYRUS_BACKEND", "PAPYRUS_CLASSIFIER", "PAPYRUS_VARIANT", "S2_API_KEY",
                "OPENALEX_API_KEY", "PAPYRUS_SERVICES"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def no_match_network(monkeypatch):
    """Matching finds nothing, without asking any API (for tests that run the watcher, which
    matches what it promotes)."""
    from papyrus.match import candidates

    monkeypatch.setattr(candidates, "resolve", lambda hint, **kw: [])
