from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIRECTORY = REPOSITORY_ROOT / "agent-profile" / "plugins" / "career-companion"


def _pinned_hermes_python() -> Path:
    configured = os.getenv("CAREERPILOT_HERMES_TEST_PYTHON", "").strip()
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return candidate
        pytest.fail(f"CAREERPILOT_HERMES_TEST_PYTHON does not exist: {candidate}")

    if importlib.util.find_spec("model_tools") is not None:
        return Path(sys.executable)

    executable = shutil.which("hermes")
    if executable:
        sibling_python = Path(executable).resolve().parent / "python"
        if sibling_python.is_file():
            return sibling_python

    pytest.skip(
        "Set CAREERPILOT_HERMES_TEST_PYTHON to run the pinned Hermes dispatch probe"
    )


def test_pinned_hermes_registry_dispatch_is_closed_by_exact_tool_name(tmp_path) -> None:
    script = textwrap.dedent(
        """
        import importlib.metadata
        import importlib.util
        import json
        import os
        from pathlib import Path
        import sys
        import yaml

        plugin_directory = Path(sys.argv[1])
        module_name = "_career_companion_runtime_boundary_probe"
        spec = importlib.util.spec_from_file_location(
            module_name,
            plugin_directory / "__init__.py",
            submodule_search_locations=[str(plugin_directory)],
        )
        assert spec is not None and spec.loader is not None
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = plugin
        spec.loader.exec_module(plugin)

        import hermes_cli.plugins as hermes_plugins
        import model_tools
        from tools.mcp_tool import _interpolate_env_vars
        from tools.registry import registry

        schema = {
            "description": "runtime boundary sentinel",
            "parameters": {"type": "object", "properties": {}},
        }
        registry.register(
            name="mcp__linkedin_search__search_jobs",
            toolset="mcp-linkedin-search",
            schema=schema,
            handler=lambda args, **kwargs: json.dumps({"executed": "mcp"}),
        )
        hermes_plugins.discover_plugins(force=True)
        loaded_manager = hermes_plugins.get_plugin_manager()
        assert loaded_manager.has_hook("pre_tool_call")
        enabled_external = {
            key
            for key, value in loaded_manager._plugins.items()
            if value.enabled and value.manifest.source != "bundled"
        }
        assert enabled_external == {"career-companion"}

        assert importlib.metadata.version("hermes-agent") == "0.18.2"
        interpolated = _interpolate_env_vars(
            {
                "command": "${CAREER_COMPANION_PLUGIN_TOKEN}",
                "args": [
                    "prefix-${API_SERVER_KEY}",
                    {"nested": "${env: OPENAI_API_KEY }"},
                ],
                "cwd": "${HERMES_HOME}",
                "url": "https://example.test/${CAREER_COMPANION_ACCOUNT_KEY}",
                "headers": {"Authorization": "Bearer ${OPENAI_API_KEY}"},
                "env": {"PROVIDER_TOKEN": "${OPENAI_API_KEY}"},
                "unset": "${ABSENT_MCP_PROBE}",
                "uppercase_prefix": "${ENV:OPENAI_API_KEY}",
                "encoded": "%24%7BOPENAI_API_KEY%7D",
                "${OPENAI_API_KEY}": "keys-are-not-interpolated",
            }
        )
        assert interpolated["command"] == "probe-bridge-secret"
        assert interpolated["args"] == [
            "prefix-probe-api-secret",
            {"nested": "probe-openai-secret"},
        ]
        assert interpolated["cwd"] == os.environ["HERMES_HOME"]
        assert interpolated["url"].endswith("probe-account-key")
        assert interpolated["headers"] == {
            "Authorization": "Bearer probe-openai-secret"
        }
        assert interpolated["env"] == {"PROVIDER_TOKEN": "probe-openai-secret"}
        assert interpolated["unset"] == "${ABSENT_MCP_PROBE}"
        assert interpolated["uppercase_prefix"] == "${ENV:OPENAI_API_KEY}"
        assert interpolated["encoded"] == "%24%7BOPENAI_API_KEY%7D"
        assert interpolated["${OPENAI_API_KEY}"] == "keys-are-not-interpolated"
        actual_kwargs = {
            "task_id": "task-1",
            "session_id": "message-1",
            "tool_call_id": "call-1",
            "turn_id": "turn-1",
            "api_request_id": "request-1",
            "middleware_trace": [],
        }
        reported_hidden_names = {
            "patch",
            "search_files",
            "web_extract",
            "browser_navigate",
            "browser_snapshot",
            "process",
            "close_terminal",
        }
        registered_names = set(registry.get_all_tool_names())
        assert reported_hidden_names <= registered_names
        surfaced = model_tools.get_tool_definitions(
            enabled_toolsets=[
                "todo",
                "skills",
                "session_search",
                "clarify",
                "career-web",
                "mcp-linkedin-search",
            ],
            disabled_toolsets=[
                "browser",
                "code_execution",
                "file",
                "terminal",
                "web",
            ],
            quiet_mode=True,
        )
        surfaced_names = {
            item["function"]["name"] for item in surfaced if "function" in item
        }
        allowed_registered_helpers = {
            "clarify",
            "session_search",
            "skill_view",
            "skills_list",
            "todo",
        }
        expected_surface = (
            allowed_registered_helpers
            | {tool.name for tool in plugin.TOOLS}
            | {"mcp__linkedin_search__search_jobs"}
        )
        assert surfaced_names == expected_surface, (
            sorted(surfaced_names - expected_surface),
            sorted(expected_surface - surfaced_names),
        )
        assert allowed_registered_helpers <= registered_names
        for name in registered_names:
            directive = plugin._guard_tool_call(name, {}, **actual_kwargs)
            if name == "career_identity_update":
                assert directive is not None and directive["action"] == "approve"
            elif name in expected_surface:
                assert directive is None, (name, directive)
            else:
                assert directive is not None and directive["action"] == "block", name

        for name in plugin._SAFE_HERMES_HELPERS:
            assert plugin._guard_tool_call(name, {}, **actual_kwargs) is None
        for name in ("tool_call", "tool_describe", "tool_search"):
            assert plugin._guard_tool_call(name, {}, **actual_kwargs)["action"] == "block"
        for name in ("career_job_queue", "career_application_decide"):
            assert plugin._guard_tool_call(name, {}, **actual_kwargs) is None
        assert plugin._guard_tool_call(
            "mcp__linkedin_search__search_jobs", {}, **actual_kwargs
        ) is None
        assert plugin._guard_tool_call(
            "mcp__linkedin_search__unlisted", {}, **actual_kwargs
        )["action"] == "block"
        assert plugin._guard_tool_call(
            "unknown_future_registry_tool", {}, **actual_kwargs
        )["action"] == "block"
        assert plugin._guard_tool_call("search_files", {})["action"] == "block"

        from hermes_cli import profiles
        from tools.session_search_tool import _resolve_profile_db
        from tools.skills_tool import skill_view

        hermes_home = Path(os.environ["HERMES_HOME"]).resolve()
        assert profiles.get_profile_dir("default").resolve() == hermes_home
        try:
            _resolve_profile_db("../../outside")
        except ValueError:
            pass
        else:
            raise AssertionError("session_search accepted a traversal profile")
        traversal = json.loads(skill_view("../../outside"))
        assert traversal["success"] is False
        assert "traversal" in traversal["error"].lower()

        manager = loaded_manager
        executed = []

        import hermes_cli.config as hermes_config

        config_path = hermes_home / "config.yaml"
        skill_dir = hermes_home / "skills" / "boundary-probe"
        skill_dir.mkdir(parents=True)
        inline_marker = hermes_home / "inline-shell-executed"
        (skill_dir / "SKILL.md").write_text(
            (
                "---\\n"
                "name: boundary-probe\\n"
                "description: Runtime boundary probe.\\n"
                "---\\n\\n"
                f"!`touch {inline_marker}`\\n"
            ),
            encoding="utf-8",
        )

        def write_boundary_config(skills_value=...):
            payload = {"tools": {"tool_search": {"enabled": False}}}
            if skills_value is not ...:
                payload["skills"] = skills_value
            config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
            hermes_config._LOAD_CONFIG_CACHE.clear()
            hermes_config._RAW_CONFIG_CACHE.clear()

        # Confirm the pinned implementation treats a non-empty string as enabled.
        write_boundary_config({"inline_shell": "true"})
        direct_skill_view = json.loads(skill_view("boundary-probe"))
        assert direct_skill_view["success"] is True
        assert inline_marker.is_file()
        inline_marker.unlink()

        for safe_skills in (..., {}, {"inline_shell": False}):
            write_boundary_config(safe_skills)
            allowed_skill_view = model_tools.handle_function_call(
                "skill_view",
                {"name": "boundary-probe"},
                enabled_toolsets=["skills"],
                disabled_toolsets=["file", "terminal"],
                **{
                    key: value
                    for key, value in actual_kwargs.items()
                    if key != "middleware_trace"
                },
                tool_request_middleware_trace=[],
            )
            assert json.loads(allowed_skill_view)["success"] is True
            assert not inline_marker.exists()

        unsafe_inline_values = [
            True,
            "true",
            "false",
            "yes",
            "",
            1,
            0,
            None,
            [],
            [False],
            {},
            {"enabled": False},
        ]
        for inline_value in unsafe_inline_values:
            write_boundary_config({"inline_shell": inline_value})
            blocked_skill_view = model_tools.handle_function_call(
                "skill_view",
                {"name": "boundary-probe"},
                enabled_toolsets=["skills"],
                disabled_toolsets=["file", "terminal"],
                **{
                    key: value
                    for key, value in actual_kwargs.items()
                    if key != "middleware_trace"
                },
                tool_request_middleware_trace=[],
            )
            assert "skill_view is unavailable" in blocked_skill_view
            assert not inline_marker.exists()

        for malformed_skills in (None, [], "disabled", 0, False, True):
            write_boundary_config(malformed_skills)
            blocked_skill_view = model_tools.handle_function_call(
                "skill_view",
                {"name": "boundary-probe"},
                enabled_toolsets=["skills"],
                disabled_toolsets=["file", "terminal"],
                **{
                    key: value
                    for key, value in actual_kwargs.items()
                    if key != "middleware_trace"
                },
                tool_request_middleware_trace=[],
            )
            assert "skill_view is unavailable" in blocked_skill_view
            assert not inline_marker.exists()

        def hidden_handler(args, **kwargs):
            executed.append(("hidden", args, kwargs))
            return json.dumps({"executed": "hidden"})

        def career_handler(args, **kwargs):
            executed.append(("career", args, kwargs))
            return json.dumps({"executed": "career"})

        registry.register(
            name="reviewer_hidden_probe",
            toolset="todo",
            schema=schema,
            handler=hidden_handler,
        )
        registry.register(
            name="career_job_queue",
            toolset="career-web",
            schema=schema,
            handler=career_handler,
        )

        blocked = model_tools.handle_function_call(
            "reviewer_hidden_probe",
            {},
            enabled_toolsets=["todo"],
            disabled_toolsets=["file"],
            **{key: value for key, value in actual_kwargs.items() if key != "middleware_trace"},
            tool_request_middleware_trace=[],
        )
        assert "outside Pilot's local task boundary" in blocked
        assert executed == []

        nested_blocked = model_tools.handle_function_call(
            "tool_call",
            {"name": "reviewer_hidden_probe", "arguments": {}},
            enabled_toolsets=["todo"],
            disabled_toolsets=["file"],
            **{key: value for key, value in actual_kwargs.items() if key != "middleware_trace"},
            tool_request_middleware_trace=[],
        )
        assert "tool_call is disabled in CareerPilot" in nested_blocked
        assert executed == []

        for allowed_wrapped_name in (
            "career_job_queue",
            "career_application_decide",
            "skills_list",
            "mcp__linkedin_search__search_jobs",
        ):
            wrapped = model_tools.handle_function_call(
                "tool_call",
                {"name": allowed_wrapped_name, "arguments": {}},
                enabled_toolsets=[
                    "career-web",
                    "skills",
                    "mcp-linkedin-search",
                ],
                disabled_toolsets=["file", "terminal", "web"],
                **{
                    key: value
                    for key, value in actual_kwargs.items()
                    if key != "middleware_trace"
                },
                tool_request_middleware_trace=[],
            )
            assert "tool_call is disabled in CareerPilot" in wrapped

        from tools import tool_search as tool_search_module
        for target in (
            "career_job_queue",
            "career_application_decide",
            "skills_list",
            "mcp__linkedin_search__search_jobs",
            "search_files",
        ):
            underlying, arguments, error = tool_search_module.resolve_underlying_call(
                {"name": target, "arguments": {}}
            )
            assert underlying is None and arguments is None
            assert error == "tool_call is disabled in CareerPilot"

        for hidden_name in reported_hidden_names:
            nested = model_tools.handle_function_call(
                "tool_call",
                {"name": hidden_name, "arguments": {}},
                enabled_toolsets=["todo"],
                disabled_toolsets=[
                    "browser",
                    "browser-cdp",
                    "file",
                    "terminal",
                    "web",
                ],
                **{
                    key: value
                    for key, value in actual_kwargs.items()
                    if key != "middleware_trace"
                },
                tool_request_middleware_trace=[],
            )
            nested_error = json.loads(nested)["error"]
            assert "tool_call is disabled in CareerPilot" in nested_error
            assert executed == []

        reproduced = model_tools.handle_function_call(
            "search_files",
            {"path": "/private/tmp", "pattern": "career-pilot-p0-never-match"},
            enabled_toolsets=["todo"],
            disabled_toolsets=["file"],
            **{key: value for key, value in actual_kwargs.items() if key != "middleware_trace"},
            tool_request_middleware_trace=[],
        )
        assert "outside Pilot's local task boundary" in reproduced
        assert executed == []

        allowed = model_tools.handle_function_call(
            "career_job_queue",
            {},
            enabled_toolsets=["todo"],
            disabled_toolsets=["file"],
            **{key: value for key, value in actual_kwargs.items() if key != "middleware_trace"},
            tool_request_middleware_trace=[],
        )
        assert json.loads(allowed) == {"executed": "career"}
        assert [entry[0] for entry in executed] == ["career"]
        print(json.dumps({"registered": len(registered_names), "ok": True}))
        """
    )
    environment = dict(os.environ)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    shutil.copytree(
        PLUGIN_DIRECTORY,
        hermes_home / "plugins" / "career-companion",
    )
    (hermes_home / "config.yaml").write_text(
            (
                "plugins:\n"
                "  enabled: [career-companion]\n"
                "  disabled: []\n"
                "skills:\n"
            "  inline_shell: false\n"
            "tools:\n"
            "  tool_search:\n"
            "    enabled: false\n"
        ),
        encoding="utf-8",
    )
    environment.update(
        {
            "CAREER_COMPANION_ALLOWED_MCP_TOOLS": json.dumps(
                ["mcp__linkedin_search__search_jobs"]
            ),
            "API_SERVER_KEY": "probe-api-secret",
            "CAREER_COMPANION_ACCOUNT_KEY": "probe-account-key",
            "CAREER_COMPANION_PLUGIN_TOKEN": "probe-bridge-secret",
            "CAREER_COMPANION_GUARD_NONCE": "g" * 48,
            "HERMES_HOME": str(hermes_home),
            "OPENAI_API_KEY": "probe-openai-secret",
        }
    )

    completed = subprocess.run(
        [str(_pinned_hermes_python()), "-c", script, str(PLUGIN_DIRECTORY)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["ok"] is True
    assert result["registered"] >= 60


def test_pinned_profile_override_writes_guard_proof_to_installed_profile(
    tmp_path,
) -> None:
    pinned_python = _pinned_hermes_python()
    hermes_executable = pinned_python.with_name("hermes")
    if not hermes_executable.is_file():
        pytest.skip("Pinned Hermes executable is unavailable")
    base_home = tmp_path / "hermes"
    profile = base_home / "profiles" / "career-companion"
    shutil.copytree(PLUGIN_DIRECTORY, profile / "plugins" / "career-companion")
    (profile / "config.yaml").write_text(
        (
            "plugins:\n"
            "  enabled: [career-companion]\n"
            "  disabled: []\n"
            "skills:\n"
            "  inline_shell: false\n"
            "tools:\n"
            "  tool_search:\n"
            "    enabled: false\n"
        ),
        encoding="utf-8",
    )
    nonce = "n" * 48
    environment = dict(os.environ)
    environment.update(
        {
            "CAREER_COMPANION_ALLOWED_MCP_TOOLS": "[]",
            "CAREER_COMPANION_GUARD_NONCE": nonce,
            "HERMES_HOME": str(base_home),
        }
    )

    completed = subprocess.run(
        [str(hermes_executable), "-p", "career-companion", "tools", "list"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not (base_home / ".career-companion-guard").exists()
    assert (profile / ".career-companion-guard").read_text(encoding="utf-8") == nonce
