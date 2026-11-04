"""A slice OOM report survives a probe that timed out before the loop read it.

``maybe_sample`` forfeits a sample whose probe outlives ``PROBE_TIMEOUT_SECS``,
but the abandoned worker still finishes and stashes the OOM report it found.
The check reports each kill once, so the next tick's result is empty; when
that empty result replaced the stash, the kill never reached the dashboard.

The timed-out tick is reproduced without a clock: the worker body runs once
on its own (what the abandoned thread does), then the next tick samples.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.notifications import resource_pressure
from kiro_crew.notifications.bus import _MAX_BODY_LEN, NotificationBus
from kiro_crew.notifications.resource_pressure import ResourcePressureNotifier
from kiro_crew.resource_status import POSTURE_AMPLE, ResourceStatus

_KILL_A = "cgroup OOM kill inside kirocrew-agents.slice: 1 new kill(s); scope A"
_KILL_B = "cgroup OOM kill inside kirocrew-agents.slice: 1 new kill(s); scope B"


def _ample() -> ResourceStatus:
    return ResourceStatus(
        available_gb=20.0,
        cpu_count=8,
        load_per_cpu=0.5,
        posture=POSTURE_AMPLE,
        pressure_gb=4.0,
        critical_gb=2.0,
    )


def _oom_notes(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [n for n in notes if n.get("group_key") == "agents-slice-oom"]


def _checks(monkeypatch: pytest.MonkeyPatch, *results: str | None) -> None:
    """Make the slice check return *results* in order, then None."""
    queue = list(results)
    monkeypatch.setattr(
        resource_pressure,
        "check_agents_slice_pressure",
        lambda: queue.pop(0) if queue else None,
    )


@pytest.mark.asyncio
async def test_a_report_from_a_timed_out_probe_is_pushed_on_the_next_tick(monkeypatch) -> None:
    notes: list[dict[str, Any]] = []
    notifier = ResourcePressureNotifier(NotificationBus(sink=notes.append), probe_fn=_ample)
    _checks(monkeypatch, _KILL_A)

    notifier._run_probe()  # the abandoned worker: found the kill, nobody read it
    await notifier.maybe_sample()  # the next tick: the check now reports nothing new

    oom = _oom_notes(notes)
    assert len(oom) == 1
    assert "scope A" in oom[0]["body"]


@pytest.mark.asyncio
async def test_a_report_waiting_is_joined_by_a_new_one_not_replaced(monkeypatch) -> None:
    notes: list[dict[str, Any]] = []
    notifier = ResourcePressureNotifier(NotificationBus(sink=notes.append), probe_fn=_ample)
    _checks(monkeypatch, _KILL_A, _KILL_B)

    notifier._run_probe()
    await notifier.maybe_sample()

    bodies = " ".join(n["body"] for n in _oom_notes(notes))
    assert "scope A" in bodies and "scope B" in bodies


def _waiting(notifier: ResourcePressureNotifier, *reports: str) -> None:
    """Reports left by probes that finished after their timeout."""
    notifier._slice_oom_reports.extend(reports)


@pytest.mark.asyncio
async def test_reports_past_the_body_limit_go_out_in_notes_that_fit(monkeypatch) -> None:
    notes: list[dict[str, Any]] = []
    notifier = ResourcePressureNotifier(NotificationBus(sink=notes.append), probe_fn=_ample)
    _checks(monkeypatch)
    reports = [f"kill {n}: " + "x" * 6000 for n in range(5)]
    _waiting(notifier, *reports)

    await notifier.maybe_sample()

    oom = _oom_notes(notes)
    assert len(oom) > 1
    assert all(len(n["body"]) <= _MAX_BODY_LEN for n in oom)
    delivered = "\n".join(n["body"] for n in oom)
    assert all(f"kill {n}:" in delivered for n in range(5))
    assert notifier._slice_oom_reports == []


@pytest.mark.asyncio
async def test_a_failed_push_keeps_the_reports_it_did_not_deliver(monkeypatch) -> None:
    notes: list[dict[str, Any]] = []
    notifier = ResourcePressureNotifier(NotificationBus(sink=notes.append), probe_fn=_ample)
    _checks(monkeypatch)
    _waiting(notifier, _KILL_A, _KILL_B)
    monkeypatch.setattr(notifier, "_push_slice_oom", lambda _body: (_ for _ in ()).throw(OSError))

    await notifier.maybe_sample()

    assert notifier._slice_oom_reports == [_KILL_A, _KILL_B]


def test_the_stash_keeps_the_newest_reports_and_counts_the_rest(monkeypatch) -> None:
    notifier = ResourcePressureNotifier(NotificationBus(sink=lambda _n: None), probe_fn=_ample)
    cap = resource_pressure.MAX_PENDING_OOM_REPORTS
    _checks(monkeypatch, *[f"kill {n}" for n in range(cap + 3)])

    for _ in range(cap + 3):
        notifier._run_probe()

    assert len(notifier._slice_oom_reports) == cap
    assert notifier._slice_oom_reports[-1] == f"kill {cap + 2}"
    assert notifier._slice_oom_dropped == 3


@pytest.mark.asyncio
async def test_a_dropped_count_is_reported_with_the_kept_reports(monkeypatch) -> None:
    notes: list[dict[str, Any]] = []
    notifier = ResourcePressureNotifier(NotificationBus(sink=notes.append), probe_fn=_ample)
    _checks(monkeypatch)
    _waiting(notifier, _KILL_A)
    notifier._slice_oom_dropped = 4

    await notifier.maybe_sample()

    body = _oom_notes(notes)[0]["body"]
    assert body.startswith("4 earlier OOM report(s) were not kept.")
    assert "scope A" in body


@pytest.mark.asyncio
async def test_a_pushed_report_is_not_pushed_again(monkeypatch) -> None:
    notes: list[dict[str, Any]] = []
    clock = [0.0]
    notifier = ResourcePressureNotifier(
        NotificationBus(sink=notes.append), probe_fn=_ample, clock=lambda: clock[0]
    )
    _checks(monkeypatch, _KILL_A)

    await notifier.maybe_sample()
    clock[0] += resource_pressure.SAMPLE_INTERVAL_SECS
    await notifier.maybe_sample()

    assert len(_oom_notes(notes)) == 1
