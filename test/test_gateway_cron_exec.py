"""The command and script cron arms, pinned through ``on_job`` before they leave ``gateway.py``.

A command or script job holds the overlap guard ``_running_script_ids`` for its run:
the arm adds the job id after the admission check, creates the claim hand-off, and
its ``finally`` is the one place that abandons the hand-off and then releases the
guard -- on every exit, the cancellation included. The capture the message arm
needs still runs first for every kind of job, so the guard's skip happens after it.

These tests drive each arm through ``on_job`` with the pool, the fire-time gate and
the hand-off replaced, and pin those orders for every disposition an arm has.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cron_gateway_integration_helpers import _make_command_job, _make_gw, _make_script_job

from kiro_crew.executors import CronQueueTimeout
from kiro_crew.slack import gateway as gw_mod
from kiro_crew.slack.gateway_runtime.cron_dispatch import CronClaimTimeDenied, CronVetOverran


class _GuardSet(set):
    """``_running_script_ids`` with each add and discard recorded."""

    def __init__(self, calls: list[str]) -> None:
        super().__init__()
        self._calls = calls

    def add(self, item: Any) -> None:
        self._calls.append("add")
        super().add(item)

    def discard(self, item: Any) -> None:
        self._calls.append("discard")
        super().discard(item)


class _Handoff:
    def __init__(self, calls: list[str], started: bool) -> None:
        self._calls = calls
        self._started = started

    def abandon(self) -> bool:
        self._calls.append("abandon")
        return self._started


class _Arm:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        self.started = False
        self.on_job: Any = None
        gw = _make_gw()
        gw._no_crons = True
        gw._running_script_ids = _GuardSet(self.calls)
        gw.dashboard_state = None
        gw._deliver_failure_alert = AsyncMock(return_value=(False, True, False))
        self.gw = gw
        self.pool = AsyncMock(return_value={"status": "ok", "output": "done"})
        self.gate = AsyncMock(return_value=(None, False))

        def _capture(on_job: Any = None, **_kw: Any) -> MagicMock:
            self.on_job = on_job
            return MagicMock(name="cron_svc")

        def _context(job: Any) -> tuple[str, str]:
            self.calls.append("capture")
            return f"cron:{job.id}", job.message

        monkeypatch.setattr(
            gw_mod, "CronService", SimpleNamespace(create=AsyncMock(side_effect=_capture))
        )
        monkeypatch.setattr(gw_mod, "sel", MagicMock(name="sel"))
        monkeypatch.setattr(gw_mod, "build_cron_session_context", _context)
        monkeypatch.setattr(gw_mod, "_await_cron_fire_time_gate", self.gate)
        monkeypatch.setattr(gw_mod, "run_in_cron_pool", self.pool)
        monkeypatch.setattr(gw_mod, "_ClaimHandoff", lambda: _Handoff(self.calls, self.started))

    async def fire(self, job: Any) -> Any:
        if self.on_job is None:
            await self.gw._init_cron()
        return await self.on_job(job)


@pytest.fixture
def arm(monkeypatch: pytest.MonkeyPatch) -> _Arm:
    return _Arm(monkeypatch)


_JOBS = {"command": _make_command_job, "script": _make_script_job}

#: (disposition, how the pool or the gate is made to produce it)
_DISPOSITIONS = [
    ("ok", {"pool": {"return_value": {"status": "ok", "output": "done"}}}),
    (
        "non-ok status",
        {"pool": {"return_value": {"status": "error", "output": "", "exit_code": 2}}},
    ),
    ("user cancel", {"pool": {"return_value": {"status": "cancelled"}}}),
    ("overlap refused", {"pool": {"return_value": {"status": "skipped"}}}),
    ("pool starved", {"pool": {"side_effect": CronQueueTimeout(42.0)}}),
    ("claim backstop", {"pool": {"side_effect": asyncio.TimeoutError()}}),
    ("claim-time deny", {"pool": {"side_effect": CronClaimTimeDenied("ceiling tightened")}}),
    ("vet overran", {"pool": {"side_effect": CronVetOverran(9.0, 5.0)}}),
    ("payload raised", {"pool": {"side_effect": RuntimeError("launcher broke")}}),
    ("fire-time deny", {"gate": {"return_value": ("cron capability off", False)}}),
    ("gate starved", {"gate": {"return_value": (None, True)}}),
]


@pytest.mark.parametrize("kind", sorted(_JOBS))
@pytest.mark.parametrize("disposition,how", _DISPOSITIONS, ids=[d for d, _ in _DISPOSITIONS])
@pytest.mark.asyncio
async def test_every_exit_abandons_the_handoff_then_releases_the_guard(
    arm: _Arm, kind: str, disposition: str, how: dict[str, dict[str, Any]]
) -> None:
    for target, kwargs in how.items():
        getattr(arm, target).configure_mock(**kwargs)
    job = _JOBS[kind]()

    await arm.fire(job)

    assert arm.calls[:2] == ["capture", "add"]
    assert arm.calls[-2:] == ["abandon", "discard"]
    assert job.id not in arm.gw._running_script_ids


@pytest.mark.parametrize("kind", sorted(_JOBS))
@pytest.mark.asyncio
async def test_a_cancelled_run_abandons_then_releases_and_reraises(arm: _Arm, kind: str) -> None:
    arm.pool.configure_mock(side_effect=asyncio.CancelledError())
    job = _JOBS[kind]()

    with pytest.raises(asyncio.CancelledError):
        await arm.fire(job)

    # The cancellation arm asks once, the finally once more.
    assert arm.calls == ["capture", "add", "abandon", "abandon", "discard"]
    assert job.run_never_started is True
    assert job.id not in arm.gw._running_script_ids


@pytest.mark.parametrize("kind", sorted(_JOBS))
@pytest.mark.parametrize("started", [False, True])
@pytest.mark.asyncio
async def test_the_claim_backstop_counts_only_a_run_that_started(
    arm: _Arm, kind: str, started: bool
) -> None:
    arm.started = started
    arm.pool.configure_mock(side_effect=asyncio.TimeoutError())
    job = _JOBS[kind]()

    await arm.fire(job)

    assert job.run_never_started is (not started)
    assert job.consecutive_failures == (1 if started else 0)


@pytest.mark.parametrize("kind", sorted(_JOBS))
@pytest.mark.asyncio
async def test_a_job_already_running_is_captured_then_skipped(arm: _Arm, kind: str) -> None:
    job = _JOBS[kind]()
    set.add(arm.gw._running_script_ids, job.id)

    assert await arm.fire(job) is None

    assert arm.calls == ["capture"]
    arm.gate.assert_not_awaited()
    arm.pool.assert_not_awaited()


@pytest.mark.parametrize("kind", sorted(_JOBS))
@pytest.mark.asyncio
async def test_a_closed_admission_defers_before_taking_the_guard(arm: _Arm, kind: str) -> None:
    arm.gw.sessions.admission_closed = True
    job = _JOBS[kind]()

    assert await arm.fire(job) is None

    assert arm.calls == ["capture"]
    assert job.run_never_started is True
    arm.pool.assert_not_awaited()
