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
        from tools.registry import registry

        assert importlib.metadata.version("hermes-agent") == "0.18.2"
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
            enabled_toolsets=["todo", "skills", "session_search", "clarify"],
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
        assert {"tool_call", "tool_describe", "tool_search"}.isdisjoint(
            surfaced_names
        )

        allowed_registered_helpers = {
            "clarify",
            "session_search",
            "skill_view",
            "skills_list",
            "todo",
        }
        assert allowed_registered_helpers <= registered_names
        for name in registered_names:
            directive = plugin._guard_tool_call(name, {}, **actual_kwargs)
            if name in allowed_registered_helpers:
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

        manager = hermes_plugins.PluginManager()
        manager._hooks["pre_tool_call"] = [plugin._guard_tool_call]
        hermes_plugins._plugin_manager = manager
        executed = []

        def hidden_handler(args, **kwargs):
            executed.append(("hidden", args, kwargs))
            return json.dumps({"executed": "hidden"})

        def career_handler(args, **kwargs):
            executed.append(("career", args, kwargs))
            return json.dumps({"executed": "career"})

        schema = {
            "description": "runtime boundary sentinel",
            "parameters": {"type": "object", "properties": {}},
        }
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
        assert "outside Pilot's local task boundary" in nested_blocked
        assert executed == []

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
            assert (
                "not available in this session" in nested_error
                or "is not a deferrable tool" in nested_error
            ), (hidden_name, nested)
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
    (hermes_home / "config.yaml").write_text(
        (
            "skills:\n"
            "  inline_shell: false\n"
            "tools:\n"
            "  tool_search:\n"
            "    enabled: 'off'\n"
        ),
        encoding="utf-8",
    )
    environment.update(
        {
            "CAREER_COMPANION_ALLOWED_MCP_TOOLS": json.dumps(
                ["mcp__linkedin_search__search_jobs"]
            ),
            "HERMES_HOME": str(hermes_home),
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
