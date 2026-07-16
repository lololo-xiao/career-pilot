from app.observability import is_langfuse_enabled


def test_langfuse_requires_both_keys(monkeypatch) -> None:
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    # Keep the key explicitly empty so a developer's local .env cannot repopulate it.
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")

    assert is_langfuse_enabled() is False


def test_langfuse_can_be_explicitly_disabled(monkeypatch) -> None:
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "false")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    assert is_langfuse_enabled() is False


def test_langfuse_is_enabled_with_credentials(monkeypatch) -> None:
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    assert is_langfuse_enabled() is True
