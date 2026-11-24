"""Crewmate heartbeat: the fourth check-in, which wakes the crewmate to work on its own.

What these tests pin (RFC "Crewmate heartbeat"):

* the switch is off by default, and a record written before it reads as off at
  the default 60-minute interval;
* the interval is one of 30, 60, 120 or 240 minutes, and its cron
  expression follows the clock's marks;
* optional active hours narrow that expression's hours, and the whole day
  (the default) leaves it as it was;
* the switch keeps exactly one gateway schedule of its own, hidden from the
  Schedule page and replaced when the interval changes;
* only the owner's route and an applied card write it: the crewmate's
  ``set_check_ins`` becomes a card and writes nothing;
* a wake is one hidden gateway turn in the crewmate's thread with the same skips
  as the other check-ins, and that turn may end quietly with ``nothing_to_do``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from guide_route_helpers import DashboardTurn, FakeState
from member_goals_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    ORDINARY,
    PROPOSE,
    _call,
    _owner,
    _owner_ok,
    _quiet_sel,
    _reminder_state,
    _schedules,
    _seed,
    _slug,
    _thread,
)

from kiro_crew import member_goals, member_goals_hours
from kiro_crew.dashboard import goal_reminder, mate_welcome
from kiro_crew.dashboard import session_directive_apply as directive_apply

# ── the record ──


def test_the_heartbeat_is_off_by_default_and_an_older_record_reads_as_off() -> None:
    _seed()
    slug = _slug(ORDINARY)
    assert member_goals.empty_record()["schedules"]["heartbeat"] is False
    path = member_goals.member_goals_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = {
        "member": ORDINARY,
        "slug": slug,
        "goals": [{"id": "g0000000a", "text": "Ship"}],
        "schedules": {"time": "09:00", "daily": True, "weekly": False, "monthly": False},
    }
    path.write_text(json.dumps(old), encoding="utf-8")
    sched = member_goals.read_goals(slug, ORDINARY)["schedules"]
    assert (sched["daily"], sched["heartbeat"], sched["interval"]) == (True, False, 60)


def test_a_stored_interval_outside_the_list_reads_as_the_default() -> None:
    _seed()
    slug = _slug(ORDINARY)
    path = member_goals.member_goals_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"member": ORDINARY, "slug": slug, "schedules": {"heartbeat": True, "interval": 7}}
    path.write_text(json.dumps(rec), encoding="utf-8")
    assert member_goals.read_goals(slug, ORDINARY)["schedules"]["interval"] == 60


@pytest.mark.parametrize("interval", [0, 15, 45, 300, True, "30", 30.0])
def test_an_interval_outside_the_list_is_refused(interval: Any) -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_schedules(slug, ORDINARY, {"interval": interval})
    assert exc.value.code == "invalid_interval"


def test_the_heartbeat_needs_a_goal_and_removing_the_last_goal_turns_it_off() -> None:
    _seed()
    slug = _slug(ORDINARY)
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_schedules(slug, ORDINARY, {"heartbeat": True})
    assert exc.value.code == "no_goal"
    added = member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    member_goals.set_schedules(slug, ORDINARY, {"heartbeat": True, "interval": 30})
    member_goals.remove_goal(slug, ORDINARY, added["goal_id"])
    sched = member_goals.read_goals(slug, ORDINARY)["schedules"]
    assert (sched["heartbeat"], sched["interval"]) == (False, 30)


@pytest.mark.parametrize(
    ("interval", "expr"),
    [
        (30, "*/30 * * * *"),
        (60, "0 * * * *"),
        (120, "0 */2 * * *"),
        (240, "0 */4 * * *"),
    ],
)
def test_each_interval_has_its_cron_expression(interval: int, expr: str) -> None:
    sched = {**member_goals.default_schedules(), "interval": interval}
    assert goal_reminder.cron_expr_for(member_goals.CADENCE_HEARTBEAT, sched) == expr


def test_every_interval_has_an_expression() -> None:
    assert set(goal_reminder._HEARTBEAT_FIELDS) == set(member_goals.HEARTBEAT_INTERVALS)


@pytest.mark.parametrize(
    ("interval", "hours", "expr"),
    [
        (30, (8, 20), "*/30 8-19 * * *"),
        (60, (8, 20), "0 8-19 * * *"),
        (120, (9, 20), "0 9-19/2 * * *"),
        (240, (8, 9), "0 8-8/4 * * *"),
    ],
)
def test_active_hours_narrow_the_heartbeats_hours(
    interval: int, hours: tuple[int, int], expr: str
) -> None:
    sched = {
        **member_goals.default_schedules(),
        "interval": interval,
        "active_from": hours[0],
        "active_to": hours[1],
    }
    assert goal_reminder.cron_expr_for(member_goals.CADENCE_HEARTBEAT, sched) == expr


@pytest.mark.parametrize(
    "changes",
    [
        {"active_from": 20, "active_to": 8},
        {"active_from": 8, "active_to": 8},
        {"active_to": 0},
        {"active_from": 24},
        {"active_from": True},
        {"active_to": "20"},
        {"active_from": 8.0},
    ],
)
def test_active_hours_that_make_no_window_are_refused(changes: dict[str, Any]) -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_schedules(slug, ORDINARY, changes)
    assert exc.value.code == "invalid_active_hours"
    sched = member_goals.read_goals(slug, ORDINARY)["schedules"]
    assert (sched["active_from"], sched["active_to"]) == member_goals_hours.ACTIVE_HOURS_ALL_DAY


def test_the_daily_bound_counts_only_the_wakes_inside_the_window() -> None:
    """One number everywhere: the card's bound is the one its schedule fires."""
    from kiro_crew import change_card_catalog_plans as plans

    assert [member_goals.wakes_per_day(m) for m in member_goals.HEARTBEAT_INTERVALS] == [
        48,
        24,
        12,
        6,
    ]
    assert member_goals.wakes_per_day(60, [8, 20]) == 12
    assert member_goals.wakes_per_day(30, [8, 20]) == 24
    # A window whose hours do not divide by the interval still names every wake.
    assert member_goals.wakes_per_day(120, [9, 20]) == 6
    # Anything that is not a window is the whole day.
    for hours in (None, [20, 8], [0, 24], (8,), "8-20"):
        assert member_goals.wakes_per_day(60, hours) == 24
    assert plans._check_in_value("interval", 60, [8, 20]) == "Every 1 hr (up to 12 runs a day)"
    assert plans._check_in_value("interval", 60) == "Every 1 hr (up to 24 runs a day)"


