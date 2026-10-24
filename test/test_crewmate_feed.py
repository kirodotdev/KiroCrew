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
from kiro_crew.testing.wait import async_wait_until

ASK = "Open a PR for the docs fix? [OPTIONS: Open the PR | Not yet]"
SLOT = "member-dm:atlas"


def _reset_feed_state() -> None:
    """Clear every module-level cache in crewmate_feed between tests.

    Includes the async-scheduling state (_keep, _pending, _timer) that
    asyncio.run() does not guarantee to drain when its main coroutine returns
    before the 0.4 s settle timer fires: the timer can fire during the cleanup
    phase of asyncio.run() (after _cancel_all_tasks snapshots tasks), creating
    _settle_grown -> _record_current_goal tasks whose _keep.discard callbacks
    end up discarded when loop.close() clears the _ready queue.  Any stale Task
    left in _keep from a closed loop causes _drain()'s asyncio.gather to hang
    forever because Python 3.12 routes the gather's child-done callback through
    the old closed loop, which never resolves the outer future.
    """
    if crewmate_feed._timer is not None:
        crewmate_feed._timer.cancel()
        crewmate_feed._timer = None
    crewmate_feed._pending.clear()
    crewmate_feed._keep.clear()
    crewmate_feed._recorded_in.clear()
    crewmate_feed._last.clear()
    crewmate_feed._inflight.clear()
    crewmate_feed._sessions.clear()
    crewmate_feed._held_seq.clear()
    crewmate_feed._held_goals.clear()
    crewmate_feed._stale_goals.clear()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    crew_log.forget_slot_folds()
    _reset_feed_state()
    yield
    _reset_feed_state()


@pytest.fixture
def appended(monkeypatch):
    seen: list[tuple[str, str, dict]] = []

    def writer(kind: str) -> Any:
        def write(sid: str, data: dict, *, on_settled: Any = None) -> None:
            seen.append((kind, sid, data))
            if on_settled is not None:
                on_settled(True)

        return write

    monkeypatch.setattr(emit, "on_waiting_changed", writer("waiting"))
    monkeypatch.setattr(emit, "on_goal_recorded", writer("goal"))
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

    def last_finished_at(self, _slot_key: str, statuses: Any) -> float:
        done = [c.get("finished_at", 0.0) for c in self.cards if c.get("status") in statuses]
        return float(max(done, default=0.0))


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
    assert appended[-1][2] == {"items": [], "total": 0}


def test_a_card_press_empties_the_waiting_list_with_no_typed_reply(appended, monkeypatch):
    slot = _Slot([{"role": "assistant", "content": ASK, "ts": "2026-10-18T18:40:00+00:00"}])
    state = _State(slot)
    _cards(monkeypatch, [{"status": "pending", "title": "Save tools", "created_at": 1792348860.0}])
    crewmate_feed.record_waiting(state, slot)
    assert [i["kind"] for i in appended[-1][2]["items"]] == ["card", "options"]
    _cards(monkeypatch, [{"status": "applied", "finished_at": 1792348900.0}])
    assert crewmate_feed.record_waiting(state, slot) is True
    assert appended[-1][2] == {"items": [], "total": 0}


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
    assert "met" not in milestone  # the crewmate's reading is its status, not judged here
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


def test_a_goal_edited_while_the_thread_is_closed_is_read_when_it_reopens(appended, monkeypatch):
    """A goal recorded once, then edited on the Goals tab while the DM thread is
    closed: nothing could hold it, so the reopened thread's first growth reads the
    record afresh instead of trusting the snapshot from before."""
    slot = _Slot([])
    state = _State(slot)
    monkeypatch.setattr(crewmate_feed, "_state", lambda: state)
    _cards(monkeypatch, [])
    old = {"goals": [{"text": "Sort plants"}], "key_results": []}
    crewmate_feed._goal_written("Atlas", old)
    assert [e[2]["goals"][0]["text"] for e in appended if e[0] == "goal"] == ["Sort plants"]
    closed = _State(slot)
    closed._slots = {}
    monkeypatch.setattr(crewmate_feed, "_state", lambda: closed)
    new = {"goals": [{"text": "Water plants"}], "key_results": []}
    crewmate_feed._goal_written("Atlas", new)
    assert len([e for e in appended if e[0] == "goal"]) == 1  # no thread to log into
    monkeypatch.setattr(crewmate_feed, "_state", lambda: state)
    monkeypatch.setattr(member_goals, "read_goals", lambda slug, member: new)
    from kiro_crew import members as members_mod

    monkeypatch.setattr(members_mod, "member_slug", lambda member: "atlas")

    async def go() -> None:
        crewmate_feed._pending.add("s-atlas")
        crewmate_feed._settle()
        await _drain()

    asyncio.run(go())
    goals = [e[2]["goals"][0]["text"] for e in appended if e[0] == "goal"]
    assert goals == ["Sort plants", "Water plants"]
    assert crewmate_feed._stale_goals == set()


