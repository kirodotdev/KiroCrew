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

import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_kas_spec_builtin_grant import _handle, _permission
from test_tool_call_classification_kas import _kas_builtin_flow

import kiro_crew
from kiro_crew import permission_floor
from kiro_crew.acp import kas_agents, kas_engine_subagents
from kiro_crew.acp._dispatch import attach_kas_custom_agents
from kiro_crew.acp.kas_permissions import (
    _MAX_ALLOWED_COMMANDS,
    CREW_ANSWERED_COMMANDS_KEY,
    CREW_SHELL_ALLOWLIST_KEY,
    CREW_SHELL_READONLY_KEY,
    allowed_tools_grant_shell,
    shell_denies_by_default,
    shell_denies_carried,
    spec_shell_allowlist,
    tools_settings_to_permissions,
)
from kiro_crew.acp.spec_tools_allowlists import spec_shell_patterns
from kiro_crew.acp.types import AcpEvent
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.dashboard.chat_turn.tool_approval import _spec_keys_notice
from kiro_crew.hooks import (
    TOOL_ALLOW,
    TOOL_DENY,
    HookManager,
    HooksConfig,
    hook_gate_kwargs,
)
from kiro_crew.platform import allowed_commands
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.tools_settings_aliases import canonical_tools_settings

#: The ``kas_subagents`` preview stored off and stored on. A test of the shell grant
#: names the one it runs under; a preview never set (``None``) has its own tests.
_OFF, _ON = False, True


