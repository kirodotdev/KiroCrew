"""The one stale-spec retry starts its second child on a live runtime.

The post-handshake spec check fails inside the failed-start guard, whose reap is a
``kill()`` that marks the runtime dead. The retry runs on the same handle, so it
has to start from a runtime that can send requests again -- otherwise its own
``initialize`` is refused with "runtime is dead" and the stale spec that caused
the retry is never reported.

The spawn tests drive a real child (the packaged fake ACP backend) through the
real ``initialize`` handshake and the real reap. They replace the spawn plan, the
native skill projection and the post-handshake spec check; the pre-spawn check is
not on this path because the plan is replaced. The revive tests below them read
the state a failed start leaves on the handle directly.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import platform_compat, runtime_death
from kiro_crew.acp import skill_projection
from kiro_crew.acp.harness.base import SpawnPlan
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError
from kiro_crew.testing import fake_acp_backend

posix_only = pytest.mark.skipif(
    not platform_compat.IS_POSIX, reason="the fake backend is launched as a POSIX child here"
)


def _runtime(tmp_path, monkeypatch) -> AcpRuntime:
    rt = AcpRuntime(work_dir=tmp_path / "wd", agent="kirocrew", sandbox_mode="off")
    rt._KILL_TERM_TIMEOUT = 1.0
    rt._KILL_REAP_TIMEOUT = 1.0

    async def _plan() -> SpawnPlan:
        return SpawnPlan(
            argv=[sys.executable, fake_acp_backend.__file__, "acp", "--agent", "kirocrew"]
        )

    monkeypatch.setattr(rt, "_resolve_spawn_plan", _plan)
    monkeypatch.setattr(skill_projection, "prepare_native_skill_projection", lambda _wd: None)
    return rt


def _stale_on(monkeypatch, failing_calls: set[int]) -> list[int]:
    """Make the post-handshake check refuse on the given 1-based call numbers."""
    calls: list[int] = []

    def _check(_snapshot) -> None:
        calls.append(len(calls) + 1)
        if calls[-1] in failing_calls:
            raise agent_mod.DerivedSpecStale(f"default spec changed during load {calls[-1]}")

    monkeypatch.setattr(agent_mod, "require_unchanged_derived_spec", _check)
    return calls


async def _teardown(rt: AcpRuntime) -> None:
    if rt._process is not None:
        await rt.kill(expected=True, reason="test teardown")


@posix_only
@pytest.mark.asyncio
async def test_a_benign_write_during_load_still_starts_the_runtime(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)
    calls = _stale_on(monkeypatch, {1})
    try:
        await rt.spawn()
        assert calls == [1, 2], "the retry never reached its own post-handshake check"
        assert rt.is_alive()
        assert rt._initialized is True
        # The first child's death is not left on the handle that is now serving.
        assert rt.death_summary() is None
        assert runtime_death.death_of(rt) is None
    finally:
        await _teardown(rt)


@posix_only
@pytest.mark.asyncio
async def test_a_spec_that_keeps_moving_reports_the_stale_spec(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)
    calls = _stale_on(monkeypatch, {1, 2})
    try:
        with pytest.raises(AcpRuntimeError) as excinfo:
            await rt.spawn()
        assert calls == [1, 2]
        assert "default spec changed during load 2" in str(excinfo.value)
        assert "runtime is dead" not in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, agent_mod.DerivedSpecStale)
        assert rt._process is None, "the second child outlived its failed start"
    finally:
        await _teardown(rt)


@posix_only
@pytest.mark.asyncio
async def test_no_retry_while_the_first_child_is_still_held(tmp_path, monkeypatch):
    """A declined reap leaves the first child on the handle: no second child is started."""
    rt = _runtime(tmp_path, monkeypatch)
    calls = _stale_on(monkeypatch, {1})
    real_kill = AcpRuntime.kill
    held: list[int] = []

    async def _declined_kill(self, *, expected: bool = False, reason: str = "") -> None:
        if reason == "failed init handshake cleanup":
            held.append(self._process.pid)
            return
        await real_kill(self, expected=expected, reason=reason)

    monkeypatch.setattr(AcpRuntime, "kill", _declined_kill)
    try:
        with pytest.raises(AcpRuntimeError) as excinfo:
            await rt.spawn()
        assert held, "the failed-start reap never ran"
        assert calls == [1]
        assert (
            rt._process is not None and rt._process.pid == held[0]
        ), "a second child replaced the first"
        assert "default spec changed during load 1" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, agent_mod.DerivedSpecStale)
    finally:
        monkeypatch.setattr(AcpRuntime, "kill", real_kill)
        await _teardown(rt)


@posix_only
@pytest.mark.asyncio
async def test_no_retry_when_the_reap_did_not_confirm_the_tree_gone(tmp_path, monkeypatch, caplog):
    """A reap that dropped the handle but left a survivor is not a start to repeat."""
    rt = _runtime(tmp_path, monkeypatch)
    calls = _stale_on(monkeypatch, {1})
    real_kill = AcpRuntime.kill

    async def _reap_with_survivor(self, *, expected: bool = False, reason: str = "") -> None:
        await real_kill(self, expected=expected, reason=reason)
        if reason == "failed init handshake cleanup":
            self._process_tree_confirmed_dead = False

    monkeypatch.setattr(AcpRuntime, "kill", _reap_with_survivor)
    try:
        with caplog.at_level("WARNING", logger="kiro_crew.acp.runtime"):
            with pytest.raises(AcpRuntimeError) as excinfo:
                await rt.spawn()
        assert calls == [1]
        assert "default spec changed during load 1" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, agent_mod.DerivedSpecStale)
        assert any("outcome=not_retried" in r.getMessage() for r in caplog.records)
        assert rt._dead is True, "a refused revive must leave the death in place"
    finally:
        monkeypatch.setattr(AcpRuntime, "kill", real_kill)
        await _teardown(rt)


# ── the revive itself, on the state a failed start leaves behind ──


def _failed_start(tmp_path) -> AcpRuntime:
    """A runtime in the state ``_failed_start_cleanup`` leaves after a confirmed reap."""
    rt = AcpRuntime(work_dir=tmp_path / "wd", agent="kirocrew", sandbox_mode="off")
    rt._stderr_lines.extend(["auth failed", "sandbox refused"])
    rt._saw_auth_failure = True
    rt._saw_sandbox_init_failure = True
    rt._scratch_dir = tmp_path / "scratch-of-the-dead-child"
    rt._mark_dead("killed (failed init handshake cleanup)")
    rt._process_tree_confirmed_dead = True
    return rt


@pytest.mark.asyncio
async def test_the_revive_clears_what_the_dead_child_left(tmp_path):
    rt = _failed_start(tmp_path)
    assert runtime_death.death_of(rt) is not None
    assert rt.death_summary() is not None

    assert await rt._revive_after_failed_start() is True

    assert rt._dead is False
    assert rt.death_summary() is None
    assert runtime_death.death_of(rt) is None
    assert rt._stderr_lines == []
    assert rt._saw_auth_failure is False
    assert rt._saw_sandbox_init_failure is False
    assert rt._scratch_dir is None, "the next launch would adopt the dead child's scratch"
    assert rt._process_tree_confirmed_dead is False


@pytest.mark.asyncio
async def test_the_revive_stops_the_dead_childs_reply_tasks(tmp_path):
    """A reply still waiting (a token refresh) must not reach the next child's stdin."""
    rt = _failed_start(tmp_path)
    wrote: list[str] = []
    refreshed = asyncio.Event()

    async def _slow_reply() -> None:
        # Never set: the reply is parked on its refresh until something cancels it.
        await refreshed.wait()
        wrote.append("reply")

    reply = asyncio.ensure_future(_slow_reply())
    rt._answer_tasks.add(reply)
    reply.add_done_callback(rt._answer_tasks.discard)
    await asyncio.sleep(0)

    assert await rt._revive_after_failed_start() is True
    assert reply.cancelled()
    assert wrote == []