def test_the_goal_fold_keeps_the_newest_snapshot_with_its_counts():
    first = {"goals": [{"text": "Ship"}], "milestones": []}
    second = {
        "goals": [{"text": "Ship 0.3"}],
        "milestones": [
            {"text": "Fixes", "target": "3", "current": "4", "at": "t"},
            {"text": "Docs", "target": "yes", "current": "met", "at": "t"},
        ],
    }
    value = _fold("goal", [("dashboard/goal_recorded", first), ("dashboard/goal_recorded", second)])
    assert value["goal"] == "Ship 0.3"
    assert value["milestones_total"] == 2
    # Reading and target side by side, as the crewmate recorded them; nothing judged.
    assert [(m["target"], m["current"]) for m in value["milestones"]] == [
        ("3", "4"),
        ("yes", "met"),
    ]
    assert not {"milestones_met", "milestones_open", "all_met"} & set(value)
    assert value["changes"] == 2


def test_the_goal_fold_counts_every_milestone_even_past_the_row_limit():
    """A thirteenth milestone past the kept rows is still counted, and the rows cut are
    counted out loud."""
    row = {"text": "m", "target": "1", "current": "1", "at": "t"}
    limit = crew_log.CREWMATE_ROW_LIMIT
    snapshot = {"goals": [{"text": "Ship"}], "milestones": [row] * (limit + 1)}
    value = _fold("goal", [("dashboard/goal_recorded", snapshot)])
    assert len(value["milestones"]) == limit
    assert value["milestones_total"] == limit + 1
    assert value["milestones_omitted"] == 1


def test_the_goal_fold_counts_goals_past_the_row_limit_too():
    limit = crew_log.CREWMATE_ROW_LIMIT
    snapshot = {"goals": [{"text": f"g{n}"} for n in range(limit + 1)], "milestones": []}
    value = _fold("goal", [("dashboard/goal_recorded", snapshot)])
    assert len(value["goals"]) == limit
    assert (value["goals_total"], value["goals_omitted"]) == (limit + 1, 1)


def test_the_waiting_fold_counts_what_the_list_cut():
    item = {"kind": "card", "text": "Save tools", "options": [], "asked_at": "t"}
    limit = crew_log.CREWMATE_WAITING_LIMIT
    value = _fold(
        "waiting", [("dashboard/waiting_changed", {"items": [item] * limit, "total": limit + 3})]
    )
    assert len(value["items"]) == limit
    assert (value["count"], value["omitted"]) == (limit + 3, 3)
    # An entry with no total counts its own rows.
    value = _fold("waiting", [("dashboard/waiting_changed", {"items": [item]})])
    assert (value["count"], value["omitted"]) == (1, 0)


def test_the_producer_cuts_at_the_folds_bound_and_records_the_total(appended, monkeypatch):
    from kiro_crew.dashboard import member_waiting

    assert member_waiting.MAX_ITEMS == crew_log.CREWMATE_WAITING_LIMIT
    cards = [
        {"status": "pending", "title": f"Card {n}", "created_at": 1792348860.0 + n}
        for n in range(member_waiting.MAX_ITEMS + 2)
    ]
    _cards(monkeypatch, cards)
    slot = _Slot([])
    crewmate_feed.record_waiting(_State(slot), slot)
    data = appended[-1][2]
    assert len(data["items"]) == member_waiting.MAX_ITEMS
    assert data["total"] == member_waiting.MAX_ITEMS + 2