def _set_switch(monkeypatch: pytest.MonkeyPatch, stored: bool | None) -> None:
    """Hand the projection a config that stores *stored* as the ``kas_subagents``
    preview (``None``: never set), for the preview's own reader to read."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.dashboard.feature_previews = (
        {} if stored is None else {kas_engine_subagents.KAS_SUBAGENTS_PREVIEW: stored}
    )
    monkeypatch.setattr(live, "snapshot", lambda: cfg)


@pytest.fixture
def switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_switch(monkeypatch, _OFF)


@pytest.fixture
def switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_switch(monkeypatch, _ON)


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


def _auto_refusal(command: str, settings: dict[str, Any]) -> str | None:
    """The refusal an auto-mode job records for *command*, or ``None`` when it runs."""
    return permission_floor.refusal_for(
        _own_request(command, settings), session_key="cron:probe", security_only=False
    )


#: Entries in Rust syntax kiro-cli accepts (a POSIX class, ``\d``, ``\w``, ``\b``, a
#: counted repeat, a lazy repeat, a flag), each with a command it matches in Rust
#: and one no entry here matches.
_RUST_SYNTAX = [
    (r"^ls [[:alnum:]]+$", "ls abc1", "ls a-b"),
    (r"^sleep \d+$", "sleep 30", "sleep 3x"),
    (r"^mkdir \w+$", "mkdir build_1", "mkdir a.b"),
    (r"^echo \bhi\b$", "echo hi", "echo hix"),
    (r"^echo a{2}$", "echo aa", "echo aaa"),
    (r"^cat .+?\.txt$", "cat notes.txt", "cat notes.md"),
    (r"(?i)^whoami$", "WhoAmI", "whoami x"),
    (r"^ls( [^;|&\x0d\x0a]+)?$", "ls foo", "cat foo"),
    (r"touch /tmp/probe-allowed-\w+\.txt", "touch /tmp/probe-allowed-a.txt", "touch /tmp/x"),
]


@pytest.mark.parametrize(("pattern", "listed", "unlisted"), _RUST_SYNTAX)
def test_an_entry_in_rust_syntax_runs_its_command_and_refuses_the_rest(
    pattern: str, listed: str, unlisted: str
) -> None:
    """kiro-cli runs *listed* under this spec and refuses *unlisted*; so must an
    auto-mode job here, and no notice says the entry is dropped."""
    settings = {"execute_bash": {"denyByDefault": True, "allowedCommands": [pattern]}}
    assert _auto_refusal(listed, settings) is None
    refused = _auto_refusal(unlisted, settings)
    assert refused is not None and "denyByDefault" in refused
    assert pattern not in refused
    assert tools_settings_to_permissions(settings)[1] == []


@pytest.mark.parametrize("pattern", [r"touch \p{L}+", "(?x)touch a", r"touch \x{41}"])
def test_an_unreadable_entry_allows_nothing_and_each_surface_names_it(pattern: str) -> None:
    """Outside the syntax Kiro Crew reads (``\\p``, the ``x`` flag, ``\\x{41}``), so a
    command only it would match is refused, never let through, while the other
    entries still apply. The refusal an auto-mode job records and the session-start
    notice name the entry and why, so the author can rewrite it."""
    settings = {"shell": {"denyByDefault": True, "allowedCommands": ["git status", pattern]}}
    refused = _auto_refusal("touch A", settings)
    assert refused is not None and "denyByDefault" in refused
    reason = allowed_commands.refusal_reason(pattern)
    assert reason and f"{pattern} ({reason})" in refused
    assert _auto_refusal("git status", settings) is None
    assert tools_settings_to_permissions(settings)[1] == ["shell.allowedCommands"]
    lost = kas_agents.spec_keys_without_carrier(
        _spec(toolsSettings=settings), translates_tools_settings=True
    )
    assert lost == [f"toolsSettings.shell.allowedCommands: {pattern} ({reason})"]
    notice = _spec_keys_notice("probe", lost)
    assert pattern in notice and "allows nothing" in notice
    assert "no effect" not in notice and "other entries still apply" in notice


def test_a_list_withheld_whole_is_never_said_to_keep_its_other_entries() -> None:
    """A shell ``ask`` withholds every entry, so the notice names the list whole
    beside the unreadable entry and never says ``git status`` still applies."""
    ask = {"rules": [{"capability": "shell", "effect": "ask"}]}
    settings = {"shell": {"allowedCommands": ["git status", "(?x)a"]}}
    lost = kas_agents.spec_keys_without_carrier(
        _spec(toolsSettings=settings, permissions=ask), translates_tools_settings=True
    )
    assert "toolsSettings.shell.allowedCommands" in lost
    notice = _spec_keys_notice("probe", lost)
    assert "(?x)a (the x flag)" in notice and "no effect" in notice
    assert "other entries still apply" not in notice


def test_an_allowlist_the_gate_enforces_is_never_said_to_have_no_effect() -> None:
    """An ``ask`` withholds what the allowlists grant, but under
    ``denyByDefault`` / ``fallbackAction: "deny"`` Crew's gate still refuses what
    they leave out, so the notice must not say they have no effect."""
    ask = {"rules": [{"capability": c, "effect": "ask"} for c in ("shell", "fs_write")]}
    settings = {
        "shell": {"denyByDefault": True, "allowedCommands": ["mytool [A-Za-z0-9_ ./=-]*"]},
        "write": {"fallbackAction": "deny", "allowedPaths": ["out/**"]},
    }
    lost = kas_agents.spec_keys_without_carrier(
        _spec(toolsSettings=settings, permissions=ask, toolAliases={"x": "y"}),
        translates_tools_settings=True,
    )
    notice = _spec_keys_notice("probe", lost)
    allowlists = "toolsSettings.shell.allowedCommands and toolsSettings.write.allowedPaths"
    assert f"sets {allowlists}, which this backend does not receive. Kiro Crew" in notice
    assert "refuses what they do not cover" in notice
    assert notice.count("toolsSettings.shell.allowedCommands") == 1
    # A key nothing reads keeps its wording.
    assert "sets toolAliases, which this backend does not receive, so they have no effect" in notice


def test_an_over_bound_deny_by_default_list_is_never_said_to_have_no_effect() -> None:
    """A list cut by the bound has its cut entries named; the gate still refuses
    what they alone match, so the notice never says the list has no effect."""
    entries = [f"c{i}" for i in range(_MAX_ALLOWED_COMMANDS + 1)]
    settings = {"shell": {"denyByDefault": True, "allowedCommands": entries}}
    notice = _spec_keys_notice(
        "probe",
        kas_agents.spec_keys_without_carrier(
            _spec(toolsSettings=settings), translates_tools_settings=True
        ),
    )
    assert "toolsSettings.shell.allowedCommands" in notice
    assert "no effect" not in notice and "refused when denyByDefault is set" in notice


@pytest.mark.usefixtures("switch_off")
def test_a_lifted_deny_by_default_keeps_the_no_effect_wording() -> None:
    """An ``allowedTools`` that names the shell, with no denies, lifts
    ``denyByDefault`` at the gate, so nothing is marked as enforced there."""
    ask = {"rules": [{"capability": "shell", "effect": "ask"}]}
    settings = {"shell": {"denyByDefault": True, "allowedCommands": ["git status"]}}
    lost = kas_agents.spec_keys_without_carrier(
        _spec(toolsSettings=settings, permissions=ask, allowedTools=["execute_bash"]),
        translates_tools_settings=True,
    )
    notice = _spec_keys_notice("probe", lost)
    assert "toolsSettings.shell.allowedCommands" in notice
    assert "refuses what they do not cover" not in notice


@pytest.mark.usefixtures("switch_on")
def test_an_allowed_tools_shell_grant_keeps_the_gate_enforced_wording() -> None:
    """With the ``kas_subagents`` preview on an ``allowedTools`` that names the
    shell does not lift ``denyByDefault``, so the notice says the gate enforces the
    list exactly as it does without the grant."""
    ask = {"rules": [{"capability": "shell", "effect": "ask"}]}
    settings = {"shell": {"denyByDefault": True, "allowedCommands": ["git status"]}}
    notices = [
        _spec_keys_notice(
            "probe",
            kas_agents.spec_keys_without_carrier(
                _spec(toolsSettings=settings, permissions=ask, **extra),
                translates_tools_settings=True,
            ),
        )
        for extra in ({"allowedTools": ["execute_bash"]}, {})
    ]
    assert notices[0] == notices[1]
    assert "toolsSettings.shell.allowedCommands" in notices[0]
    assert "refuses what they do not cover" in notices[0] and "no effect" not in notices[0]


def test_a_repeat_count_rust_cannot_compile_allows_nothing_even_repeated_zero_times() -> None:
    """Rust reads a count as a ``u32`` and refuses ``a{4294967296}``, so kiro-cli
    refuses ``touch marker`` under this spec. An outer ``{0}`` leaves the group out
    of Kiro Crew's own program, so the count is refused as it is read; the same
    shape at the largest ``u32`` still compiles in Rust and still grants here."""
    overflow = "touch marker(a{4294967296}){0}"
    settings = {
        "execute_bash": {
            "denyByDefault": True,
            "allowedCommands": [overflow, "touch other(a{4294967295}){0}"],
        }
    }
    refused = _auto_refusal("touch marker", settings)
    assert refused is not None and "denyByDefault" in refused
    reason = allowed_commands.refusal_reason(overflow)
    assert reason == "a counted repeat past 4294967295"
    assert f"{overflow} ({reason})" in refused
    verdict = _judge(_own_request("touch marker", settings))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-allowlist")
    assert _auto_refusal("touch other", settings) is None


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
        (r"touch /tmp/probe-allowed-\w+\.txt", "touch /tmp/probe-allowed-a.txt"),
        ("touch .{1,64}", "touch /tmp/probe-allowed-a.txt"),
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
#
# That holds while the ``kas_subagents`` preview is off or never set (the
# ``switch_off`` tests). With it on (the ``switch_on`` tests) the allowlist holds beside
# the grant, as kiro-cli's agent engine keeps it: its upgrade of such a file writes
# ``deny shell ["*"] exclude [<allowedCommands>]`` beside ``allow shell``, and the
# deny wins.

#: A user's own agent: shell granted in ``allowedTools``, and a short
#: ``denyByDefault`` list that does not name ``aws``.
_AWS_SETTINGS: dict[str, Any] = {
    "execute_bash": {"denyByDefault": True, "allowedCommands": ["git status.*"]}
}

#: Every ``allowedTools`` spelling that names the shell.
_SHELL_GRANTS = [
    ["execute_bash"],
    ["shell"],
    ["@builtin"],
    ["@builtin/"],
    ["@builtin/*"],
    ["@builtin/execute_bash"],
    ["@builtin/shell"],
    ["fs_read", " execute_bash "],
    ["executeCmd"],
    ["execute_cmd"],
    ["@builtin/execute_cmd"],
]


@pytest.mark.usefixtures("switch_off")
@pytest.mark.parametrize(
    "allowed_tools",
    [
        ["execute_bash"],
        ["shell"],
        ["@builtin"],
        ["@builtin/"],
        ["@builtin/*"],
        ["@builtin/execute_bash"],
        ["@builtin/shell"],
        ["fs_read", " execute_bash "],
        ["executeCmd"],
        ["execute_cmd"],
        ["@builtin/execute_cmd"],
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


@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize("allowed_tools", _SHELL_GRANTS)
def test_an_agent_whose_allowed_tools_names_the_shell_is_still_refused_with_the_preview_on(
    allowed_tools: list[str],
) -> None:
    assert allowed_tools_grant_shell(allowed_tools)
    event = _own_request("aws sts get-caller-identity", _AWS_SETTINGS, allowedTools=allowed_tools)
    assert event.spec_shell_allowlist == ("git status.*",)
    verdict = _judge(event)
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-allowlist")
    reason = permission_floor.refusal_for(event, session_key="cron:probe", security_only=False)
    assert reason is not None and "denyByDefault" in reason
    # A listed command is judged exactly as without the grant.
    listed = _own_request("git status --short", _AWS_SETTINGS, allowedTools=allowed_tools)
    plain = _own_request("git status --short", _AWS_SETTINGS)
    assert _judge(listed) == _judge(plain)
    assert _judge(listed).tier != "spec-shell-allowlist"


#: The reported file: an unsplit deny beside the allowlist, shell in ``allowedTools``.
_REPORTED: dict[str, Any] = {
    "execute_bash": {
        "denyByDefault": True,
        "allowedCommands": ["git status.*"],
        "deniedCommands": ["touch .*"],
    }
}


@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize("key", ["execute_bash", "shell"])
@pytest.mark.parametrize("allowed_tools", _SHELL_GRANTS)
def test_an_unsplit_deny_beside_a_shell_grant_keeps_the_allowlist_with_the_preview_on(
    allowed_tools: list[str], key: str
) -> None:
    """kiro-cli refuses ``cp`` under this file with ``deny shell matching "*"``."""
    settings = {key: _REPORTED["execute_bash"]}
    out = kas_agents.to_client_custom_agent(
        "probe", _spec(toolsSettings=settings, allowedTools=allowed_tools), "p"
    )
    assert out[CREW_SHELL_ALLOWLIST_KEY] == ["git status.*"]
    # The named deny still travels to the engine as its own rule.
    assert {"capability": "shell", "match": ["touch *"], "effect": "deny"} in out["permissions"][
        "rules"
    ]
    event = _own_request("cp seed copy", settings, allowedTools=allowed_tools)
    verdict = _judge(event)
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-allowlist")
    reason = permission_floor.refusal_for(event, session_key="cron:probe", security_only=False)
    assert reason is not None and "denyByDefault" in reason
    listed = _own_request("git status --short", settings, allowedTools=allowed_tools)
    assert _judge(listed).tier != "spec-shell-allowlist"
    floor = permission_floor.refusal_for(listed, session_key="cron:probe", security_only=False)
    assert floor is None


@pytest.mark.usefixtures("switch_off")
@pytest.mark.parametrize("key", ["execute_bash", "shell"])
@pytest.mark.parametrize("allowed_tools", _SHELL_GRANTS)
def test_an_unsplit_deny_beside_a_shell_grant_lifts_the_allowlist_with_the_preview_off(
    allowed_tools: list[str], key: str
) -> None:
    """The reported file while the preview is off: no allowlist is
    recorded, ``cp`` takes the gate's usual path, and the named deny still travels."""
    settings = {key: _REPORTED["execute_bash"]}
    out = kas_agents.to_client_custom_agent(
        "probe", _spec(toolsSettings=settings, allowedTools=allowed_tools), "p"
    )
    assert CREW_SHELL_ALLOWLIST_KEY not in out
    assert {"capability": "shell", "match": ["touch *"], "effect": "deny"} in out["permissions"][
        "rules"
    ]
    event = _own_request("cp seed copy", settings, allowedTools=allowed_tools)
    assert event.spec_shell_allowlist is None
    assert _judge(event).tier != "spec-shell-allowlist"


