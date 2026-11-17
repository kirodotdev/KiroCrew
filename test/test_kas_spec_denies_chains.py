"""The KAS ``spec-denies`` row judges each command of a chain.

A chain ``allowed_commands.chain_commands`` splits is refused when a recorded
``deniedCommands`` entry names any one of its commands. A command it cannot split
(a redirect, a substitution, a quote in a chain) is not refused by this row, and
still gets no grant, so it asks.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_tool_call_classification_kas import _kas_builtin_flow

from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_permissions import CREW_ANSWERED_COMMANDS_KEY
from kiro_crew.acp.kas_spec_denies import projected_spec_denies
from kiro_crew.acp.types import AcpEvent
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY, HookManager, HooksConfig, hook_gate_kwargs
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
        "allowedCommands": ["make build", "docker\\s+push.*", "make clean.*"],
        "deniedCommands": ["docker\\s+push.*", "make clean"],
    }
}


def _judge(command: str):
    out = kas_agents.to_client_custom_agent(
        "a", {"name": "a", "prompt": "p", "toolsSettings": _SETTINGS}, "p"
    )
    event: AcpEvent = _kas_builtin_flow(
        "run_command", title="Run Command", kind="execute", raw_input={"command": command}
    )
    event.spec_granted_builtins = frozenset(out.get("_crewAnsweredBuiltins", ()))
    event.spec_granted_commands = tuple(out.get(CREW_ANSWERED_COMMANDS_KEY, ()))
    event.spec_denied_grant = projected_spec_denies([out], "a", "")
    return HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))


@pytest.mark.parametrize(
    "command",
    [
        "docker push x && true",
        "true; docker push x",
        "make build | make clean now",
        "make build || docker push x",
    ],
)
def test_a_chain_a_deny_names_one_command_of_is_refused(command: str) -> None:
    verdict = _judge(command)
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-denies")


@pytest.mark.parametrize(
    "command",
    [
        "make build && make cleanup",
        "docker push $(cat tag)",
        "docker push x > out",
        "echo `docker push x`",
        "true && docker push 'x'",
        "echo ready # then; docker push x",
        "[[ -n a || docker push x ]]",
    ],
    ids=[
        "no-deny-names-a-command",
        "substitution",
        "redirect",
        "backtick",
        "quoted-chain",
        "comment-holds-a-separator",
        "test-body-holds-a-separator",
    ],
)
def test_a_chain_no_deny_names_or_crew_cannot_split_is_not_refused_here(command: str) -> None:
    """Not refused by this row, as before, and not granted by the recorded grant."""
    verdict = _judge(command)
    assert verdict.tier != "spec-denies"
    assert verdict.action != TOOL_AUTO_APPROVE
