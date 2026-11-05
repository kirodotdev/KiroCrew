"""A spec's ``toolsSettings.shell.denyByDefault`` on KAS: an unlisted command is
refused, on every approval policy.

On kiro-cli a spec that sets ``denyByDefault`` refuses every shell command its
``allowedCommands`` do not match, unless its ``allowedTools`` names the shell. KAS has no rule for it, so the projection records
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
    allowed_tools_grant_shell,
    shell_denies_carried,
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


def _own_request(
    command: str | None, settings: dict[str, Any] = _SETTINGS, **spec_extra: Any
) -> AcpEvent:
    """The permission event the session handle builds for its own agent's shell call,
    with the record the projection of a spec carrying *settings* registered."""
    entry = kas_agents.to_client_custom_agent(
        "probe", _spec(toolsSettings=settings, **spec_extra), "p"
    )
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
    "pattern",
    ["touch [[:alpha:]]+", r"touch /tmp/probe-allowed-\w+\.txt", "touch .{1,64}"],
)
def test_an_unusable_pattern_allows_nothing(pattern: str) -> None:
    """Outside the syntax Crew reads like kiro-cli (a POSIX class, an alphanumeric
    escape, a counted repeat), so a command only it would match is refused, never
    let through; the session-start notice names the entry so the author can
    rewrite it."""
    settings = {"shell": {"denyByDefault": True, "allowedCommands": [pattern]}}
    assert _judge(_own_request("touch /tmp/probe-allowed-a.txt", settings)).action == TOOL_DENY
    assert tools_settings_to_permissions(settings)[1] == ["shell.allowedCommands"]


#: A shape real specs write: a path class, an alternation group, an optional
#: argument tail whose class itself excludes chaining characters.
_SCRIPTS = r"^python3 /[A-Za-z0-9_./-]*/scripts/(compute_signature|staleness)\.py( [^;|&`$]+)?$"


@pytest.mark.parametrize(
    ("pattern", "command"),
    [
        (r"^touch /tmp/probe-allowed-[a-z]+\.txt$", "touch /tmp/probe-allowed-a.txt"),
        (_SCRIPTS, "python3 /opt/probe/scripts/staleness.py"),
        (_SCRIPTS, "python3 /opt/probe/scripts/compute_signature.py --days 7"),
        ("^git (commit|status)( .*)?$", "git status"),
        ("^git (commit|status)( .*)?$", "git commit -m x"),
        ("^git commit -m .*$", 'git commit -m "docs(skills): weekly notes"'),
        ("^git commit -m .*$", "git commit -m 'fix(gate): {quoted}'"),
    ],
)
def test_a_command_a_class_or_group_pattern_matches_is_not_refused(
    pattern: str, command: str
) -> None:
    """kiro-cli allows these under the same spec; so must the gate."""
    settings = {"shell": {"denyByDefault": True, "allowedCommands": [pattern]}}
    assert _judge(_own_request(command, settings)).tier != "spec-shell-allowlist"
    assert tools_settings_to_permissions(settings)[1] == []


@pytest.mark.parametrize(
    ("pattern", "command"),
    [
        (_SCRIPTS, "python3 /opt/probe/scripts/other.py"),
        (_SCRIPTS, "python3 /opt/probe/scripts/staleness.pyc"),
        (_SCRIPTS, "python3 /opt/probe/scripts/staleness.py; mkfifo /tmp/x"),
        ("^git (commit|status)( .*)?$", "git pull"),
        ("^git (commit|status)( .*)?$", "git statusx"),
        # Alternation is whole-command: never "starts with git status OR ends with ls".
        ("git status|ls", "git status --porcelain"),
        ("git status|ls", "mkfifo /tmp/ls"),
        # A grouping character outside a quoted argument is never granted.
        ("^git commit -m .*$", "git commit -m (x)"),
        ("^git commit -m .*$", "git commit -m {a,b}"),
        ("^git commit -m .*$", 'git commit -m "docs(x)'),
        ("^git commit -m .*$", 'git commit -m \\"(x)\\"'),
        # Quoting never shelters a chaining or substitution character.
        ("^git commit -m .*$", 'git commit -m "x(y)"; mkfifo /tmp/x'),
        ("^git commit -m .*$", 'git commit -m "$(whoami)"'),
        ("^git commit -m .*$", "git commit -m '(x)' && mkfifo /tmp/x"),
    ],
)
def test_a_command_a_class_or_group_pattern_does_not_match_is_refused(
    pattern: str, command: str
) -> None:
    settings = {"shell": {"denyByDefault": True, "allowedCommands": [pattern]}}
    verdict = _judge(_own_request(command, settings))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-allowlist")


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


# ── an agent whose allowedTools names the shell ───────────────────────────────
#
# kiro-cli's ``ExecuteCommand::eval_perm`` returns Allow for an agent whose
# ``allowedTools`` names the shell before it ever reads ``denyByDefault``; only a
# ``deniedCommands`` match still denies. So such an agent's commands are not
# refused by this row: they take the gate's usual path and run or prompt.

#: A user's own agent: shell granted in ``allowedTools``, and a short
#: ``denyByDefault`` list that does not name ``aws``.
_AWS_SETTINGS: dict[str, Any] = {
    "execute_bash": {"denyByDefault": True, "allowedCommands": ["git status.*"]}
}


@pytest.mark.parametrize(
    "allowed_tools",
    [
        ["execute_bash"],
        ["shell"],
        ["*"],
        ["@builtin"],
        ["@builtin/"],
        ["@builtin/*"],
        ["@builtin/execute_bash"],
        ["execute_*"],
        ["fs_read", " execute_bash "],
    ],
)
def test_an_agent_whose_allowed_tools_names_the_shell_is_not_refused(
    allowed_tools: list[str],
) -> None:
    assert allowed_tools_grant_shell(allowed_tools)
    event = _own_request("aws sts get-caller-identity", _AWS_SETTINGS, allowedTools=allowed_tools)
    assert event.spec_shell_allowlist is None
    verdict = _judge(event)
    assert verdict.action != TOOL_DENY and verdict.tier != "spec-shell-allowlist"
    reason = permission_floor.refusal_for(event, session_key="cron:probe", security_only=False)
    assert reason is None or "denyByDefault" not in reason


@pytest.mark.parametrize(
    "allowed_tools",
    [
        None,
        [],
        ["fs_read"],
        ["@builder-mcp"],
        ["@builder-mcp/*"],
        ["@*"],
        ["execute_bas[h]"],
        ["{execute_bash}"],
        ["!execute_bash"],
        [7],
        "execute_bash",
    ],
)
def test_an_agent_whose_allowed_tools_does_not_name_the_shell_is_still_refused(
    allowed_tools: Any,
) -> None:
    """No shell entry (an MCP server, a read tool, an entry with glob syntax only
    one matcher reads, a list that is not one): ``denyByDefault`` still holds."""
    extra = {} if allowed_tools is None else {"allowedTools": allowed_tools}
    assert not allowed_tools_grant_shell(allowed_tools)
    event = _own_request("aws sts get-caller-identity", _AWS_SETTINGS, **extra)
    verdict = _judge(event)
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-allowlist")


def test_an_agent_without_tools_settings_is_judged_as_before() -> None:
    assert spec_shell_allowlist(None, ["execute_bash"]) is None
    event = _own_request("aws sts get-caller-identity", {}, allowedTools=["execute_bash"])
    assert event.spec_shell_allowlist is None


def test_an_agent_with_allowed_commands_and_no_deny_by_default_is_judged_as_before() -> None:
    settings = {"execute_bash": {"allowedCommands": ["aws sts .*"]}}
    assert spec_shell_allowlist(settings, ["execute_bash"]) is None
    assert spec_shell_allowlist(settings) is None
    assert _judge(_own_request("aws s3 ls", settings)).action != TOOL_DENY


def test_a_denied_command_is_still_refused_when_allowed_tools_names_the_shell() -> None:
    settings = {
        "execute_bash": {"denyByDefault": True, "deniedCommands": ["rm .*"]},
    }
    spec = _spec(toolsSettings=settings, allowedTools=["execute_bash"])
    out = kas_agents.to_client_custom_agent("probe", spec, "p")
    assert CREW_SHELL_ALLOWLIST_KEY not in out
    # The deny travels to the engine as a rule, whatever the allowlist record says.
    assert {"capability": "shell", "match": ["rm *"], "effect": "deny"} in out["permissions"][
        "rules"
    ]


def test_the_projection_drops_the_record_when_allowed_tools_names_the_shell() -> None:
    spec = _spec(toolsSettings=_AWS_SETTINGS, allowedTools=["execute_bash"])
    assert CREW_SHELL_ALLOWLIST_KEY not in kas_agents.to_client_custom_agent("probe", spec, "p")
    # A duplicated agent (same spec, new id) reads the same way.
    assert CREW_SHELL_ALLOWLIST_KEY not in kas_agents.to_client_custom_agent(
        "probe-copy", spec, "p"
    )


@pytest.mark.parametrize(
    "denied",
    [
        [r"^git (commit|push).*$"],
        ["rm .*", r"^git (commit|push).*$"],
        ["cat .* > .*"],
        "rm .*",
        [7],
    ],
)
def test_a_denied_command_that_does_not_travel_keeps_the_allowlist(denied: Any) -> None:
    """A ``deniedCommands`` entry with no KAS deny rule (grouped regex, a redirect,
    a malformed value) lives only in the spec. Lifting ``denyByDefault`` would let
    the command it names run, so the allowlist stays even though ``allowedTools``
    names the shell."""
    settings = {
        "execute_bash": {
            "denyByDefault": True,
            "allowedCommands": ["git status.*"],
            "deniedCommands": denied,
        }
    }
    event = _own_request("git commit -m checkpoint", settings, allowedTools=["execute_bash"])
    assert event.spec_shell_allowlist == ("git status.*",)
    verdict = _judge(event)
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-allowlist")
    reason = permission_floor.refusal_for(event, session_key="cron:probe", security_only=False)
    assert reason is not None and "denyByDefault" in reason


def test_a_translated_deny_the_policy_lacks_is_not_carried() -> None:
    settings = {"execute_bash": {"deniedCommands": ["rm .*"]}}
    assert shell_denies_carried(settings, None) is False
    assert shell_denies_carried(settings, {"rules": []}) is False
    deny = {"capability": "shell", "match": ["rm *"], "effect": "deny"}
    assert shell_denies_carried(settings, {"rules": [deny]}) is True
    assert shell_denies_carried({"execute_bash": {"denyByDefault": True}}, None) is True
    assert shell_denies_carried("not-a-dict", None) is False


@pytest.mark.parametrize("value", [False, 0, "", {}])
def test_a_falsy_malformed_denied_commands_is_not_carried(value: Any) -> None:
    assert shell_denies_carried({"shell": {"deniedCommands": value}}, None) is False


def test_a_deny_with_an_exclude_does_not_count_as_carried() -> None:
    """A rule that carves commands out (``exclude``) does not prove the authored
    deny holds whole, so the allowlist stays."""
    settings = {"execute_bash": {"deniedCommands": ["git .*"]}}
    narrowed = {
        "capability": "shell",
        "match": ["git *"],
        "effect": "deny",
        "exclude": ["git commit*"],
    }
    assert shell_denies_carried(settings, {"rules": [narrowed]}) is False
    whole = {"capability": "shell", "match": ["git *"], "effect": "deny"}
    assert shell_denies_carried(settings, {"rules": [narrowed, whole]}) is True
