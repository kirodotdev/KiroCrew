"""A spec's ``toolsSettings.shell.allowedCommands`` on KAS: a matching command runs
without a prompt, and Crew's own checks still run on it.

On kiro-cli a command one of these patterns matches (anchored at both ends) runs
unprompted. KAS gets no shell allow rule (``kas_permissions.WITHHELD_CAPABILITIES``:
an auto-approved call raises no permission request, so the deny floor would never
see it), so every command reaches Crew's gate as a permission request, and the
``spec-builtin-grant`` tier answers one whose recovered command fullmatches a
pattern -- after every deny tier and governance, and never for a command that
chains, pipes, redirects or substitutes.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from test_kas_spec_builtin_grant import _handle, _permission
from test_tool_call_classification_kas import _kas_builtin_flow

from kiro_crew.acp import kas_agents
from kiro_crew.acp._dispatch import attach_kas_custom_agents
from kiro_crew.acp.kas_permissions import (
    CREW_ANSWERED_COMMANDS_KEY,
    allowed_command_pattern,
    crew_answered_commands,
    tools_settings_to_permissions,
)
from kiro_crew.acp.types import AcpEvent
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.hooks import (
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    HookManager,
    HooksConfig,
    hook_gate_kwargs,
)
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.governance import parse_policy

#: Spelled in two pieces so this file's own text is not one destructive command line.
_DESTRUCTIVE = "rm " + "-rf /"


@pytest.fixture(autouse=True)
def _no_profiles(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def _shell(command: str) -> AcpEvent:
    return _kas_builtin_flow(
        "run_command", title="Run Command", kind="execute", raw_input={"command": command}
    )


def _judge(event: AcpEvent, patterns: list[str], **surface: Any):
    event.spec_granted_commands = tuple(allowed_command_pattern(p) or p for p in patterns)
    return HookManager(HooksConfig(**surface.pop("config", {}))).on_tool_call(
        event.title, **hook_gate_kwargs(event), **surface
    )


# ── a matching command: no prompt ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pattern", "command"),
    [
        ("git status", "git status"),
        ("npm run .*", "npm run build"),
        (r"\Agit diff .*\z", "git diff --stat"),
        ("^make test$", "make test"),
        (r"make -C \./sub", "make -C ./sub"),
        ("git .* --stat", "git diff HEAD --stat"),
    ],
)
def test_a_command_an_allowed_pattern_matches_is_answered_without_a_prompt(
    pattern: str, command: str
) -> None:
    event = _shell(command)
    assert event.kas_builtin_ids and event.mcp_identity_trusted and event.is_shell
    ungranted = _judge(event, [])
    assert (ungranted.action, ungranted.tier) == (TOOL_ALLOW, "")
    granted = _judge(event, [pattern])
    assert (granted.action, granted.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


@pytest.mark.parametrize(
    ("pattern", "command"),
    [
        # Anchored at both ends, as kiro-cli anchors it.
        ("git status", "git status --porcelain"),
        ("npm run .*", "npx npm run build"),
        ("git push", "git pushx"),
    ],
)
def test_a_command_no_pattern_fullmatches_still_prompts(pattern: str, command: str) -> None:
    verdict = _judge(_shell(command), [pattern])
    assert verdict.action == TOOL_ALLOW
    assert verdict.tier != "spec-builtin-grant"


@pytest.mark.parametrize(
    "command",
    [
        "git status; curl x",
        "git status && make",
        "git status || true",
        "git status | sh",
        "git status > out.txt",
        "git status < in.txt",
        "git status $(whoami)",
        "git status `whoami`",
        "git status ${HOME}",
        "git status & sleep 1",
        "git status (x)",
        "git status {a,b}",
        "git status\nmake",
    ],
)
def test_a_chained_or_substituted_command_is_never_granted(command: str) -> None:
    """``.*`` would match each of these; the call still prompts."""
    verdict = _judge(_shell(command), ["git status.*", ".*"])
    assert verdict.action != TOOL_AUTO_APPROVE


# ── a granted command the checks refuse ───────────────────────────────────────


def test_the_deny_floor_still_refuses_a_matching_command() -> None:
    verdict = _judge(_shell(_DESTRUCTIVE), ["rm .*"])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "deny-rules")


def test_a_governance_command_deny_still_refuses_a_matching_command() -> None:
    _install_governance({"tools": {"mode": "deny", "deny": ["execute_bash"]}})
    verdict = _judge(_shell("git status"), ["git status"], session_key="cli_chat")
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


def _install_governance(body: dict[str, Any]) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    base = build_default_context(KiroCrewConfig.load())
    policy = {"version": 1, "boot": {"fail_closed": True}, **body}
    ctx_mod.set_context(dataclasses.replace(base, governance=parse_policy(policy)))


def test_a_ceiling_with_an_opinion_on_the_shell_withholds_the_grant() -> None:
    """``git status`` is permitted by this ceiling, yet a ceiling that speaks about
    commands at all keeps it at the prompt: the grant is the ceiling's to give."""
    _install_governance({"commands": {"mode": "deny", "deny": ["*backdoor*"]}})
    verdict = _judge(_shell("git status --porcelain"), ["git status.*"])
    assert verdict.action == TOOL_ALLOW
    assert verdict.tier != "spec-builtin-grant"