#: ``denyByDefault`` files and files without it, each read with and without the grant.
_GRANT_SHAPES: list[dict[str, Any]] = [
    {"denyByDefault": True},
    {"denyByDefault": True, "deniedCommands": []},
    {"denyByDefault": True, "deniedCommands": ["rm .*"]},
    {"denyByDefault": True, "allowedCommands": ["git status.*"]},
    {"denyByDefault": True, "allowedCommands": ["git status.*"], "deniedCommands": ["rm .*"]},
    {"denyByDefault": True, "allowedCommands": ["git status.*"], "autoAllowReadonly": True},
    {
        "denyByDefault": True,
        "allowedCommands": ["git status.*"],
        "autoAllowReadonly": True,
        "deniedCommands": ["touch .*"],
    },
    {"denyByDefault": True, "allowedCommands": ["git status.*"], "deniedCommands": ["a > b"]},
    {"denyByDefault": True, "allowedCommands": ["git status.*"], "deniedCommands": ["a [|] b"]},
    {"denyByDefault": True, "allowedCommands": ["git status.*"], "deniedCommands": "rm .*"},
    {"allowedCommands": ["git status.*"], "deniedCommands": ["touch .*"]},
    {"denyByDefault": False, "allowedCommands": ["git status.*"]},
    {"deniedCommands": ["touch .*"]},
]


