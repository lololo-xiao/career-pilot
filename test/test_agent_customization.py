from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
import yaml

from app.auth import AuthStore
from app.dependencies import get_local_companion_paths
from app.main import app, get_hermes_runtime_manager, get_store
from app.schemas import MCPServerSettingsRequest
from career_companion.database import MCPServerRecord
from career_companion.paths import CompanionPaths
from career_companion.persistence import account_session
from career_companion.router import set_mcp
from career_companion.schemas import MCPServerConfig
from career_companion.services.mcp_servers import (
    _configured_mcp_tool_names_from_servers,
    _preset_payload,
    _render_server,
    _validate_mcp_server_runtime_config,
    configured_mcp_tool_names,
    synchronize_mcp_profile_config,
    upsert_mcp_server,
    validate_mcp_environment_references,
    validate_mcp_tool_allowlist,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def customization_client(tmp_path):
    store = AuthStore(tmp_path / "auth.db", "a" * 48)
    paths = CompanionPaths.at_root(tmp_path / "companion")
    paths.create()
    profile = paths.hermes_profile / "profiles" / "career-companion"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: {}\n", encoding="utf-8")

    class FakeRuntime:
        invalidated: list[str] = []

        async def invalidate(self, account_id: str) -> None:
            self.invalidated.append(account_id)

        async def reconfigure(self, account_id, _paths, operation) -> None:
            await self.invalidate(account_id)
            operation()

    runtime = FakeRuntime()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_local_companion_paths] = lambda: paths
    app.dependency_overrides[get_hermes_runtime_manager] = lambda: runtime
    try:
        yield TestClient(app), paths, runtime
    finally:
        app.dependency_overrides.clear()


def test_memory_and_skill_files_are_visible_and_user_files_are_editable(
    customization_client,
) -> None:
    client, paths, runtime = customization_client
    initial = client.get("/settings/agent-resources")

    assert initial.status_code == 200
    assert {skill["name"] for skill in initial.json()["skills"]} == {
        "application-assistance",
        "evidence-first-tailoring",
        "job-discovery",
    }
    assert all(skill["built_in"] for skill in initial.json()["skills"])

    created = client.post(
        "/settings/agent-resources/memory",
        json={
            "name": "search-preferences",
            "content": "# Search preferences\n\nPrefer Berlin and remote roles.\n",
        },
    )
    assert created.status_code == 200
    assert created.json()["memories"][0]["editable"] is True
    assert (
        paths.workspace / "agent" / "memories" / "search-preferences.md"
    ).is_file()
    assert (
        paths.hermes_profile
        / "profiles"
        / "career-companion"
        / "memories"
        / "search-preferences.md"
    ).is_file()

    updated = client.put(
        "/settings/agent-resources/memory/search-preferences",
        json={
            "name": "search-preferences",
            "content": "# Search preferences\n\nPrefer Hamburg and remote roles.\n",
        },
    )
    assert updated.status_code == 200
    assert "Hamburg" in updated.json()["memories"][0]["content"]

    skill = client.post(
        "/settings/agent-resources/skill",
        json={
            "name": "salary-review",
            "content": (
                "---\n"
                "name: salary-review\n"
                "description: Compare a role's compensation with stated preferences.\n"
                "---\n\n"
                "# Salary review\n\n- Show assumptions.\n"
            ),
        },
    )
    assert skill.status_code == 200
    assert any(item["name"] == "salary-review" for item in skill.json()["skills"])

    protected = client.delete("/settings/agent-resources/skill/job-discovery")
    assert protected.status_code == 403

    removed = client.delete(
        "/settings/agent-resources/memory/search-preferences"
    )
    assert removed.status_code == 200
    assert removed.json()["memories"] == []
    assert len(runtime.invalidated) >= 4