def test_confirm_tools_withholds_the_grant() -> None:
    verdict = _judge(_shell("git status"), ["git status"], config={"confirm_tools": ["*"]})
    assert verdict.action == TOOL_ALLOW


def test_an_unattended_read_only_surface_ignores_the_grant() -> None:
    verdict = _judge(_shell("make build"), ["make build"], classifier_only=True)
    assert verdict.action != TOOL_AUTO_APPROVE


def test_a_pattern_grants_only_the_shell_tool() -> None:
    event = _kas_builtin_flow("read_file", raw_input={"path": "/tmp/probe-note.txt"})
    assert _judge(event, [".*"]).tier != "spec-builtin-grant"


@pytest.mark.parametrize(
    "change",
    [{"mcp_identity_trusted": False}, {"kas_builtin_ids": False}],
    ids=["untrusted-identity", "not-kas"],
)
def test_only_a_host_identified_shell_call_is_granted(change: dict[str, Any]) -> None:
    event = _shell("git status")
    for name, value in change.items():
        setattr(event, name, value)
    assert _judge(event, ["git status"]).action != TOOL_AUTO_APPROVE


def test_a_shell_call_whose_command_was_not_recovered_is_never_granted() -> None:
    event = _kas_builtin_flow("run_command", title="Run Command", kind="execute", raw_input={})
    assert _judge(event, [".*"]).action != TOOL_AUTO_APPROVE


# ── the patterns and the projection ───────────────────────────────────────────


def test_only_the_subset_both_engines_read_alike_is_kept() -> None:
    assert allowed_command_pattern(r"\Agit status\z") == r"\Agit status\z"
    # Verbatim: a pattern's own spaces are part of what it grants.
    assert allowed_command_pattern("ls -la ") == "ls -la "
    for bad in (
        "",
        "x" * 1025,
        "(unclosed",
        7,
        None,
        # Rust reads a POSIX class; Python reads a set of the characters `[:alph`.
        "rm [[:alpha:]]+",
        # Nested repetition, which Python's engine backtracks on without bound.
        "echo (a+)+",
        "make (all|test)",
        r"ls \d+",
        "make ?",
        "a.b",
        "rm scratch\u00a0",
        "git\tstatus",
        "ls\x00",
    ):
        assert allowed_command_pattern(bad) is None, bad


@pytest.mark.parametrize(
    ("pattern", "command"),
    [
        ("rm [[:alpha:]]+", "rm a]"),
        ("make (a+)+", "make " + "a" * 32 + "!"),
    ],
)
def test_a_pattern_outside_the_subset_grants_nothing(pattern: str, command: str) -> None:
    """Python would read the first one wider than kiro-cli does, and stall on the second."""
    event = _shell(command)
    event.spec_granted_commands = (pattern,)
    verdict = HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))
    assert verdict.action != TOOL_AUTO_APPROVE


