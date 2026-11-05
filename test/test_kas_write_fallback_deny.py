"""A spec's ``toolsSettings.fs_write.fallbackAction: "deny"`` on KAS: a write its
``allowedPaths`` do not cover is refused, on every approval policy.

On kiro-cli such a spec refuses every file write outside ``allowedPaths`` instead of
asking. KAS has no rule for it, so the projection records the spec's usable entries
(``kas_permissions.spec_write_allowlist``), the session handle anchors them at the
session's directory and stamps them on its own agent's permission requests, and the
gate's ``spec-write-allowlist`` row refuses a write none of them covers. A deny, so a
scheduled job running with ``approval_mode: auto`` (``ToolApprovalPolicy.AUTO_APPROVE``,
which consults the gate through ``permission_floor.refusal_for``) refuses it too,
instead of running it unprompted.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from test_kas_spec_builtin_grant import _handle, _permission
from test_tool_call_classification_kas import _kas_builtin_flow

from kiro_crew import permission_floor
from kiro_crew.acp import kas_agents, kas_write_allowlist
from kiro_crew.acp._dispatch import attach_kas_custom_agents
from kiro_crew.acp.kas_permissions import (
    CREW_WRITE_ALLOWLIST_KEY,
    spec_write_allowlist,
    tools_settings_to_permissions,
)
from kiro_crew.acp.types import AcpEvent
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig, hook_gate_kwargs
from kiro_crew.platform import allowed_paths
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp

#: Creating a symlink on Windows needs a privilege a runner may not hold.
_needs_symlinks = pytest.mark.skipif(
    sys.platform == "win32", reason="symlink creation needs a Windows privilege"
)


@pytest.fixture(autouse=True)
def _no_profiles(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """The session's directory, with an allowed ``out`` folder inside it. Resolved,
    so a symlinked temp root does not change what the test spells."""
    root = Path(os.path.realpath(tmp_path)) / "proj"
    (root / "out").mkdir(parents=True)
    return root


def _settings(*paths: str) -> dict[str, Any]:
    return {"fs_write": {"fallbackAction": "deny", "allowedPaths": list(paths)}}


def _spec(**extra: Any) -> dict[str, Any]:
    return {"name": "probe", "prompt": "p", "tools": ["fs_write"], **extra}


def _write(path: str, *, tool: str = "fs_write", kind: str = "edit") -> AcpEvent:
    raw = {"targetFile": path} if tool == "delete_file" else {"path": path, "command": "create"}
    return _kas_builtin_flow(tool, title="Write", kind=kind, raw_input=raw)


def _own_request(event: AcpEvent, settings: dict[str, Any] | None, cwd: Path | str) -> AcpEvent:
    """*event* as the session handle stamps its own agent's request, with the record
    the projection of a spec carrying *settings* registered, opened at *cwd*."""
    spec = _spec(toolsSettings=settings) if settings is not None else _spec()
    entry = kas_agents.to_client_custom_agent("probe", spec, "p")
    handle = _handle(ACP_BACKEND_KAS)
    handle._bound_cwd = str(cwd)
    handle.kas_spec_write_allowlist = kas_write_allowlist.projected_write_allowlist(
        [entry], "probe", str(cwd)
    )
    stamped = handle._build_permission_event(_permission("own-sid"))
    assert stamped is not None
    event.spec_write_allowlist = stamped.spec_write_allowlist
    event.spec_write_cwd = stamped.spec_write_cwd
    return event


def _judge(event: AcpEvent):
    return HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))


# ── the reported case: an auto-mode job ───────────────────────────────────────


def test_an_auto_mode_job_refuses_a_write_outside_allowed_paths(work: Path) -> None:
    """``AUTO_APPROVE`` approves whatever the gate does not deny. The write outside
    ``allowedPaths`` must be denied there, not run."""
    event = _own_request(_write(str(work / "elsewhere.txt")), _settings("out"), work)
    reason = permission_floor.refusal_for(event, session_key="cron:probe", security_only=False)
    assert reason is not None and "fallbackAction" in reason


def test_an_auto_mode_job_still_writes_inside_allowed_paths(work: Path) -> None:
    event = _own_request(_write(str(work / "out" / "a.txt")), _settings("out"), work)
    assert (
        permission_floor.refusal_for(event, session_key="cron:probe", security_only=False) is None
    )


def test_an_interactive_session_refuses_it_at_once_instead_of_prompting(work: Path) -> None:
    verdict = _judge(_own_request(_write(str(work / "elsewhere.txt")), _settings("out"), work))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-write-allowlist")
    covered = _judge(_own_request(_write(str(work / "out" / "a.txt")), _settings("out"), work))
    assert covered.action != TOOL_DENY


@pytest.mark.parametrize(
    ("tool", "kind"), [("fs_write", "edit"), ("str_replace", "edit"), ("delete_file", "delete")]
)
def test_every_kas_write_tool_is_refused(work: Path, tool: str, kind: str) -> None:
    event = _own_request(
        _write(str(work / "elsewhere.txt"), tool=tool, kind=kind), _settings("out"), work
    )
    assert _judge(event).action == TOOL_DENY


# ── what counts as covered ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("entry", "target", "covered"),
    [
        ("out", "out/a.txt", True),
        ("out", "out/deep/a.txt", True),
        ("out", "outside.txt", False),
        ("./out/", "out/a.txt", True),
        ("out/*.md", "out/a.md", True),
        ("out/*.md", "out/a.txt", False),
        ("out/**/x.py", "out/x.py", True),
        ("out/**/x.py", "out/a/b/x.py", True),
        ("out/?.txt", "out/a.txt", True),
        ("out/?.txt", "out/ab.txt", False),
        ("out", "out/../escape.txt", False),
    ],
)
def test_relative_entries_are_anchored_at_the_session_directory(
    work: Path, entry: str, target: str, covered: bool
) -> None:
    event = _own_request(_write(str(work) + "/" + target), _settings(entry), work)
    assert (_judge(event).action != TOOL_DENY) is covered


def test_an_absolute_and_a_home_entry_are_read(work: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(work))
    monkeypatch.setenv("USERPROFILE", str(work))
    settings = _settings(str(work / "abs"), "~/home-out")
    assert _judge(_own_request(_write(str(work / "abs" / "a")), settings, "/")).action != TOOL_DENY
    assert (
        _judge(_own_request(_write(str(work / "home-out" / "a")), settings, "/")).action
        != TOOL_DENY
    )
    assert _judge(_own_request(_write(str(work / "other")), settings, "/")).action == TOOL_DENY


@_needs_symlinks
def test_a_symlink_inside_an_allowed_directory_cannot_carry_a_write_out(work: Path) -> None:
    outside = work.parent / "outside"
    outside.mkdir()
    (work / "out" / "link").symlink_to(outside)
    event = _own_request(_write(str(work / "out" / "link" / "a.txt")), _settings("out"), work)
    verdict = _judge(event)
    assert verdict.action == TOOL_DENY and "symlink" in verdict.reason


@_needs_symlinks
def test_a_symlink_then_dot_dot_is_followed_before_it_is_folded(work: Path) -> None:
    """``out/link/..`` folds to ``out`` on paper, but the filesystem follows ``link``
    first: the write lands beside the link's target, outside ``out``."""
    elsewhere = work.parent / "elsewhere" / "sub"
    elsewhere.mkdir(parents=True)
    (work / "out" / "link").symlink_to(elsewhere)
    event = _own_request(
        _write(str(work / "out" / "link" / ".." / "r.txt")), _settings("out"), work
    )
    assert _judge(event).action == TOOL_DENY