def test_linkedin_mcp_preset_is_allowlisted_and_mcp_crud_updates_runtime_config(
    customization_client,
) -> None:
    client, paths, runtime = customization_client
    initial = client.get("/settings/mcp")

    assert initial.status_code == 200
    linkedin = next(
        server
        for server in initial.json()["servers"]
        if server["name"] == "linkedin-search"
    )
    assert linkedin["command"] == "uvx"
    assert linkedin["args"] == ["mcp-server-linkedin@latest"]
    assert linkedin["tool_allowlist"] == ["search_jobs", "get_job_details"]
    assert linkedin["enabled"] is False
    assert linkedin["preset"] is True

    editable = {
        key: value
        for key, value in linkedin.items()
        if key not in {"preset", "command_available"}
    }
    editable["enabled"] = True
    enabled = client.put("/settings/mcp/linkedin-search", json=editable)

    assert enabled.status_code == 200
    configured = yaml.safe_load(
        (
            paths.hermes_profile
            / "profiles"
            / "career-companion"
            / "config.yaml"
        ).read_text(encoding="utf-8")
    )["mcp_servers"]["linkedin-search"]
    assert configured["command"] == "uvx"
    assert configured["args"] == ["mcp-server-linkedin@latest"]
    assert configured["env"] == {"UV_HTTP_TIMEOUT": "300"}
    assert configured["tools"]["include"] == ["search_jobs", "get_job_details"]
    assert configured_mcp_tool_names(paths, REPOSITORY_ROOT / "agent-profile") == [
        "mcp__linkedin_search__get_job_details",
        "mcp__linkedin_search__search_jobs",
    ]

    unsafe_linkedin = editable | {
        "tool_allowlist": ["search_jobs", "send_message"]
    }
    rejected_linkedin = client.put(
        "/settings/mcp/linkedin-search",
        json=unsafe_linkedin,
    )
    assert rejected_linkedin.status_code == 422

    colliding = {
        "name": "normalization-collision",
        "display_name": "Normalization collision",
        "description": "Must not reach the runtime registry.",
        "transport": "http",
        "command": None,
        "args": [],
        "url": "http://127.0.0.1:9912/mcp",
        "tool_allowlist": ["search-jobs", "search_jobs"],
        "forwarded_environment": [],
        "environment": {},
        "source_url": None,
        "warning": None,
        "enabled": True,
    }
    rejected_collision = client.post("/settings/mcp", json=colliding)
    assert rejected_collision.status_code == 422
    assert not any(
        server["name"] == "normalization-collision"
        for server in client.get("/settings/mcp").json()["servers"]
    )

    coerced_activation = colliding | {
        "name": "coerced-activation",
        "tool_allowlist": ["search"],
        "enabled": "true",
    }
    assert client.post("/settings/mcp", json=coerced_activation).status_code == 422

    custom = {
        "name": "local-research",
        "display_name": "Local research",
        "description": "A local read-only research endpoint.",
        "transport": "http",
        "command": None,
        "args": [],
        "url": "http://127.0.0.1:9911/mcp",
        "tool_allowlist": ["search"],
        "forwarded_environment": [],
        "environment": {},
        "source_url": None,
        "warning": None,
        "enabled": False,
    }
    created = client.post("/settings/mcp", json=custom)
    assert created.status_code == 200
    assert any(server["name"] == "local-research" for server in created.json()["servers"])

    invalid = custom | {"name": "unsafe", "enabled": True, "tool_allowlist": []}
    rejected = client.post("/settings/mcp", json=invalid)
    assert rejected.status_code == 422

    removed = client.delete("/settings/mcp/local-research")
    assert removed.status_code == 200
    assert not any(server["name"] == "local-research" for server in removed.json()["servers"])
    assert len(runtime.invalidated) == 3


def test_mcp_reconfiguration_failure_is_fail_closed_before_or_after_stop(
    customization_client,
    monkeypatch,
) -> None:
    client, paths, runtime = customization_client
    payload = _custom_stdio_mcp("failure-probe")
    profile_config = (
        paths.hermes_profile / "profiles" / "career-companion" / "config.yaml"
    )
    original_profile = profile_config.read_bytes()

    async def fail_before_stop(_account_id, _paths, _operation):
        from app.hermes_runtime import HermesRuntimeUnavailable

        raise HermesRuntimeUnavailable("runtime stop failed")

    monkeypatch.setattr(runtime, "reconfigure", fail_before_stop)
    stopped_failure = client.post("/settings/mcp", json=payload)
    assert stopped_failure.status_code == 503
    with account_session(paths) as session:
        assert session.get(MCPServerRecord, "failure-probe") is None
    assert profile_config.read_bytes() == original_profile

    async def stopped_then_run(account_id, _paths, operation):
        runtime.invalidated.append(account_id)
        operation()

    monkeypatch.setattr(runtime, "reconfigure", stopped_then_run)

    def fail_synchronization(*_args, **_kwargs):
        raise OSError("profile replace failed")

    monkeypatch.setattr(
        "app.main.synchronize_mcp_profile_config_from_session",
        fail_synchronization,
    )
    synchronization_failure = client.post("/settings/mcp", json=payload)
    assert synchronization_failure.status_code == 409
    assert runtime.invalidated
    with account_session(paths) as session:
        assert session.get(MCPServerRecord, "failure-probe") is None
    assert profile_config.read_bytes() == original_profile


