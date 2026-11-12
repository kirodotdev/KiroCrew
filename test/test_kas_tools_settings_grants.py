"""A kiro-cli agent's ``toolsSettings`` grants on KAS: what ran unasked still runs unasked.

On kiro-cli a shell command an ``allowedCommands`` entry matches runs without a
prompt, whatever ``denyByDefault`` and ``autoAllowReadonly`` say beside it; with
``autoAllowReadonly: true`` a read-only command runs unasked even under
``denyByDefault``; and a write every target of which a write ``allowedPaths``
entry covers runs unasked. KAS reads none of these keys, so Crew carries them:
the projection records them, the session handle stamps them on its own agent's
permission requests, and the gate answers (or lets past) what the spec grants,
after every deny row. These tests drive the real projection, the real session
record and the real gate on recorded-shape KAS requests.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from test_kas_spec_builtin_grant import _handle, _permission
from test_tool_call_classification_kas import _kas_builtin_flow

from kiro_crew.acp import kas_agents
from kiro_crew.acp._dispatch import attach_kas_custom_agents
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.kas_permissions import (
    CREW_ANSWERED_PATHS_KEY,
    CREW_ANSWERED_READ_PATHS_KEY,
    CREW_SHELL_READONLY_KEY,
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
from kiro_crew.testing.links import make_dir_link

#: The fields the session handle stamps on its own agent's KAS request.
_STAMPED = (
    "spec_granted_builtins",
    "spec_granted_commands",
    "spec_shell_allowlist",
    "spec_write_allowlist",
    "spec_write_cwd",
    "spec_shell_readonly",
    "spec_granted_paths",
    "spec_granted_read_paths",
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
    """The session's directory, with an ``out`` folder inside it. Resolved, so a
    symlinked temp root does not change what the test spells."""
    root = Path(os.path.realpath(tmp_path)) / "proj"
    (root / "out").mkdir(parents=True)
    return root


def _spec(settings: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "name": "probe",
        "prompt": "p",
        "tools": ["execute_bash", "fs_read", "fs_write"],
        "toolsSettings": settings,
        **extra,
    }


def _own_request(event: AcpEvent, spec: dict[str, Any], cwd: Path) -> AcpEvent:
    """*event* as the session handle stamps its own agent's request, after the
    harness recorded the batch projected from *spec* for a session at *cwd*."""
    entry = kas_agents.to_client_custom_agent("probe", spec, "p")
    handle = _handle(ACP_BACKEND_KAS)
    handle._bound_cwd = str(cwd)
    KasHarness.record_session_projection(object.__new__(KasHarness), handle, [entry], "probe")
    stamped = handle._build_permission_event(_permission("own-sid"))
    assert stamped is not None
    for field in _STAMPED:
        setattr(event, field, getattr(stamped, field))
    return event


def _shell(command: str) -> AcpEvent:
    return _kas_builtin_flow(
        "run_command", title="Run Command", kind="execute", raw_input={"command": command}
    )


def _write(path: str, *, tool: str = "fs_write") -> AcpEvent:
    raw = {"targetFile": path} if tool == "delete_file" else {"path": path, "command": "create"}
    return _kas_builtin_flow(tool, title="Write", kind="edit", raw_input=raw)


def _judge(event: AcpEvent):
    return HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))


def _notice_keys(spec: dict[str, Any]) -> list[str]:
    return kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True)


# ── shell.allowedCommands beside denyByDefault and autoAllowReadonly ─────────

#: The reported shell entry, less the deny list: an allowlist, deny by default,
#: read-only commands allowed.
_SHELL = {
    "allowedCommands": ["kubectl get .*", "\\./scripts/report\\.sh.*"],
    "denyByDefault": True,
    "autoAllowReadonly": True,
}


@pytest.mark.parametrize("key", ["execute_bash", "shell"])
@pytest.mark.parametrize("command", ["kubectl get pods -n web", "./scripts/report.sh --today"])
def test_an_allowed_command_runs_without_a_prompt(work: Path, key: str, command: str) -> None:
    verdict = _judge(_own_request(_shell(command), _spec({key: dict(_SHELL)}), work))
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


@pytest.mark.parametrize("sibling", [{"denyByDefault": True}, {"autoAllowReadonly": True}])
def test_each_sibling_key_alone_keeps_the_grant(work: Path, sibling: dict[str, Any]) -> None:
    settings = {"execute_bash": {"allowedCommands": ["kubectl get .*"], **sibling}}
    verdict = _judge(_own_request(_shell("kubectl get pods"), _spec(settings), work))
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


def test_an_unlisted_command_stays_refused(work: Path) -> None:
    verdict = _judge(
        _own_request(_shell("kubectl delete pod web-1"), _spec({"shell": dict(_SHELL)}), work)
    )
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-allowlist")


def test_a_denied_command_stays_denied_and_withholds_the_grant(work: Path) -> None:
    """A ``deniedCommands`` entry travels as a KAS deny rule, and it still holds
    the ``allowedCommands`` grant back: this change reads no deny any wider."""
    settings = {"shell": {**_SHELL, "deniedCommands": ["kubectl get secrets.*"]}}
    entry = kas_agents.to_client_custom_agent("probe", _spec(settings), "p")
    rules = entry["permissions"]["rules"]
    assert {"capability": "shell", "match": ["kubectl get secrets*"], "effect": "deny"} in rules
    verdict = _judge(_own_request(_shell("kubectl get pods"), _spec(settings), work))
    assert verdict.action != TOOL_AUTO_APPROVE


def test_a_malformed_auto_allow_readonly_still_withholds_the_grant(work: Path) -> None:
    settings = {"shell": {"allowedCommands": ["kubectl get .*"], "autoAllowReadonly": "yes"}}
    verdict = _judge(_own_request(_shell("kubectl get pods"), _spec(settings), work))
    assert verdict.action != TOOL_AUTO_APPROVE
    assert "toolsSettings.shell.autoAllowReadonly" in _notice_keys(_spec(settings))


# ── autoAllowReadonly under denyByDefault ─────────────────────────────────────


@pytest.mark.parametrize("command", ["grep -rn TODO src", "ls -la", "cat README.md"])
def test_a_read_only_command_runs_under_deny_by_default(work: Path, command: str) -> None:
    verdict = _judge(_own_request(_shell(command), _spec({"shell": dict(_SHELL)}), work))
    assert verdict.action == TOOL_AUTO_APPROVE and verdict.read_only


def test_without_auto_allow_readonly_a_read_only_command_stays_refused(work: Path) -> None:
    settings = {"shell": {"allowedCommands": ["kubectl get .*"], "denyByDefault": True}}
    verdict = _judge(_own_request(_shell("grep -rn TODO src"), _spec(settings), work))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-allowlist")


@pytest.mark.parametrize("command", ["rm -rf out", "grep x > out/y", "git status"])
def test_a_command_that_is_not_read_only_stays_refused(work: Path, command: str) -> None:
    verdict = _judge(_own_request(_shell(command), _spec({"shell": dict(_SHELL)}), work))
    assert verdict.action == TOOL_DENY


def test_a_deny_kas_cannot_carry_keeps_read_only_commands_refused(work: Path) -> None:
    """kiro-cli checks ``deniedCommands`` before ``autoAllowReadonly``. A deny with
    no KAS spelling does not travel, so the read-only exception is not recorded."""
    settings = {"shell": {**_SHELL, "deniedCommands": ["(cat|head) private\\.txt"]}}
    entry = kas_agents.to_client_custom_agent("probe", _spec(settings), "p")
    assert CREW_SHELL_READONLY_KEY not in entry
    verdict = _judge(_own_request(_shell("cat private.txt"), _spec(settings), work))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-allowlist")


def test_the_readonly_flag_is_recorded_only_beside_deny_by_default() -> None:
    with_deny = kas_agents.to_client_custom_agent("probe", _spec({"shell": dict(_SHELL)}), "p")
    assert with_deny[CREW_SHELL_READONLY_KEY] is True
    alone = kas_agents.to_client_custom_agent(
        "probe", _spec({"shell": {"autoAllowReadonly": True}}), "p"
    )
    assert CREW_SHELL_READONLY_KEY not in alone


# ── write.allowedPaths ────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["write", "fs_write"])
@pytest.mark.parametrize("tool", ["fs_write", "str_replace"])
def test_a_write_inside_allowed_paths_runs_without_a_prompt(
    work: Path, key: str, tool: str
) -> None:
    """``write`` is not in ``allowedTools``: on kiro-cli ``allowedPaths`` alone
    lets the write run unasked."""
    spec = _spec({key: {"allowedPaths": ["out/**"]}})
    verdict = _judge(_own_request(_write(str(work / "out" / "a.txt"), tool=tool), spec, work))
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


def test_a_write_outside_allowed_paths_still_asks(work: Path) -> None:
    spec = _spec({"write": {"allowedPaths": ["out/**"]}})
    verdict = _judge(_own_request(_write(str(work / "elsewhere.txt")), spec, work))
    assert verdict.action == TOOL_ALLOW


def test_a_delete_is_never_granted_by_allowed_paths(work: Path) -> None:
    spec = _spec({"write": {"allowedPaths": ["out/**"]}})
    event = _own_request(_write(str(work / "out" / "a.txt"), tool="delete_file"), spec, work)
    assert _judge(event).action != TOOL_AUTO_APPROVE


def test_a_denied_path_beside_allowed_paths_withholds_the_grant(work: Path) -> None:
    spec = _spec({"write": {"allowedPaths": ["out/**"], "deniedPaths": ["out/secret/**"]}})
    covered = _judge(_own_request(_write(str(work / "out" / "a.txt")), spec, work))
    assert covered.action != TOOL_AUTO_APPROVE


def test_fallback_deny_still_refuses_and_a_covered_write_runs(work: Path) -> None:
    spec = _spec({"write": {"allowedPaths": ["out/**"], "fallbackAction": "deny"}})
    outside = _judge(_own_request(_write(str(work / "elsewhere.txt")), spec, work))
    assert (outside.action, outside.tier) == (TOOL_DENY, "spec-write-allowlist")
    covered = _judge(_own_request(_write(str(work / "out" / "a.txt")), spec, work))
    assert (covered.action, covered.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


def test_a_write_through_a_symlink_out_of_allowed_paths_still_asks(work: Path) -> None:
    outside = work.parent / "outside"
    outside.mkdir()
    make_dir_link(work / "out" / "link", outside)
    spec = _spec({"write": {"allowedPaths": ["out/**"]}})
    verdict = _judge(_own_request(_write(str(work / "out" / "link" / "a.txt")), spec, work))
    assert verdict.action != TOOL_AUTO_APPROVE


def test_the_answered_paths_never_reach_the_wire() -> None:
    entry = kas_agents.to_client_custom_agent(
        "probe", _spec({"write": {"allowedPaths": ["out/**"]}, "shell": dict(_SHELL)}), "p"
    )
    assert entry[CREW_ANSWERED_PATHS_KEY] == ["out/**"]
    params: dict[str, Any] = {}
    attach_kas_custom_agents(params, [entry])
    wire = str(params)
    assert "_crewAnsweredPaths" not in wire and "_crewShellReadonly" not in wire


# ── read.allowedPaths ─────────────────────────────────────────────────────────


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    """A folder outside the session's directory, holding one file."""
    root = Path(os.path.realpath(tmp_path)) / "docs"
    root.mkdir()
    (root / "a.md").write_text("a")
    return root