@pytest.mark.parametrize(("target", "covered"), [("out/r.md", True), ("r.md", False)])
def test_a_relative_target_is_judged_under_the_session_directory(
    work: Path, target: str, covered: bool
) -> None:
    """KAS resolves a relative ``path`` against its workspace root, the session's
    own directory, so a covered relative write keeps running."""
    event = _own_request(_write(target), _settings("out"), work)
    assert (_judge(event).action != TOOL_DENY) is covered
    floor = permission_floor.refusal_for(event, session_key="cron:probe", security_only=False)
    assert (floor is None) is covered


def test_a_relative_target_with_no_session_directory_is_refused(work: Path) -> None:
    event = _own_request(_write("out/r.md"), _settings(str(work / "out")), work)
    event.spec_write_cwd = ""
    assert _judge(event).action == TOOL_DENY


@_needs_symlinks
def test_an_allowed_directory_reached_through_a_symlink_is_still_covered(work: Path) -> None:
    """The session directory spelled through a link, the write spelled through its
    real path: kiro-cli canonicalizes the entry, so this is covered."""
    alias = work.parent / "alias"
    alias.symlink_to(work)
    event = _own_request(_write(str(work / "out" / "a.txt")), _settings("out"), alias)
    assert _judge(event).action != TOOL_DENY


