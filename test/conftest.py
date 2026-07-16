import pytest


@pytest.fixture(autouse=True)
def disable_remote_observability(monkeypatch: pytest.MonkeyPatch) -> None:
    """Automated tests must never emit candidate data to remote telemetry."""

    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "false")