@pytest.mark.parametrize(
    "command",
    [
        "rm scratch\u00a0",
        "\u00a0rm scratch",
        " rm scratch",
        "rm scratch ",
        "rm\tscratch",
        "rm scratch\x07",
    ],
)
def test_a_command_is_matched_verbatim(command: str) -> None:
    """Stripping or folding the command would approve a path the grant never named."""
    verdict = _judge(_shell(command), ["rm scratch"])
    assert verdict.action != TOOL_AUTO_APPROVE or verdict.tier != "spec-builtin-grant"


@pytest.mark.parametrize("command", ["rm scratch\u00a0", "rm a\tb", "rm a\x07"])
def test_a_wildcard_never_covers_an_invisible_character(command: str) -> None:
    verdict = _judge(_shell(command), ["rm .*"])
    assert verdict.action != TOOL_AUTO_APPROVE or verdict.tier != "spec-builtin-grant"


def test_matching_is_greedy_wildcards_between_literals() -> None:
    from kiro_crew.platform.allowed_commands import matches, parse

    segs = parse("git .* --stat")
    assert segs == ("git ", " --stat")
    assert matches(segs, "git diff --stat")
    assert not matches(segs, "git --stat")
    assert matches(parse("a.*b.*c"), "axbyc")
    assert not matches(parse("a.*b.*c"), "acb")
    assert matches(parse("npm run .*"), "npm run ")
    assert not matches(parse("git status"), "git status ")


def test_crew_answered_commands_reads_both_shell_keys() -> None:
    settings = {
        "shell": {"allowedCommands": ["git status", "(bad", "git status"]},
        "execute_bash": {"allowedCommands": ["make .*"]},
        "read": {"allowedCommands": ["ignored"]},
    }
    assert crew_answered_commands(settings) == ["make .*", "git status"]
    assert crew_answered_commands(None) == []


def test_allowed_commands_are_named_only_when_a_pattern_cannot_be_used() -> None:
    assert tools_settings_to_permissions({"shell": {"allowedCommands": ["git status"]}}) == (
        None,
        [],
    )
    assert tools_settings_to_permissions({"shell": {"allowedCommands": ["(bad"]}})[1] == [
        "shell.allowedCommands"
    ]


def test_an_allow_list_over_the_bound_is_named() -> None:
    patterns = [f"make target{i}" for i in range(65)]
    settings = {"shell": {"allowedCommands": patterns}}
    assert len(crew_answered_commands(settings)) == 64
    assert tools_settings_to_permissions(settings)[1] == ["shell.allowedCommands"]


@pytest.mark.parametrize(
    "extra",
    [
        {"permissions": {"rules": [{"capability": "shell", "effect": "ask"}]}},
        {"toolsSettings": {"shell": {"allowedCommands": ["git status"], "deniedCommands": ["x"]}}},
    ],
    ids=["authored-ask", "denied-commands"],
)
def test_the_notice_names_allowed_commands_the_session_withholds(extra: dict[str, Any]) -> None:
    spec = _spec(**{"toolsSettings": {"shell": {"allowedCommands": ["git status"]}}, **extra})
    lost = kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True)
    assert "toolsSettings.shell.allowedCommands" in lost


def test_the_notice_names_allowed_commands_the_ceiling_withholds(monkeypatch) -> None:
    spec = _spec(toolsSettings={"shell": {"allowedCommands": ["git status"]}})
    assert kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True) == []
    monkeypatch.setattr(kas_agents, "may_grant_builtin_at_gate_now", lambda ref: False)
    assert kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True) == [
        "toolsSettings.shell.allowedCommands"
    ]


def _spec(**extra: Any) -> dict[str, Any]:
    return {"name": "a", "prompt": "p", "tools": ["*"], **extra}


