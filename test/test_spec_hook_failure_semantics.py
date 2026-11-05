"""End-to-end tests for an agent spec's own hooks on the KAS backend.

On KAS, Crew runs a spec's ``hooks`` in the harness's place. Two reports from 0.11
users drive these tests, each through the real loader, the real hook spawn and
the real PreToolUse gate:

* A user-level PreToolUse telemetry hook whose command matched a shell deny
  rule was refused, reported as exit 2, and so blocked every tool call. The hook
  is operator-written into the read-only user-level agents tree; it must run as
  kiro-cli runs it, and the tool must go ahead.
* A PreToolUse guard hook killed by endpoint security (exit -9) blocked every
  tool call. kiro-cli warns on any exit but 2 and lets the tool run; so must Crew.

A delivered exit 2 still blocks, and a Hooks-page hook keeps failing closed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.config.paths as paths_mod
import kiro_crew.hooks as hooks_mod
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHook, ScriptHookStore

_AGENT = "a1"


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


@pytest.fixture(autouse=True)
def _passthrough_sandbox(_floor_monkeypatch):
    # A plain shell spawn, so each assertion turns on the deny gate and the exit
    # code, not on the host's sandbox backend.
    _floor_monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))
    _floor_monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: list(argv))


@pytest.fixture
def agents_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: d)
    return d


def _witness(tmp_path: Path, name: str) -> tuple[str, Path]:
    """A command that creates a file when it runs, and that file."""
    sentinel = tmp_path / f"{name}.ran"
    script = tmp_path / f"{name}.py"
    script.write_text(f"open({str(sentinel)!r}, 'w').close()\n", encoding="utf-8")
    return f'"{sys.executable}" "{script}"', sentinel


def _deny_listed(tmp_path: Path, name: str) -> tuple[str, Path]:
    """A harmless command that a default shell deny rule (``chmod 777``) refuses."""
    witness, sentinel = _witness(tmp_path, name)
    probe = tmp_path / "probe"
    probe.write_text("", encoding="utf-8")
    return f"{witness} && chmod 777 {probe}", sentinel


def _write_spec(agents_dir: Path, command: str) -> None:
    spec = {"name": _AGENT, "prompt": "p", "hooks": {"preToolUse": [{"command": command}]}}
    (agents_dir / f"{_AGENT}.json").write_text(json.dumps(spec), encoding="utf-8")


def _gate(tmp_path: Path, store: ScriptHookStore) -> str | None:
    """Load the agent's spec hooks as a KAS turn does, then run the PreToolUse gate."""
    provider = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd=str(tmp_path))
    turn = asyncio.run(spec_hooks.turn_spec_hooks(provider, _AGENT))
    assert not turn.unreadable and turn.gated and turn.hooks
    return asyncio.run(
        hooks_mod.permission_pre_tool_block(store, turn.hooks, turn.cwd, "Running: ReadFile")
    )


def test_a_deny_listed_user_spec_hook_runs_and_the_tool_goes_ahead(tmp_path, agents_dir):
    from kiro_crew.security import is_denied

    command, sentinel = _deny_listed(tmp_path, "spec")
    assert is_denied(command), "precondition: the command matches a default deny rule"
    _write_spec(agents_dir, command)
    assert _gate(tmp_path, ScriptHookStore(config_dir=tmp_path)) is None
    assert sentinel.exists(), "the user-level spec hook must run, as on kiro-cli"


def test_a_governance_pinned_rule_still_refuses_a_user_spec_hook(tmp_path, agents_dir, monkeypatch):
    # The exemption covers the operator's own deny rules, not an administrator's
    # pin: a pinned rule refuses the user-level spec hook, so its command never
    # runs. A refused spec hook warns, so the tool itself still goes ahead.
    monkeypatch.setattr(
        "kiro_crew.security.pinned_builtin_command_ids",
        lambda: {"local-destructive-chmod-777"},
    )
    command, sentinel = _deny_listed(tmp_path, "pinned")
    _write_spec(agents_dir, command)
    assert _gate(tmp_path, ScriptHookStore(config_dir=tmp_path)) is None
    assert not sentinel.exists(), "a governance-pinned command must not run"


