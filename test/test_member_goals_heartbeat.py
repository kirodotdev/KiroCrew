"""Crewmate heartbeat: the fourth check-in, which wakes the crewmate to work on its own.

What these tests pin (RFC "Crewmate heartbeat"):

* the switch is off by default, and a record written before it reads as off at
  the default 60-minute interval;
* the interval is one of 30, 60, 120 or 240 minutes, and its cron
  expression follows the clock's marks;
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

from kiro_crew import member_goals
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
    assert set(goal_reminder._HEARTBEAT_EXPRS) == set(member_goals.HEARTBEAT_INTERVALS)


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
    assert args[1] is slot and kwargs == {"human_seen": False}
    prompt = args[2]
    assert prompt.startswith("[Heartbeat]")
    assert "Keep docs current" in prompt and "pages reviewed" in prompt
    assert "nothing_to_do" in prompt and "[OPTIONS:" in prompt
    assert ("session_create" in prompt) is has_tools
    assert ("session_read_message" in prompt) is has_tools
    # The next wake is the patrol, whichever hand-off applies: a heartbeat turn
    # cannot arm a monitor loop.
    assert "next heartbeat is the patrol" in prompt and "monitor loop" in prompt
    # Naming a template or leaving the agent out are both the crewmate's call.
    assert "agent out" not in prompt and "template" not in prompt
    for word in ("must", "never", "exactly", "do not"):
        assert word not in prompt.lower()


def test_a_wake_skips_like_the_other_check_ins(_floor_monkeypatch) -> None:
    state = FakeState()
    slug, slot = _armed(state)
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )
    _floor_monkeypatch.setattr(goal_reminder, "member_has_session_tools", lambda _m: True)
    slot.task = DashboardTurn()  # type: ignore[assignment]
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == "skipped: the crewmate is busy"
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
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == "skipped: the crewmate is busy"
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
        context_mod, "_member_backend_can_dispatch", lambda cfg=None: can_dispatch
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
    # Every hidden turn holds its pre-tool narration (test_heartbeat_quiet_end).
    assert seen["_hidden_turn"] is True
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
    # Checked before the wake's own reads, so a stopping gateway does none of them.
    assert sent == [] and reads == []


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


def test_a_running_gateway_is_not_stopping() -> None:
    # The real process-wide event, read inside a loop as the cron callback reads it.
    async def read() -> bool:
        return goal_reminder.gateway_stopping()

    assert asyncio.run(read()) is False


# ── milestones that are all met ──


class _Cards:
    """A card store holding *cards* for every thread."""

    def __init__(self, cards: list[dict[str, Any]]) -> None:
        self.cards = cards

    async def warm(self) -> None:
        return None

    def pending(self, _slot_key: str) -> list[dict[str, Any]]:
        return self.cards


def _wake_prompt(state: FakeState, slug: str, _floor_monkeypatch, cards: list) -> str:
    from kiro_crew.dashboard import change_cards

    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )
    _floor_monkeypatch.setattr(goal_reminder, "member_has_session_tools", lambda _m: False)
    _floor_monkeypatch.setattr(change_cards, "card_store_for", lambda _s: _Cards(cards))
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == "started"
    ((args, _kwargs),) = sent
    return args[2]


def _read(slug: str, value: str) -> None:
    (kr,) = member_goals.read_goals(slug, ORDINARY)["key_results"]
    member_goals.record_key_result(slug, ORDINARY, kr["id"], value)


def test_a_wake_prompt_leaves_met_milestones_a_next_step(_floor_monkeypatch) -> None:
    state = FakeState()
    slug, _slot = _armed(state)
    prompt = _wake_prompt(state, slug, _floor_monkeypatch, [])
    # Met milestones leave work that needs no one: proposing the next ones is
    # the crewmate's own tool, not a reason to idle.
    assert "set_key_results" in prompt
    assert "a push, a merge, a release" in prompt
    # On main the crewmate does its own work; nothing names a worker of its own.
    assert "your worker" not in prompt and "[Worker report]" not in prompt
    assert "a wake every 1 hr " in prompt


def test_a_wake_after_every_milestone_is_met_carries_the_gateways_line(
    _floor_monkeypatch,
) -> None:
    state = FakeState()
    slug, _slot = _armed(state)
    _read(slug, "12")
    prompt = _wake_prompt(state, slug, _floor_monkeypatch, [])
    assert goal_reminder.MILESTONES_MET_LINE not in prompt
    assert goal_reminder.MILESTONES_PENDING_LINE not in prompt
    _read(slug, "40")
    prompt = _wake_prompt(state, slug, _floor_monkeypatch, [])
    assert goal_reminder.MILESTONES_MET_LINE in prompt
    # The line sits under the progress block, before the wake's own text.
    assert prompt.index(goal_reminder.MILESTONES_MET_LINE) < prompt.index("This is your heartbeat")
    for word in ("must", "never", "exactly", "do not"):
        assert word not in goal_reminder.MILESTONES_MET_LINE.lower()


def test_the_line_asks_for_the_next_milestones_card_before_any_work() -> None:
    line = goal_reminder.MILESTONES_MET_LINE
    assert "first step is the next milestones" in line
    assert "set_key_results" in line
    assert line.index("set_key_results") < line.index("Work toward the goal")


def test_a_waiting_milestones_card_turns_the_line_into_doing_work(_floor_monkeypatch) -> None:
    state = FakeState()
    slug, _slot = _armed(state)
    _read(slug, "done")
    waiting = [{"kind": "crewmate.key_results", "status": "pending"}]
    applying = [{"kind": "crewmate.key_results", "status": "applying"}]
    finished = [{"kind": "crewmate.key_results", "status": "cancelled"}]
    other = [{"kind": "crewmate.goal", "status": "pending"}]
    for cards in (waiting, applying):
        prompt = _wake_prompt(state, slug, _floor_monkeypatch, cards)
        assert goal_reminder.MILESTONES_MET_LINE not in prompt
        assert goal_reminder.MILESTONES_PENDING_LINE in prompt
    for cards in (finished, other):
        prompt = _wake_prompt(state, slug, _floor_monkeypatch, cards)
        assert goal_reminder.MILESTONES_MET_LINE in prompt
        assert goal_reminder.MILESTONES_PENDING_LINE not in prompt
    for word in ("must", "never", "exactly", "do not"):
        assert word not in goal_reminder.MILESTONES_PENDING_LINE.lower()


def test_an_unreadable_card_store_counts_as_no_card(_floor_monkeypatch) -> None:
    from kiro_crew.dashboard import change_cards

    state = FakeState()
    slug, _slot = _armed(state)
    _read(slug, "done")

    def boom(_s: Any) -> Any:
        raise RuntimeError("no store")

    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )
    _floor_monkeypatch.setattr(goal_reminder, "member_has_session_tools", lambda _m: False)
    _floor_monkeypatch.setattr(change_cards, "card_store_for", boom)
    assert asyncio.run(goal_reminder.fire(state, _Job(slug))) == "started"
    ((args, _kwargs),) = sent
    assert goal_reminder.MILESTONES_MET_LINE in args[2]


@pytest.mark.parametrize(
    ("text", "target", "readings", "met"),
    [
        ("Tests written", "40", ["12", "40"], True),
        ("Tests written", "40", ["12", "39"], False),
        ("Tests", "0 failing", ["3 failing", "0 failing"], True),
        ("Tests", "0 failing", ["3 failing", "1 failing"], False),
        ("Reduce open bugs", "below 5", ["9", "4"], True),
        ("Reduce open bugs", "5 or fewer", ["9", "6"], False),
        # Only the target sets the direction: a verb in the milestone's own text does not.
        ("Open 5 pull requests", "5", ["1"], False),
        ("Open 5 pull requests", "5", ["5"], True),
        ("Cut a release", "1", ["0"], False),
        ("Docs pages", "5", ["2", "6"], True),
        ("Docs pages", "5", ["4"], False),
        ("Flaky tests", "<= 2", ["4", "2"], True),
        ("Coverage", "> 80%", ["85%"], True),
        ("Coverage", "> 80%", ["80%"], False),
        ("Docs", "all docs in sync", ["all docs in sync since the 4pm pass"], True),
        ("Docs", "all docs in sync", ["two docs still behind"], False),
        ("Merged", "yes", ["done: merged on main"], True),
        ("Steps", "5 of 5", ["done 3 of 5"], False),
        ("Tests written", "40", [], False),
        ("Fixes proposed for triaged issues", "3", ["4 fixes proposed"], True),
        ("Fixes proposed for triaged issues", "3", ["2 fixes proposed"], False),
        ("Fixes proposed", "3 fixes", ["4 fixes proposed"], True),
        ("Passing test count", ">= 5", ["5"], True),
        ("Proposed fixes landed with a passing test", "1 or more", ["1 (ISSUE-1 fixed)"], True),
        ("Known bugs found and logged into issues.md", "1 or more", ["0 so far"], False),
        ("README commands match the real CLI (mismatch count)", "0", ["2 mismatches"], False),
        ("CI runs the tests on every push", "yes", ["workflow added; not yet run"], False),
        ("Bugs below", "at most 2", ["3"], False),
        ("Open issues triaged this week", "2 or more", ["3 triaged"], True),
        ("Pages written", "at most 4", ["5"], False),
        ("Pages", "3 pages", ["4 failing"], False),
    ],
)
def test_a_milestone_is_met_only_when_its_reading_settles_it(
    text: str, target: str, readings: list[str], met: bool
) -> None:
    from kiro_crew.member_milestones import key_result_met

    kr = {
        "text": text,
        "target": target,
        "current": readings[-1] if readings else "",
        "readings": [{"value": v, "at": float(i)} for i, v in enumerate(readings)],
    }
    assert key_result_met(kr) is met


def test_a_goal_with_no_milestones_counts_as_met_and_no_goal_does_not() -> None:
    from kiro_crew.member_milestones import milestones_all_met

    goal = {"goals": [{"id": "g0000000a", "text": "Ship"}], "key_results": []}
    assert milestones_all_met(goal) is True
    assert milestones_all_met({"goals": [], "key_results": []}) is False