def _deny_rules(entry: dict[str, Any]) -> list[Any]:
    rules = (entry.get("permissions") or {}).get("rules") or []
    return [rule for rule in rules if rule.get("effect") == "deny"]


@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize("shell", _GRANT_SHAPES)
def test_a_shell_grant_changes_nothing_the_projection_records_with_the_preview_on(
    shell: dict[str, Any],
) -> None:
    """With or without the shell in ``allowedTools``, the same allowlist, read-only
    pass and engine rules: nothing in a ``denyByDefault`` file depends on the grant,
    and a file without ``denyByDefault`` records no allowlist either way."""
    settings = {"execute_bash": shell}
    plain = kas_agents.to_client_custom_agent("probe", _spec(toolsSettings=settings), "p")
    granted = kas_agents.to_client_custom_agent(
        "probe", _spec(toolsSettings=settings, allowedTools=["execute_bash"]), "p"
    )
    for key in (CREW_SHELL_ALLOWLIST_KEY, CREW_SHELL_READONLY_KEY):
        assert granted.get(key) == plain.get(key)
    assert (CREW_SHELL_ALLOWLIST_KEY in granted) is bool(shell.get("denyByDefault"))
    assert _deny_rules(granted) == _deny_rules(plain)


@pytest.mark.parametrize("shell", _GRANT_SHAPES)
def test_the_preview_changes_nothing_but_the_lifted_allowlist(
    shell: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the preview off and with it on, a spec projects to the same entry, but for the
    allowlist and read-only pass of a ``denyByDefault`` file whose shell grant lifts
    them. A file without the grant, or without ``denyByDefault``, is the same entry."""
    settings = {"execute_bash": shell}
    for extra in ({}, {"allowedTools": ["execute_bash"]}):
        spec = _spec(toolsSettings=settings, **extra)
        _set_switch(monkeypatch, _OFF)
        off = kas_agents.to_client_custom_agent("probe", spec, "p")
        _set_switch(monkeypatch, _ON)
        on = kas_agents.to_client_custom_agent("probe", spec, "p")
        differing = {key for key in {*off, *on} if off.get(key) != on.get(key)}
        assert differing <= {CREW_SHELL_ALLOWLIST_KEY, CREW_SHELL_READONLY_KEY}
        if not extra or not shell.get("denyByDefault"):
            assert off == on
        if CREW_SHELL_ALLOWLIST_KEY in off:
            assert off[CREW_SHELL_ALLOWLIST_KEY] == on[CREW_SHELL_ALLOWLIST_KEY]


def test_a_read_only_command_still_passes_beside_a_shell_grant_with_the_preview_on(
    switch_on: None,
) -> None:
    """``autoAllowReadonly: true`` beside ``denyByDefault`` lets a read-only command
    past the allowlist, with the shell in ``allowedTools`` as without it; a command
    that writes stays refused."""
    settings = {"execute_bash": {**_REPORTED["execute_bash"], "autoAllowReadonly": True}}
    for extra in ({"allowedTools": ["execute_bash"]}, {}):
        entry = kas_agents.to_client_custom_agent(
            "probe", _spec(toolsSettings=settings, **extra), "p"
        )
        readonly = kas_agents.projected_shell_readonly([entry], "probe")
        assert readonly is True
        ls = _own_request("ls", settings, **extra)
        ls.spec_shell_readonly = readonly
        assert _judge(ls).tier != "spec-shell-allowlist"
        cp = _own_request("cp seed copy", settings, **extra)
        cp.spec_shell_readonly = readonly
        verdict = _judge(cp)
        assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-allowlist")


@pytest.mark.parametrize(
    "allowed_tools",
    [
        None,
        [],
        ["fs_read"],
        ["@builder-mcp"],
        ["@builder-mcp/*"],
        ["@*"],
        ["*"],
        ["e*"],
        ["*bash"],
        ["sh*"],
        ["?hell"],
        ["execute_*"],
        ["@builtin/e*"],
        ["@builtin/execute_*"],
        ["Execute_Bash"],
        ["execute_bas[h]"],
        ["{execute_bash}"],
        ["!execute_bash"],
        ["@builtin/executeCmd"],
        ["@builtin/@builtin/execute_cmd"],
        [7],
        "execute_bash",
    ],
)
def test_an_agent_whose_allowed_tools_does_not_name_the_shell_is_still_refused(
    allowed_tools: Any,
) -> None:
    """No exact shell entry (an MCP server, a read tool, a pattern rather than the
    tool's own name, a list that is not one): ``denyByDefault`` still holds."""
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


@pytest.mark.usefixtures("switch_off")
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


@pytest.mark.usefixtures("switch_on")
def test_a_deny_beside_an_empty_allowlist_refuses_every_command_with_the_preview_on() -> None:
    settings = {
        "execute_bash": {"denyByDefault": True, "deniedCommands": ["rm .*"]},
    }
    spec = _spec(toolsSettings=settings, allowedTools=["execute_bash"])
    out = kas_agents.to_client_custom_agent("probe", spec, "p")
    # No `allowedCommands`: the allowlist is empty, so every command is refused.
    assert out[CREW_SHELL_ALLOWLIST_KEY] == []
    # The deny travels to the engine as a rule, whatever the allowlist record says.
    assert {"capability": "shell", "match": ["rm *"], "effect": "deny"} in out["permissions"][
        "rules"
    ]
    verdict = _judge(_own_request("ls", settings, allowedTools=["execute_bash"]))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-allowlist")


@pytest.mark.usefixtures("switch_off")
def test_the_projection_drops_the_record_when_allowed_tools_names_the_shell() -> None:
    spec = _spec(toolsSettings=_AWS_SETTINGS, allowedTools=["execute_bash"])
    assert CREW_SHELL_ALLOWLIST_KEY not in kas_agents.to_client_custom_agent("probe", spec, "p")
    # A duplicated agent (same spec, new id) reads the same way.
    assert CREW_SHELL_ALLOWLIST_KEY not in kas_agents.to_client_custom_agent(
        "probe-copy", spec, "p"
    )


@pytest.mark.usefixtures("switch_on")
def test_the_projection_keeps_the_record_with_the_preview_on_and_the_shell_granted() -> None:
    spec = _spec(toolsSettings=_AWS_SETTINGS, allowedTools=["execute_bash"])
    # A duplicated agent (same spec, new id) reads the same way.
    for agent_id in ("probe", "probe-copy"):
        out = kas_agents.to_client_custom_agent(agent_id, spec, "p")
        assert out[CREW_SHELL_ALLOWLIST_KEY] == ["git status.*"]


# ── the preview: one reading, every reader ────────────────────────────────────


def _lift():
    from kiro_crew.acp import kas_shell_grant_lift

    return kas_shell_grant_lift


@pytest.mark.parametrize(
    ("stored", "lifts"),
    [(True, False), (False, True), (None, True), ("true", True), (1, True)],
    ids=["on", "off", "never-set", "a-string", "a-number"],
)
def test_the_lift_is_what_the_one_reader_says(
    stored: Any, lifts: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision is the preview reader's answer and nothing else: only a stored
    ``true`` keeps the allowlist, and the reader is handed the config the gateway
    last applied."""
    from kiro_crew.config import live

    _set_switch(monkeypatch, stored)
    cfg = live.snapshot()
    reader = kas_engine_subagents.kas_subagents_preview_on
    seen: list[Any] = []
    monkeypatch.setattr(
        kas_engine_subagents, "kas_subagents_preview_on", lambda c: seen.append(c) or reader(c)
    )
    assert _lift().shell_grant_lifts_deny_by_default() is lifts
    assert seen == [cfg]


def test_a_build_with_previews_locked_lifts_as_with_the_preview_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An LTS build reads every preview off, so a stored ``true`` keeps no allowlist there."""
    from kiro_crew import edition_locks

    spec = _spec(toolsSettings=_REPORTED, allowedTools=["execute_bash"])
    _set_switch(monkeypatch, _ON)
    assert _lift().shell_grant_lifts_deny_by_default() is False
    assert CREW_SHELL_ALLOWLIST_KEY in kas_agents.to_client_custom_agent("probe", spec, "p")
    monkeypatch.setattr(
        edition_locks, "local_enable_locked", lambda name: name == "feature_previews"
    )
    assert _lift().shell_grant_lifts_deny_by_default() is True
    assert CREW_SHELL_ALLOWLIST_KEY not in kas_agents.to_client_custom_agent("probe", spec, "p")


def test_a_config_that_cannot_be_read_lifts_as_with_the_preview_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No applied config and a ``config.json`` that cannot be loaded read as off."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    def unreadable() -> None:
        raise OSError("config unreadable")

    monkeypatch.setattr(live, "snapshot", lambda: None)
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(unreadable))
    assert _lift().shell_grant_lifts_deny_by_default() is True
    spec = _spec(toolsSettings=_REPORTED, allowedTools=["execute_bash"])
    assert CREW_SHELL_ALLOWLIST_KEY not in kas_agents.to_client_custom_agent("probe", spec, "p")


def test_the_lift_reads_the_preview_only_through_its_reader() -> None:
    """The module asks ``kas_subagents_preview_on`` and reads no config value itself:
    the config it loads is only handed to that reader."""
    source = Path(_lift().__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "kas_subagents_preview_on(cfg)" in source
    assert "feature_previews" not in source and "getattr" not in source
    read_off_the_config = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Attribute, ast.Subscript))
        and isinstance(node.value, ast.Name)
        and node.value.id == "cfg"
    ]
    assert read_off_the_config == []


