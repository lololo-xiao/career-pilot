import pytest

from app.auth import AuthConfigurationError, AuthStore


def test_local_identity_and_provider_credentials_are_stable_and_encrypted(tmp_path) -> None:
    path = tmp_path / "auth.db"
    store = AuthStore(path, "s" * 48)
    api_key = "sk-project-secret-that-must-not-be-plaintext"

    account = store.ensure_local_account()
    store.save_provider_connection(
        account_id=account.user_id,
        provider="api_key",
        credential=api_key.encode(),
        plan_type="usage-based",
    )
    resolved = store.ensure_local_account()

    assert resolved.user_id == account.user_id
    assert resolved.identity_method == "local"
    assert resolved.active_provider == "api_key"
    assert resolved.provider_connection is not None
    assert resolved.provider_connection.credential.decode() == api_key
    assert api_key.encode() not in path.read_bytes()


def test_provider_selection_and_disconnect_use_the_local_identity(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.db", "s" * 48)
    account = store.ensure_local_account()
    store.save_provider_connection(
        account_id=account.user_id,
        provider="api_key",
        credential=b"sk-test-key",
    )
    store.save_provider_connection(
        account_id=account.user_id,
        provider="codex",
        credential=b'{"tokens":{"access_token":"secret"}}',
        provider_email="chatgpt@example.com",
        plan_type="plus",
    )

    assert store.select_provider(account.user_id, "api_key") is True
    assert store.load_account(account.user_id, "local").active_provider == "api_key"
    assert store.select_provider(account.user_id, "codex") is True

    store.disconnect_provider(account.user_id, "codex")

    settings = store.provider_settings(account.user_id)
    assert settings.active_provider is None
    assert settings.connections[0].provider == "codex"
    assert settings.connections[0].connected is False
    assert settings.connections[1].connected is True


def test_service_credentials_are_encrypted_without_changing_provider(tmp_path) -> None:
    path = tmp_path / "auth.db"
    store = AuthStore(path, "s" * 48)
    account = store.ensure_local_account()
    search_key = b"brave-search-secret-that-must-stay-encrypted"

    store.save_service_credential(account.user_id, "brave_search", search_key)

    assert store.load_service_credential(account.user_id, "brave_search") == search_key
    assert store.load_account(account.user_id, "local").active_provider is None
    assert search_key not in path.read_bytes()

    store.delete_service_credential(account.user_id, "brave_search")
    assert store.load_service_credential(account.user_id, "brave_search") is None


def test_auth_store_rejects_weak_secret(tmp_path) -> None:
    with pytest.raises(AuthConfigurationError, match="at least 32"):
        AuthStore(tmp_path / "auth.db", "short")
