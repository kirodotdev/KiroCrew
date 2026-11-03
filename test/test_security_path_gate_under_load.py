"""The sensitive-path gate under CPU load: refusals are earned by the filesystem, not the GIL.

Under load, the thread that asked the resolver child for a ``realpath`` can get the GIL
back after its deadline although the child answered in time, and an off-loop thread
spends most of a contended wait waiting for the GIL.  Neither says anything about the
filesystem, so neither may refuse a plain file or charge its prefix.  What must still
hold is pinned beside it: a sensitive link is refused as a match however late its
answer was read, and a child wedged in a stat syscall still charges the prefix and
refuses every later path under it without a probe, from any thread.

The late answer is produced by :class:`LateAnswerClock`, which jumps the executor's
clock past the deadline once the child's answer is in the pipe; the pool's children
are real.  The wedged child is a double, because no real mount can be wedged here.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from path_gate_late_answer_helpers import LateAnswerClock

import kiro_crew.executors as ex
from kiro_crew import security
from kiro_crew.security import paths
from kiro_crew.subprocess_pool import SubprocessPoolTimeout
from kiro_crew.subprocess_pool import executor as executor_mod
from kiro_crew.testing.wait import wait_until

needs_select = pytest.mark.skipif(
    not executor_mod._CAN_SELECT_PIPES, reason="the per-read deadline needs select on a pipe"
)


@pytest.fixture(autouse=True)
def _fresh_gate_state(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Empty stall, load-probe and allowance books, and a home under *tmp_path*."""
    _floor_monkeypatch.setattr(paths, "_path_resolve_degraded", {})
    _floor_monkeypatch.setattr(paths, "_path_resolve_load_probes", {})
    _floor_monkeypatch.setattr(paths, "_path_resolve_thread_waits", {})
    home = tmp_path / "home"
    home.mkdir()
    _floor_monkeypatch.setenv("HOME", str(home))
    _floor_monkeypatch.setenv("USERPROFILE", str(home))
    security._home_targets_cache.clear()
    yield
    security._home_targets_cache.clear()
    # Drop the resolver pool so this file's children cannot reach the next test.
    ex.shutdown_maintenance_executor()


def _plain_file(tmp_path: Path) -> str:
    target = tmp_path / "ws" / "notes.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hello", encoding="utf-8")
    return str(target)


def _sensitive_link(tmp_path: Path) -> str:
    key = tmp_path / "home" / ".ssh" / "id_rsa"
    key.parent.mkdir(parents=True)
    key.write_text("not a key", encoding="utf-8")
    link = tmp_path / "ws" / "notes.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(key)
    return str(link)


def _pool_ready() -> None:
    """Wait for the reaper to start every resolver child, so no call waits on a lease."""
    pool = ex.path_resolve_executor()
    wait_until(
        lambda: all(child.proc is not None for child in pool._children),
        timeout=30.0,
        describe=lambda: "the reaper never started every resolver child",
    )


def _spend_allowance() -> None:
    """Record this thread's cumulative wait allowance as spent for the next 100 s."""
    paths._path_resolve_thread_waits[threading.get_ident()] = (
        paths._path_resolve_clock() + 100.0,
        paths._PATH_RESOLVE_WAIT_CAP_SECS + 1.0,
    )


def _on_loop(fn, *args):
    """Call *fn* from inside a running event loop on this thread."""

    async def _call():
        return fn(*args)

    return asyncio.run(_call())


@needs_select
def test_late_answer_never_charges_the_prefix(tmp_path: Path, monkeypatch) -> None:
    target = _plain_file(tmp_path)
    _pool_ready()
    clock = LateAnswerClock(monkeypatch)
    for _ in range(paths._PATH_RESOLVE_LOAD_MAX_PROBES + 1):
        assert security.sensitive_path_refusal(target) is None
    assert clock.late_reads >= paths._PATH_RESOLVE_LOAD_MAX_PROBES + 1
    assert paths._path_resolve_degraded == {}, "a late read of a real answer charged a prefix"