def test_legacy_mcp_mutation_is_denied_after_strict_validation(
    customization_client,
) -> None:
    _, paths, _ = customization_client
    payload = {
        key: value
        for key, value in _custom_stdio_mcp("legacy-safe").items()
        if key not in {"source_url", "warning"}
    }

    with account_session(paths) as session:
        validated = MCPServerConfig.model_validate(payload)
        with pytest.raises(HTTPException) as denied:
            set_mcp("legacy-safe", validated, session)
        assert denied.value.status_code == 403
        assert session.get(MCPServerRecord, "legacy-safe") is None


@pytest.mark.parametrize(
    "servers",
    [
        [
            {
                "name": "company-jobs",
                "enabled": True,
                "tool_allowlist": ["search-jobs", "search_jobs"],
            }
        ],
        [
            {
                "name": "company-jobs",
                "enabled": True,
                "tool_allowlist": ["search"],
            },
            {
                "name": "company_jobs",
                "enabled": True,
                "tool_allowlist": ["search"],
            },
        ],
    ],
)
def test_mcp_registry_name_collisions_fail_closed(servers) -> None:
    with pytest.raises(ValueError, match="collide after Hermes name normalization"):
        _configured_mcp_tool_names_from_servers(servers)


def test_mcp_runtime_name_construction_rejects_wildcards() -> None:
    with pytest.raises(ValueError, match="exact non-wildcard names"):
        _configured_mcp_tool_names_from_servers(
            [
                {
                    "name": "company-jobs",
                    "enabled": True,
                    "tool_allowlist": ["*"],
                }
            ]
        )


@pytest.mark.parametrize(
    "tool_name",
    [
        "send-message",
        "send.message",
        "send:message",
        "send_message",
        "Submit-Application",
        "apply-to-job",
        "connect-with-person",
        "contact-person",
        "create-post",
        "create-post-now",
        "delete_record",
        "read-secret",
        "tool-call",
        "career-job-queue",
        "skill-manage",
        "search-files",
        "browser.navigate",
    ],
)
def test_mcp_prohibited_tool_aliases_are_blocked_after_hermes_normalization(
    tool_name,
) -> None:
    with pytest.raises(ValueError, match="prohibited after Hermes normalization"):
        validate_mcp_tool_allowlist([tool_name])
    with pytest.raises(ValueError, match="prohibited after Hermes normalization"):
        MCPServerSettingsRequest.model_validate(
            _custom_stdio_mcp("blocked-alias")
            | {"tool_allowlist": [tool_name]}
        )


def test_direct_service_rejects_normalized_prohibited_tool_without_mutation(
    customization_client,
) -> None:
    _, paths, _ = customization_client
    with account_session(paths) as session:
        with pytest.raises(ValueError, match="prohibited after Hermes normalization"):
            upsert_mcp_server(
                session,
                _custom_stdio_mcp("direct-blocked")
                | {"tool_allowlist": ["submit.application"]},
            )
        assert session.get(MCPServerRecord, "direct-blocked") is None


@pytest.mark.parametrize(
    "enabled",
    ["true", "false", "yes", "", 1, 0, None, [], {}],
)
def test_mcp_runtime_activation_requires_an_exact_boolean(enabled) -> None:
    with pytest.raises(ValueError, match="enabled must be an exact boolean"):
        _configured_mcp_tool_names_from_servers(
            [
                {
                    "name": "company-jobs",
                    "enabled": enabled,
                    "tool_allowlist": ["search"],
                }
            ]
        )