def test_a_snapshot_the_writer_rejects_is_appended_again(monkeypatch):
    """The digest counts only once the append commits: a rejection (the writer's
    memory ceiling) leaves the snapshot to be appended on the next check."""
    _cards(monkeypatch, [])
    outcomes = iter([False, True])
    seen: list[dict] = []

    def write(_sid: str, data: dict, *, on_settled: Any = None) -> None:
        seen.append(data)
        on_settled(next(outcomes))

    monkeypatch.setattr(emit, "on_waiting_changed", write)
    monkeypatch.setattr(crewmate_feed, "_session_of", lambda slot: "s-atlas")
    slot = _Slot([{"role": "assistant", "content": ASK, "ts": "2026-10-18T18:40:00+00:00"}])
    state = _State(slot)
    assert crewmate_feed.record_waiting(state, slot) is True  # rejected
    assert crewmate_feed.record_waiting(state, slot) is True  # appended again, committed
    assert crewmate_feed.record_waiting(state, slot) is False  # now recorded
    assert len(seen) == 2


def test_a_snapshot_still_being_written_is_not_appended_twice(monkeypatch):
    _cards(monkeypatch, [])
    pending: list[Any] = []
    monkeypatch.setattr(
        emit,
        "on_waiting_changed",
        lambda _sid, _data, *, on_settled=None: pending.append(on_settled),
    )
    monkeypatch.setattr(crewmate_feed, "_session_of", lambda slot: "s-atlas")
    slot = _Slot([{"role": "assistant", "content": ASK, "ts": "2026-10-18T18:40:00+00:00"}])
    state = _State(slot)
    assert crewmate_feed.record_waiting(state, slot) is True
    # The append's own growth checks again before the writer settles.
    assert crewmate_feed.record_waiting(state, slot) is False
    pending[0](True)
    assert crewmate_feed.record_waiting(state, slot) is False
    assert len(pending) == 1


def _queued_writer(monkeypatch) -> list[tuple[dict, Any]]:
    """A writer that settles nothing until the test says so, in append order."""
    queue: list[tuple[dict, Any]] = []
    monkeypatch.setattr(
        emit,
        "on_goal_recorded",
        lambda _sid, data, *, on_settled=None: queue.append((data, on_settled)),
    )
    monkeypatch.setattr(crewmate_feed, "_session_of", lambda slot: "s-atlas")
    return queue


def _goal(text: str) -> dict[str, Any]:
    return {"goals": [{"text": text}], "key_results": []}


def test_a_goal_changed_back_while_the_first_append_is_queued_is_appended_again(monkeypatch):
    queue = _queued_writer(monkeypatch)
    slot = _Slot([])
    crewmate_feed.record_goal(slot, _goal("A"))
    queue.pop()[1](True)  # A committed
    crewmate_feed.record_goal(slot, _goal("B"))  # queued behind a busy writer
    crewmate_feed.record_goal(slot, _goal("A"))  # changed back before B lands
    assert [d["goals"][0]["text"] for d, _ in queue] == ["B", "A"]


def test_an_older_equal_append_does_not_clear_the_newest_retry(monkeypatch):
    """A -> B -> A with the writer backed up: the older A commits, B commits, the
    newest A is rejected. The held A must survive and be appended again."""
    queue = _queued_writer(monkeypatch)
    _cards(monkeypatch, [])
    slot = _Slot([])
    state = _State(slot)
    crewmate_feed.record_goal(slot, _goal("A"))
    crewmate_feed.record_goal(slot, _goal("B"))
    crewmate_feed.record_goal(slot, _goal("A"))
    assert [d["goals"][0]["text"] for d, _ in queue] == ["A", "B", "A"]
    (_a1, settle_a1), (_b, settle_b), (_a2, settle_a2) = queue
    queue.clear()
    settle_a1(True)
    settle_b(True)
    settle_a2(False)
    assert crewmate_feed._held_goals[SLOT]["goals"][0]["text"] == "A"

    async def grow() -> None:
        crewmate_feed._pending.add("s-atlas")
        crewmate_feed._settle()
        await _drain()

    monkeypatch.setattr(crewmate_feed, "_state", lambda: state)
    asyncio.run(grow())
    ((data, settle),) = queue
    assert data["goals"][0]["text"] == "A"
    settle(True)
    assert crewmate_feed._held_goals == {}


def test_an_older_append_settling_never_clears_a_newer_held_goal(monkeypatch):
    """A is queued, B is held: A's commit must not take B with it, so B's rejection
    still leaves B to retry."""
    queue = _queued_writer(monkeypatch)
    slot = _Slot([])
    crewmate_feed.record_goal(slot, _goal("A"))
    _a, settle_a = queue.pop()
    crewmate_feed.record_goal(slot, _goal("B"))
    _b, settle_b = queue.pop()
    settle_a(True)
    assert crewmate_feed._held_goals[SLOT]["goals"][0]["text"] == "B"
    settle_b(False)
    assert crewmate_feed._held_goals[SLOT]["goals"][0]["text"] == "B"


