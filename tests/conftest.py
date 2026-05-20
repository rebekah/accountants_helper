import pytest


@pytest.fixture(autouse=True)
def suppress_llm_keys(monkeypatch):
    """Remove LLM API keys for every test — we only test deterministic logic."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