def test_the_window_is_no_conflict_for_a_check_ins_card() -> None:
    """The card reads the window for its bound; moving it is not a card conflict.

    An Apply or an Undo compares the whole snapshot, so a window the user moved
    on the Schedules tab meanwhile — a field this card cannot set — would refuse
    the card for good, and a card stored before the window existed would never
    undo.
    """
    from kiro_crew import change_card_catalog as cards
    from kiro_crew.dashboard import change_cards_readers as readers

    kind = cards.KIND_CREWMATE_CHECK_INS
    shown = {"slug": "scout", "schedules": {"heartbeat": True, "interval": 60}}
    assert not readers.undo_snapshot_changed(
        kind, {**shown, "heartbeat_hours": [8, 20]}, {**shown, "heartbeat_hours": [0, 24]}
    )
    # A snapshot stored before the window existed carries no key at all.
    assert not readers.undo_snapshot_changed(kind, {**shown, "heartbeat_hours": [8, 20]}, shown)
    # A field the card does set is still a conflict.
    assert readers.undo_snapshot_changed(
        kind,
        {**shown, "schedules": {"heartbeat": True, "interval": 120}},
        {**shown, "heartbeat_hours": [8, 20]},
    )


def test_the_wake_prompt_names_the_window_only_when_there_is_one() -> None:
    rec = member_goals.empty_record()
    assert goal_reminder.active_hours_line(rec) == ""
    rec["schedules"].update({"active_from": 8, "active_to": 20})
    assert goal_reminder.active_hours_line(rec) == " between 08:00 and 20:00"