def test_mcp_runtime_activation_accepts_only_literal_true_or_false() -> None:
    server = {
        "name": "company-jobs",
        "tool_allowlist": ["search"],
    }
    assert _configured_mcp_tool_names_from_servers(
        [server | {"enabled": False}]
    ) == []
    assert _configured_mcp_tool_names_from_servers(
        [server | {"enabled": True}]
    ) == ["mcp__company_jobs__search"]


@pytest.mark.parametrize(
    "tool_allowlist",
    [None, "search", ("search",), {"search"}, {}, 1, True, []],
)
def test_enabled_mcp_runtime_include_requires_a_nonempty_exact_list(
    tool_allowlist,
) -> None:
    with pytest.raises(ValueError):
        _configured_mcp_tool_names_from_servers(
            [
                {
                    "name": "company-jobs",
                    "enabled": True,
                    "tool_allowlist": tool_allowlist,
                }
            ]
        )


@pytest.mark.parametrize(
    "servers",
    [None, {}, "server", ({},), [None], ["server"], [{}]],
)
def test_mcp_runtime_server_container_types_fail_closed(servers) -> None:
    with pytest.raises(ValueError):
        _configured_mcp_tool_names_from_servers(servers)


@pytest.mark.parametrize("enabled", ["true", "false", 1, 0, None, [], {}])
def test_mcp_presets_reject_coerced_activation(enabled) -> None:
    assert _preset_payload(
        {
            "name": "company-jobs",
            "transport": "http",
            "url": "https://example.test/mcp",
            "tool_allowlist": ["search"],
            "enabled": enabled,
        }
    ) is None


def _custom_stdio_mcp(name: str = "safe-custom") -> dict:
    return {
        "name": name,
        "display_name": "Safe custom",
        "description": "A bounded test server.",
        "transport": "stdio",
        "command": "safe-mcp-command",
        "args": [],
        "url": None,
        "tool_allowlist": ["search"],
        "forwarded_environment": [],
        "environment": {},
        "source_url": None,
        "warning": None,
        "enabled": False,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("args", "search"),
        ("args", {"tool": "search"}),
        ("args", None),
        ("args", True),
        ("tool_allowlist", "search"),
        ("tool_allowlist", {"search": True}),
        ("tool_allowlist", None),
        ("forwarded_environment", "SAFE_MCP_ARG"),
        ("forwarded_environment", {"SAFE_MCP_ARG": True}),
        ("forwarded_environment", None),
        ("environment", ["SAFE_MCP_ARG"]),
        ("environment", "SAFE_MCP_ARG"),
        ("environment", None),
    ],
)
def test_public_mcp_schema_rejects_coercive_boundary_containers(
    field,
    value,
) -> None:
    with pytest.raises(ValueError):
        MCPServerSettingsRequest.model_validate(_custom_stdio_mcp() | {field: value})


@pytest.mark.parametrize(
    "payload",
    [
        _custom_stdio_mcp() | {"transport": "sse"},
        _custom_stdio_mcp() | {"headers": {"Authorization": "${OPENAI_API_KEY}"}},
        _custom_stdio_mcp()
        | {"environment": {"SAFE_VALUE": {"nested": "${OPENAI_API_KEY}"}}},
    ],
)
def test_public_mcp_schema_rejects_unrendered_transports_and_nested_fields(
    payload,
) -> None:
    with pytest.raises(ValueError):
        MCPServerSettingsRequest.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "reference", "transport"),
    [
        ("command", "${CAREER_COMPANION_PLUGIN_TOKEN}", "stdio"),
        ("args", "prefix-${API_SERVER_KEY}", "stdio"),
        ("environment", "${OPENAI_API_KEY}", "stdio"),
        ("url", "https://example.test/${HERMES_HOME}", "http"),
        ("args", "${HERMES_WRITE_SAFE_ROOT}", "stdio"),
        ("args", "${CAREER_COMPANION_GUARD_NONCE}", "stdio"),
        ("environment", "${CAREER_COMPANION_ACCOUNT_KEY}", "stdio"),
        ("args", "${openai_api_key}", "stdio"),
        ("args", "${career_companion_guard_nonce}", "stdio"),
        ("args", "${ENV:OPENAI_API_KEY}", "stdio"),
        ("args", "${env:OPENAI_API_KEY}", "stdio"),
        ("args", "${OPENAI_API_KEY", "stdio"),
        ("args", "${SAFE${OPENAI_API_KEY}}", "stdio"),
        ("args", "%24%7BOPENAI_API_KEY%7D", "stdio"),
        ("url", "https://example.test/%2524%257BAPI_SERVER_KEY%257D", "http"),
    ],
)
def test_public_mcp_rejects_runtime_secret_and_malformed_interpolation_without_mutation(
    customization_client,
    field,
    reference,
    transport,
) -> None:
    client, paths, _ = customization_client
    before_names = {
        server["name"] for server in client.get("/settings/mcp").json()["servers"]
    }
    config_path = (
        paths.hermes_profile / "profiles" / "career-companion" / "config.yaml"
    )
    before_render = config_path.read_bytes()
    payload = _custom_stdio_mcp("interpolation-probe")
    if transport == "http":
        payload |= {"transport": "http", "command": None, "args": [], "url": reference}
    elif field == "args":
        payload["args"] = [reference]
    elif field == "environment":
        payload["environment"] = {"SAFE_VALUE": reference}
    else:
        payload[field] = reference

    response = client.post("/settings/mcp", json=payload)

    assert response.status_code == 422
    assert {
        server["name"] for server in client.get("/settings/mcp").json()["servers"]
    } == before_names
    assert config_path.read_bytes() == before_render