def test_a_spec_without_allowed_paths_refuses_every_write(work: Path) -> None:
    settings = {"write": {"fallbackAction": "deny"}}
    assert spec_write_allowlist(settings) == []
    assert _judge(_own_request(_write(str(work / "out" / "a")), settings, work)).action == TOOL_DENY


@pytest.mark.parametrize(
    "entry",
    [
        "out/[ab].txt",
        "out/{a,b}",
        "$HOME/out",
        "~bob/out",
        # A separator on Windows, an escape everywhere else.
        pytest.param(
            "o\\ut", marks=pytest.mark.skipif(sys.platform == "win32", reason="separator")
        ),
    ],
)
def test_an_unusable_entry_covers_nothing(work: Path, entry: str) -> None:
    """Outside the subset Crew reads like kiro-cli, so a write only it would cover
    is refused, never let through; the notice names ``allowedPaths``."""
    settings = _settings(entry)
    assert spec_write_allowlist(settings) == []
    assert (
        _judge(_own_request(_write(str(work / "out" / "a.txt")), settings, work)).action
        == TOOL_DENY
    )
    assert "fs_write.allowedPaths" in tools_settings_to_permissions(settings)[1]


def test_a_relative_entry_with_no_session_directory_covers_nothing(work: Path) -> None:
    event = _own_request(_write(str(work / "out" / "a.txt")), _settings("out"), "")
    assert _judge(event).action == TOOL_DENY


def test_a_write_with_no_readable_target_is_refused(work: Path) -> None:
    event = _own_request(
        _kas_builtin_flow("fs_write", title="Write", kind="edit", raw_input={}),
        _settings("out"),
        work,
    )
    assert _judge(event).action == TOOL_DENY


# ── what it leaves alone ──────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, "", "interactive", "ask-me"])
def test_a_spec_without_deny_is_judged_as_before(work: Path, value: Any) -> None:
    settings = {"fs_write": {"fallbackAction": value, "allowedPaths": ["out"]}}
    assert spec_write_allowlist(settings) is None
    assert spec_write_allowlist({"fs_write": {"allowedPaths": ["out"]}}) is None
    event = _own_request(_write(str(work / "elsewhere.txt")), settings, work)
    assert event.spec_write_allowlist is None
    assert _judge(event).action != TOOL_DENY


def test_a_read_and_a_shell_call_are_not_refused(work: Path) -> None:
    read = _kas_builtin_flow(
        "read_file", title="Read", kind="read", raw_input={"path": "/etc/hosts"}
    )
    read.spec_write_allowlist = ()
    assert _judge(read).action != TOOL_DENY
    shell = _kas_builtin_flow(
        "run_command", title="Run Command", kind="execute", raw_input={"command": "ls"}
    )
    shell.spec_write_allowlist = ()
    assert _judge(shell).action != TOOL_DENY