def test_a_stored_window_that_is_not_one_reads_as_the_whole_day() -> None:
    _seed()
    slug = _slug(ORDINARY)
    path = member_goals.member_goals_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"member": ORDINARY, "slug": slug, "schedules": {"active_from": 20, "active_to": 8}}
    path.write_text(json.dumps(rec), encoding="utf-8")
    sched = member_goals.read_goals(slug, ORDINARY)["schedules"]
    assert (sched["active_from"], sched["active_to"]) == member_goals_hours.ACTIVE_HOURS_ALL_DAY


# ── the owner's route and its schedule ──


def test_the_switch_keeps_one_schedule_replaced_when_its_interval_changes(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    status, body = _owner(
        state, "PUT", base + "/schedules", {"member": ORDINARY, "heartbeat": True}
    )
    assert status == 200, body
    assert (body["schedules"]["heartbeat"], body["schedules"]["interval"]) == (True, 60)
    (first,) = _schedules(state, slug, "heartbeat")
    assert first.schedule.cron_expr == "0 * * * *"
    assert first.managed_by == f"goal-heartbeat:{slug}"
    assert goal_reminder.is_reminder_job(first)
    status, body = _owner(state, "PUT", base + "/schedules", {"member": ORDINARY, "interval": 120})
    assert status == 200, body
    (second,) = _schedules(state, slug, "heartbeat")
    assert second.schedule.cron_expr == "0 */2 * * *" and second.id != first.id
    status, body = _owner(
        state, "PUT", base + "/schedules", {"member": ORDINARY, "heartbeat": False}
    )
    assert status == 200 and body["schedules"]["heartbeat"] is False
    assert _schedules(state, slug, "heartbeat") == []


def test_the_route_keeps_the_heartbeat_inside_its_active_hours(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from kiro_crew.cron_service.schedule import compute_next_run_ts

    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    window = {"heartbeat": True, "timezone": "UTC", "active_from": 8, "active_to": 20}
    status, body = _owner(state, "PUT", base + "/schedules", {"member": ORDINARY, **window})
    assert status == 200, body
    assert (body["schedules"]["active_from"], body["schedules"]["active_to"]) == (8, 20)
    (job,) = _schedules(state, slug, "heartbeat")
    assert job.schedule.cron_expr == "0 8-19 * * *"
    late = datetime(2026, 11, 13, 23, 0, tzinfo=timezone.utc).timestamp()
    morning = datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc).timestamp()
    assert compute_next_run_ts(job, late) == morning
    status, body = _owner(
        state, "PUT", base + "/schedules", {"member": ORDINARY, "active_from": 21}
    )
    assert (status, body["code"]) == (400, "invalid_active_hours")
    assert [j.id for j in _schedules(state, slug, "heartbeat")] == [job.id]


def test_the_route_refuses_an_interval_outside_the_list(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    status, body = _owner(state, "PUT", base + "/schedules", {"member": ORDINARY, "interval": 45})
    assert (status, body["code"]) == (400, "invalid_interval")


# ── the crewmate proposes, the user decides ──


def test_the_crewmates_set_check_ins_with_a_heartbeat_is_a_card_and_writes_nothing(
    _floor_monkeypatch,
) -> None:
    from aiohttp import web

    from kiro_crew.dashboard.handlers import change_cards as card_routes

    seen: list[dict] = []

    async def capture(_request, _slot_key, _sk, *, kind, params, reason):
        seen.append({"kind": kind, "params": params})
        return web.json_response({"ok": True})

    _floor_monkeypatch.setattr(card_routes, "propose_for_caller", capture)
    _seed()
    slug = _slug(ORDINARY)
    state = FakeState()
    slot = _thread(state, ORDINARY, user_sent=False)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    params = {"heartbeat": True, "interval": 30}
    status, body = _call(state, PROPOSE, {"request": "check_ins", "params": params}, slot.key)
    assert status == 200, body
    assert seen == [{"kind": "crewmate.check_ins", "params": {"member": ORDINARY, **params}}]
    assert member_goals.read_goals(slug, ORDINARY)["schedules"]["heartbeat"] is False


def test_the_tool_schema_offers_the_heartbeat_and_its_intervals() -> None:
    from kiro_crew import mcp_mate

    (tool,) = [t for t in mcp_mate._list_tools() if t["name"] == "set_check_ins"]
    props = tool["inputSchema"]["properties"]
    assert props["heartbeat"] == {"type": "boolean"}
    assert props["interval"]["enum"] == list(member_goals.HEARTBEAT_INTERVALS)


# ── a wake ──


def _armed(state: FakeState, *, user_sent: bool = False) -> tuple[str, Any]:
    _seed()
    slug = _slug(ORDINARY)
    slot = _thread(state, ORDINARY, user_sent=user_sent)
    slot.task = None
    member_goals.set_goal(slug, ORDINARY, "Keep docs current", by="user")
    member_goals.set_key_results(slug, ORDINARY, [{"text": "pages reviewed", "target": "40"}])
    member_goals.set_schedules(slug, ORDINARY, {"heartbeat": True, "interval": 60})
    return slug, slot


class _Job:
    def __init__(self, slug: str) -> None:
        self.managed_by = goal_reminder.managed_key(slug, member_goals.CADENCE_HEARTBEAT)


@pytest.mark.parametrize("has_tools", [True, False])
def test_a_wake_is_one_hidden_turn_whose_handoff_follows_the_session_tools(
    _floor_monkeypatch, has_tools: bool
) -> None:
    state = FakeState()
    slug, slot = _armed(state)
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )
    _floor_monkeypatch.setattr(goal_reminder, "member_has_session_tools", lambda _m: has_tools)
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == "started"
    ((args, kwargs),) = sent
    assert args[1] is slot and kwargs == {"human_seen": False, "mirror_reply_only": True}
    prompt = args[2]
    assert prompt.startswith("[Heartbeat]")
    assert "Keep docs current" in prompt and "pages reviewed" in prompt
    assert "nothing_to_do" in prompt and "[OPTIONS:" in prompt
    assert ("session_create" in prompt) is has_tools
    # The worker's report wakes the thread, so the wake does not poll it.
    assert ("[Worker report]" in prompt) is has_tools
    # The next wake is the patrol, whichever hand-off applies: a heartbeat turn
    # cannot arm a monitor loop.
    assert "next heartbeat is the patrol" in prompt and "monitor loop" in prompt
    # Naming a template or leaving the agent out are both the crewmate's call.
    assert "agent out" not in prompt and "template" not in prompt
    for word in ("must", "never", "exactly", "do not"):
        assert word not in prompt.lower()


def test_with_the_ledger_a_heartbeat_handoff_is_a_tracked_item() -> None:
    rec = {"goals": [{"id": "g1", "text": "Keep docs current"}], "key_results": [], "schedules": {}}
    plain = goal_reminder.heartbeat_prompt(rec, has_session_tools=True)
    tracked = goal_reminder.heartbeat_prompt(rec, has_session_tools=True, ledger=True)
    assert "bind it to the item" not in plain
    order = [
        tracked.index(w)
        for w in ("item in your work ledger", "session_create", "bind it", "session_send")
    ]
    assert order == sorted(order)
    assert "[Worker report]" in tracked
    # Without the session tools the ledger changes nothing.
    assert goal_reminder.heartbeat_prompt(
        rec, has_session_tools=False, ledger=True
    ) == goal_reminder.heartbeat_prompt(rec, has_session_tools=False)
    for word in ("must", "never", "exactly", "do not"):
        assert word not in goal_reminder._HEARTBEAT_WORKERS_LEDGER.lower()


def test_a_wake_skips_like_the_other_check_ins(_floor_monkeypatch) -> None:
    state = FakeState()
    slug, slot = _armed(state)
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )
    _floor_monkeypatch.setattr(goal_reminder, "member_has_session_tools", lambda _m: True)
    slot.task = DashboardTurn()  # type: ignore[assignment]
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == "skipped: the Bot is busy"
    slot.task = None
    member_goals.set_schedules(slug, ORDINARY, {"heartbeat": False})
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == "skipped: switch off or no goal"
    assert sent == []