def _read(path: str, *, tool: str = "read_file") -> AcpEvent:
    return _kas_builtin_flow(tool, title="Read", kind="read", raw_input={"path": path})


@pytest.mark.parametrize("key", ["read", "fs_read", "fsRead"])
def test_a_read_inside_allowed_paths_is_answered_by_the_grant(
    work: Path, docs: Path, key: str
) -> None:
    """``read`` is not in ``allowedTools``: on kiro-cli ``allowedPaths`` alone lets
    the read run unasked."""
    spec = _spec({key: {"allowedPaths": [f"{docs}/**"]}})
    verdict = _judge(_own_request(_read(str(docs / "a.md")), spec, work))
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


def test_a_relative_read_is_anchored_at_the_session_directory(work: Path) -> None:
    (work / "out" / "b.txt").write_text("b")
    spec = _spec({"read": {"allowedPaths": ["out/**"]}})
    verdict = _judge(_own_request(_read("out/b.txt"), spec, work))
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


@pytest.mark.parametrize(
    "case", ["outside", "missing", "folder", "listing", "tilde-user", "symlink-out", "denied"]
)
def test_a_read_the_entries_do_not_name_exactly_is_not_granted(
    work: Path, docs: Path, case: str
) -> None:
    """Each keeps main's answer: no ``spec-builtin-grant``. A missing file is not
    granted, because KAS's ``read_file`` would retry another name."""
    settings: dict[str, Any] = {"allowedPaths": [f"{docs}/**"]}
    event = _read(str(docs / "a.md"))
    if case == "outside":
        (work / "c.txt").write_text("c")
        event = _read(str(work / "c.txt"))
    elif case == "missing":
        event = _read(str(docs / "gone.md"))
    elif case == "folder":
        event = _read(str(docs))
    elif case == "listing":
        event = _read(str(docs), tool="list_directory")
    elif case == "tilde-user":
        event = _read("~root/a.md")
    elif case == "symlink-out":
        outside = work.parent / "outside"
        outside.mkdir()
        (outside / "s.md").write_text("s")
        make_dir_link(docs / "link", outside)
        event = _read(str(docs / "link" / "s.md"))
    else:
        settings["deniedPaths"] = [f"{docs}/secret/**"]
    verdict = _judge(_own_request(event, _spec({"read": settings}), work))
    assert verdict.tier != "spec-builtin-grant"


