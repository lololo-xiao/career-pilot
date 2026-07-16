import pytest

from app.codex_runtime import (
    CodexOAuthManager,
    CodexUnavailableError,
    _build_codex_environment,
    read_codex_account_snapshot,
    run_codex_structured_turn,
)


def test_codex_environment_does_not_inherit_application_secrets(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("CAREERPILOT_AUTH_SECRET", "must-not-leak")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "must-not-leak-either")
    monkeypatch.setenv("OPENAI_API_KEY", "operator-key")

    environment = _build_codex_environment(tmp_path)

    assert environment["PATH"] == "/usr/bin"
    assert environment["CODEX_HOME"] == str(tmp_path)
    assert environment["HOME"] == str(tmp_path)
    assert "CAREERPILOT_AUTH_SECRET" not in environment
    assert "LANGFUSE_SECRET_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment


def test_codex_login_attempts_are_bounded() -> None:
    manager = CodexOAuthManager(max_active_attempts=0)

    with pytest.raises(CodexUnavailableError, match="Too many"):
        manager.start("user-1")


def test_structured_turn_uses_ephemeral_read_only_protocol(
    monkeypatch, tmp_path
) -> None:
    class FakeTurnServer:
        def __init__(self, credentials) -> None:
            assert credentials == b"codex-auth"
            self.workspace = tmp_path
            self.requests = []
            self.notification_index = 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def request(self, method, params, timeout):
            self.requests.append((method, params, timeout))
            if method == "thread/start":
                assert params["sandbox"] == "read-only"
                assert params["approvalPolicy"] == "never"
                assert params["ephemeral"] is True
                assert params["developerInstructions"] == "Never use tools."
                return {"thread": {"id": "thread-1"}}
            assert method == "turn/start"
            assert params["sandboxPolicy"] == {"type": "readOnly"}
            assert params["outputSchema"] == {"type": "object"}
            return {"turn": {"id": "turn-1"}}

        def wait_for_notification(self, predicate, cursor, timeout):
            notifications = [
                {
                    "method": "item/completed",
                    "params": {
                        "item": {"type": "agentMessage", "text": '{"ok":true}'}
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-1", "status": "completed"}},
                },
            ]
            message = notifications[self.notification_index]
            self.notification_index += 1
            assert predicate(message)
            return message, cursor + 1

        def read_credentials(self):
            return b"refreshed-auth"

    fake_server = FakeTurnServer(b"codex-auth")
    monkeypatch.setattr(
        "app.codex_runtime.CodexAppServer", lambda credentials: fake_server
    )

    output, refreshed = run_codex_structured_turn(
        b"codex-auth",
        "Analyze this source data.",
        {"type": "object"},
        developer_instructions="Never use tools.",
    )

    assert output == '{"ok":true}'
    assert refreshed == b"refreshed-auth"


def test_codex_oauth_manager_uses_device_code_and_captures_account(
    monkeypatch,
) -> None:
    class FakeServer:
        def __init__(self) -> None:
            self.closed = False
            self.login_id = "login-123"

        def request(self, method, params, timeout):
            if method == "account/login/start":
                assert params == {"type": "chatgptDeviceCode"}
                return {
                    "loginId": self.login_id,
                    "verificationUrl": "https://auth.openai.com/codex/device",
                    "userCode": "WXYZ-9876",
                }
            assert method == "account/read"
            assert params == {"refreshToken": True}
            return {
                "account": {
                    "type": "chatgpt",
                    "email": "person@example.com",
                    "planType": "pro",
                }
            }

        def notifications(self):
            return [
                {
                    "method": "account/login/completed",
                    "params": {
                        "loginId": self.login_id,
                        "success": True,
                        "error": None,
                    },
                }
            ]

        def read_credentials(self):
            return b'{"tokens":{"access_token":"secret"}}'

        def close(self):
            self.closed = True

    monkeypatch.setattr("app.codex_runtime.CodexAppServer", FakeServer)
    monkeypatch.setattr(
        "app.codex_runtime.get_codex_login_timeout_seconds", lambda: 900
    )
    manager = CodexOAuthManager()

    attempt = manager.start("user-1")

    assert attempt.user_id == "user-1"
    completed = manager.poll(attempt.attempt_id)

    assert completed is not None
    assert completed.status == "completed"
    assert completed.email == "person@example.com"
    assert completed.plan_type == "pro"
    assert completed.credentials == b'{"tokens":{"access_token":"secret"}}'
    assert completed.server.closed is True
    manager.finish(attempt.attempt_id)


def test_account_snapshot_reads_models_limits_usage_and_contexts(
    monkeypatch,
) -> None:
    class FakeServer:
        def __init__(self, credentials) -> None:
            assert credentials == b"codex-auth"
            self.requests = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def request(self, method, params, timeout):
            self.requests.append((method, params, timeout))
            if method == "model/list":
                return {
                    "data": [
                        {
                            "model": "gpt-test",
                            "displayName": "GPT Test",
                            "description": "A test model",
                            "isDefault": True,
                            "defaultReasoningEffort": "medium",
                            "supportedReasoningEfforts": [],
                        }
                    ],
                    "nextCursor": None,
                }
            if method == "account/rateLimits/read":
                assert params is None
                return {"rateLimits": {"primary": {"usedPercent": 20}}}
            assert method == "account/usage/read"
            assert params is None
            return {"summary": {"lifetimeTokens": 1234}}

        def read_credentials(self):
            return b"refreshed-auth"

    fake_server = FakeServer(b"codex-auth")
    monkeypatch.setattr(
        "app.codex_runtime.CodexAppServer", lambda credentials: fake_server
    )
    monkeypatch.setattr(
        "app.codex_runtime._read_codex_context_windows",
        lambda credentials: {"gpt-test": 128_000},
    )

    snapshot = read_codex_account_snapshot(b"codex-auth")

    assert snapshot.models[0]["model"] == "gpt-test"
    assert snapshot.rate_limits == {
        "rateLimits": {"primary": {"usedPercent": 20}}
    }
    assert snapshot.account_usage == {"summary": {"lifetimeTokens": 1234}}
    assert snapshot.context_windows == {"gpt-test": 128_000}
    assert snapshot.refreshed_credentials == b"refreshed-auth"
    assert snapshot.warnings == []