def test_a_turn_that_starts_during_the_tools_check_skips_the_wake(_floor_monkeypatch) -> None:
    state = FakeState()
    slug, slot = _armed(state)
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )

    def busy_meanwhile(_member: str) -> bool:
        slot.task = DashboardTurn()  # type: ignore[assignment]
        return True

    _floor_monkeypatch.setattr(goal_reminder, "member_has_session_tools", busy_meanwhile)
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == "skipped: the Bot is busy"
    assert sent == []


def test_the_session_tools_check_answers_false_when_it_cannot_tell(_floor_monkeypatch) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("no config")

    # The class's own loader, not a name on the context facade: a facade rebind of an
    # owner-defined name would not reach the owner (test_context_composition_contract).
    _floor_monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(boom))
    assert goal_reminder.member_has_session_tools(ORDINARY) is False


@pytest.mark.parametrize(
    ("can_dispatch", "disabled", "expected"),
    [(True, False, True), (True, True, False), (False, False, False)],
)
def test_the_session_tools_check_is_the_context_builders_member_backend_answer(
    _floor_monkeypatch, can_dispatch: bool, disabled: bool, expected: bool
) -> None:
    # The same answer the context builder gives a member turn without live
    # session evidence (the configured member backend mounts session control),
    # narrowed by the user switching kirocrew-dashboard off.
    import kiro_crew.acp.session_mcp as session_mcp
    import kiro_crew.context as context_mod

    _seed()
    _floor_monkeypatch.setattr(
        context_mod, "_member_backend_can_dispatch", lambda cfg=None, crew="": can_dispatch
    )
    _floor_monkeypatch.setattr(
        session_mcp, "session_mcp_server_is_disabled", lambda *_a, **_k: disabled
    )
    assert goal_reminder.member_has_session_tools(ORDINARY) is expected