def test_off_loop_caller_has_no_allowance(tmp_path: Path) -> None:
    target = _plain_file(tmp_path)
    _pool_ready()
    _spend_allowance()
    assert security.sensitive_path_refusal(target) is None

    # The same thread, spent the same way, but running an event loop: the
    # allowance still refuses there, and without charging the prefix.
    reason = _on_loop(security.sensitive_path_refusal, target)
    assert reason is not None and security.is_unverifiable_path_refusal(reason)
    assert paths._path_resolve_degraded == {}


@needs_select
@pytest.mark.parametrize("when", ["late_answer", "off_loop_allowance_spent"])
def test_sensitive_link_refused_when_answer_is_late(tmp_path: Path, monkeypatch, when) -> None:
    link = _sensitive_link(tmp_path)
    _pool_ready()
    if when == "late_answer":
        LateAnswerClock(monkeypatch)
    else:
        _spend_allowance()
    reason = security.sensitive_path_refusal(link)
    assert reason is not None
    assert not security.is_unverifiable_path_refusal(reason), reason
    assert "access to sensitive path" in reason


class _WedgedPool:
    """A resolver pool whose child takes every request and misses the deadline in lstat."""

    def __init__(self) -> None:
        self.calls = 0
        # A syscall the resolver reads as "blocked in the filesystem" on this host's
        # architecture; on an unmapped one every sample reads that way.
        syscall = min(paths._FS_BLOCKING_SYSCALLS) if paths._FS_BLOCKING_SYSCALLS else 6
        self._sample = str(syscall).encode("ascii")

    def call_op(self, op: int, payload: bytes, timeout: float | None = None) -> bytes:
        self.calls += 1
        raise SubprocessPoolTimeout("stub child wedged", child_syscall=self._sample)


def test_wedged_child_still_charges_off_loop(tmp_path: Path, monkeypatch) -> None:
    first = _plain_file(tmp_path)
    second = os.path.join(os.path.dirname(first), "other.md")
    pool = _WedgedPool()
    monkeypatch.setattr(paths, "path_resolve_executor", lambda: pool)
    # On the loop this thread would have no allowance left; off the loop it is
    # probed, and the wedge it meets is charged.
    _spend_allowance()

    reason = security.sensitive_path_refusal(first)
    assert reason is not None and security.is_unverifiable_path_refusal(reason)
    prefix = paths._stall_prefix(os.path.expanduser(first))
    assert prefix in paths._path_resolve_degraded, "a wedged child did not charge its prefix"

    probes = pool.calls
    reason = security.sensitive_path_refusal(second)
    assert reason is not None and security.is_unverifiable_path_refusal(reason)
    assert pool.calls == probes, "a path under a charged prefix was probed again"


def test_the_tool_gate_judges_a_call_inside_one_resolver_budget(monkeypatch) -> None:
    # Every path a gate call names is checked inside one judgment, so off the loop a
    # call naming many slow mounts holds its thread for one budget, not one per path.
    from kiro_crew import hooks
    from kiro_crew.security import paths_resolver

    seen: list[float | None] = []

    def recording_refusal(path: str) -> str | None:
        seen.append(getattr(paths_resolver._judgment_budget, "left", None))
        return None

    # The tiers read the gate functions through the ``hooks`` facade.
    monkeypatch.setattr(hooks, "sensitive_path_refusal", recording_refusal)
    hooks.HookManager(hooks.HooksConfig.from_dict({})).on_tool_call(
        "Editing notes", tool_kind="edit", raw_params={"path": "/tmp/kc-notes.md"}
    )
    assert seen and all(left is not None for left in seen)
    assert getattr(paths_resolver._judgment_budget, "left", None) is None


def test_default_pool_has_eight_children(monkeypatch) -> None:
    # Pinned as a literal so that changing the shipped size has to edit a test.
    monkeypatch.delenv(ex._PATH_RESOLVE_WORKERS_ENV, raising=False)
    assert ex._PATH_RESOLVE_WORKERS_DEFAULT == 8
    assert ex._path_resolve_worker_count() == 8
