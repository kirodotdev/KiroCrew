"""Enterprise gate: a proven non-shell call is judged in its own scope.

``classify_tool_title`` reads an unprefixed name as a command body AND a tool
name, so with the enterprise baseline's ``commands`` off every bare tool name was
refused, read-only tools included. ``gate_decision(own_scopes=...)`` keeps those
off-scope pairs as deny-only, and ``GateFacts.governance_own_scopes`` proves when
a call may use it. RFC ``rfc-crew-enterprise-edition.md`` appendix: the ``tools``
minimal set is read-only tools; ``commands`` and writes stay off.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew import hooks
from kiro_crew.platform import governance
from kiro_crew.platform.governance import gate_decision, parse_policy, parse_profile

_WS_FILE = os.path.expanduser(os.path.join("~", "workplace", "kirocrew-workspace", "notes.txt"))
_OUTSIDE = os.path.abspath(os.path.join(os.sep, "etc", "passwd"))


_ROW_READ = ("tools", "filesystem.read")
_ROW_WRITE = ("tools", "filesystem.write")


def _ceiling(**controls: object) -> governance.GovernanceCeiling:
    return parse_policy({"version": 1, "boot": {}, **controls})


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


# The shapes kiro-cli and KAS send (test/fixtures/acp_frames/kiro/side-read-only-live.jsonl).
def _read(path: str = _WS_FILE, **kw: object) -> governance.Decision:
    return gate_decision(
        kw.pop("ceiling", _ceiling()),  # type: ignore[arg-type]
        kw.pop("profile", None),  # type: ignore[arg-type]
        f"Reading {path}:1",
        tool_kind="read",
        raw_params={"operations": [{"mode": "Line", "path": path}]},
        alias_groups=(("read", "fs_read"),),
        **kw,  # type: ignore[arg-type]
    )


def _grep(path: str = _WS_FILE, **kw: object) -> governance.Decision:
    return gate_decision(
        kw.pop("ceiling", _ceiling()),  # type: ignore[arg-type]
        kw.pop("profile", None),  # type: ignore[arg-type]
        "Searching for 'hello'",
        tool_kind="search",
        raw_params={"pattern": "hello", **({"path": path} if path else {})},
        extra_titles=("grep",),
        **kw,  # type: ignore[arg-type]
    )


def _write(**kw: object) -> governance.Decision:
    return gate_decision(
        kw.pop("ceiling", _ceiling()),  # type: ignore[arg-type]
        kw.pop("profile", None),  # type: ignore[arg-type]
        "Creating notes.txt",
        tool_kind="edit",
        raw_params={"command": "create", "path": _WS_FILE},
        alias_groups=(("write", "fs_write"),),
        **kw,  # type: ignore[arg-type]
    )


def _mcp(server: str, **kw: object) -> governance.Decision:
    return gate_decision(
        _ceiling(),
        kw.pop("profile", None),  # type: ignore[arg-type]
        f"Running: @{server}/work_report",
        extra_titles=("work_report",),
        mcp_ref=f"@{server}/work_report",
        **kw,  # type: ignore[arg-type]
    )


def test_root_cause_the_commands_reading_refuses_a_read(enterprise: None) -> None:
    """Without the proof the bare names are also asked as commands, and refused there."""
    decision = _grep()
    assert not decision.permitted
    assert decision.reason.startswith("policy denies: enterprise baseline")


def test_enterprise_read_only_tools_pass_on_an_empty_policy(enterprise: None) -> None:
    assert _read(own_scopes=_ROW_READ).permitted
    assert _grep(own_scopes=_ROW_READ).permitted
    kas = gate_decision(
        _ceiling(),
        None,
        "Read File",
        tool_kind="read",
        raw_params={"path": _WS_FILE},
        alias_groups=(("read_file", "fs_read"),),
        own_scopes=_ROW_READ,
    )
    assert kas.permitted


def test_enterprise_read_outside_the_workspace_stays_denied(enterprise: None) -> None:
    assert not _read(_OUTSIDE, own_scopes=_ROW_READ).permitted
    assert not _grep(_OUTSIDE, own_scopes=_ROW_READ).permitted


def test_enterprise_pathless_search_is_refused_by_the_read_confinement(enterprise: None) -> None:
    """A search with no path runs in a directory the gate cannot see."""
    decision = _grep("", own_scopes=_ROW_READ)
    assert not decision.permitted
    assert decision.item == governance._NO_TARGET_ITEM
    # An ungoverned or deny-mode read scope still lets it run.
    open_reads = _ceiling(filesystem={"read": {"mode": "deny", "deny": []}})
    assert _grep("", ceiling=open_reads, own_scopes=_ROW_READ).permitted


def test_enterprise_missing_target_marker_never_lands_in_the_working_directory(
    enterprise: None, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """The gateway may run inside the allowed root; the marker must still miss it."""
    root = os.path.abspath(str(tmp_path))
    monkeypatch.chdir(root)
    confined = _ceiling(
        tools={"mode": "allow", "allow": ["grep", "fs_write"]},
        filesystem={
            "read": {"mode": "allow", "allow": [os.path.join(root, "**")]},
            "write": {"mode": "allow", "allow": [os.path.join(root, "**")]},
        },
    )
    assert not _grep("", ceiling=confined, own_scopes=_ROW_READ).permitted
    write = gate_decision(
        confined,
        None,
        "Creating notes.txt",
        tool_kind="",
        raw_params={"command": "create", "path": _OUTSIDE},
        alias_groups=(("write", "fs_write"),),
        own_scopes=_ROW_WRITE,
    )
    assert not write.permitted


def test_enterprise_write_not_sent_as_an_edit_meets_the_write_scope(enterprise: None) -> None:
    """A kindless write names no ``filesystem.write`` target, so the row asks the marker."""
    both = _ceiling(
        tools={"mode": "allow", "allow": ["fs_write"]},
        filesystem={"write": {"mode": "allow", "allow": [_WS_FILE]}},
    )
    for kind in ("", "read", "other"):
        decision = gate_decision(
            both,
            None,
            "Creating notes.txt",
            tool_kind=kind,
            raw_params={"command": "create", "path": _WS_FILE},
            alias_groups=(("write", "fs_write"),),
            own_scopes=_ROW_WRITE,
        )
        assert not decision.permitted, kind


def test_enterprise_core_mcp_passes_and_other_servers_do_not(enterprise: None) -> None:
    assert _mcp("kirocrew-core", own_scopes=("mcp",)).permitted
    assert not _mcp("other", own_scopes=("mcp",)).permitted
    assert not _mcp("kirocrew-core").permitted


def test_enterprise_writes_stay_denied(enterprise: None) -> None:
    assert not _write(own_scopes=_ROW_WRITE).permitted
    # A remote tools grant alone does not open the write path.
    granted = _ceiling(tools={"mode": "allow", "allow": ["fs_write"]})
    assert not _write(ceiling=granted, own_scopes=_ROW_WRITE).permitted
    # Both scopes named by the remote policy: the write is allowed, commands unnamed.
    both = _ceiling(
        tools={"mode": "allow", "allow": ["fs_write"]},
        filesystem={"write": {"mode": "allow", "allow": [_WS_FILE]}},
    )
    assert _write(ceiling=both, own_scopes=_ROW_WRITE).permitted


def test_enterprise_shell_stays_governed(enterprise: None) -> None:
    assert not gate_decision(_ceiling(), None, "Running: ls", tool_kind="execute").permitted
    ceiling = _ceiling(commands={"mode": "deny", "deny": []})
    profile = parse_profile({"name": "p", "commands": {"mode": "deny", "deny": ["curl *"]}})
    assert gate_decision(ceiling, profile, "Running: ls").permitted
    assert not gate_decision(ceiling, profile, "Running: curl https://example.com").permitted


def test_enterprise_local_curl_deny_binds_an_off_scope_pair(enterprise: None) -> None:
    """An explicit deny still refuses through a pair the call is not judged in."""
    profile = parse_profile({"name": "p", "commands": {"mode": "deny", "deny": ["curl *"]}})
    for ceiling in (_ceiling(), _ceiling(commands={"mode": "deny", "deny": []})):
        decision = gate_decision(
            ceiling,
            profile,
            "Running: curl https://example.com",
            extra_titles=("grep",),
            own_scopes=_ROW_READ,
        )
        assert not decision.permitted
        assert decision.explicit_deny
        assert decision.item == "curl https://example.com"
    remote = _ceiling(commands={"mode": "deny", "deny": ["*"]})
    assert not _grep(ceiling=remote, own_scopes=_ROW_READ).permitted


def test_enterprise_profile_only_narrows_the_tools_baseline(enterprise: None) -> None:
    profile = parse_profile({"name": "p", "tools": {"mode": "deny", "deny": ["grep"]}})
    assert not _grep(profile=profile, own_scopes=_ROW_READ).permitted
    assert _read(profile=profile, own_scopes=_ROW_READ).permitted
    widen = parse_profile({"name": "p", "tools": {"mode": "allow", "allow": ["fs_write"]}})
    assert not _write(profile=widen, own_scopes=_ROW_WRITE).permitted
    assert not _read(profile=widen, own_scopes=_ROW_READ).permitted


def test_own_scopes_without_an_identity_changes_nothing(enterprise: None) -> None:
    assert not gate_decision(_ceiling(), None, "grep", own_scopes=_ROW_READ).permitted
    assert not gate_decision(
        _ceiling(), None, "Running: @kirocrew-core/x", own_scopes=("mcp",)
    ).permitted


def test_oss_gate_is_unchanged() -> None:
    """oss with no policy permits; an oss commands allow-set still reads bare names."""
    assert _grep(ceiling=None).permitted
    commands = _ceiling(commands={"mode": "allow", "allow": ["git *"]})
    assert not _grep(ceiling=commands).permitted


# ── the proof ────────────────────────────────────────────────────────────────


def _scope(**fields: object) -> tuple[str, ...]:
    base: dict[str, object] = {"title": "t", "identity_trusted": True}
    base.update(fields)
    call = hooks.ToolCall(**base)  # type: ignore[arg-type]
    facts = hooks.GateFacts(
        None,  # type: ignore[arg-type]
        call,
        session_key="s",
        agent="a",
        app="",
        classifier_only=False,
    )
    return facts.governance_own_scopes


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"mcp_tool": "fs_read"}, _ROW_READ),
        ({"mcp_tool": "read"}, _ROW_READ),
        ({"mcp_tool": "grep"}, _ROW_READ),
        ({"mcp_tool": "write"}, _ROW_WRITE),
        ({"mcp_tool": "read_file", "kas_builtin_ids": True}, _ROW_READ),
        ({"mcp_tool": "work_report", "mcp_server": "kirocrew-core"}, ("mcp",)),
        # Commands in its own row, or no row at all: not proven.
        ({"mcp_tool": "code"}, ()),
        ({"mcp_tool": "shell"}, ()),
        ({"mcp_tool": "run_command", "kas_builtin_ids": True}, ()),
        ({"mcp_tool": "delete_file", "kas_builtin_ids": True}, ()),
        ({"mcp_tool": "use_subagent"}, ()),
        # A KAS id read on another harness is not folded.
        ({"mcp_tool": "read_file"}, ()),
        # No proof from an untrusted, unreadable, shell or nameless call.
        ({"mcp_tool": "fs_read", "identity_trusted": False}, ()),
        ({"mcp_tool": "fs_read", "identity_unreadable": True}, ()),
        ({"mcp_tool": "fs_read", "is_shell": True}, ()),
        ({"mcp_tool": "fs_read", "command": "ls"}, ()),
        ({"mcp_tool": ""}, ()),
    ],
)
def test_enterprise_own_scope_proof(
    enterprise: None, fields: dict[str, object], expected: str
) -> None:
    assert _scope(**fields) == expected


def test_oss_own_scope_proof_is_off() -> None:
    assert _scope(mcp_tool="fs_read") == ()
    assert _scope(mcp_tool="work_report", mcp_server="kirocrew-core") == ()


# ── end to end through the gate ──────────────────────────────────────────────


@pytest.fixture
def enterprise_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """An enterprise build whose bound profile names nothing, so the baseline answers.

    The standalone context is composed first: in enterprise an unbooted
    ``current_context`` refuses to compose (the boot gate's job, not this one's).
    """
    import kiro_crew.platform.governance_profiles as profiles_mod
    from kiro_crew.platform.context import current_context

    current_context()
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")
    monkeypatch.setattr(
        profiles_mod,
        "resolve_active_scope",
        lambda *a, **k: governance.Profile(name="p", controls={}),
    )


def _gate(title: str, **kw: object) -> hooks.ToolHookResult:
    return hooks.HookManager().on_tool_call(title, mcp_identity_trusted=True, **kw)  # type: ignore[arg-type]


def test_enterprise_gate_passes_reads_and_refuses_writes_and_shell(enterprise_gate: None) -> None:
    grep = _gate(
        "Searching for 'hello'",
        mcp_tool_name="grep",
        tool_kind="search",
        raw_params={"pattern": "hello", "path": _WS_FILE},
    )
    assert grep.action != hooks.TOOL_DENY, grep.reason
    core = _gate(
        "Running: @kirocrew-core/work_report",
        mcp_server_name="kirocrew-core",
        mcp_tool_name="work_report",
    )
    assert core.action != hooks.TOOL_DENY, core.reason
    write = _gate(
        "Creating notes.txt",
        mcp_tool_name="write",
        tool_kind="edit",
        raw_params={"command": "create", "path": _WS_FILE},
    )
    assert write.action == hooks.TOOL_DENY
    assert "governance" in write.reason
    shell = _gate(
        "Running: ls",
        mcp_tool_name="shell",
        tool_kind="execute",
        raw_params={"command": "ls"},
        command="ls",
        is_shell=True,
    )
    assert shell.action == hooks.TOOL_DENY
    assert "governance" in shell.reason