# ── the quiet end ──


def test_a_hidden_turn_carries_no_person_so_nothing_to_do_ends_it(_floor_monkeypatch) -> None:
    """``dispatch_hidden_turn`` opens the turn as the gateway, which the quiet end admits."""
    import kiro_crew.dashboard.chat as chat_mod
    import kiro_crew.dashboard.chat_handlers as chat_handlers
    import kiro_crew.dashboard.turn_dispatch as turn_dispatch

    seen: dict[str, Any] = {}

    def fake_run_chat(_state: Any, _slot: Any, _prompt: str, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "turn"

    _floor_monkeypatch.setattr(chat_mod, "_run_chat", fake_run_chat)
    _floor_monkeypatch.setattr(chat_handlers, "_sweep_stale_permissions", lambda _slot: None)
    _floor_monkeypatch.setattr(turn_dispatch, "spawn_guarded_turn", lambda _s, _slot, c: c)

    class State:
        def run_background_turn(self, _slot: Any, coro: Any) -> Any:
            return coro

        def push_slots_update(self) -> None:
            return None

    class Slot:
        key = "member-dm:scout"

    mate_welcome.dispatch_hidden_turn(State(), Slot(), "[Heartbeat] ...", human_seen=False)
    assert seen["_turn_actor"] == "gateway"
    # Any other hidden turn mirrors as before; the Heartbeat's stamp reaches the turn.
    assert seen["_mirror_reply_only"] is False
    mate_welcome.dispatch_hidden_turn(
        State(), Slot(), "[Heartbeat] ...", human_seen=False, mirror_reply_only=True
    )
    assert seen["_mirror_reply_only"] is True
    flags = {
        "producer_is_user_facing": bool(seen.get("_directive_user_origin")),
        "producer_is_self_wake": bool(seen.get("_directive_self_wake")),
        "producer_is_channel": bool(seen.get("_directive_channel_origin")),
    }
    assert flags == dict.fromkeys(flags, False)
    outcome = directive_apply._nothing_to_do({"note": "heartbeat: nothing to move"}, **flags)
    assert outcome.ends_turn is True


# ── cost: the interval floor, the default and a stopping gateway ──


def test_the_floor_and_the_default_keep_wakes_to_at_most_two_an_hour() -> None:
    # Each wake pays for the crewmate's whole context with no warm prompt cache
    # (RFC "Crewmate heartbeat", Cost and cache), so the cheapest choice is the
    # default and nothing wakes more often than every 30 minutes.
    assert min(member_goals.HEARTBEAT_INTERVALS) == 30
    assert member_goals.DEFAULT_HEARTBEAT_INTERVAL == 60
    assert member_goals.default_schedules()["interval"] == 60


def test_a_wake_while_the_gateway_is_stopping_starts_no_turn(_floor_monkeypatch) -> None:
    state = FakeState()
    slug, _slot = _armed(state)
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )
    reads: list[str] = []
    _floor_monkeypatch.setattr(
        goal_reminder, "member_has_session_tools", lambda m: reads.append(m) or False
    )
    _floor_monkeypatch.setattr(goal_reminder, "gateway_stopping", lambda: True)
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == goal_reminder.STOPPING_SKIP
    # The cron timer already stops firing on shutdown, so the one check sits
    # after the wake's reads: they run, and no turn starts.
    assert sent == [] and reads == [ORDINARY]