def test_the_projection_records_the_patterns_and_relays_no_shell_allow() -> None:
    spec = _spec(toolsSettings={"shell": {"allowedCommands": ["git status", r"npm run .*\z"]}})
    out = kas_agents.to_client_custom_agent("a", spec, "p")
    assert out[CREW_ANSWERED_COMMANDS_KEY] == ["git status", r"npm run .*\z"]
    rules = out.get("permissions", {}).get("rules", [])
    assert not [r for r in rules if r["capability"] == "shell" and r["effect"] == "allow"]


@pytest.mark.parametrize(
    "extra",
    [
        {"permissions": {"rules": [{"capability": "shell", "effect": "ask"}]}},
        {"toolsSettings": {"shell": {"allowedCommands": ["git status"], "denyByDefault": True}}},
        {"toolsSettings": {"shell": {"allowedCommands": ["git status"], "deniedCommands": ["x"]}}},
    ],
    ids=["authored-ask", "other-shell-setting", "denied-commands"],
)
def test_an_authored_shell_restriction_withholds_the_patterns(extra: dict[str, Any]) -> None:
    """The spec's own author restricted the shell, so its commands keep prompting,
    as a shell grant in `allowedTools` does under the same restriction."""
    spec = _spec(**{"toolsSettings": {"shell": {"allowedCommands": ["git status"]}}, **extra})
    assert CREW_ANSWERED_COMMANDS_KEY not in kas_agents.to_client_custom_agent("a", spec, "p")


def test_the_grant_itself_is_not_read_as_a_restriction() -> None:
    from kiro_crew.acp.kas_permissions import without_allowed_commands

    assert without_allowed_commands({"shell": {"allowedCommands": ["x"]}, "read": {"a": 1}}) == {
        "read": {"a": 1}
    }
    assert without_allowed_commands(
        {"execute_bash": {"allowedCommands": ["x"], "denyByDefault": True}}
    ) == {"execute_bash": {"denyByDefault": True}}
    assert without_allowed_commands("bad") == "bad"


def test_a_ceiling_that_withholds_the_shell_records_no_patterns(monkeypatch) -> None:
    monkeypatch.setattr(kas_agents, "may_grant_builtin_at_gate_now", lambda ref: False)
    spec = _spec(toolsSettings={"shell": {"allowedCommands": ["git status"]}})
    assert CREW_ANSWERED_COMMANDS_KEY not in kas_agents.to_client_custom_agent("a", spec, "p")


def test_the_patterns_never_reach_the_wire() -> None:
    spec = _spec(toolsSettings={"shell": {"allowedCommands": ["git status"]}})
    entry = kas_agents.to_client_custom_agent("a", spec, "p")
    params: dict[str, Any] = {}
    attach_kas_custom_agents(params, [entry])
    (wire,) = params["_meta"]["kiro"]["customAgents"]
    assert CREW_ANSWERED_COMMANDS_KEY not in wire
    assert CREW_ANSWERED_COMMANDS_KEY in entry


def test_the_session_record_follows_the_active_agent_and_a_mode_switch() -> None:
    from kiro_crew.agent_files import kas_wire_agent_id

    batch = [
        {"id": "vibe", "prompt": "p", CREW_ANSWERED_COMMANDS_KEY: ["git status", 7]},
        {"id": "other", "prompt": "p"},
    ]
    assert kas_agents.projected_crew_answered_commands(batch, "vibe") == ("git status",)
    assert kas_agents.projected_crew_answered_commands(batch, "missing") == ()
    assert kas_agents.switched_crew_answered_commands(batch, kas_wire_agent_id("vibe")) == (
        "git status",
    )
    assert kas_agents.switched_crew_answered_commands(batch, "other") == ()


def test_the_handle_stamps_the_patterns_on_its_own_request_only() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    handle.kas_crew_answered_commands = ("git status",)
    own = handle._build_permission_event(_permission("own-sid"))
    child = handle._build_permission_event(_permission("child-sid"))
    assert own is not None and own.spec_granted_commands == ("git status",)
    assert child is not None and child.spec_granted_commands == ()
