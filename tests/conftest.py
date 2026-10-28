import pytest


@pytest.fixture(autouse=True)
def _isolated_library(tmp_path, monkeypatch):
    """Keep every test's managed store (and HTTP cache) out of the user's ~/.papyrus."""
    monkeypatch.setenv("PAPYRUS_HOME", str(tmp_path / "papyrus-home"))
    monkeypatch.setenv("PAPYRUS_HTTP_CACHE", "0")
