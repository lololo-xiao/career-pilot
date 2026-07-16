from scripts.demo_preflight import run_preflight


def test_preflight_accepts_build_week_model_without_network(monkeypatch) -> None:
    monkeypatch.setenv("CAREERPILOT_AUTH_SECRET", "t" * 48)
    monkeypatch.setenv("CODEX_BINARY", "python")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-sol")

    checks = run_preflight(warm_embeddings=False)
    required_checks = [check for check in checks if check.required]

    assert required_checks
    assert all(check.passed for check in required_checks)


def test_preflight_rejects_non_gpt_56_model(monkeypatch) -> None:
    monkeypatch.setenv("CAREERPILOT_AUTH_SECRET", "t" * 48)
    monkeypatch.setenv("CODEX_BINARY", "python")
    monkeypatch.setenv("OPENAI_MODEL", "some-other-model")

    checks = run_preflight(warm_embeddings=False)
    model_check = next(check for check in checks if check.label == "Build Week model")

    assert model_check.passed is False
