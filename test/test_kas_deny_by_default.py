"""A spec's ``toolsSettings.shell.denyByDefault`` on KAS: an unlisted command is
refused, on every approval policy.

On kiro-cli a spec that sets ``denyByDefault`` refuses every shell command its
``allowedCommands`` do not match. KAS has no rule for it, so the projection records
the spec's usable patterns (``kas_permissions.spec_shell_allowlist``), the session
handle stamps them on its own agent's permission requests, and the gate's
``spec-shell-allowlist`` row refuses a shell call none of them matches. A deny, so a
scheduled job running with ``approval_mode: auto`` (``ToolApprovalPolicy.AUTO_APPROVE``,
which consults the gate through ``permission_floor.refusal_for``) refuses it too,
instead of running it unprompted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_kas_spec_builtin_grant import _handle, _permission
from test_tool_call_classification_kas import _kas_builtin_flow

from kiro_crew import permission_floor
from kiro_crew.acp import kas_agents
from kiro_crew.acp._dispatch import attach_kas_custom_agents
from kiro_crew.acp.kas_permissions import (
    CREW_ANSWERED_COMMANDS_KEY,
    CREW_SHELL_ALLOWLIST_KEY,
    spec_shell_allowlist,
    tools_settings_to_permissions,
)
from kiro_crew.acp.types import AcpEvent
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.hooks import (
    TOOL_ALLOW,
    TOOL_DENY,
    HookManager,
    HooksConfig,
    hook_gate_kwargs,
)
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp

#: The reporter's spec, its pattern written in the subset Crew reads like kiro-cli
#: (literals, escaped punctuation, ``.*``): a deny-by-default shell, one shape allowed.
_PATTERN = r"^touch /tmp/probe-allowed-.*\.txt$"
_SETTINGS: dict[str, Any] = {"execute_bash": {"denyByDefault": True, "allowedCommands": [_PATTERN]}}


@pytest.fixture(autouse=True)
def _no_profiles(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def _spec(**extra: Any) -> dict[str, Any]:
    return {"name": "probe", "prompt": "p", "tools": ["execute_bash"], **extra}


def _shell(command: str) -> AcpEvent:
    return _kas_builtin_flow(
        "run_command", title="Run Command", kind="execute", raw_input={"command": command}
    )


def _own_request(command: str | None, settings: dict[str, Any] = _SETTINGS) -> AcpEvent:
    """The permission event the session handle builds for its own agent's shell call,
    with the record the projection of a spec carrying *settings* registered."""
    entry = kas_agents.to_client_custom_agent("probe", _spec(toolsSettings=settings), "p")
    handle = _handle(ACP_BACKEND_KAS)
    handle.kas_spec_shell_allowlist = kas_agents.projected_shell_allowlist([entry], "probe")
    event = (
        _shell(command)
        if command is not None
        else _kas_builtin_flow("run_command", title="Run Command", kind="execute", raw_input={})
    )
    stamped = handle._build_permission_event(_permission("own-sid"))
    assert stamped is not None
    event.spec_shell_allowlist = stamped.spec_shell_allowlist
    return event


def _judge(event: AcpEvent):
    return HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))


# ── the reported case: an auto-mode job ───────────────────────────────────────


def test_an_auto_mode_job_refuses_a_command_the_spec_never_allowed() -> None:
    """``AUTO_APPROVE`` approves whatever the gate does not deny. The unlisted
    ``mkfifo`` must be denied there, not run."""
    reason = permission_floor.refusal_for(
        _own_request("mkfifo /tmp/probe-notlisted-fifo"),
        session_key="cron:probe",
        security_only=False,
    )
    assert reason is not None and "denyByDefault" in reason


def test_an_auto_mode_job_still_runs_a_listed_command() -> None:
    reason = permission_floor.refusal_for(
        _own_request("touch /tmp/probe-allowed-a.txt"),
        session_key="cron:probe",
        security_only=False,
    )
    assert reason is None


def test_an_interactive_session_refuses_it_at_once_instead_of_prompting() -> None:
    verdict = _judge(_own_request("mkfifo /tmp/probe-notlisted-fifo"))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-allowlist")
    # A listed command keeps its prompt: answering it is the allowedCommands grant's
    # job, which the author's own denyByDefault withholds.
    listed = _judge(_own_request("touch /tmp/probe-allowed-a.txt"))
    assert (listed.action, listed.tier) == (TOOL_ALLOW, "")


# ── what counts as listed ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "touch /tmp/probe-allowed-a.txt; mkfifo /tmp/x",
        "touch /tmp/probe-allowed-a.txt && mkfifo /tmp/x",
        "touch /tmp/probe-allowed-a.txt | sh",
        "touch /tmp/probe-allowed-$(whoami).txt",
        "touch /tmp/probe-allowed-a.txt\nmkfifo /tmp/x",
        "touch /tmp/probe-allowed-a.txt.bak",
        " touch /tmp/probe-allowed-a.txt",
    ],
)
def test_a_command_the_pattern_does_not_match_whole_and_plain_is_refused(command: str) -> None:
    assert _judge(_own_request(command)).action == TOOL_DENY


def test_a_spec_without_allowed_commands_refuses_every_shell_call() -> None:
    settings = {"shell": {"denyByDefault": True}}
    assert spec_shell_allowlist(settings) == []
    assert _judge(_own_request("ls", settings)).action == TOOL_DENY


@pytest.mark.parametrize(
    "pattern", [r"^touch /tmp/probe-allowed-[a-z]+\.txt$", "ls [[:alpha:]]+", "make (a+)+"]
)
def test_an_unusable_pattern_allows_nothing(pattern: str) -> None:
    """Outside the subset Crew reads like kiro-cli (the reporter's own ``[a-z]+``
    included), so a command only it would match is refused, never let through; the
    session-start notice names the entry so the author can rewrite it."""
    settings = {"shell": {"denyByDefault": True, "allowedCommands": [pattern]}}
    assert _judge(_own_request("touch /tmp/probe-allowed-a.txt", settings)).action == TOOL_DENY
    assert tools_settings_to_permissions(settings)[1] == ["shell.allowedCommands"]


def test_an_unrecovered_command_is_refused() -> None:
    event = _own_request(None)
    assert event.shell_command is None and event.spec_shell_allowlist == (_PATTERN,)
    assert _judge(event).action == TOOL_DENY


# ── what it leaves alone ──────────────────────────────────────────────────────


def test_a_spec_without_deny_by_default_is_judged_as_before() -> None:
    settings = {"shell": {"allowedCommands": ["git status"]}}
    assert spec_shell_allowlist(settings) is None
    assert spec_shell_allowlist({"shell": {"denyByDefault": False}}) is None
    event = _own_request("mkfifo /tmp/x", settings)
    assert event.spec_shell_allowlist is None
    assert _judge(event).action == TOOL_ALLOW
    assert permission_floor.refusal_for(event, security_only=False) is None


def test_a_non_shell_call_is_not_refused() -> None:
    read = _kas_builtin_flow(
        "read_file", title="Read", kind="read", raw_input={"path": "/tmp/notes.txt"}
    )
    read.spec_shell_allowlist = ()
    assert _judge(read).action != TOOL_DENY


def test_an_mcp_tool_is_not_refused() -> None:
    event = AcpEvent(
        kind="permission_request",
        title="List the fleet",
        mcp_server_name="ops",
        tool_name="list",
        mcp_identity_trusted=True,
        spec_shell_allowlist=(),
    )
    assert _judge(event).action != TOOL_DENY


def test_the_handle_stamps_the_allowlist_on_its_own_request_only() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    handle.kas_spec_shell_allowlist = ("git status",)
    own = handle._build_permission_event(_permission("own-sid"))
    child = handle._build_permission_event(_permission("child-sid"))
    assert own is not None and own.spec_shell_allowlist == ("git status",)
    assert child is not None and child.spec_shell_allowlist is None


# ── the record ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [True, "yes", 1, {}])
def test_any_value_but_false_turns_it_on(value: Any) -> None:
    assert spec_shell_allowlist({"shell": {"denyByDefault": value}}) == []


def test_the_projection_records_it_whatever_the_ceiling_says(monkeypatch) -> None:
    monkeypatch.setattr(kas_agents, "may_grant_builtin_at_gate_now", lambda ref: False)
    out = kas_agents.to_client_custom_agent("probe", _spec(toolsSettings=_SETTINGS), "p")
    assert out[CREW_SHELL_ALLOWLIST_KEY] == [_PATTERN]
    assert CREW_ANSWERED_COMMANDS_KEY not in out


def test_the_notice_no_longer_names_it() -> None:
    lost = kas_agents.spec_keys_without_carrier(
        _spec(toolsSettings=_SETTINGS), translates_tools_settings=True
    )
    assert "toolsSettings.execute_bash.denyByDefault" not in lost


def test_the_record_never_reaches_the_wire() -> None:
    entry = kas_agents.to_client_custom_agent("probe", _spec(toolsSettings=_SETTINGS), "p")
    params: dict[str, Any] = {}
    attach_kas_custom_agents(params, [entry])
    (wire,) = params["_meta"]["kiro"]["customAgents"]
    assert CREW_SHELL_ALLOWLIST_KEY not in wire
    assert CREW_SHELL_ALLOWLIST_KEY in entry


def test_the_session_record_follows_the_active_agent_and_a_mode_switch() -> None:
    from kiro_crew.agent_files import kas_wire_agent_id

    batch = [
        {"id": "probe", "prompt": "p", CREW_SHELL_ALLOWLIST_KEY: ["git status", 7]},
        {"id": "broken", "prompt": "p", CREW_SHELL_ALLOWLIST_KEY: "git status"},
        {"id": "other", "prompt": "p"},
    ]
    assert kas_agents.projected_shell_allowlist(batch, "probe") == ("git status",)
    # A malformed record restricts everything; it never lifts the restriction.
    assert kas_agents.projected_shell_allowlist(batch, "broken") == ()
    assert kas_agents.projected_shell_allowlist(batch, "other") is None
    assert kas_agents.projected_shell_allowlist(batch, "missing") is None
    assert kas_agents.switched_shell_allowlist(batch, kas_wire_agent_id("probe")) == ("git status",)
    assert kas_agents.switched_shell_allowlist(batch, "other") is None
