"""The Heartbeat's hours: an optional window of whole hours its wakes stay inside.

What these tests pin:

* the record holds ``active_from`` / ``active_until``, the whole day by default,
  and a record written before them, or a stored window that is not one, reads
  as the whole day;
* the whole day keeps the round-the-clock cron expressions; a narrower window
  puts the wakes on the same marks inside it, and the daily bound counts only
  those wakes;
* a window off the hours, or one that ends where or before it starts, is refused;
* the owner's route replaces the schedule when the hours change.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from croniter import croniter  # type: ignore[import-untyped]
from guide_route_helpers import FakeState
from member_goals_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    ORDINARY,
    _owner,
    _quiet_sel,
    _reminder_state,
    _schedules,
    _seed,
    _slug,
    _thread,
)

from kiro_crew import member_goals
from kiro_crew.dashboard import goal_reminder, mate_welcome


def _at(hour: int, minute: int = 0) -> float:
    """A fixed Monday in January, in UTC at *hour*:*minute*, as an epoch."""
    return datetime(2026, 1, 5, hour, minute, tzinfo=timezone.utc).timestamp()


class _Job:
    """The heartbeat's managed job, in the zone its expression is matched in."""

    def __init__(self, slug: str, tz: str = "UTC") -> None:
        self.id = "hb-job"
        self.managed_by = goal_reminder.managed_key(slug, member_goals.CADENCE_HEARTBEAT)
        self.timezone = tz


def _hb(interval: int, start: int, end: int) -> dict[str, Any]:
    return {
        **member_goals.default_schedules(),
        "interval": interval,
        "active_from": start,
        "active_until": end,
    }


def _wakes_in_a_day(expr: str) -> list[datetime]:
    # One fixed day: the first match at or after its midnight, then every one in it.
    it = croniter(expr, datetime(2026, 1, 4, 23, 59))
    out = []
    while True:
        nxt = it.get_next(datetime)
        if nxt.day != 5:
            return out
        out.append(nxt)


def test_the_hours_default_to_the_whole_day_and_an_older_record_reads_as_it() -> None:
    _seed()
    slug = _slug(ORDINARY)
    sched = member_goals.default_schedules()
    assert (sched["active_from"], sched["active_until"]) == (0, 24)
    path = member_goals.member_goals_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = {"member": ORDINARY, "slug": slug, "schedules": {"heartbeat": True, "interval": 60}}
    path.write_text(json.dumps(old), encoding="utf-8")
    sched = member_goals.read_goals(slug, ORDINARY)["schedules"]
    assert (sched["active_from"], sched["active_until"]) == (0, 24)


@pytest.mark.parametrize("stored", [(20, 8), (8, 8), (-1, 24), (0, 25), ("8", 20), (True, 20)])
def test_a_stored_window_that_is_not_one_reads_as_the_whole_day(stored: tuple[Any, Any]) -> None:
    _seed()
    slug = _slug(ORDINARY)
    path = member_goals.member_goals_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "member": ORDINARY,
        "slug": slug,
        "schedules": {"active_from": stored[0], "active_until": stored[1]},
    }
    path.write_text(json.dumps(rec), encoding="utf-8")
    sched = member_goals.read_goals(slug, ORDINARY)["schedules"]
    assert (sched["active_from"], sched["active_until"]) == (0, 24)


@pytest.mark.parametrize("interval", list(member_goals.HEARTBEAT_INTERVALS))
def test_the_whole_day_keeps_the_round_the_clock_expression(interval: int) -> None:
    expr = goal_reminder.cron_expr_for(member_goals.CADENCE_HEARTBEAT, _hb(interval, 0, 24))
    assert expr == goal_reminder._HEARTBEAT_EXPRS[interval]


@pytest.mark.parametrize(
    ("interval", "start", "end", "expr"),
    [
        (30, 8, 20, "*/30 8-19 * * *"),
        (60, 8, 20, "0 8-19 * * *"),
        (120, 8, 20, "0 8-19/2 * * *"),
        (240, 8, 20, "0 8-19/4 * * *"),
        (60, 9, 10, "0 9 * * *"),
        (240, 0, 1, "0 0 * * *"),
    ],
)
def test_a_window_keeps_the_wakes_inside_its_hours(
    interval: int, start: int, end: int, expr: str
) -> None:
    assert (
        goal_reminder.cron_expr_for(member_goals.CADENCE_HEARTBEAT, _hb(interval, start, end))
        == expr
    )


@pytest.mark.parametrize("interval", list(member_goals.HEARTBEAT_INTERVALS))
@pytest.mark.parametrize(("start", "end"), [(0, 24), (8, 20), (7, 18), (9, 10), (0, 1), (23, 24)])
def test_every_wake_falls_inside_the_hours_and_the_daily_bound_counts_them(
    interval: int, start: int, end: int
) -> None:
    expr = goal_reminder.cron_expr_for(member_goals.CADENCE_HEARTBEAT, _hb(interval, start, end))
    wakes = _wakes_in_a_day(expr)
    assert wakes and all(start <= w.hour < end for w in wakes)
    assert wakes[0].hour == start and wakes[0].minute == 0
    assert len(wakes) == member_goals.wakes_per_day(interval, start, end)