def test_only_the_kas_projection_imports_the_lift() -> None:
    """Every other backend ignores the preview: nothing but ``kas_agents`` imports
    the module that asks it."""
    root = Path(kiro_crew.__file__).parent
    imports = re.compile(r"import kas_shell_grant_lift|kas_shell_grant_lift import")
    importers = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if path.name != "kas_shell_grant_lift.py"
        and imports.search(path.read_text(encoding="utf-8"))
    )
    assert importers == ["acp/kas_agents.py"]


def test_the_lift_follows_the_reader_for_a_config_without_the_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config with no ``dashboard`` section, and a config that cannot be read at
    all: the answer is whatever the reader returns for it, so what a missing value
    means is the reader's alone."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    reader = kas_engine_subagents.kas_subagents_preview_on
    absent = SimpleNamespace()
    monkeypatch.setattr(live, "snapshot", lambda: absent)
    assert _lift().shell_grant_lifts_deny_by_default() is (not reader(absent))

    def unreadable() -> None:
        raise OSError("config unreadable")

    monkeypatch.setattr(live, "snapshot", lambda: None)
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(unreadable))
    assert _lift().shell_grant_lifts_deny_by_default() is (not reader(None))


@pytest.fixture
def stored_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real ``config.json`` for the projection to load, with no applied snapshot."""
    from kiro_crew.config import live

    crew = tmp_path / "crew-home"
    crew.mkdir()
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: crew)
    monkeypatch.setattr(live, "snapshot", lambda: None)

    def store(body: dict[str, Any]) -> None:
        (crew / "config.json").write_text(json.dumps(body), encoding="utf-8")

    return store


#: What a person's ``config.json`` holds while the preview is not on.
_NOT_ON: dict[str, dict[str, Any]] = {
    "never-set": {},
    "off": {"dashboard": {"feature_previews": {"kas_subagents": False}}},
}


def _mains_reading(spec: dict[str, Any], tools_settings: Any, policy: Any) -> list[str] | None:
    """The allowlist as the projection reads it with no preview: the shell grant lifts."""
    return spec_shell_allowlist(tools_settings, spec.get("allowedTools"), policy)


def _projected(specs: list[dict[str, Any]]) -> list[tuple[dict[str, Any], list[str]]]:
    return [
        (
            kas_agents.to_client_custom_agent("probe", spec, "p"),
            kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True),
        )
        for spec in specs
    ]


@pytest.mark.parametrize("shape", sorted(_NOT_ON))
@pytest.mark.parametrize("shell", _GRANT_SHAPES)
def test_off_and_never_set_project_what_main_projects(
    shell: dict[str, Any], shape: str, stored_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the preview stored off, and with it never stored, every spec gets the
    entry and the notice keys of the reading that asks no preview, on every
    ``allowedTools`` spelling that names the shell and with none."""
    stored_config(_NOT_ON[shape])
    settings = {"execute_bash": shell}
    specs = [_spec(toolsSettings=settings)] + [
        _spec(toolsSettings=settings, allowedTools=grant) for grant in _SHELL_GRANTS
    ]
    ours = _projected(specs)
    # The name the projection and the notice call; absent where no preview is asked.
    monkeypatch.setattr(kas_agents, "kas_shell_allowlist", _mains_reading, raising=False)
    assert ours == _projected(specs)


