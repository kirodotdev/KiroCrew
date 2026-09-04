"""Characterization tests for terminal lifecycle arbitration and task ownership."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from kiro_crew.subagent_lifecycle import SubagentLifecycle


@dataclass
class LifecycleInfo:
    id: str = "run-1"
    done: bool = False
    reaped: bool = False
    _reap_started: bool = False
    _recovering: bool = False
    _finalized: bool = False
    _slot_released: bool = False


def test_record_and_report_claims_are_independent_and_one_shot() -> None:
    lifecycle = SubagentLifecycle()
    info = LifecycleInfo()

    assert lifecycle.claim_record(info) is True
    assert lifecycle.claim_record(info) is False
    assert lifecycle.claim_report(info) is True
    assert lifecycle.claim_report(info) is False


def test_recovery_withholds_report_until_definitive_reap_supersedes_it() -> None:
    lifecycle = SubagentLifecycle()
    info = LifecycleInfo(_recovering=True)

    assert lifecycle.claim_report(info) is False
    assert info._finalized is False
    assert lifecycle.claim_report(info, supersede_recovery=True) is True
    assert info._recovering is False


def test_reap_markers_are_explicit_transitions() -> None:
    lifecycle = SubagentLifecycle()
    info = LifecycleInfo()

    lifecycle.begin_reap(info)
    lifecycle.mark_reaped(info)

    assert info._reap_started is True
    assert info.reaped is True


@pytest.mark.asyncio
async def test_report_task_is_shielded_and_owned_until_completion() -> None:
    lifecycle: SubagentLifecycle[LifecycleInfo] = SubagentLifecycle()
    info = LifecycleInfo()
    started = asyncio.Event()
    finish = asyncio.Event()

    async def report() -> None:
        started.set()
        await finish.wait()

    task = lifecycle.spawn_report(info, report)
    await started.wait()
    waiter = asyncio.create_task(lifecycle.await_report(task))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert task.done() is False
    assert lifecycle.owner_for(task) is info
    assert lifecycle.pending_reports() == [task]

    finish.set()
    await task
    await asyncio.sleep(0)
    assert lifecycle.pending_reports() == []
    assert lifecycle.owner_for(task) is None


@pytest.mark.asyncio
async def test_teardown_gate_stays_reachable_until_explicit_close() -> None:
    lifecycle = SubagentLifecycle()

    gate = lifecycle.open_teardown("run-1")

    assert lifecycle.gate_for("run-1") is gate
    lifecycle.close_teardown("run-1", gate)
    assert gate.is_set()
    assert lifecycle.gate_for("run-1") is None