def test_an_mcp_tool_is_not_refused() -> None:
    event = AcpEvent(
        kind="permission_request",
        title="Save the report",
        mcp_server_name="ops",
        tool_name="save",
        mcp_identity_trusted=True,
        spec_write_allowlist=(),
    )
    assert _judge(event).action != TOOL_DENY


def test_the_handle_stamps_the_allowlist_on_its_own_request_only() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    handle.kas_spec_write_allowlist = ("/w/out",)
    own = handle._build_permission_event(_permission("own-sid"))
    child = handle._build_permission_event(_permission("child-sid"))
    assert own is not None and own.spec_write_allowlist == ("/w/out",)
    assert child is not None and child.spec_write_allowlist is None


# ── the record ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["deny", "DENY", " Deny ", True, 1, {}])
def test_deny_or_a_malformed_value_turns_it_on(value: Any) -> None:
    assert spec_write_allowlist({"fs_write": {"fallbackAction": value}}) == []


def test_the_projection_records_it_and_it_never_reaches_the_wire() -> None:
    entry = kas_agents.to_client_custom_agent("probe", _spec(toolsSettings=_settings("out")), "p")
    assert entry[CREW_WRITE_ALLOWLIST_KEY] == ["out"]
    params: dict[str, Any] = {}
    attach_kas_custom_agents(params, [entry])
    (wire,) = params["_meta"]["kiro"]["customAgents"]
    assert CREW_WRITE_ALLOWLIST_KEY not in wire


def test_the_notice_no_longer_names_fallback_action() -> None:
    lost = kas_agents.spec_keys_without_carrier(
        _spec(toolsSettings=_settings("out")), translates_tools_settings=True
    )
    assert "toolsSettings.fs_write.fallbackAction" not in lost
    # A covered write still prompts on KAS, so the allow list stays named.
    assert "toolsSettings.fs_write.allowedPaths" in lost


def test_the_session_record_follows_the_active_agent_and_a_mode_switch() -> None:
    from kiro_crew.agent_files import kas_wire_agent_id

    batch = [
        {"id": "probe", "prompt": "p", CREW_WRITE_ALLOWLIST_KEY: ["out", 7]},
        {"id": "broken", "prompt": "p", CREW_WRITE_ALLOWLIST_KEY: "out"},
        {"id": "other", "prompt": "p"},
    ]
    assert kas_write_allowlist.projected_write_allowlist(batch, "probe", "/nowhere-w") == (
        "/nowhere-w/out",
    )
    # A malformed record restricts everything; it never lifts the restriction.
    assert kas_write_allowlist.projected_write_allowlist(batch, "broken", "/w") == ()
    assert kas_write_allowlist.projected_write_allowlist(batch, "other", "/w") is None
    assert kas_write_allowlist.projected_write_allowlist(batch, "missing", "/w") is None
    assert kas_write_allowlist.switched_write_allowlist(
        batch, kas_wire_agent_id("probe"), "/nowhere-w"
    ) == ("/nowhere-w/out",)
    assert kas_write_allowlist.switched_write_allowlist(batch, "other", "/w") is None


# ── the matcher ───────────────────────────────────────────────────────────────


def test_the_matcher_is_bounded() -> None:
    assert allowed_paths.parse("a/**/b/**/c/**/d/**/e") is None
    assert allowed_paths.parse("x" * (allowed_paths.MAX_PATTERN_LENGTH + 1)) is None
    long_path = "/" + "a" * allowed_paths.MAX_PATH_LENGTH
    assert not allowed_paths.matches("/*", long_path)
    # Many stars over a long near-miss path stays fast (leftmost-match, no backtracking).
    pattern = "/" + "*a" * 200 + "*b"
    assert not allowed_paths.matches(pattern, "/" + "a" * 4000)


@pytest.mark.parametrize("target", ["/w/out/x.py", "/w/out/a/x.py", "/w/out/a/b/c/x.py"])
def test_adjacent_double_star_segments_still_match_zero_directories(target: str) -> None:
    assert allowed_paths.matches("/w/out/**/**/x.py", target)
    assert allowed_paths.matches("/w/out/**/**/**/x.py", target)