@pytest.mark.parametrize("key", ["execute_bash", "shell"])
def test_a_stored_true_keeps_the_reported_files_allowlist(key: str, stored_config) -> None:
    """The preview as a person stores it: ``true`` in ``config.json`` keeps the list."""
    stored_config({"dashboard": {"feature_previews": {"kas_subagents": True}}})
    spec = _spec(toolsSettings={key: _REPORTED["execute_bash"]}, allowedTools=["execute_bash"])
    out = kas_agents.to_client_custom_agent("probe", spec, "p")
    assert out[CREW_SHELL_ALLOWLIST_KEY] == ["git status.*"]
    lost = kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True)
    assert lost == kas_agents.spec_keys_without_carrier(
        {k: v for k, v in spec.items() if k != "allowedTools"}, translates_tools_settings=True
    )


def test_a_spec_the_grant_does_not_lift_reads_no_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    """The preview is read only for a ``denyByDefault`` file whose shell grant would
    lift the allowlist; every other spec is projected without a config read."""

    def never(cfg: Any) -> bool:
        raise AssertionError("the preview was read")

    monkeypatch.setattr(kas_engine_subagents, "kas_subagents_preview_on", never)
    kept = {**_REPORTED["execute_bash"], "deniedCommands": ["a > b"]}
    for settings, extra in (
        (_REPORTED, {}),
        ({"execute_bash": {"allowedCommands": ["git status.*"]}}, {"allowedTools": ["shell"]}),
        ({"execute_bash": kept}, {"allowedTools": ["execute_bash"]}),
        ({}, {"allowedTools": ["execute_bash"]}),
    ):
        spec = _spec(toolsSettings=settings, **extra)
        kas_agents.to_client_custom_agent("probe", spec, "p")
        kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True)


