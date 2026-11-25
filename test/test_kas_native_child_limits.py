"""A sub-agent the KAS engine runs is held to its parent's limits.

A native KAS sub-agent's tool call shares the session's id, and the id its
``agentSubtaskId`` frame recorded marks it. Kiro Crew's gate enforces some of an
agent spec's limits itself (the shell allowlist, the write allowlist, the
``deniedCommands`` the engine cannot express): the session handle stamps them on
the sub-agent's request as it does on its own agent's, and stamps none of the
parent's grants. So for each limit, the parent's call and the sub-agent's call
are refused alike, and the sub-agent is never given a grant.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from test_kas_spec_builtin_grant import _handle, _permission
from test_tool_call_classification_kas import _kas_builtin_flow

from kiro_crew.acp import kas_agents, kas_write_allowlist
from kiro_crew.acp._dispatch import scoped_tool_cache_key
from kiro_crew.acp.kas_spec_denies import projected_spec_denies
from kiro_crew.acp.types import AcpEvent
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY, HookManager, HooksConfig, hook_gate_kwargs
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp

#: Every field the handle stamps on a request, copied onto the judged event.
_STAMPED = (
    "spec_granted_builtins",
    "spec_hook_held",
    "spec_granted_commands",
    "spec_shell_allowlist",
    "spec_write_allowlist",
    "spec_write_cwd",
    "spec_denied_grant",
    "spec_unjudged_denies",
    "spec_shell_readonly",
    "spec_granted_paths",
    "spec_granted_read_paths",
    "native_child_call",
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
    root = Path(os.path.realpath(tmp_path)) / "proj"
    (root / "out").mkdir(parents=True)
    return root


def _shell(command: str) -> AcpEvent:
    return _kas_builtin_flow(
        "run_command", title="Run Command", kind="execute", raw_input={"command": command}
    )


def _write(path: str) -> AcpEvent:
    return _kas_builtin_flow(
        "fs_write", title="Write", kind="edit", raw_input={"path": path, "command": "create"}
    )


def _requests(event: AcpEvent, settings: dict[str, Any], tools: list[str], cwd: Path):
    """*event* as the parent's own request and as a native sub-agent's, each stamped
    by a session handle whose agent's spec carries *settings*."""
    spec = {"name": "probe", "prompt": "p", "tools": tools, "toolsSettings": settings}
    entry = kas_agents.to_client_custom_agent("probe", spec, "p")
    out = []
    for child in (False, True):
        handle = _handle(ACP_BACKEND_KAS)
        handle._bound_cwd = str(cwd)
        handle.kas_spec_shell_allowlist = kas_agents.projected_shell_allowlist([entry], "probe")
        handle.kas_spec_shell_readonly = kas_agents.projected_shell_readonly([entry], "probe")
        handle.kas_spec_write_allowlist = kas_write_allowlist.projected_write_allowlist(
            [entry], "probe", str(cwd)
        )
        handle.kas_spec_denies = projected_spec_denies([entry], "probe", str(cwd))
        if child:
            handle._native_child_tool_call_ids.add(scoped_tool_cache_key("own-sid", "tc-1"))
        stamped = handle._build_permission_event(_permission("own-sid"))
        assert stamped is not None
        judged = AcpEvent(**{**event.__dict__})
        for name in _STAMPED:
            setattr(judged, name, getattr(stamped, name))
        out.append(judged)
    parent, child = out
    assert child.native_child_call is True and parent.native_child_call is not True
    return parent, child


def _judge(event: AcpEvent):
    return HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))


_SHELL_ALLOWLIST = {"shell": {"denyByDefault": True, "allowedCommands": ["git status"]}}


def test_a_command_outside_the_parents_shell_allowlist_is_refused_for_the_sub_agent(work):
    parent, child = _requests(_shell("ls /"), _SHELL_ALLOWLIST, ["execute_bash"], work)
    for event in (parent, child):
        verdict = _judge(event)
        assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-allowlist")


def test_the_read_only_flag_beside_the_allowlist_reaches_the_sub_agent_as_it_is(work):
    settings = {"shell": {**_SHELL_ALLOWLIST["shell"], "autoAllowReadonly": True}}
    parent, child = _requests(_shell("ls /"), settings, ["execute_bash"], work)
    assert parent.spec_shell_readonly is True and child.spec_shell_readonly is True
    for event in (parent, child):
        assert _judge(event).tier != "spec-shell-allowlist"


def test_a_write_outside_the_parents_write_allowlist_is_refused_for_the_sub_agent(work):
    settings = {"fs_write": {"fallbackAction": "deny", "allowedPaths": ["out"]}}
    event = _write(str(work / "elsewhere.txt"))
    parent, child = _requests(event, settings, ["fs_write"], work)
    for judged in (parent, child):
        verdict = _judge(judged)
        assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-write-allowlist")


def test_a_denied_command_the_engine_cannot_express_is_refused_for_the_sub_agent(work):
    """A regex deny the engine cannot carry as a glob lives only at Kiro Crew's gate."""
    settings = {"shell": {"allowedCommands": ["make build", "make clean.*"]}}
    settings["shell"]["deniedCommands"] = ["make clean"]
    parent, child = _requests(_shell("make clean now"), settings, ["execute_bash"], work)
    for event in (parent, child):
        verdict = _judge(event)
        assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-denies")


def test_the_sub_agent_is_given_none_of_the_parents_grants(work):
    settings = {"shell": {"allowedCommands": ["make build"], "deniedCommands": ["make clean"]}}
    parent, child = _requests(_shell("make build"), settings, ["execute_bash"], work)
    assert _judge(parent).action == TOOL_AUTO_APPROVE
    assert child.spec_granted_builtins == frozenset()
    assert child.spec_granted_commands == ()
    assert child.spec_granted_paths == () and child.spec_granted_read_paths == ()
    assert child.spec_denied_grant is not None
    assert child.spec_denied_grant.builtins == frozenset()
    assert child.spec_denied_grant.commands == ()
    assert child.spec_denied_grant.denied_commands == parent.spec_denied_grant.denied_commands
    assert _judge(child).action != TOOL_AUTO_APPROVE
