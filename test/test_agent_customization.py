from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest
import yaml

from app.auth import AuthStore
from app.dependencies import get_local_companion_paths
from app.main import app, get_hermes_runtime_manager, get_store
from career_companion.paths import CompanionPaths


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

    unsafe_linkedin = editable | {
        "tool_allowlist": ["search_jobs", "send_message"]
    }
    rejected_linkedin = client.put(
        "/settings/mcp/linkedin-search",
        json=unsafe_linkedin,
    )
    assert rejected_linkedin.status_code == 422

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
