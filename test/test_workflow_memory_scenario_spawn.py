"""Spawn routing checks; these do not stand in for the kernel isolation E2E."""

import io
import json
from unittest.mock import Mock

import pytest

from kiro_crew.agent_sdk.drivers import acp as agent_sdk
from kiro_crew.testing import workflow_memory_scenario as scenario


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