def test_explicit_non_reserved_mcp_reference_is_preserved_for_hermes(
    customization_client,
) -> None:
    client, paths, _ = customization_client
    payload = _custom_stdio_mcp("explicit-safe-reference") | {
        "args": ["--scope=${SAFE_MCP_ARG}"],
        "environment": {"SAFE_VALUE": "${env:SAFE_MCP_ARG}"},
        "forwarded_environment": ["SAFE_MCP_ARG"],
    }

    accepted = client.post("/settings/mcp", json=payload)

    assert accepted.status_code == 200
    rendered = yaml.safe_load(
        (
            paths.hermes_profile
            / "profiles"
            / "career-companion"
            / "config.yaml"
        ).read_text(encoding="utf-8")
    )["mcp_servers"]["explicit-safe-reference"]
    assert rendered["args"] == ["--scope=${SAFE_MCP_ARG}"]
    assert rendered["env"] == {"SAFE_VALUE": "${env:SAFE_MCP_ARG}"}

    unforwarded = payload | {
        "name": "unforwarded-reference",
        "forwarded_environment": [],
    }
    assert client.post("/settings/mcp", json=unforwarded).status_code == 422


def test_recursive_mcp_reference_validation_covers_nested_runtime_values() -> None:
    with pytest.raises(ValueError, match="Reserved runtime variables"):
        validate_mcp_environment_references(
            {
                "headers": {"Authorization": "Bearer ${OPENAI_API_KEY}"},
                "nested": ["safe", {"cwd": "${HERMES_HOME}"}],
            },
            [],
        )
    validate_mcp_environment_references(
        {"nested": ["${SAFE_ONE}", {"header": "${env:SAFE_TWO}"}]},
        ["SAFE_ONE", "SAFE_TWO"],
    )


def test_guard_nonce_is_reserved_at_service_and_persisted_render_boundaries(
    customization_client,
) -> None:
    _, paths, _ = customization_client
    payload = _custom_stdio_mcp("guard-nonce-service") | {
        "args": ["${career_companion_guard_nonce}"],
        "forwarded_environment": ["career_companion_guard_nonce"],
    }
    with account_session(paths) as session:
        with pytest.raises(ValueError, match="Reserved runtime variables"):
            upsert_mcp_server(session, payload)
        assert session.get(MCPServerRecord, "guard-nonce-service") is None

    profile_config = (
        paths.hermes_profile / "profiles" / "career-companion" / "config.yaml"
    )
    before = profile_config.read_bytes()
    with account_session(paths) as session:
        session.add(
            MCPServerRecord(
                name="guard-nonce-persisted",
                transport="stdio",
                enabled=False,
                config={
                    "display_name": "Guard nonce persisted",
                    "description": "Must fail closed.",
                    "command": "safe-mcp-command",
                    "args": ["${CAREER_COMPANION_GUARD_NONCE}"],
                    "tool_allowlist": ["search"],
                    "forwarded_environment": ["CAREER_COMPANION_GUARD_NONCE"],
                    "environment": {},
                    "preset": False,
                },
            )
        )
    with pytest.raises(ValueError, match="Reserved runtime variables"):
        synchronize_mcp_profile_config(paths, REPOSITORY_ROOT / "agent-profile")
    assert profile_config.read_bytes() == before