@pytest.mark.asyncio
async def test_no_revive_while_a_reply_task_will_not_stop(tmp_path):
    rt = _failed_start(tmp_path)
    rt._REVIVE_ANSWER_DRAIN_TIMEOUT = 0.05
    release = asyncio.Event()

    async def _stubborn_reply() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    reply = asyncio.ensure_future(_stubborn_reply())
    rt._answer_tasks.add(reply)
    await asyncio.sleep(0)
    try:
        assert await rt._revive_after_failed_start() is False
        assert rt._dead is True
        assert runtime_death.death_of(rt) is not None
    finally:
        release.set()
        await reply


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsettled",
    ["alive", "process_held", "tree_unconfirmed", "initialized", "session_registered"],
)
async def test_no_revive_of_a_start_that_is_not_safely_over(tmp_path, unsettled):
    rt = _failed_start(tmp_path)
    if unsettled == "alive":
        rt._dead = False
    elif unsettled == "process_held":
        rt._process = object()  # type: ignore[assignment]
    elif unsettled == "tree_unconfirmed":
        rt._process_tree_confirmed_dead = False
    elif unsettled == "initialized":
        rt._initialized = True
    else:
        rt._session_queues["sid-1"] = asyncio.Queue()
    summary = rt.death_summary()

    assert await rt._revive_after_failed_start() is False
    assert rt.death_summary() == summary
    assert rt._scratch_dir is not None
    rt._process = None
