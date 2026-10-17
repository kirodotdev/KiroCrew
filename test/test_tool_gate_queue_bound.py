"""The tool gate's wait for an ``mc-toolgate`` worker is bounded.

``executors.run_in_tool_gate_pool`` runs the synchronous gate on a four-worker
pool so a stalled path resolution parks a worker, not the event loop. With every
worker held, a further call waits in the pool's FIFO, and that wait is bounded
by ``_TOOL_GATE_QUEUE_WAIT_SECS``: a call no worker claimed in time never runs
and the caller gets its own
``on_queue_timeout`` refusal; a call a worker claimed still runs to its verdict.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

import kiro_crew.executors as ex
from kiro_crew.hooks import TOOL_DENY
from kiro_crew.tool_gate_busy import TOOL_GATE_BUSY_REASON, hook_gate_busy

_BOUND = 0.3
# Generous for a loaded runner, and far below the pre-bound behaviour (forever).
_DEADLINE = 5.0


@pytest.fixture
def small_bound(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(ex, "_TOOL_GATE_QUEUE_WAIT_SECS", _BOUND, raising=False)
    ex.shutdown_maintenance_executor()
    yield
    ex.shutdown_maintenance_executor()


async def _hold_every_worker(release: threading.Event) -> list[asyncio.Future]:
    """Occupy all ``_MAX_TOOL_GATE_WORKERS`` gate workers until *release* is set."""
    started = threading.Semaphore(0)

    def held() -> str:
        started.release()
        release.wait(_DEADLINE)
        return "held"

    loop = asyncio.get_running_loop()
    pool = ex.tool_gate_executor()
    futs = [loop.run_in_executor(pool, held) for _ in range(ex._MAX_TOOL_GATE_WORKERS)]
    for _ in futs:
        assert await asyncio.to_thread(started.acquire, True, _DEADLINE)
    return futs


def test_a_call_no_worker_claims_is_refused_unjudged_and_never_runs(small_bound) -> None:
    ran: list[str] = []
    waited_seen: list[float] = []

    def gate(title: str, **kwargs) -> str:
        ran.append(title)
        return "allow"

    def refuse(waited: float) -> str:
        waited_seen.append(waited)
        return "refused"

    async def main() -> str:
        release = threading.Event()
        held = await _hold_every_worker(release)
        try:
            return await asyncio.wait_for(
                ex.run_in_tool_gate_pool(gate, "queued call", on_queue_timeout=refuse),
                _DEADLINE,
            )
        finally:
            release.set()
            await asyncio.gather(*held)
            # Drain the pool: a wrongly-surviving queued call would run here.
            await asyncio.to_thread(ex.tool_gate_executor().shutdown, True)

    assert asyncio.run(main()) == "refused"
    assert ran == [], "a call refused in the queue must never run afterwards"
    assert waited_seen and waited_seen[0] >= _BOUND * 0.9


def test_a_call_a_worker_claims_runs_to_its_verdict_past_the_bound(small_bound) -> None:
    """The bound is on the wait only: a slow verdict is not cut off."""

    finish = threading.Event()

    def slow_gate(title: str) -> str:
        # Held by the worker until the loop sets ``finish``, three bounds later.
        assert finish.wait(_DEADLINE)
        return "verdict"

    def refuse(waited: float) -> str:
        return "refused"

    async def main() -> str:
        asyncio.get_running_loop().call_later(_BOUND * 3, finish.set)
        return await asyncio.wait_for(
            ex.run_in_tool_gate_pool(slow_gate, "t", on_queue_timeout=refuse), _DEADLINE
        )

    assert asyncio.run(main()) == "verdict"


def test_a_queued_call_frees_inside_the_bound_and_runs(small_bound, monkeypatch) -> None:
    monkeypatch.setattr(ex, "_TOOL_GATE_QUEUE_WAIT_SECS", 5.0)

    def gate(title: str) -> str:
        return "allow"

    async def main() -> str:
        release = threading.Event()
        held = await _hold_every_worker(release)
        asyncio.get_running_loop().call_later(_BOUND, release.set)
        try:
            return await asyncio.wait_for(
                ex.run_in_tool_gate_pool(gate, "t", on_queue_timeout=lambda w: "refused"),
                _DEADLINE,
            )
        finally:
            release.set()
            await asyncio.gather(*held)

    assert asyncio.run(main()) == "allow"


def test_the_hook_gate_refusal_is_a_policy_deny_naming_the_busy_gate() -> None:
    result = hook_gate_busy(15.0)
    assert result.action == TOOL_DENY
    assert result.reason == TOOL_GATE_BUSY_REASON
    # Load, not the attempt: must not count against a cron's security budget.
    assert result.security_deny is False


def test_settle_refuses_a_call_the_gate_never_judged(small_bound) -> None:
    """``tool_permission.settle`` (subagents, task runner) fails closed."""
    from kiro_crew import tool_permission as tp

    judged: list[object] = []

    class _Gate:
        def judge(self, ask):
            judged.append(ask)
            return None

    async def main():
        release = threading.Event()
        held = await _hold_every_worker(release)
        try:
            return await asyncio.wait_for(
                ex.run_in_tool_gate_pool(
                    _Gate().judge, SimpleNamespace(), on_queue_timeout=tp._gate_unjudged
                ),
                _DEADLINE,
            )
        finally:
            release.set()
            await asyncio.gather(*held)

    verdict = asyncio.run(main())
    assert isinstance(verdict, tp.Refusal)
    assert (verdict.by, verdict.rung, verdict.reason) == (
        "host",
        "hook_deny",
        TOOL_GATE_BUSY_REASON,
    )
    assert judged == []