def _win_cover(entry: str, path: str, cwd: str = "C:\\work\\proj") -> bool:
    parsed = allowed_paths.parse(entry, windows=True)
    assert parsed is not None, entry
    anchored = allowed_paths.anchor(parsed, cwd, "C:\\Users\\me", windows=True)
    assert anchored is not None, entry
    return allowed_paths.covered([anchored], path, windows=True)


@pytest.mark.parametrize(
    ("entry", "path", "covered"),
    [
        ("out", "C:\\work\\proj\\out\\report.md", True),
        ("C:/work/proj/out", "C:\\work\\proj\\out\\notes.md", True),
        ("C:\\work\\proj\\out", "c:\\WORK\\Proj\\OUT\\notes.md", True),
        ("~/out", "C:\\Users\\me\\out\\stale.txt", True),
        ("out\\*.md", "C:\\work\\proj\\out\\a.md", True),
        ("out", "C:\\work\\proj\\elsewhere.md", False),
        ("out", "D:\\work\\proj\\out\\report.md", False),
        ("~/out", "C:\\Users\\other\\out\\stale.txt", False),
    ],
)
def test_windows_paths_are_read_like_kiro_cli_reads_them(
    entry: str, path: str, covered: bool
) -> None:
    """``\\`` is a separator there, a drive root is absolute, and case does not
    tell two files apart."""
    assert _win_cover(entry, path) is covered


def test_an_odd_windows_entry_covers_nothing() -> None:
    assert allowed_paths.parse("%USERPROFILE%\\out", windows=True) is None
    assert allowed_paths.parse("$env:USERPROFILE\\out", windows=True) is None
    assert allowed_paths.parse("\\\\srv\\share", windows=True) is None
    climbed = allowed_paths.parse("../../../D:/sec", windows=True)
    assert climbed is not None
    assert allowed_paths.anchor(climbed, "C:\\work\\proj", "C:\\h", windows=True) is None


def test_windows_case_folding_is_ascii_only() -> None:
    """NTFS keeps ``docſ`` (U+017F) and ``docs`` apart."""
    assert not _win_cover("docs", "C:\\work\\proj\\doc\u017f\\x")
    assert _win_cover("docs", "C:\\work\\proj\\DOCS\\x")


def test_posix_reading_does_not_take_a_drive_path_as_absolute() -> None:
    assert allowed_paths.parse("C:\\work\\out", windows=False) is None
    assert allowed_paths.anchor("out", "C:\\work", "/h", windows=False) is None
    assert not allowed_paths.matches("/w/out", "C:\\w\\out\\a.txt", windows=False)


def test_anchoring_does_no_filesystem_work(monkeypatch) -> None:
    """It runs on the event loop at session start and on a mode switch."""

    def _refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("filesystem touched while anchoring")

    import inspect

    # The module never reaches the resolver, and nothing it calls touches the disk.
    assert "paths_resolver" not in inspect.getsource(kas_write_allowlist)
    with monkeypatch.context() as mp:
        mp.setattr(os.path, "realpath", _refuse)
        mp.setattr(os, "lstat", _refuse)
        anchored = kas_write_allowlist.anchor_write_allowlist(["out", "~/x", "/abs/*"], "/w")
    home = allowed_paths.to_glob_path(os.path.expanduser("~")).rstrip("/")
    assert anchored == ("/w/out", home + "/x", "/abs/*")


def test_a_home_entry_stays_under_home() -> None:
    assert allowed_paths.anchor("~//etc", "/w", "/h") == "/h/etc"
    assert allowed_paths.anchor("~/x/*", "/w", "/h") == "/h/x/*"


def test_a_dot_segment_after_a_wildcard_covers_nothing() -> None:
    assert allowed_paths.anchor("out/*/../x", "/w", "/h") is None
    assert allowed_paths.anchor("out/../x/*", "/w", "/h") == "/w/x/*"