def test_a_stop_that_begins_during_the_wakes_reads_skips_it(_floor_monkeypatch) -> None:
    state = FakeState()
    slug, _slot = _armed(state)
    sent: list[tuple] = []
    stopping = [False]
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )

    def stop_meanwhile(_member: str) -> bool:
        stopping[0] = True
        return False

    _floor_monkeypatch.setattr(goal_reminder, "member_has_session_tools", stop_meanwhile)
    _floor_monkeypatch.setattr(goal_reminder, "gateway_stopping", lambda: stopping[0])
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == goal_reminder.STOPPING_SKIP
    assert sent == []


def test_a_wake_while_a_monitor_loop_drives_the_thread_starts_no_turn(_floor_monkeypatch) -> None:
    import kiro_crew.autonudge as autonudge
    from kiro_crew import ledger_wake

    state = FakeState()
    slug, slot = _armed(state)
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )
    _floor_monkeypatch.setattr(goal_reminder, "member_has_session_tools", lambda _m: False)
    svc = object()
    _floor_monkeypatch.setattr(autonudge, "get_instance", lambda: svc)
    asked: list[tuple] = []
    looping = [True]

    def worker_looping(service: Any, key: str) -> bool:
        asked.append((service, key))
        return looping[0]

    # The work ledger's own answer, asked about this thread with the live service.
    _floor_monkeypatch.setattr(ledger_wake, "worker_looping", worker_looping)
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == goal_reminder.LOOP_SKIP
    assert sent == [] and asked == [(svc, slot.key)]
    looping[0] = False
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == "started"
    assert len(sent) == 1


def test_a_running_gateway_is_not_stopping() -> None:
    # The real process-wide event, read inside a loop as the cron callback reads it.
    async def read() -> bool:
        return goal_reminder.gateway_stopping()

    assert asyncio.run(read()) is False


# ── the wake prompt ──


