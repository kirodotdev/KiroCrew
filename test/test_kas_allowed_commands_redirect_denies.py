"""A spec's ``deniedCommands`` beside ``allowedCommands`` on KAS still refuses or
withholds a command that carries a plain redirect.

``allowedCommands`` matches a command with its plain redirects taken off
(``allowed_commands.split_redirects``). A deny that names the program part must
not be skipped because the redirect keeps the ``spec-denies`` row from splitting
the command: such a call is never granted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_tool_call_classification_kas import _kas_builtin_flow

from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_permissions import CREW_ANSWERED_COMMANDS_KEY
from kiro_crew.acp.kas_spec_denies import projected_spec_denies
from kiro_crew.acp.types import AcpEvent
from kiro_crew.hooks import TOOL_AUTO_APPROVE, HookManager, HooksConfig, hook_gate_kwargs
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp


@pytest.fixture(autouse=True)
def _no_profiles(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


_SETTINGS = {
    "shell": {
        "allowedCommands": ["make build", "make clean.*"],
        "deniedCommands": ["make clean"],
    }
}


def _judge(command: str, cwd: Path):
    out = kas_agents.to_client_custom_agent(
        "a", {"name": "a", "prompt": "p", "toolsSettings": _SETTINGS}, "p"
    )
    event: AcpEvent = _kas_builtin_flow(
        "run_command", title="Run Command", kind="execute", raw_input={"command": command}
    )
    event.spec_granted_builtins = frozenset(out.get("_crewAnsweredBuiltins", ()))
    event.spec_granted_commands = tuple(out.get(CREW_ANSWERED_COMMANDS_KEY, ()))
    event.spec_denied_grant = projected_spec_denies([out], "a", "")
    event.spec_write_cwd = str(cwd)
    return HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))


@pytest.mark.parametrize("command", ["make clean > out.log", "make clean > out.log 2>&1"])
def test_a_denied_command_with_a_redirect_is_never_granted(tmp_path: Path, command: str) -> None:
    assert _judge(command, tmp_path).action != TOOL_AUTO_APPROVE