def test_a_goal_held_with_no_session_survives_an_older_append_settling(monkeypatch):
    queue = _queued_writer(monkeypatch)
    slot = _Slot([])
    crewmate_feed.record_goal(slot, _goal("A"))
    _a, settle_a = queue.pop()
    # The thread loses its session (a restart) before B is written: B is only held.
    crewmate_feed._sessions.clear()
    monkeypatch.setattr(crewmate_feed, "_session_of", lambda slot: "")
    crewmate_feed.record_goal(slot, _goal("B"))
    assert queue == []
    settle_a(True)
    assert crewmate_feed._held_goals[SLOT]["goals"][0]["text"] == "B"


def test_a_rejected_goal_is_retried_even_after_an_older_one_committed(monkeypatch):
    queue = _queued_writer(monkeypatch)
    _cards(monkeypatch, [])
    slot = _Slot([])
    state = _State(slot)
    crewmate_feed.record_goal(slot, _goal("A"))
    queue.pop()[1](True)
    crewmate_feed.record_goal(slot, _goal("B"))
    queue.pop()[1](False)  # the writer's memory ceiling refused it
    assert crewmate_feed._held_goals[SLOT]["goals"][0]["text"] == "B"

    async def grow() -> None:
        crewmate_feed._pending.add("s-atlas")
        crewmate_feed._settle()
        await _drain()

    monkeypatch.setattr(crewmate_feed, "_state", lambda: state)
    asyncio.run(grow())
    data, settle = queue.pop()
    assert data["goals"][0]["text"] == "B"
    settle(True)
    assert crewmate_feed._held_goals == {}


def test_a_growth_after_teardown_still_finds_its_thread(appended, monkeypatch):
    """The final [OPTIONS] reply grows the log, then teardown clears the live client
    before the settle runs: the thread's last session still names it."""
    _cards(monkeypatch, [])
    live = {"sid": "s-atlas"}
    monkeypatch.setattr(crewmate_feed, "_session_of", lambda _slot: live["sid"])
    slot = _Slot([{"role": "assistant", "content": ASK, "ts": "2026-10-18T18:40:00+00:00"}])
    state = _State(slot)
    monkeypatch.setattr(crewmate_feed, "_state", lambda: state)

    async def go() -> None:
        crewmate_feed._grew("s-atlas")
        live["sid"] = ""  # turn teardown
        crewmate_feed._timer.cancel()
        crewmate_feed._timer = None
        crewmate_feed._settle()
        await _drain()

    asyncio.run(go())
    ((kind, sid, data),) = [e for e in appended if e[0] == "waiting"]
    assert sid == "s-atlas"
    assert [i["text"] for i in data["items"]] == ["Open a PR for the docs fix?"]


def test_a_goal_of_wide_characters_still_fits_one_log_line():
    from kiro_crew.crew_log import emit_text
    from kiro_crew.crew_log.emit_state import _SRC_GATEWAY

    # An astral character escapes to twelve bytes on the log line.
    wide = "\U0001f600" * 2000
    rec = {
        "goals": [{"text": wide} for _ in range(6)],
        "key_results": [{"text": wide, "target": wide, "current": wide} for _ in range(12)],
    }
    snap = crewmate_feed.goal_snapshot(rec)
    assert emit_text._entry_line_fits("dashboard/goal_recorded", snap, src=_SRC_GATEWAY)
    assert (snap["goals_total"], snap["milestones_total"]) == (6, 12)
    assert snap["goals"][0]["text"].startswith("\U0001f600")


def test_the_goal_fold_takes_the_snapshots_own_totals():
    snapshot = {
        "goals": [{"text": "g"}],
        "milestones": [{"text": "m", "target": "1", "current": "1", "at": "t"}],
        "goals_total": 3,
        "milestones_total": 20,
    }
    value = _fold("goal", [("dashboard/goal_recorded", snapshot)])
    assert (value["goals_total"], value["goals_omitted"]) == (3, 2)
    assert value["milestones_total"] == 20
    assert value["milestones_omitted"] == 19