def _wake_prompt(state: FakeState, slug: str, _floor_monkeypatch) -> str:
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )
    _floor_monkeypatch.setattr(goal_reminder, "member_has_session_tools", lambda _m: False)
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == "started"
    ((args, _kwargs),) = sent
    return args[2]


def test_a_wake_prompt_leaves_met_milestones_a_next_step(_floor_monkeypatch) -> None:
    state = FakeState()
    slug, _slot = _armed(state)
    prompt = _wake_prompt(state, slug, _floor_monkeypatch)
    # Met milestones leave work that needs no one: proposing the next ones is
    # the crewmate's own tool, not a reason to idle.
    assert "set_key_results" in prompt
    assert "a push, a merge, a release" in prompt
    # On main the crewmate does its own work; nothing names a worker of its own.
    assert "your worker" not in prompt and "[Worker report]" not in prompt
    assert "a wake every 1 hr " in prompt


class _Cards:
    """A card store holding *cards* for every thread."""

    def __init__(self, cards: list[dict[str, Any]]) -> None:
        self.cards = cards

    async def warm(self) -> None:
        return None

    def pending(self, _slot_key: str) -> list[dict[str, Any]]:
        return self.cards


def test_a_waiting_milestones_card_keeps_the_wake_from_proposing_another(
    _floor_monkeypatch,
) -> None:
    from kiro_crew.dashboard import change_cards

    state = FakeState()
    slug, _slot = _armed(state)
    waiting = [{"kind": "crewmate.key_results", "status": "pending"}]
    applying = [{"kind": "crewmate.key_results", "status": "applying"}]
    for cards in (waiting, applying):
        _floor_monkeypatch.setattr(change_cards, "card_store_for", lambda _s, c=cards: _Cards(c))
        prompt = _wake_prompt(state, slug, _floor_monkeypatch)
        assert "set_key_results" not in prompt
        assert goal_reminder._HEARTBEAT_MILESTONES_WAITING in prompt
    # A finished card, another kind of card, or none: new milestones are a next step.
    finished = [{"kind": "crewmate.key_results", "status": "cancelled"}]
    other = [{"kind": "crewmate.goal", "status": "pending"}]
    for cards in (finished, other, []):
        _floor_monkeypatch.setattr(change_cards, "card_store_for", lambda _s, c=cards: _Cards(c))
        prompt = _wake_prompt(state, slug, _floor_monkeypatch)
        assert goal_reminder._HEARTBEAT_NEW_MILESTONES in prompt
        assert goal_reminder._HEARTBEAT_MILESTONES_WAITING not in prompt


def test_an_unreadable_card_store_counts_as_no_waiting_card(_floor_monkeypatch) -> None:
    from kiro_crew.dashboard import change_cards

    def boom(_s: Any) -> Any:
        raise RuntimeError("no store")

    _floor_monkeypatch.setattr(change_cards, "card_store_for", boom)
    assert asyncio.run(goal_reminder.milestones_card_waiting(FakeState(), "member-dm:x")) is False


def test_a_wake_that_cannot_move_anything_ends_at_once(_floor_monkeypatch) -> None:
    state = FakeState()
    slug, _slot = _armed(state)
    prompt = _wake_prompt(state, slug, _floor_monkeypatch)
    # The turn's text shows in the chat like any reply, so the first ask after
    # the goals is the silent end, before the wake's own work.
    line = (
        "When nothing above can move forward, call nothing_to_do at once, with no text before it."
    )
    assert line in prompt
    assert prompt.index(line) < prompt.index("This is your heartbeat")


# ── a wake into an unloaded thread ──