def test_a_read_grant_never_answers_a_write(work: Path) -> None:
    spec = _spec({"read": {"allowedPaths": ["out/**"]}})
    verdict = _judge(_own_request(_write(str(work / "out" / "a.txt")), spec, work))
    assert verdict.action != TOOL_AUTO_APPROVE


def test_the_read_paths_never_reach_the_wire_and_grep_is_not_recorded() -> None:
    entry = kas_agents.to_client_custom_agent(
        "probe",
        _spec({"read": {"allowedPaths": ["docs/**"]}, "grep": {"allowedPaths": ["x"]}}),
        "p",
    )
    assert entry[CREW_ANSWERED_READ_PATHS_KEY] == ["docs/**"]
    params: dict[str, Any] = {}
    attach_kas_custom_agents(params, [entry])
    assert "_crewAnsweredReadPaths" not in str(params)


def test_a_withheld_read_grant_is_named() -> None:
    spec = _spec({"read": {"allowedPaths": ["docs/**"], "deniedPaths": ["docs/x/**"]}})
    assert CREW_ANSWERED_READ_PATHS_KEY not in kas_agents.to_client_custom_agent("probe", spec, "p")
    assert "toolsSettings.read.allowedPaths" in _notice_keys(spec)


# ── the session-start notice ──────────────────────────────────────────────────


def test_the_notice_no_longer_names_what_is_now_carried() -> None:
    """The reported notice named ``allowedCommands``, ``autoAllowReadonly`` and
    ``write.allowedPaths``; each now runs as on kiro-cli."""
    spec = _spec(
        {
            "shell": dict(_SHELL),
            "write": {"allowedPaths": ["out/**"]},
            "read": {"allowedPaths": ["docs/**"]},
        }
    )
    assert _notice_keys(spec) == []


def test_the_notice_still_names_an_unusable_allowed_paths_entry() -> None:
    spec = _spec({"write": {"allowedPaths": ["out/[ab]/**"]}})
    assert "toolsSettings.write.allowedPaths" in _notice_keys(spec)
