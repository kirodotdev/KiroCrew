"""Transport checks; these do not replace the real workflow MCP E2E."""

import io
import json
from unittest.mock import Mock

import pytest

from kiro_crew.agent_sdk.drivers import acp as agent_sdk
from kiro_crew.testing import workflow_memory_scenario as scenario


@pytest.mark.parametrize("refresh", [False, True])
def test_title_transcript_does_not_execute_quoted_workflow_command(tmp_path, monkeypatch, refresh):
    from kiro_crew.dashboard.chat_title import _build_refresh_prompt, _build_title_prompt

    messages = [{"role": "user", "content": "[[WF_E2E:START:A]]"}]
    prompt = (
        _build_refresh_prompt(messages, "Workflow check")
        if refresh
        else _build_title_prompt(messages)
    )
    assert prompt is not None
    projection = Mock(side_effect=AssertionError("a quoted command cannot invoke MCP"))
    monkeypatch.setattr(agent_sdk, "projected_session_mcp_servers", projection)
    assert scenario.respond(prompt, [], str(tmp_path)) is None
    projection.assert_not_called()


@pytest.mark.parametrize("core", [None, {"name": "kirocrew-core", "type": "invalid"}])
def test_actual_workflow_command_requires_real_supported_transport(tmp_path, monkeypatch, core):
    monkeypatch.setattr(
        agent_sdk, "projected_session_mcp_servers", lambda *a, **k: [] if core is None else [core]
    )
    with pytest.raises(RuntimeError, match="real projected core MCP transport"):
        scenario.respond("[[WF_E2E:WORK:A]]", [], str(tmp_path))


def test_projected_mcp_uses_wrapped_command_environment_and_cleanup(tmp_path, monkeypatch):
    core = {"name": "kirocrew-core", "command": "spec-command", "args": ["spec-argument"]}
    monkeypatch.setattr(agent_sdk, "projected_session_mcp_servers", lambda *a, **k: [core])
    profile = tmp_path / "launcher"
    profile.write_text("test launcher")
    prepared_env = {"ONLY_SCRUBBED": "yes"}
    prepare = Mock(return_value=(["wrapped-command"], prepared_env, str(profile)))
    monkeypatch.setattr(scenario, "sandboxed_spawn_argv", prepare)
    spawn = Mock(side_effect=OSError("test spawn failure"))
    monkeypatch.setattr(scenario, "popen_limited", spawn)
    with pytest.raises(OSError, match="test spawn failure"):
        scenario.respond("[[WF_E2E:WORK:A]]", [], str(tmp_path))
    assert prepare.call_args.args == (["spec-command", "spec-argument"],)
    assert "first_party_fixed_argv" not in prepare.call_args.kwargs
    assert spawn.call_args.args == (["wrapped-command"],)
    assert spawn.call_args.kwargs["env"] is prepared_env
    assert "preexec_fn" not in spawn.call_args.kwargs
    assert spawn.call_args.kwargs["cwd"] == str(tmp_path)
    assert not profile.exists()


@pytest.mark.parametrize("rpc_error", [False, True])
def test_projected_mcp_cleans_up_after_rpc(tmp_path, monkeypatch, rpc_error):
    core = {"name": "kirocrew-core", "command": "spec-command"}
    monkeypatch.setattr(agent_sdk, "projected_session_mcp_servers", lambda *a, **k: [core])
    profile = tmp_path / "launcher"
    profile.write_text("test launcher")
    monkeypatch.setattr(
        scenario, "sandboxed_spawn_argv", Mock(return_value=(["wrapped"], {}, str(profile)))
    )
    replies = [
        {"id": 1, "error": "test RPC failure"} if rpc_error else {"id": 1, "result": {}},
        {"id": 2, "result": {"written": True}},
        {"id": 3, "result": {"memory": "private"}},
    ]
    process = Mock(
        stdin=io.StringIO(),
        stdout=io.StringIO("".join(json.dumps(reply) + "\n" for reply in replies)),
    )
    spawn = Mock(return_value=process)
    monkeypatch.setattr(scenario, "popen_limited", spawn)
    if rpc_error:
        with pytest.raises(RuntimeError, match="test RPC failure"):
            scenario.respond("[[WF_E2E:WORK:A]]", [], str(tmp_path))
    else:
        result = scenario.respond("[[WF_E2E:WORK:A]]", [], str(tmp_path))
        assert json.loads(result) == {"write": {"written": True}, "recall": {"memory": "private"}}
    spawn.assert_called_once()
    assert "preexec_fn" not in spawn.call_args.kwargs
    process.wait.assert_called_once_with(timeout=5)
    assert process.stdin.closed and process.stdout.closed
    assert not profile.exists()