def test_the_same_command_as_a_hooks_page_hook_is_still_refused(tmp_path, agents_dir):
    # hooks.json is writable in-sandbox, so a stored hook stays deny-list vetted
    # and fail-closed: it does not run, and the tool is blocked with the reason.
    _write_spec(agents_dir, _witness(tmp_path, "benign")[0])
    command, sentinel = _deny_listed(tmp_path, "page")
    store = ScriptHookStore(config_dir=tmp_path)
    store._hooks = {
        "page": ScriptHook(id="page", name="page", event=HOOK_EVENT_PRE_TOOL_USE, command=command)
    }
    reason = _gate(tmp_path, store)
    assert reason is not None and "security policy" in reason.lower()
    assert not sentinel.exists()


def test_a_hooks_page_hook_cannot_pass_as_a_spec_hook_by_its_id(tmp_path, agents_dir):
    # The spec-hook rules key on an in-memory origin, never on the id a
    # hand-written hooks.json controls.
    _write_spec(agents_dir, _witness(tmp_path, "benign")[0])
    command, sentinel = _deny_listed(tmp_path, "forged")
    store = ScriptHookStore(config_dir=tmp_path)
    forged = ScriptHook.from_dict(
        {
            "id": f"spec:{_AGENT}:PreToolUse:0",
            "event": HOOK_EVENT_PRE_TOOL_USE,
            "command": command,
            "spec_origin": "user",
        }
    )
    store._hooks = {forged.id: forged}
    assert _gate(tmp_path, store) is not None
    assert not sentinel.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal")
def test_a_killed_spec_hook_does_not_block_the_tool(tmp_path, agents_dir):
    # Endpoint security killing the hook process leaves exit -9.
    _write_spec(agents_dir, "kill -9 $$")
    assert _gate(tmp_path, ScriptHookStore(config_dir=tmp_path)) is None


def test_a_crashing_spec_hook_does_not_block_the_tool(tmp_path, agents_dir):
    _write_spec(agents_dir, "exit 1")
    assert _gate(tmp_path, ScriptHookStore(config_dir=tmp_path)) is None


def test_a_spec_hook_that_exits_2_still_blocks(tmp_path, agents_dir):
    # ``&&`` chains in both ``/bin/sh -c`` and Windows ``cmd /c``; ``;`` is sh-only.
    _write_spec(agents_dir, "echo no-reads 1>&2 && exit 2")
    reason = _gate(tmp_path, ScriptHookStore(config_dir=tmp_path))
    assert reason is not None and "no-reads" in reason


def test_a_spec_linked_in_from_outside_the_agents_dir_stays_vetted(tmp_path, monkeypatch):
    from kiro_crew.agent_sdk.spec_hooks import _user_dir_origin
    from kiro_crew.config import paths

    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: agents)
    mates = tmp_path / "mates"
    mates.mkdir()
    (mates / "conductor.json").write_text("{}")
    assert _user_dir_origin(mates) == "project"  # a sibling tree is not sealed
    (agents / "plain.json").write_text("{}")
    assert _user_dir_origin(agents) == "user"

    # Any symlinked spec stays vetted, even one resolving inside the tree: a link
    # it passes through could be retargeted after the spec is read.
    (agents / "inner.json").symlink_to(agents / "plain.json")
    assert _user_dir_origin(agents) == "project"
    (agents / "inner.json").unlink()

    outside = tmp_path / "checkout" / "linked.json"
    outside.parent.mkdir()
    outside.write_text("{}")
    (agents / "linked.json").symlink_to(outside)
    assert _user_dir_origin(agents) == "project"