def test_a_goal_snapshot_redacts_a_credential_in_goal_and_milestone_text():
    token = "ghp_" + "a" * 36
    rec = {
        "goals": [{"text": f"Rotate {token}"}],
        "key_results": [
            {"text": f"use {token}", "target": token, "current": f"read {token}"},
        ],
    }
    snap = crewmate_feed.goal_snapshot(rec)
    assert token not in repr(snap)
    assert snap["goals"][0]["text"].startswith("Rotate ")


def test_a_goal_write_during_the_bootstrap_read_is_not_overwritten(appended, monkeypatch):
    """The bootstrap reads the goal off the loop; a goal write that lands meanwhile is
    newer, so the bootstrap must not append the older record after it."""
    from kiro_crew import members as members_mod

    slot = _Slot([])
    state = _State(slot)
    monkeypatch.setattr(crewmate_feed, "_state", lambda: state)
    monkeypatch.setattr(members_mod, "member_slug", lambda _n, _c=None: "atlas")
    old = {"goals": [{"text": "Old goal"}], "key_results": []}
    new = {"goals": [{"text": "New goal"}], "key_results": []}
    monkeypatch.setattr(member_goals, "has_goal", lambda rec: True)

    async def go() -> None:
        # The read runs in a worker thread; the write is handed to the loop the way
        # _on_goal_written does, so it lands before the read's await resumes.
        def via_loop(_slug, _member, loop=asyncio.get_running_loop()):
            loop.call_soon_threadsafe(crewmate_feed._goal_written, "Atlas", new)
            return old

        monkeypatch.setattr(member_goals, "read_goals", via_loop)
        await crewmate_feed._record_current_goal(slot)

    asyncio.run(go())
    goals = [data["goals"][0]["text"] for kind, _sid, data in appended if kind == "goal"]
    assert goals == ["New goal"]


def test_an_empty_goal_record_is_recorded_after_a_restart(appended, monkeypatch):
    """After a restart no digest is held, yet the log may still carry a goal deleted
    before it: the first read records the empty record, or the page keeps the deleted
    goal."""
    from kiro_crew import members as members_mod

    slot = _Slot([])
    monkeypatch.setattr(members_mod, "member_slug", lambda _n, _c=None: "atlas")
    monkeypatch.setattr(member_goals, "read_goals", lambda _s, _m: {"goals": [], "key_results": []})
    recorded: list[dict] = []
    monkeypatch.setattr(crewmate_feed, "record_goal", lambda _slot, rec: recorded.append(rec))

    asyncio.run(crewmate_feed._record_current_goal(slot))
    assert recorded == [{"goals": [], "key_results": []}]


def test_a_damaged_options_field_reads_as_no_choices():
    """A line the writer would refuse can still be on disk (written by hand, or by an
    older build): the reader parses it, and the fold must not crash on it."""
    from types import SimpleNamespace

    from kiro_crew.crew_log import projection_crewmate as pc

    bad = {"kind": "options", "text": "Pick", "options": 1, "asked_at": "t"}
    entry = SimpleNamespace(
        type="dashboard/waiting_changed", data={"items": [bad]}, time=1792348800000
    )
    state = pc._waiting_start()
    pc._waiting_step(state, entry)  # type: ignore[arg-type]
    value = pc._waiting_render(state)
    assert value["items"][0]["options"] == []
    assert value["items"][0]["options_omitted"] == 0


def test_the_waiting_fold_says_how_many_choices_an_item_cut():
    item = {
        "kind": "options",
        "text": "Pick",
        "options": ["a"] * 6,
        "options_total": 9,
        "asked_at": "t",
    }
    value = _fold("waiting", [("dashboard/waiting_changed", {"items": [item], "total": 1})])
    assert value["items"][0]["options_omitted"] == 3
    assert "options_total" not in value["items"][0]


def test_the_waiting_fold_empties_on_the_next_snapshot_and_reclamps():
    item = {"kind": "options", "text": "x" * 999, "options": ["a"] * 20, "asked_at": "t"}
    value = _fold("waiting", [("dashboard/waiting_changed", {"items": [item] * 20})])
    assert len(value["items"]) == crew_log.CREWMATE_WAITING_LIMIT
    assert (value["count"], value["omitted"]) == (20, 20 - crew_log.CREWMATE_WAITING_LIMIT)
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