@pytest.mark.parametrize("value", [None, _OFF, _ON], ids=["never-set", "off", "on"])
def test_every_reader_gives_the_previews_answer(
    value: bool | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One stored value, one answer, in every place that reads the allowlist of a
    ``denyByDefault`` file with a shell grant: the projection's record, the session
    handle's stamp and the gate, a mode switch, the session-start notice, the engine
    sub-agent target check and the remote-workspace check."""
    from kiro_crew.acp import runtime_remote_workspace
    from kiro_crew.acp.kas_subagent_targets import _gate_only_limits
    from kiro_crew.agent_files import kas_wire_agent_id

    _set_switch(monkeypatch, value)
    held = value is _ON
    expected = ("git status.*",) if held else None
    extra = {"allowedTools": ["execute_bash"]}
    entry = kas_agents.to_client_custom_agent(
        "probe", _spec(toolsSettings=_AWS_SETTINGS, **extra), "p"
    )
    # The projection's record.
    assert (CREW_SHELL_ALLOWLIST_KEY in entry) is held
    assert kas_agents.projected_shell_allowlist([entry], "probe") == expected
    # A mode switch to that agent reads the same record.
    assert kas_agents.switched_shell_allowlist([entry], kas_wire_agent_id("probe")) == expected
    # The session handle's stamp, and the gate on it.
    event = _own_request("cp seed copy", _AWS_SETTINGS, **extra)
    assert event.spec_shell_allowlist == expected
    assert (_judge(event).tier == "spec-shell-allowlist") is held
    refusal = permission_floor.refusal_for(event, session_key="cron:probe", security_only=False)
    assert (refusal is not None and "denyByDefault" in refusal) is held
    # The session-start notice.
    ask = {"rules": [{"capability": "shell", "effect": "ask"}]}
    lost = kas_agents.spec_keys_without_carrier(
        _spec(toolsSettings=_AWS_SETTINGS, permissions=ask, **extra),
        translates_tools_settings=True,
    )
    assert ("refuses what they do not cover" in _spec_keys_notice("probe", lost)) is held
    # The engine sub-agent target check and the remote-workspace check.
    assert _gate_only_limits(entry) is held
    assert runtime_remote_workspace._carries_spec_allowlist(entry) is held


def test_a_flipped_preview_reaches_a_session_at_its_next_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preview is read when an agent is projected, at session start. A session
    already open keeps the record it was stamped with; the gate reads that record on
    every call and never the preview."""
    extra = {"allowedTools": ["execute_bash"]}
    spec = _spec(toolsSettings=_AWS_SETTINGS, **extra)
    for first, second in ((_OFF, _ON), (_ON, _OFF)):
        _set_switch(monkeypatch, first)
        entry = kas_agents.to_client_custom_agent("probe", spec, "p")
        handle = _handle(ACP_BACKEND_KAS)
        handle.kas_spec_shell_allowlist = kas_agents.projected_shell_allowlist([entry], "probe")
        before = handle._build_permission_event(_permission("own-sid"))
        _set_switch(monkeypatch, second)
        after = handle._build_permission_event(_permission("own-sid"))
        assert before is not None and after is not None
        assert after.spec_shell_allowlist == before.spec_shell_allowlist
        assert (before.spec_shell_allowlist is None) is (first == _OFF)
        # The next projection reads the new value.
        again = kas_agents.to_client_custom_agent("probe", spec, "p")
        assert (CREW_SHELL_ALLOWLIST_KEY in again) is (second == _ON)


@pytest.mark.parametrize(
    "denied",
    [
        [r"^git commit.*|git push.*$"],
        ["rm .*", r"^git commit.*|git push.*$"],
        ["cat .* > .*"],
        "rm .*",
        [7],
    ],
)
def test_a_denied_command_that_does_not_travel_keeps_the_allowlist(denied: Any) -> None:
    """A ``deniedCommands`` entry with no KAS deny rule (top-level alternation, a redirect,
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


# ── a deny KAS receives split into branches ───────────────────────────────────

#: kiro-cli's upgrade of this file writes ``allow shell ["git status*"]``, the split
#: deny, ``deny shell ["*"] exclude ["git status*"]`` and ``allow shell``: every
#: command but ``git status`` is refused by the engine without a prompt.
_SPLIT_SETTINGS: dict[str, Any] = {
    "execute_bash": {
        "denyByDefault": True,
        "allowedCommands": ["git status.*"],
        "deniedCommands": ["(touch|mkdir) .*"],
    }
}


@pytest.mark.parametrize("denied", ["(touch|mkdir) .*", "touch .*|mkdir .*"])
def test_a_split_deny_keeps_the_allowlist_when_allowed_tools_names_the_shell(denied: str) -> None:
    settings = {"execute_bash": {**_SPLIT_SETTINGS["execute_bash"], "deniedCommands": [denied]}}
    out = kas_agents.to_client_custom_agent(
        "probe", _spec(toolsSettings=settings, allowedTools=["execute_bash"]), "p"
    )
    assert out[CREW_SHELL_ALLOWLIST_KEY] == ["git status.*"]
    # The deny still travels to the engine as one glob per branch.
    assert {"capability": "shell", "match": ["touch *", "mkdir *"], "effect": "deny"} in out[
        "permissions"
    ]["rules"]
    event = _own_request("cp seed copy", settings, allowedTools=["execute_bash"])
    verdict = _judge(event)
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-allowlist")
    reason = permission_floor.refusal_for(event, session_key="cron:probe", security_only=False)
    assert reason is not None and "denyByDefault" in reason
    listed = _own_request("git status --short", settings, allowedTools=["execute_bash"])
    assert _judge(listed).tier != "spec-shell-allowlist"


def test_a_split_deny_records_no_read_only_pass() -> None:
    """``autoAllowReadonly`` beside a split deny stays as with an uncarried one: the
    read-only pass is not recorded, so the allowlist alone decides."""
    from kiro_crew.acp.kas_permissions import CREW_SHELL_READONLY_KEY

    settings = {"execute_bash": {**_SPLIT_SETTINGS["execute_bash"], "autoAllowReadonly": True}}
    out = kas_agents.to_client_custom_agent("probe", _spec(toolsSettings=settings), "p")
    assert out[CREW_SHELL_ALLOWLIST_KEY] == ["git status.*"]
    assert CREW_SHELL_READONLY_KEY not in out


def test_an_empty_allowlist_beside_a_split_deny_refuses_every_command() -> None:
    settings = {"execute_bash": {"denyByDefault": True, "deniedCommands": ["(rm|shred) .*"]}}
    event = _own_request("ls", settings, allowedTools=["execute_bash"])
    assert event.spec_shell_allowlist == ()
    verdict = _judge(event)
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-allowlist")


# ── the agents Kiro Crew ships ────────────────────────────────────────────────


def _shipped_agent_files() -> list[Path]:
    root = Path(kiro_crew.__file__).parent
    return [root / "config" / "defaults.json", *sorted(root.glob("apps/builtins/*/agents/*.json"))]


@pytest.mark.parametrize("value", [None, _OFF, _ON], ids=["never-set", "off", "on"])
@pytest.mark.parametrize("path", _shipped_agent_files(), ids=lambda path: path.stem)
def test_no_shipped_agent_sets_deny_by_default(
    path: Path, value: bool | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allowlist binds only a file that sets ``denyByDefault``. No agent Kiro
    Crew ships does, so with the preview on, off or never set this row refuses none of their
    commands on any backend, with the shell added to ``allowedTools`` or not."""
    _set_switch(monkeypatch, value)
    spec = json.loads(path.read_text(encoding="utf-8"))
    tools_settings, _ = canonical_tools_settings(spec.get("toolsSettings"))
    assert not shell_denies_by_default(tools_settings)
    granted = {**spec, "allowedTools": ["execute_bash"]}
    for variant in (spec, granted):
        assert _lift().kas_shell_allowlist(variant, tools_settings, None) is None
        assert spec_shell_patterns(variant) is None