def _unloaded(state: FakeState, _floor_monkeypatch, *, builds: bool) -> tuple[str, Any, list]:
    """An armed crewmate whose thread no slot holds, as after a restart."""
    from kiro_crew.dashboard import chat_persistence, slot_queue_repository

    slug, slot = _armed(state)
    state._slots.pop(slot.key)
    loads: list[tuple[str, bool]] = []

    async def rehydrate(st: Any, key: str, *, adopt_closed: bool = False, **_k: Any) -> Any:
        loads.append((key, adopt_closed))
        if not builds:
            return None
        st._slots[key] = slot
        return slot

    _floor_monkeypatch.setattr(chat_persistence, "rehydrate_slot_from_history_async", rehydrate)
    _floor_monkeypatch.setattr(goal_reminder, "member_has_session_tools", lambda _m: False)

    async def drain(st: Any) -> None:
        loads.append(("drain", st is state))

    _floor_monkeypatch.setattr(slot_queue_repository, "drain_restored_parked_sends", drain)
    return slug, slot, loads


def test_a_wake_loads_an_unloaded_thread_and_starts_one_turn(_floor_monkeypatch) -> None:
    state = FakeState()
    slug, slot, loads = _unloaded(state, _floor_monkeypatch, builds=True)
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == "started"
    # Loaded once, from its transcript, adopting a closed DM thread (the
    # heartbeat switch stops a wake, not the tab), then the parked-send pass runs.
    assert loads == [(slot.key, True), ("drain", True)]
    ((args, kwargs),) = sent
    assert args[1] is slot and kwargs == {"human_seen": False, "mirror_reply_only": True}
    assert args[2].startswith("[Heartbeat]")
    # The next wake finds the thread loaded and loads nothing.
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == "started"
    assert len(loads) == 2 and len(sent) == 2


def test_a_wake_whose_thread_cannot_be_loaded_says_so(_floor_monkeypatch) -> None:
    state = FakeState()
    slug, slot, loads = _unloaded(state, _floor_monkeypatch, builds=False)
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == goal_reminder.UNLOADABLE_SKIP
    assert loads == [(slot.key, True)] and sent == []


def test_a_loaded_thread_that_is_busy_still_skips(_floor_monkeypatch) -> None:
    state = FakeState()
    slug, slot, _loads = _unloaded(state, _floor_monkeypatch, builds=True)
    slot.task = DashboardTurn()  # type: ignore[assignment]
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == "skipped: the Bot is busy"
    assert sent == []


@pytest.mark.parametrize("closer", ["user", "idle-sweep"])
def test_a_closed_mate_thread_is_reloaded_and_woken(_floor_monkeypatch, closer: str) -> None:
    """A mate's heartbeat always wakes the mate: a ✕ or an idle sweep does not stop it."""
    from kiro_crew.dashboard.channel_slots import note_slot_closed

    state = FakeState()
    slug, slot, loads = _unloaded(state, _floor_monkeypatch, builds=True)
    # Both closers go through the same close path, which records this tombstone.
    note_slot_closed(state, slot.key)  # type: ignore[arg-type]
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == "started"
    assert loads[0] == (slot.key, True)
    ((args, _kwargs),) = sent
    assert args[1] is slot


def test_only_a_mate_dm_key_adopts_a_closed_thread(_floor_monkeypatch) -> None:
    """Any other thread keeps the restore's close-stays-closed rule."""
    from kiro_crew.dashboard import chat_persistence

    state = FakeState()
    seen: list[bool] = []

    async def rehydrate(_st: Any, _key: str, *, adopt_closed: bool = False, **_k: Any) -> Any:
        seen.append(adopt_closed)
        return None

    _floor_monkeypatch.setattr(chat_persistence, "rehydrate_slot_from_history_async", rehydrate)
    assert asyncio.run(goal_reminder._load_thread(state, "chat-1-1")) == (
        goal_reminder.UNLOADABLE_SKIP
    )
    assert seen == [False]


def test_a_stopping_gateway_loads_no_thread(_floor_monkeypatch) -> None:
    state = FakeState()
    slug, _slot, loads = _unloaded(state, _floor_monkeypatch, builds=True)
    _floor_monkeypatch.setattr(goal_reminder, "gateway_stopping", lambda: True)
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == goal_reminder.STOPPING_SKIP
    assert loads == []
