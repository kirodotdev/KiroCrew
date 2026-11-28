"""A scoped shell ``ask`` or ``deny`` rule on KAS keeps only the commands it covers
at the prompt.

Beside a plain shell grant, ``{"capability": "shell", "match": ["sudo *"],
"effect": "ask"}`` restricts only the commands its globs cover. The projection
lifts the grant into the spec-denies record with the rule's globs
(``askedCommands``), and the gate answers only a command none of them may match.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from test_tool_call_classification_kas import _kas_builtin_flow

from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_permissions import (
    CREW_ANSWERED_BUILTINS_KEY,
    CREW_ANSWERED_COMMANDS_KEY,
    CREW_SPEC_DENIES_KEY,
    _shell_glob_pattern,
)
from kiro_crew.acp.kas_spec_denies import projected_spec_denies
from kiro_crew.acp.kas_subagent_targets import _record_refuses
from kiro_crew.acp.types import AcpEvent
from kiro_crew.hooks import (
    TOOL_AUTO_APPROVE,
    HookManager,
    HooksConfig,
    ToolCall,
    hook_gate_kwargs,
)
from kiro_crew.platform import allowed_commands
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.spec_denied_gate import denied_grant_applies
from kiro_crew.platform.spec_denied_grant import SpecDeniedGrant


@pytest.fixture(autouse=True)
def _no_profiles(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def _issue_spec(*shell_rules: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "a",
        "prompt": "p",
        "allowedTools": ["read", "write", "shell", "grep", "glob"],
        "toolsSettings": {"write": {"allowedPaths": ["./**"]}},
        "permissions": {
            "rules": [
                {"capability": "fs_read", "effect": "allow"},
                {"capability": "fs_write", "match": ["./**"], "effect": "allow"},
                {"capability": "shell", "effect": "allow"},
                *shell_rules,
            ]
        },
    }


_SUDO_ASK = {"capability": "shell", "match": ["sudo *"], "effect": "ask"}


def _judge(spec: dict[str, Any], command: str):
    out = kas_agents.to_client_custom_agent("a", spec, "p")
    event: AcpEvent = _kas_builtin_flow(
        "run_command", title="Run Command", kind="execute", raw_input={"command": command}
    )
    event.spec_granted_builtins = frozenset(out.get(CREW_ANSWERED_BUILTINS_KEY, ()))
    event.spec_granted_commands = tuple(out.get(CREW_ANSWERED_COMMANDS_KEY, ()))
    event.spec_denied_grant = projected_spec_denies([out], "a", "")
    return HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))


def test_the_issue_spec_lifts_the_shell_with_the_asked_globs() -> None:
    out = kas_agents.to_client_custom_agent("a", _issue_spec(_SUDO_ASK), "p")
    answered = out[CREW_ANSWERED_BUILTINS_KEY]
    # write.allowedPaths only grants (`without_grant_only_settings`), so fs_write is answered.
    assert {"fs_read", "fs_write", "glob", "grep"} <= set(answered)
    assert "execute_bash" not in answered
    record = out[CREW_SPEC_DENIES_KEY]
    assert record["builtins"] == ["execute_bash", "execute_pwsh"]
    assert record["askedCommands"] == ["sudo .*"]
    assert record["deniedCommands"] == [] and record["deniedPaths"] == []
    # The ask still reaches the engine, so a sub-agent's call there asks too.
    assert _SUDO_ASK in out["permissions"]["rules"]
    assert not _record_refuses(record)


@pytest.mark.parametrize("effect", ["ask", "deny"])
def test_only_a_command_the_scoped_rule_covers_keeps_its_prompt(effect: str) -> None:
    spec = _issue_spec({**_SUDO_ASK, "effect": effect})
    granted = _judge(spec, "make build")
    assert (granted.action, granted.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")
    for command in ("sudo make build", "make build && sudo rm x"):
        assert _judge(spec, command).action != TOOL_AUTO_APPROVE


@pytest.mark.parametrize(
    "rule",
    [
        {"capability": "shell", "effect": "ask"},
        {"capability": "shell", "match": [], "effect": "ask"},
        {"capability": "shell", "match": ["sudo ?"], "effect": "ask"},
        {"capability": "shell", "match": ["sudo *", 3], "effect": "ask"},
        {"capability": "builtin", "match": ["sudo *"], "effect": "ask"},
    ],
    ids=["unscoped", "empty-match", "unread-glob", "non-string", "meta-capability"],
)
def test_a_rule_crew_cannot_scope_still_withholds_the_whole_shell(rule: dict[str, Any]) -> None:
    spec = _issue_spec(rule)
    out = kas_agents.to_client_custom_agent("a", spec, "p")
    assert "execute_bash" not in out.get(CREW_ANSWERED_BUILTINS_KEY, [])
    assert CREW_SPEC_DENIES_KEY not in out
    assert _judge(spec, "make build").action != TOOL_AUTO_APPROVE


def test_a_glob_past_the_length_bound_lifts_nothing() -> None:
    assert _shell_glob_pattern("sudo " + "x" * 900 + " *") is not None
    glob = "sudo " + "x" * 1100 + " *"
    assert _shell_glob_pattern(glob) is None
    rule = {"capability": "shell", "match": [glob], "effect": "ask"}
    out = kas_agents.to_client_custom_agent("a", _issue_spec(rule), "p")
    assert CREW_SPEC_DENIES_KEY not in out


def test_globs_past_the_bound_lift_nothing() -> None:
    rule = {"capability": "shell", "match": [f"tool{i} *" for i in range(65)], "effect": "ask"}
    out = kas_agents.to_client_custom_agent("a", _issue_spec(rule), "p")
    assert CREW_SPEC_DENIES_KEY not in out


def test_a_scoped_ask_beside_a_denied_command_records_both() -> None:
    spec = _issue_spec(_SUDO_ASK)
    spec["toolsSettings"] = {"shell": {"deniedCommands": ["make clean"]}}
    record = kas_agents.to_client_custom_agent("a", spec, "p")[CREW_SPEC_DENIES_KEY]
    assert record["builtins"] == ["execute_bash", "execute_pwsh"]
    assert record["askedCommands"] == ["sudo .*"]
    assert record["deniedCommands"] == ["make clean"]
    assert _judge(spec, "make build").action == TOOL_AUTO_APPROVE
    assert _judge(spec, "sudo make build").action != TOOL_AUTO_APPROVE


def test_a_command_the_asked_globs_cannot_judge_keeps_its_prompt() -> None:
    lifted = SpecDeniedGrant(builtins=frozenset({"execute_bash"}))
    asked = dataclasses.replace(lifted, asked_commands=("sudo .*",))
    names = ("execute_bash",)
    for command, match, granted in (("ls", False, True), ("ls $(id)", None, False)):
        assert allowed_commands.denied_match(asked.asked_commands, command) is match
        call = ToolCall(title="Run Command", is_shell=True, command=command)
        assert denied_grant_applies(dataclasses.replace(call, spec_denied_grant=lifted), names)
        call = dataclasses.replace(call, spec_denied_grant=asked)
        assert denied_grant_applies(call, names) is granted


def test_a_malformed_asked_list_grants_nothing() -> None:
    out = kas_agents.to_client_custom_agent("a", _issue_spec(_SUDO_ASK), "p")
    out[CREW_SPEC_DENIES_KEY] = {**out[CREW_SPEC_DENIES_KEY], "askedCommands": "sudo .*"}
    assert projected_spec_denies([out], "a", "") is None
