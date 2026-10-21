"""The ``crewmate.check_ins`` card and the nudge a crewmate gets when its goal is set."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kiro_crew import change_card_catalog as catalog
from kiro_crew.dashboard import goal_setup
from kiro_crew.testing.wait import async_wait_until

SNAP = {
    "exists": True,
    "slug": "scout",
    "member_id": "m1",
    "has_goal": True,
    "schedules": {
        "daily": False,
        "weekly": False,
        "monthly": False,
        "weekday": 1,
        "month_day": 1,
        "time": "09:00",
    },
}


def _params(**over: Any) -> dict[str, Any]:
    return catalog.validate_params(catalog.KIND_CREWMATE_CHECK_INS, {"member": "scout", **over})


def test_the_card_turns_check_ins_on_and_undo_puts_them_back() -> None:
    p = _params(daily=True, weekly=True, weekday=3, time="08:30")
    preview = catalog.build_preview(catalog.KIND_CREWMATE_CHECK_INS, p, SNAP, {"label": "Scout"})
    assert preview["title"] == "Set the check-ins for “Scout”"
    rows = {r["field"]: (r["before"], r["after"]) for r in preview["changes"]}
    assert rows == {
        "daily": ("Off", "On"),
        "weekly": ("Off", "On"),
        "weekday": ("Monday", "Wednesday"),
        "time": ("09:00", "08:30"),
    }
    (step,) = preview["apply"]
    assert step["method"] == "PUT" and step["path"] == "/api/members/scout/goals/schedules"
    assert step["body"] == {
        "member": "scout",
        "daily": True,
        "weekly": True,
        "weekday": 3,
        "time": "08:30",
        "expected": {"daily": False, "weekly": False, "weekday": 1, "time": "09:00"},
    }
    plan, reason = catalog.build_undo(catalog.KIND_CREWMATE_CHECK_INS, p, SNAP, [{}], 1)
    assert reason is None
    (undo,) = plan
    assert undo["body"] == {
        "member": "scout",
        "daily": False,
        "weekly": False,
        "weekday": 1,
        "time": "09:00",
        "expected": {"daily": True, "weekly": True, "weekday": 3, "time": "08:30"},
    }


def test_the_card_refuses_what_changes_nothing_and_a_switch_with_no_goal() -> None:
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(catalog.KIND_CREWMATE_CHECK_INS, _params(daily=False), SNAP, {})
    assert exc.value.code == "no_change"
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(
            catalog.KIND_CREWMATE_CHECK_INS, _params(daily=True), {**SNAP, "has_goal": False}, {}
        )
    assert exc.value.code == "no_goal"


@pytest.mark.parametrize(
    "bad",
    [{}, {"time": "9:00"}, {"weekday": 7}, {"month_day": 29}, {"daily": "yes"}, {"other": 1}],
)
def test_the_card_validates_its_fields(bad: dict) -> None:
    with pytest.raises(catalog.CardCatalogError):
        _params(**bad)


def test_after_shows_the_write_compares_the_fields_the_card_set() -> None:
    from kiro_crew.dashboard import change_cards as cards

    p = _params(daily=True)
    on = {"schedules": {**SNAP["schedules"], "daily": True}}
    assert cards.after_shows_the_write(catalog.KIND_CREWMATE_CHECK_INS, p, on, [{}])
    assert not cards.after_shows_the_write(catalog.KIND_CREWMATE_CHECK_INS, p, SNAP, [{}])


@pytest.mark.parametrize(
    ("before", "after", "nudge"),
    [
        ("", "Keep the release notes current", True),
        ("Keep the release notes current", "Cut the build time in half", True),
        ("Keep the release notes current", "Keep the release notes curent", False),
        ("Keep the release notes current", "  keep the release   notes current ", False),
        ("Keep the release notes current", "", False),
    ],
    ids=["first-goal", "new-goal", "a-typo", "spacing-and-case", "cleared"],
)
def test_only_a_new_goal_is_worth_a_nudge(before: str, after: str, nudge: bool) -> None:
    assert goal_setup.changed_enough(before, after) is nudge


GOAL_A = {"id": "g00000001", "text": "Keep the release notes current"}
GOAL_B = {"id": "g00000002", "text": "Cut the build time in half"}


def test_the_kickoff_hands_the_research_to_a_worker_then_proposes() -> None:
    rec = {
        "goals": [GOAL_A],
        "key_results": [],
        "schedules": {"daily": False, "weekly": False, "monthly": False},
    }
    text = goal_setup.kickoff_prompt(rec, GOAL_A["id"])
    assert text is not None
    assert "Keep the release notes current" in text
    # A crewmate's chat starts no sub-agents: the research goes to a worker session.
    assert "session_create" in text and "session_send" in text
    # It dispatches and ends the turn; the worker's report wakes it, so nothing polls.
    assert "end this turn" in text and "[Worker report]" in text
    assert "session_read_message" not in text and "call wait" not in text
    assert "spawn_run" not in text and "sub-agent" not in text
    assert "set_key_results" in text and "goal_ids" in text
    # Guidance toward a small set, not a cap.
    assert "easier to track" in text and "merging" in text
    # Check-ins wait for the milestones to be confirmed (milestones_prompt).
    assert "set_check_ins" not in text


def test_the_kickoff_names_the_goal_that_changed_and_lists_them_all() -> None:
    rec = {"goals": [GOAL_A, GOAL_B], "key_results": [], "schedules": {}}
    text = goal_setup.kickoff_prompt(rec, GOAL_B["id"])
    assert text is not None
    assert "set goal 2 (g00000002)" in text
    assert "1. Keep the release notes current" in text and "2. Cut the build time" in text
    # A goal removed before the turn runs sends nothing.
    assert goal_setup.kickoff_prompt({**rec, "goals": [GOAL_A]}, GOAL_B["id"]) is None


def test_the_milestones_nudge_offers_check_ins_only_when_none_is_on() -> None:
    rec = {
        "goals": [GOAL_A],
        "key_results": [
            {"id": "k1", "text": "notes out", "target": "yes", "goal_ids": [GOAL_A["id"]]}
        ],
        "schedules": {"daily": False, "weekly": False, "monthly": False},
    }
    off = goal_setup.milestones_prompt(rec)
    assert off is not None and "[Milestones confirmed]" in off
    assert "notes out" in off and "set_check_ins" in off
    assert "(serves goal 1)" in off and "1. Keep the release notes current" in off
    on = goal_setup.milestones_prompt({**rec, "schedules": {"weekly": True}})
    assert on is not None and "[Milestones confirmed]" in on and "set_check_ins" not in on
    assert goal_setup.milestones_prompt({**rec, "key_results": []}) is None


@pytest.mark.parametrize("has_mate_tools", [True, False])
def test_the_milestones_nudge_offers_the_heartbeat_too(has_mate_tools: bool) -> None:
    """The Heartbeat is off by default and only a check-ins card turns it on, so the
    offer after the milestones are confirmed names it beside the three reminders."""
    rec = {
        "goals": [GOAL_A],
        "key_results": [{"id": "k1", "text": "notes out", "target": "yes"}],
        "schedules": {},
    }
    text = goal_setup.milestones_prompt(rec, has_mate_tools=has_mate_tools)
    assert text is not None
    for name in ("Daily Goal Reminder", "Weekly report", "Monthly review", "Heartbeat"):
        assert name in text
    assert "keeps the work moving while they are away" in text
    assert "Dashboard tab" in text


def test_the_removed_nudge_names_the_goal_and_its_orphaned_milestones() -> None:
    rec = {
        "goals": [GOAL_B],
        "key_results": [
            {"id": "k1", "text": "notes out", "target": "yes", "goal_ids": []},
            {"id": "k2", "text": "build under 5m", "target": "yes", "goal_ids": [GOAL_B["id"]]},
        ],
        "schedules": {"daily": True},
        "removed_goal": {"goal": GOAL_A, "index": 0, "at": 1, "key_result_ids": ["k1"]},
    }
    text = goal_setup.removed_prompt(rec, GOAL_A["id"])
    assert text is not None and "[Goal removed]" in text
    assert GOAL_A["text"] in text and "1. Cut the build time in half" in text
    assert "- notes out" in text and "build under 5m" not in text
    assert "set_key_results" in text
    # Nothing orphaned: acknowledge only.
    plain = goal_setup.removed_prompt(
        {**rec, "removed_goal": {**rec["removed_goal"], "key_result_ids": ["k2"]}}, GOAL_A["id"]
    )
    assert plain is not None and "set_key_results" not in plain
    assert plain.endswith("stop work that only served it. Ask nothing else.")
    # No goal left: say the check-ins went off.
    empty = goal_setup.removed_prompt({**rec, "goals": []}, GOAL_A["id"])
    assert empty is not None and "every check-in was switched off" in empty
    # Undone (or superseded by another removal) before the turn runs: nothing.
    assert goal_setup.removed_prompt({**rec, "removed_goal": None}, GOAL_A["id"]) is None
    assert goal_setup.removed_prompt(rec, GOAL_B["id"]) is None


def test_every_goal_nudge_quotes_goals_and_milestones_scrubbed() -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    goal = {"id": "g00000001", "text": f"Rotate {secret} weekly"}
    other = {"id": "g00000002", "text": f"Audit {secret} usage"}
    gone = {"id": "g00000003", "text": f"Drop {secret} keys"}
    kr = {"id": "k1", "text": f"key {secret} gone", "target": f"no {secret}", "goal_ids": []}
    rec = {
        "goals": [goal, other],
        "key_results": [kr],
        "schedules": {},
        "removed_goal": {"goal": gone, "index": 2, "at": 1, "key_result_ids": ["k1"]},
    }
    for text in (
        goal_setup.kickoff_prompt(rec, other["id"]),
        goal_setup.removed_prompt(rec, gone["id"]),
        goal_setup.milestones_prompt(rec),
    ):
        assert text is not None and secret not in text and "[REDACTED" in text


class _Slot:
    def __init__(self) -> None:
        from kiro_crew import members as members_mod

        self.key = "member-dm:scout"
        self.mode = members_mod.DM_SLOT_MODE
        self.agent = "scout"
        self.executor = ""
        self.running = True
        self._queue: list = []


class _State:
    def __init__(self, slot: _Slot) -> None:
        self.slot = slot

    def get_slot(self, key: str) -> _Slot | None:
        return self.slot if key == self.slot.key else None


def test_the_nudge_waits_for_a_busy_thread_then_sends_once(monkeypatch) -> None:
    from kiro_crew import member_goals
    from kiro_crew import members as members_mod
    from kiro_crew.dashboard import goal_reminder, mate_welcome

    slot = _Slot()
    state = _State(slot)
    sent: list[tuple] = []
    monkeypatch.setattr(goal_setup, "POLL_SECS", 0)
    busy_checks: list[str] = []
    real_idle_thread = goal_reminder._idle_thread

    def idle_thread(*args):
        found, skip = real_idle_thread(*args)
        if slot.running:
            assert found is None
            busy_checks.append(skip)
        return found, skip

    monkeypatch.setattr(goal_reminder, "_idle_thread", idle_thread)
    monkeypatch.setattr(
        members_mod, "read_dm_binding", lambda _slug: {"member": "scout", "slot_key": slot.key}
    )
    monkeypatch.setattr(
        member_goals,
        "read_goals",
        lambda _s, _m: {"goals": [GOAL_A, GOAL_B], "key_results": [], "schedules": {}},
    )
    monkeypatch.setattr(mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k)))

    async def go() -> None:
        goal_setup.schedule_nudge(state, "scout", "scout", GOAL_A["id"])
        first = state._goal_setup_tasks["scout"]
        await async_wait_until(lambda: bool(busy_checks))
        # A newer save replaces a nudge that has already examined the busy slot.
        goal_setup.schedule_nudge(state, "scout", "scout", GOAL_B["id"])
        second = state._goal_setup_tasks["scout"]
        await async_wait_until(first.done)
        assert first.cancelled()
        busy_checks.clear()
        await async_wait_until(lambda: bool(busy_checks))
        assert sent == []
        slot.running = False
        await asyncio.wait_for(second, timeout=30)
        assert state._goal_setup_tasks == {}

    asyncio.run(go())
    assert len(sent) == 1
    args, kwargs = sent[0]
    assert args[1] is slot and "[Goal set]" in args[2]
    assert "set goal 2" in args[2]
    assert kwargs == {"human_seen": True}


def test_an_applied_capabilities_card_wakes_its_crewmate_to_use_it(monkeypatch) -> None:
    """A card press starts no turn; the grant used to wait until the user typed."""
    from kiro_crew import member_goals
    from kiro_crew import members as members_mod
    from kiro_crew.dashboard import mate_welcome

    slot = _Slot()
    slot.running = False
    state = _State(slot)
    sent: list[tuple] = []
    monkeypatch.setattr(goal_setup, "POLL_SECS", 0)
    monkeypatch.setattr(members_mod, "member_slug", lambda _n, _c=None: "scout")
    monkeypatch.setattr(
        members_mod, "read_dm_binding", lambda _slug: {"member": "scout", "slot_key": slot.key}
    )
    monkeypatch.setattr(
        member_goals, "read_goals", lambda _s, _m: {"goals": [], "key_results": [], "schedules": {}}
    )
    monkeypatch.setattr(mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k)))

    async def go() -> None:
        goal_setup.schedule_capabilities_nudge(state, slot.key, "Add teamchat to Scout's worker")
        goal_setup.schedule_capabilities_nudge(state, "member-dm:other", "Add x")
        await async_wait_until(lambda: bool(sent))

    asyncio.run(go())
    ((args, _kwargs),) = sent
    assert args[1] is slot
    assert args[2].startswith("[Capabilities added]")
    assert "Add teamchat to Scout's worker" in args[2]
    assert "session_create" in args[2]