def test_both_gateway_entrypoints_start_the_feed():
    """The dashboard and the API-only server each start the feed once they own a loop."""
    import ast
    import inspect

    from kiro_crew.dashboard import server
    from kiro_crew.dashboard.server_runtime import api_entrypoint

    starters = set()
    source = inspect.getsource(server) + "\n" + inspect.getsource(api_entrypoint)
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for call in ast.walk(fn):
            func = getattr(call, "func", None)
            if (
                isinstance(call, ast.Call)
                and isinstance(func, ast.Attribute)
                and func.attr == "start"
                and getattr(func.value, "id", None) == "crewmate_feed"
            ):
                starters.add(fn.name)
    assert starters == {"start_dashboard", "start_api_server"}


def test_a_retired_question_refreshes_the_waiting_list_with_no_log_growth(appended, monkeypatch):
    """Dismissing or answering a card writes no log entry, so the retirement itself
    hands the thread to the settle pass."""
    from kiro_crew import member_goals
    from kiro_crew import members as members_mod

    slot = _Slot([])
    state = _State(slot)
    state.serving_loop = None
    monkeypatch.setattr(crewmate_feed, "_state", lambda: state)
    monkeypatch.setattr(members_mod, "member_slug", lambda _n, _c=None: "atlas")
    monkeypatch.setattr(member_goals, "read_goals", lambda _s, _m: {"goals": [], "key_results": []})
    _cards(monkeypatch, [])

    async def go() -> None:
        state.serving_loop = asyncio.get_running_loop()
        crewmate_feed.question_retired(SLOT)
        # Wait for the append itself rather than for a guessed time.
        await async_wait_until(lambda: appended, describe=lambda: "no waiting-list append")
        await _drain()

    asyncio.run(go())
    # The thread's first settle also records its (empty) goal; this test is the list's.
    assert [entry[0] for entry in appended if entry[0] == "waiting"] == ["waiting"]


def test_retiring_a_question_tells_the_feed(monkeypatch):
    from kiro_crew.dashboard.state_parts import dashboard_services

    told: list[str] = []
    monkeypatch.setattr(crewmate_feed, "question_retired", told.append)

    class _Questions(dashboard_services.QuestionCoordinator):
        def clear_pending(self, *_a: Any, **_k: Any) -> bool:
            return True

    class _Host:
        # The coordinator the state holds; ``_questions_for`` reads it from here.
        _question_coordinator = _Questions()

    assert dashboard_services.clear_question_pending(_Host(), SLOT, blocking=False) is True
    assert told == [SLOT]


def test_opening_a_question_tells_the_feed(monkeypatch):
    """A retried open can land after its tool call's growth settled, so registering
    the question refreshes the waiting list itself."""
    from kiro_crew.dashboard.state_parts import dashboard_services

    told: list[str] = []
    monkeypatch.setattr(crewmate_feed, "question_retired", told.append)

    class _Questions(dashboard_services.QuestionCoordinator):
        def mark_pending(self, *_a: Any, **_k: Any) -> None:
            return None

    class _Host:
        _question_coordinator = _Questions()
        _slots: dict[str, Any] = {}

    dashboard_services.mark_question_pending(_Host(), SLOT, blocking=False, card_id="c1")
    assert told == [SLOT]


def test_a_new_session_on_the_same_thread_records_its_snapshots_afresh(monkeypatch):
    """A thread deleted and opened again (or reset) logs into a new session that holds
    none of the old snapshots, so its first growth records them again."""
    crewmate_feed._recorded_in.clear()
    slot = _Slot([])
    live = {"sid": "s-old"}
    monkeypatch.setattr(crewmate_feed, "_session_of", lambda _slot: live["sid"])
    crewmate_feed._forget_if_new_session(slot)
    crewmate_feed._last[(SLOT, "goal")] = "digest"
    crewmate_feed._last[(SLOT, "waiting")] = "digest"
    crewmate_feed._forget_if_new_session(slot)  # same session: kept
    assert (SLOT, "goal") in crewmate_feed._last
    live["sid"] = "s-new"
    crewmate_feed._forget_if_new_session(slot)
    assert (SLOT, "goal") not in crewmate_feed._last
    assert (SLOT, "waiting") not in crewmate_feed._last
    crewmate_feed._recorded_in.clear()


def test_the_goal_and_waiting_folds_read_units_in_succession(monkeypatch):
    from kiro_crew.crew_log import projection_readers as readers

    monkeypatch.setattr(readers, "units_in_succession", lambda slot: ("chain", slot))
    for name in ("goal", "waiting"):
        assert readers._slot_units_for_fold(SLOT, name) == ("chain", SLOT)
