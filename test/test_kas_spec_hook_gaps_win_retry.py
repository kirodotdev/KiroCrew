"""A momentary Windows share-deny hold on ``hooks.json`` is retried.

The saved-hooks read a store-less ``kirocrew run`` task does goes through
``atomic_write.read_bytes_with_retry``, the helper the other saved-state readers
use, so a foreign process holding ``hooks.json`` without read sharing for a few
hundred ms does not fail the gate closed for a call no hook covers. The retry
fires only on Windows, only for ``PermissionError``, and only off the event
loop; a hold that outlasts the budget still refuses.

A sibling of :mod:`test_kas_spec_hook_gaps`; see that module for the rest of the
store-less saved-hooks gate behaviour.
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

import kiro_crew.config.paths as paths_mod
import kiro_crew.hooks as hooks_mod
from kiro_crew.acp import kas_agents
from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHook, ScriptHookStore


@pytest.fixture
def agents_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: d)
    return d


def _write_spec(agents_dir: Path, name: str, **fields) -> None:
    spec = {"name": name, "prompt": "p", **fields}
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _save_page_hook(config_dir, matcher: str) -> None:
    store = ScriptHookStore(config_dir=config_dir)
    store._hooks = {
        "saved": ScriptHook(id="saved", event=HOOK_EVENT_PRE_TOOL_USE, matcher=matcher, command="s")
    }
    store._save()


class _ShareDeny:
    """A ``Path.read_bytes`` replacement that refuses ``hooks.json`` for the first
    ``fail_times`` reads, then reads normally.

    ``fail_times=None`` is a persistent denial: every read of ``hooks.json``
    raises, as a read-deny ACL would. ``patch`` is a plain function so that,
    assigned onto ``Path.read_bytes``, ``some_path.read_bytes()`` binds the Path
    as its first argument (a bound method would not); ``attempts`` counts the
    reads the gate actually drove.
    """

    def __init__(self, fail_times):
        self._fail_times = fail_times
        self._seen = 0
        self.attempts = 0
        self._real_bytes = Path.read_bytes
        self._real_text = Path.read_text
        state = self

        def _deny(this) -> bool:
            if this.name != "hooks.json":
                return False
            state.attempts += 1
            if state._fail_times is None or state._seen < state._fail_times:
                state._seen += 1
                return True
            return False

        def patch_bytes(this, *a, **k):
            if _deny(this):
                # errno 13 is the shape a Windows share-mode refusal raises.
                raise PermissionError(13, "Permission denied", str(this))
            return state._real_bytes(this, *a, **k)

        def patch_text(this, *a, **k):
            if _deny(this):
                raise PermissionError(13, "Permission denied", str(this))
            return state._real_text(this, *a, **k)

        self.patch_bytes = patch_bytes
        self.patch_text = patch_text


def _no_sleep_clock():
    """A stand-in for ``atomic_write``'s ``time`` binding that stubs only
    ``sleep`` and passes every other name through to the real module.

    Rebinding the module's own ``time`` name (not mutating ``time.sleep``) keeps
    the stub inside ``atomic_write`` instead of replacing ``sleep`` for every
    thread in the worker.
    """
    import time as _real_time

    clock = types.SimpleNamespace()
    for name in dir(_real_time):
        if not name.startswith("__"):
            setattr(clock, name, getattr(_real_time, name))
    clock.sleep = lambda _s: None
    return clock


def _arm_windows_retry(monkeypatch, deny: "_ShareDeny") -> None:
    """Patch the OS facts the retry turns on: Windows, no event loop, no sleep.

    Both ``read_bytes`` (the read boundary the fix uses) and ``read_text`` carry
    the same share-deny, so the same test is a faithful red on a bare-``read_text``
    base -- where the read refuses with no retry -- and green here, where the
    ``read_bytes_with_retry`` boundary clears the hold within budget.
    """
    from kiro_crew import atomic_write, platform_compat

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(atomic_write.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(atomic_write, "time", _no_sleep_clock())
    monkeypatch.setattr(Path, "read_bytes", deny.patch_bytes)
    monkeypatch.setattr(Path, "read_text", deny.patch_text)


def test_a_transient_hold_on_hooks_json_is_retried_so_the_gate_does_not_refuse(
    tmp_path, monkeypatch
):
    """Criterion 1: with no store and ``hooks.json`` held share-deny for a few
    attempts, the gate returns ``None`` for a call no saved hook covers. A bare
    read refuses with the ``could not be read`` reason; here the retry clears the
    hold within the shared budget."""
    from kiro_crew.config import loader

    monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(hooks_mod, "_global_script_hook_store", None)
    monkeypatch.setattr(hooks_mod, "get_global_hook_store", lambda: None)
    _save_page_hook(tmp_path, "probe_tool")  # does NOT cover "other_tool"

    deny = _ShareDeny(fail_times=3)
    _arm_windows_retry(monkeypatch, deny)

    reason = asyncio.run(hooks_mod.permission_pre_tool_block(None, [], None, "Running: other_tool"))
    assert reason is None
    assert deny.attempts >= 4  # 3 refusals then a clean read


def test_a_transient_hold_on_hooks_json_is_retried_for_the_matcher_projection(
    tmp_path, agents_dir, monkeypatch
):
    """Criterion 2: under the same hold, ``pre_tool_hook_matchers`` (called off
    the event loop by ``build_kas_custom_agents``) returns the saved matchers,
    not the fail-closed ``("*",)``."""
    from kiro_crew.config import loader

    monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(hooks_mod, "_global_script_hook_store", None)
    monkeypatch.setattr(hooks_mod, "get_global_hook_store", lambda: None)
    monkeypatch.setattr(kas_agents, "get_global_hook_store", lambda: None)
    _save_page_hook(tmp_path, "probe_tool")
    _write_spec(agents_dir, "a1", allowedTools=["probe_tool"])
    spec = kas_agents.load_agent_spec(agents_dir, "a1")

    deny = _ShareDeny(fail_times=3)
    _arm_windows_retry(monkeypatch, deny)

    assert kas_agents.pre_tool_hook_matchers("a1", spec) == ("probe_tool",)
    assert deny.attempts >= 4


def test_a_persistent_hold_on_hooks_json_still_refuses_the_gate(tmp_path, monkeypatch):
    """Criterion 3: a ``hooks.json`` that stays unreadable past the budget (a
    read-deny ACL, not a transient scan) still raises, so the gate fails closed
    with the same text, on Windows and off."""
    from kiro_crew.config import loader

    monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(hooks_mod, "_global_script_hook_store", None)
    monkeypatch.setattr(hooks_mod, "get_global_hook_store", lambda: None)
    _save_page_hook(tmp_path, "probe_tool")

    deny = _ShareDeny(fail_times=None)  # never clears
    _arm_windows_retry(monkeypatch, deny)

    reason = asyncio.run(hooks_mod.permission_pre_tool_block(None, [], None, "Running: other_tool"))
    assert reason is not None and "could not be read" in reason


def test_a_persistent_hold_still_withholds_every_approval(tmp_path, agents_dir, monkeypatch):
    """Criterion 3, projection side: a persistent denial still makes
    ``pre_tool_hook_matchers`` fail closed to ``("*",)``."""
    from kiro_crew.config import loader

    monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(hooks_mod, "_global_script_hook_store", None)
    monkeypatch.setattr(hooks_mod, "get_global_hook_store", lambda: None)
    monkeypatch.setattr(kas_agents, "get_global_hook_store", lambda: None)
    _save_page_hook(tmp_path, "probe_tool")
    _write_spec(agents_dir, "a1", allowedTools=["probe_tool"])
    spec = kas_agents.load_agent_spec(agents_dir, "a1")

    deny = _ShareDeny(fail_times=None)
    _arm_windows_retry(monkeypatch, deny)

    assert kas_agents.pre_tool_hook_matchers("a1", spec) == ("*",)


def test_a_posix_permission_error_is_not_retried(tmp_path, monkeypatch):
    """A ``PermissionError`` on POSIX is a genuine access fault, re-raised at
    once rather than slept over: the gate fails closed on the first attempt."""
    from kiro_crew import atomic_write, platform_compat
    from kiro_crew.config import loader

    monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(hooks_mod, "_global_script_hook_store", None)
    monkeypatch.setattr(hooks_mod, "get_global_hook_store", lambda: None)
    _save_page_hook(tmp_path, "probe_tool")

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(atomic_write.platform_compat, "IS_WINDOWS", False)
    deny = _ShareDeny(fail_times=3)
    monkeypatch.setattr(Path, "read_bytes", deny.patch_bytes)

    reason = asyncio.run(hooks_mod.permission_pre_tool_block(None, [], None, "Running: other_tool"))
    assert reason is not None and "could not be read" in reason
    assert deny.attempts == 1  # no retry on POSIX