def test_the_daily_bound_all_day_is_unchanged() -> None:
    assert [member_goals.wakes_per_day(m) for m in member_goals.HEARTBEAT_INTERVALS] == [
        48,
        24,
        12,
        6,
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"active_from": 24},
        {"active_from": -1},
        {"active_until": 0},
        {"active_until": 25},
        {"active_from": "8"},
        {"active_until": True},
        {"active_from": 8.0},
        {"active_from": 20, "active_until": 8},
        {"active_from": 8, "active_until": 8},
    ],
)
def test_hours_off_the_clock_or_out_of_order_are_refused(changes: dict[str, Any]) -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_schedules(slug, ORDINARY, changes)
    assert exc.value.code == "invalid_active_hours"


def test_one_end_is_checked_against_the_other_already_stored() -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    member_goals.set_schedules(slug, ORDINARY, {"active_from": 8, "active_until": 20})
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_schedules(slug, ORDINARY, {"active_until": 8})
    assert exc.value.code == "invalid_active_hours"
    member_goals.set_schedules(slug, ORDINARY, {"active_until": 9})
    sched = member_goals.read_goals(slug, ORDINARY)["schedules"]
    assert (sched["active_from"], sched["active_until"]) == (8, 9)


def test_the_route_replaces_the_schedule_when_the_hours_change(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    status, body = _owner(
        state, "PUT", base + "/schedules", {"member": ORDINARY, "heartbeat": True}
    )
    assert status == 200, body
    (first,) = _schedules(state, slug, "heartbeat")
    assert first.schedule.cron_expr == "0 * * * *"
    status, body = _owner(
        state,
        "PUT",
        base + "/schedules",
        {"member": ORDINARY, "active_from": 8, "active_until": 20},
    )
    assert status == 200, body
    assert (body["schedules"]["active_from"], body["schedules"]["active_until"]) == (8, 20)
    (second,) = _schedules(state, slug, "heartbeat")
    assert second.schedule.cron_expr == "0 8-19 * * *" and second.id != first.id
    status, body = _owner(
        state, "PUT", base + "/schedules", {"member": ORDINARY, "active_from": 20}
    )
    assert (status, body["code"]) == (400, "invalid_active_hours")
    (still,) = _schedules(state, slug, "heartbeat")
    assert still.id == second.id


# ── a run that starts late ──


@pytest.mark.parametrize(
    ("now", "inside"),
    [(_at(8), True), (_at(19, 59), True), (_at(20), False), (_at(7, 59), False), (_at(23), False)],
)
def test_the_hours_are_read_at_the_moment_the_run_starts(now: float, inside: bool) -> None:
    assert member_goals.in_active_hours(_hb(60, 8, 20), now, timezone.utc) is inside


def test_the_hours_are_read_in_the_jobs_own_zone(_floor_monkeypatch) -> None:
    # A fixed UTC-05:00 zone, so no host timezone data decides the answer:
    # 01:30 UTC is 20:30 the evening before there, past an 08:00-20:00 window.
    zone = timezone(timedelta(hours=-5))
    jobs: list[Any] = []

    def job_tz(job: Any) -> Any:
        jobs.append(job)
        return zone

    _floor_monkeypatch.setattr(goal_reminder, "_job_tz", job_tz)
    assert member_goals.in_active_hours(_hb(60, 8, 20), _at(1, 30), zone) is False
    assert member_goals.in_active_hours(_hb(60, 8, 20), _at(14), zone) is True
    job = _Job("x")
    assert goal_reminder.heartbeat_in_hours(job, _hb(60, 8, 20), _at(1, 30)) is False
    assert goal_reminder.heartbeat_in_hours(job, _hb(60, 8, 20), _at(14)) is True
    assert jobs == [job, job]


def test_an_all_day_heartbeat_reads_no_clock(_floor_monkeypatch) -> None:
    def no_clock() -> float:
        raise AssertionError("an all-day heartbeat must not read the clock")

    _floor_monkeypatch.setattr(goal_reminder, "_clock", no_clock)
    assert goal_reminder.heartbeat_in_hours(_Job("x"), _hb(60, 0, 24)) is True


def _armed(state: FakeState) -> str:
    _seed()
    slug = _slug(ORDINARY)
    slot = _thread(state, ORDINARY, user_sent=False)
    slot.task = None
    member_goals.set_goal(slug, ORDINARY, "Keep docs current", by="user")
    member_goals.set_schedules(
        slug, ORDINARY, {"heartbeat": True, "interval": 60, "active_from": 8, "active_until": 20}
    )
    return slug


@pytest.mark.parametrize(
    ("now", "outcome"),
    [
        # The 19:00 tick, run on time: it starts.
        (_at(19, 5), "started"),
        # The same 19:00 tick, run after the host woke at 20:10: no turn.
        (_at(20, 10), goal_reminder.HOURS_SKIP),
    ],
)
def test_a_tick_due_inside_the_hours_that_runs_after_them_starts_no_turn(
    _floor_monkeypatch, now: float, outcome: str
) -> None:
    state = FakeState()
    slug = _armed(state)
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )
    _floor_monkeypatch.setattr(goal_reminder, "member_has_session_tools", lambda _m: False)
    _floor_monkeypatch.setattr(goal_reminder, "_clock", lambda: now)
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == outcome
    assert len(sent) == (1 if outcome == "started" else 0)