def test_direct_service_and_render_reject_malformed_mcp_without_mutation(
    customization_client,
) -> None:
    _, paths, _ = customization_client
    malformed = _custom_stdio_mcp("direct-malformed") | {
        "args": "${OPENAI_API_KEY}",
        "enabled": "yes",
    }
    with account_session(paths) as session:
        with pytest.raises(ValueError):
            upsert_mcp_server(session, malformed)
        assert session.get(MCPServerRecord, "direct-malformed") is None
    with pytest.raises(ValueError):
        _render_server(malformed)
    with pytest.raises(ValueError):
        _validate_mcp_server_runtime_config(
            _custom_stdio_mcp() | {"environment": {"NESTED": ["secret"]}}
        )


def test_persisted_raw_mcp_config_fails_before_rendering(
    customization_client,
) -> None:
    client, paths, _ = customization_client
    assert client.get("/settings/mcp").status_code == 200
    profile_config = (
        paths.hermes_profile / "profiles" / "career-companion" / "config.yaml"
    )
    before = profile_config.read_bytes()
    with account_session(paths) as session:
        session.add(
            MCPServerRecord(
                name="raw-malformed",
                transport="stdio",
                enabled=True,
                config={
                    "command": "safe-mcp-command",
                    "args": "search",
                    "tool_allowlist": "search",
                    "forwarded_environment": {},
                    "environment": [],
                    "preset": "false",
                },
            )
        )

    with pytest.raises(ValueError):
        configured_mcp_tool_names(paths, REPOSITORY_ROOT / "agent-profile")
    with pytest.raises(ValueError):
        synchronize_mcp_profile_config(paths, REPOSITORY_ROOT / "agent-profile")
    assert profile_config.read_bytes() == before


@pytest.mark.parametrize("raw", [b"false\n", b"0\n", b"[]\n", b"''\n"])
def test_mcp_render_rejects_every_falsy_non_mapping_profile_root(
    customization_client,
    raw,
) -> None:
    _, paths, _ = customization_client
    profile_config = (
        paths.hermes_profile / "profiles" / "career-companion" / "config.yaml"
    )
    profile_config.write_bytes(raw)

    with pytest.raises(ValueError, match="invalid"):
        synchronize_mcp_profile_config(paths, REPOSITORY_ROOT / "agent-profile")

    assert profile_config.read_bytes() == raw


def test_mcp_presets_reject_runtime_interpolation_and_malformed_containers() -> None:
    base = {
        "name": "preset-probe",
        "transport": "http",
        "url": "https://example.test/mcp",
        "tool_allowlist": ["search"],
        "enabled": False,
    }
    environment_url_base = {key: value for key, value in base.items() if key != "url"}
    assert _preset_payload(environment_url_base | {"url_env": "OPENAI_API_KEY"}) is None
    assert _preset_payload(base | {"args": "search"}) is None
    assert _preset_payload(base | {"tool_allowlist": "search"}) is None
    assert _preset_payload(base | {"forwarded_environment": "SAFE_MCP_ARG"}) is None
    assert _preset_payload(base | {"environment": []}) is None
    assert _preset_payload(base | {"headers": {"Token": "${OPENAI_API_KEY}"}}) is None
    assert _preset_payload(base | {"cwd": "${HERMES_HOME}"}) is None
    assert _preset_payload(base | {"url": None, "url_env": "SAFE_MCP_URL"}) is None
    assert (
        _preset_payload(environment_url_base | {"url_env": "SAFE_MCP_URL"})
        is not None
    )


def test_parallel_first_load_seeds_mcp_presets_once(customization_client) -> None:
    client, _, _ = customization_client

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda path: client.get(path),
                ["/settings/capabilities", "/settings/mcp"],
            )
        )

    assert [response.status_code for response in responses] == [200, 200]
    names = [server["name"] for server in responses[1].json()["servers"]]
    assert names.count("linkedin-search") == 1
