"""A crewmate's goal and "waiting on you" list as crew-log folds (``dashboard.crewmate_feed``).

Pins the round-5 contract for a crewmate's own Dashboard page:

* the gateway appends a snapshot when the fact changes and only then, so a page
  bound to the ``goal`` or ``waiting`` fold moves with no ``dashboard_write``;
* a reply, an [OPTIONS] chip or a card press empties the waiting fold by the next
  snapshot;
* a goal-record write reaches the feed through ``member_goals``' change listener;
* the folds keep the newest snapshot whole and re-clamp what they read.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kiro_crew import member_goals
from kiro_crew.crew_log import CrewLog, emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import schema as lg
from kiro_crew.dashboard import crewmate_feed
from kiro_crew.history import HUMAN_TURN_META_KEY

ASK = "Open a PR for the docs fix? [OPTIONS: Open the PR | Not yet]"
SLOT = "member-dm:atlas"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    crew_log.forget_slot_folds()
    crewmate_feed._last.clear()
    crewmate_feed._held_goals.clear()
    yield
    crewmate_feed._last.clear()
    crewmate_feed._held_goals.clear()


@pytest.fixture
def appended(monkeypatch):
    seen: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        emit, "on_waiting_changed", lambda sid, data: seen.append(("waiting", sid, data))
    )
    monkeypatch.setattr(
        emit, "on_goal_recorded", lambda sid, data: seen.append(("goal", sid, data))
    )
    monkeypatch.setattr(crewmate_feed, "_session_of", lambda slot: "s-atlas")
    return seen


async def _drain() -> None:
    """Await the settle task and every task it starts (a goal read)."""
    while crewmate_feed._keep:
        await asyncio.gather(*list(crewmate_feed._keep))


class _Slot:
    key = SLOT
    mode = "member"
    agent = "Atlas"

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self._question_pending: dict[str, Any] = {}

    def sidebar_messages(self) -> list[dict[str, Any]]:
        return self.rows


class _State:
    def __init__(self, slot: _Slot) -> None:
        self._slots = {slot.key: slot}
        self._pending_questions: dict[str, Any] = {}

    def get_slot(self, key: str) -> Any:
        return self._slots.get(key)


class _Cards:
    def __init__(self, cards: list[dict[str, Any]]) -> None:
        self.cards = cards

    async def warm(self) -> None:
        return None

    def pending(self, _slot_key: str) -> list[dict[str, Any]]:
        return self.cards


def _cards(monkeypatch, cards: list[dict[str, Any]]) -> None:
    from kiro_crew.dashboard import change_cards

    monkeypatch.setattr(change_cards, "card_store_for", lambda _s: _Cards(cards))


def test_the_waiting_list_is_recorded_when_it_changes_and_only_then(appended, monkeypatch):
    _cards(monkeypatch, [])
    slot = _Slot([{"role": "assistant", "content": ASK, "ts": "2026-10-18T18:40:00+00:00"}])
    state = _State(slot)
    assert crewmate_feed.record_waiting(state, slot) is True
    # The append the check causes, or a wake that writes no row, records nothing new.
    assert crewmate_feed.record_waiting(state, slot) is False
    ((kind, sid, data),) = appended
    assert (kind, sid) == ("waiting", "s-atlas")
    assert [i["text"] for i in data["items"]] == ["Open a PR for the docs fix?"]
    # The person presses the OPTIONS chip in the chat: a human row, and the list empties.
    slot.rows.append(
        {
            "role": "user",
            "content": "Open the PR",
            "ts": "2026-10-18T18:41:00+00:00",
            "meta": {HUMAN_TURN_META_KEY: True},
        }
    )
    assert crewmate_feed.record_waiting(state, slot) is True
    assert appended[-1][2] == {"items": []}


def test_a_card_press_empties_the_waiting_list_with_no_typed_reply(appended, monkeypatch):
    slot = _Slot([{"role": "assistant", "content": ASK, "ts": "2026-10-18T18:40:00+00:00"}])
    state = _State(slot)
    _cards(monkeypatch, [{"status": "pending", "title": "Save tools", "created_at": 1792348860.0}])
    crewmate_feed.record_waiting(state, slot)
    assert [i["kind"] for i in appended[-1][2]["items"]] == ["card", "options"]
    _cards(monkeypatch, [{"status": "applied", "finished_at": 1792348900.0}])
    assert crewmate_feed.record_waiting(state, slot) is True
    assert appended[-1][2] == {"items": []}


def test_a_goal_write_reaches_the_feed_and_is_recorded_once(appended, monkeypatch):
    slot = _Slot([])
    state = _State(slot)
    heard: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(member_goals, "_change_listeners", [lambda *a: heard.append(a)])
    slug = "atlas"
    member_goals.set_goal(slug, "Atlas", "Keep docs current", by="user")
    member_goals.set_key_results(slug, "Atlas", [{"text": "Fixes proposed", "target": "3"}])
    (kr,) = member_goals.read_goals(slug, "Atlas")["key_results"]
    member_goals.record_key_result(slug, "Atlas", kr["id"], "4 fixes proposed")
    assert len(heard) == 3
    monkeypatch.setattr(crewmate_feed, "_state", lambda: state)
    crewmate_feed._goal_written("Atlas", heard[-1][2])
    crewmate_feed._goal_written("Atlas", heard[-1][2])
    ((kind, _sid, data),) = appended
    assert kind == "goal"
    assert data["goals"] == [{"text": "Keep docs current"}]
    (milestone,) = data["milestones"]
    assert milestone["current"] == "4 fixes proposed"
    assert milestone["met"] is True
    assert milestone["at"]


def test_a_goal_written_on_an_idle_thread_is_recorded_on_its_next_growth(appended, monkeypatch):
    """The sync smoke's Pim: milestones pressed while the thread had no live client, so
    there was no session to log into and the page stayed at 0/0/0. The snapshot is held
    (newest wins) and recorded when the thread next grows and names its session."""
    slot = _Slot([])
    state = _State(slot)
    monkeypatch.setattr(crewmate_feed, "_state", lambda: state)
    live = {"sid": ""}
    monkeypatch.setattr(crewmate_feed, "_session_of", lambda _slot: live["sid"])
    _cards(monkeypatch, [])
    before = {"goals": [{"text": "Sort plants"}], "key_results": []}
    after = {
        "goals": [{"text": "Sort plants"}],
        "key_results": [{"text": "list sorts by last watered", "target": "yes"}],
    }
    crewmate_feed._goal_written("Atlas", before)
    crewmate_feed._goal_written("Atlas", after)
    assert appended == []  # nothing to log into yet, and nothing lost
    live["sid"] = "s-atlas"

    async def go() -> None:
        crewmate_feed._pending.add("s-atlas")
        crewmate_feed._settle()
        await _drain()

    asyncio.run(go())
    goals = [entry for entry in appended if entry[0] == "goal"]
    assert len(goals) == 1
    assert goals[0][1] == "s-atlas"
    assert [m["text"] for m in goals[0][2]["milestones"]] == ["list sorts by last watered"]
    assert crewmate_feed._held_goals == {}


_units = iter(range(1000))


def _fold(name: str, entries: list[tuple[str, dict]]) -> dict[str, Any]:
    unit = f"s-feed-{next(_units)}"
    slot = f"{SLOT}-{unit}"
    handle = CrewLog.create(lg.KIND_SESSION, unit, owner="raymond", agent="kirocrew", slot=slot)
    for entry_type, data in entries:
        handle.append(entry_type, data, src="gateway")
    handle.release_ownership()
    fold = crew_log._FOLDS[name]
    return fold.render(crew_log.fold_slot_warm(name, (unit,), slot=slot).state)


def test_the_goal_fold_keeps_the_newest_snapshot_with_its_counts():
    first = {"goals": [{"text": "Ship"}], "milestones": []}
    second = {
        "goals": [{"text": "Ship 0.3"}],
        "milestones": [
            {"text": "Fixes", "target": "3", "current": "4", "met": True, "at": "t"},
            {"text": "Docs", "target": "yes", "current": "no", "met": False, "at": "t"},
        ],
    }
    value = _fold("goal", [("dashboard/goal_recorded", first), ("dashboard/goal_recorded", second)])
    assert value["goal"] == "Ship 0.3"
    assert (value["milestones_total"], value["milestones_met"], value["milestones_open"]) == (
        2,
        1,
        1,
    )
    assert value["all_met"] is False
    assert value["changes"] == 2


def test_the_waiting_fold_empties_on_the_next_snapshot_and_reclamps():
    item = {"kind": "options", "text": "x" * 999, "options": ["a"] * 20, "asked_at": "t"}
    value = _fold("waiting", [("dashboard/waiting_changed", {"items": [item] * 20})])
    assert value["count"] == crew_log.CREWMATE_WAITING_LIMIT
    assert len(value["newest"]) == crew_log.CREWMATE_TEXT_LIMIT
    assert len(value["items"][0]["options"]) == crew_log.CREWMATE_OPTION_LIMIT
    value = _fold(
        "waiting",
        [
            ("dashboard/waiting_changed", {"items": [item]}),
            ("dashboard/waiting_changed", {"items": []}),
        ],
    )
    assert (value["count"], value["newest"]) == (0, "")


def test_start_listens_to_log_growth_and_goal_writes(monkeypatch):
    import kiro_crew.constants as constants

    grown: list[Any] = []
    monkeypatch.setattr(constants, "crew_log_enabled", lambda: True)
    monkeypatch.setattr(emit, "add_growth_listener", grown.append)
    monkeypatch.setattr(member_goals, "_change_listeners", [])
    monkeypatch.setattr(crewmate_feed, "_registered", False)
    crewmate_feed.start(_State(_Slot([])))
    assert grown == [crewmate_feed._on_log_growth]
    assert member_goals._change_listeners == [crewmate_feed._on_goal_written]


def test_a_save_reply_lists_each_field_with_its_source_and_flags_the_manual_ones():
    from kiro_crew.dashboard.handlers import agent_panel

    package = {
        "model": {
            "types": {
                "milestones": {"type": "array", "source": {"fold": "goal", "path": "milestones"}},
                "waiting": {"type": "number", "source": {"fold": "waiting", "path": "count"}},
                "verdict": {"type": "string", "source": {"agentic": True}},
            }
        }
    }
    assert agent_panel.field_sources(package) == [
        {"field": "milestones", "source": "fold:goal.milestones", "type": "array"},
        {"field": "waiting", "source": "fold:waiting.count", "type": "number"},
        {"field": "verdict", "source": "manual", "type": "string"},
    ]


def test_a_goal_set_before_the_thread_had_a_session_is_recorded_on_its_first_growth(
    appended, monkeypatch
):
    """The create flow sets the goal before the thread's first turn opens a session,
    so the write's own snapshot had nowhere to land; the thread's first growth records it."""
    from kiro_crew import members as members_mod

    _cards(monkeypatch, [])
    slot = _Slot([])
    state = _State(slot)
    monkeypatch.setattr(crewmate_feed, "_state", lambda: state)
    monkeypatch.setattr(members_mod, "member_slug", lambda _n, _c=None: "atlas")
    member_goals.set_goal("atlas", "Atlas", "Keep docs current", by="user")

    async def go() -> None:
        crewmate_feed._pending.add("s-atlas")
        crewmate_feed._settle()
        await _drain()

    asyncio.run(go())
    kinds = [kind for kind, _sid, _data in appended]
    assert kinds == ["waiting", "goal"]
    assert appended[-1][2]["goals"] == [{"text": "Keep docs current"}]
