"""The ``crew_work`` slot field: a conductor's open ledger work, in brief, for its row.

Three promises. The summary counts what the row needs and nothing else (open items,
the ones still on a person, and per other open item its worker and the moment the
ledger's own ``is_stale`` flags it). A followed board's fold is kept, and a one-row
``slot_patch`` is pushed only when its open items moved. The slot frame follows each
live Crew Mode root, primes it once, and releases a root that left the frame.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.crew_log import bus
from kiro_crew.dashboard import crew_work
from kiro_crew.dashboard.handlers.crew_log import CrewLogPublisher
from kiro_crew.dashboard.state_parts import slots_frame
from kiro_crew.work_ledger import DEFAULT_STALE_WINDOW_SECS

NOW = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
WINDOW_MS = int(DEFAULT_STALE_WINDOW_SECS * 1000)


@pytest.fixture(autouse=True)
def _clean():
    bus.reset_for_tests()
    crew_work.forget_for_tests()
    yield
    crew_work.forget_for_tests()
    bus.reset_for_tests()


def _stamp(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _item(item_id: str, **over):
    base = {
        "item_id": item_id,
        "title": item_id,
        "state": "open",
        "worker_session_key": "dashboard:chat-w",
        "status": "progress",
        "reason": None,
        "last_report_at": _stamp(5),
        "created_at": _stamp(60),
        "decided_at": None,
    }
    base.update(over)
    return base


def _summary(items, *, closed=(), looping=()):
    return crew_work.summarize(
        items, closed=lambda k: k in closed, looping=lambda k: k in looping, now=NOW
    )


def test_summary_counts_open_items_and_splits_out_the_person():
    summary = _summary(
        [
            _item("a"),
            _item("b", status="question", reason="needs_human"),
            _item("c", status="blocked", reason="approval"),
            _item("e", status="question"),
        ]
    )
    assert summary["open"] == 4
    assert summary["person"] == 2
    a, e = summary["items"]
    assert a == {"worker": "chat-w", "stale_at_ms": NOW_MS - 5 * 60_000 + WINDOW_MS}
    # A question for the conductor: the ledger never flags it while the move is not
    # the worker's.
    assert e["stale_at_ms"] == 0


def test_an_answered_person_wait_is_the_worker_s_again():
    answered = _item("b", status="question", reason="needs_human", decided_at=_stamp(2))
    summary = _summary([answered])
    assert summary["person"] == 0
    (only,) = summary["items"]
    assert only["stale_at_ms"] == NOW_MS - 2 * 60_000 + WINDOW_MS


def test_a_quiet_item_is_stale_now_and_a_looping_worker_never():
    quiet = _item("q", last_report_at=_stamp(40))
    assert _summary([quiet])["items"][0]["stale_at_ms"] == NOW_MS
    assert _summary([quiet], looping={"dashboard:chat-w"})["items"][0]["stale_at_ms"] == 0


def test_a_closed_worker_that_reported_is_stale_at_once():
    assert _summary([_item("a")], closed={"dashboard:chat-w"})["items"][0]["stale_at_ms"] == NOW_MS


def test_a_remote_worker_is_never_flagged_stalled_here():
    quiet = _item("r", worker_instance_id="peer-1", last_report_at=_stamp(40))
    (only,) = _summary([quiet], closed={"dashboard:chat-w"})["items"]
    assert only == {"worker": "", "stale_at_ms": 0}


def test_remember_keeps_open_items_by_slot_and_reports_only_a_move():
    value = {"items": [_item("a"), _item("d", state="accepted")]}
    assert crew_work.remember("dashboard:chat-7", value) == "chat-7"
    assert crew_work.remember("dashboard:chat-7", value) is None
    assert crew_work.remember("dashboard:chat-7", {"items": "nope"}) is None
    assert crew_work.remember("dashboard:chat-7", None) is None
    state = SimpleNamespace(_slots={})
    assert crew_work.summary_for_state("chat-7", state)["open"] == 1
    assert crew_work.summary_for_state("chat-8", state) is None


def _fold_event(key: str, value, revision: int = 3):
    return bus.FoldAdvanced(
        scope=bus.SCOPE_SLOT, key=key, fold="work", revision=revision, value=value, seq=1
    )


def test_a_followed_board_s_fold_pushes_one_row_patch_when_it_moves(monkeypatch):
    from kiro_crew.crew_log import slot_prime

    monkeypatch.setattr(slot_prime, "schedule", lambda *a, **k: True)
    state = MagicMock()
    state._slots = {"chat-7": object()}
    state._has_legacy_slots_audience.return_value = False
    state._has_slot_patch_clients.return_value = True
    publisher = CrewLogPublisher(state)
    loop = MagicMock()
    loop.call_soon_threadsafe.side_effect = lambda fn, *args: fn(*args)
    publisher._loop = loop
    crew_work.set_listener(publisher.on_crew_work)
    crew_work.follow("chat-7", ("chat-7", "dashboard:chat-7"))

    value = {"items": [_item("a")]}
    bus.publish(bus.FOLD_ADVANCED, _fold_event("dashboard:chat-7", value))
    bus.publish(bus.FOLD_ADVANCED, _fold_event("dashboard:chat-7", value, revision=4))
    # A board nobody follows is heard by nothing here.
    bus.publish(bus.FOLD_ADVANCED, _fold_event("dashboard:chat-8", value))

    state._send_slot_patch.assert_called_once()
    (frame,), _ = state._send_slot_patch.call_args
    (row,) = frame["slots"]
    assert row["key"] == "chat-7"
    assert row[crew_work.CREW_WORK_FIELD]["open"] == 1
    assert crew_work.summary_for_state("chat-8", state) is None


def test_following_keeps_other_boards_on_demand_and_retain_releases(monkeypatch):
    from kiro_crew.crew_log import slot_prime

    monkeypatch.setattr(slot_prime, "schedule", lambda *a, **k: True)
    crew_work.follow("chat-7", ("chat-7", "dashboard:chat-7"))
    wanted = lambda key: bus.has_subscriber(  # noqa: E731
        bus.FOLD_ADVANCED, scope=bus.SCOPE_SLOT, key=key, fold="work"
    )
    assert wanted("dashboard:chat-7")
    assert not wanted("dashboard:chat-8")
    crew_work.retain([])
    assert not wanted("dashboard:chat-7")


def test_a_closed_root_gives_its_follow_place_back(monkeypatch):
    from kiro_crew.crew_log import slot_prime

    monkeypatch.setattr(slot_prime, "schedule", lambda *a, **k: True)
    for n in range(crew_work.CREW_WORK_FOLLOW_LIMIT):
        crew_work.follow(f"chat-{n}", (f"chat-{n}",))
    crew_work.follow("chat-new", ("chat-new",))
    assert not bus.has_subscriber(
        bus.FOLD_ADVANCED, scope=bus.SCOPE_SLOT, key="chat-new", fold="work"
    )
    crew_work.retain(["chat-new"])
    crew_work.follow("chat-new", ("chat-new",))
    assert bus.has_subscriber(bus.FOLD_ADVANCED, scope=bus.SCOPE_SLOT, key="chat-new", fold="work")


def test_a_delta_event_s_whole_value_is_read_from_the_publish(monkeypatch):
    from kiro_crew.crew_log import eager

    monkeypatch.setattr(eager, "published_value", lambda key, fold, rev: {"items": [_item("a")]})
    crew_work._on_fold(SimpleNamespace(key="chat-9", fold="work", revision=2, value=None))
    assert crew_work.summary_for_state("chat-9", SimpleNamespace(_slots={}))["open"] == 1


def test_slot_frame_follows_each_root_and_primes_it_once(monkeypatch):
    from kiro_crew.crew_log import slot_prime

    primed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        slot_prime, "schedule", lambda slot, name, **_: primed.append((slot, name)) or True
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key",
        lambda slot: f"dashboard:{slot.key}",
    )
    crew_work.remember("chat-1", {"items": [_item("a")]})
    state = SimpleNamespace(
        _slots={key: SimpleNamespace(key=key) for key in ("chat-1", "chat-2", "chat-3")}
    )
    rows = [
        {"key": "chat-1", "crew_mode_root": True},
        {"key": "chat-2", "crew_mode_root": True},
        {"key": "chat-3"},
    ]

    slots_frame._attach_crew_work(state, rows)  # type: ignore[arg-type]
    slots_frame._attach_crew_work(state, rows)  # type: ignore[arg-type]

    assert rows[0]["crew_work"]["open"] == 1
    assert "crew_work" not in rows[1] and "crew_work" not in rows[2]
    # Only a root with nothing held is primed, once, under its ledger key.
    assert primed == [("chat-2", "work")]
    assert not bus.has_subscriber(
        bus.FOLD_ADVANCED, scope=bus.SCOPE_SLOT, key="chat-3", fold="work"
    )


def test_a_channel_board_s_fold_lands_on_the_slot_it_is_shown_under(monkeypatch):
    from kiro_crew.crew_log import slot_prime

    primed: list[str] = []
    monkeypatch.setattr(slot_prime, "schedule", lambda slot, name, **_: primed.append(slot) or True)
    crew_work.follow("discord_agent_direct_1", ("discord:agent:direct:1", "discord_agent_direct_1"))
    assert primed == ["discord:agent:direct:1"]
    bus.publish(bus.FOLD_ADVANCED, _fold_event("discord:agent:direct:1", {"items": [_item("a")]}))
    state = SimpleNamespace(_slots={})
    assert crew_work.summary_for_state("discord_agent_direct_1", state)["open"] == 1
    crew_work.retain([])
    assert crew_work.summary_for_state("discord_agent_direct_1", state) is None


def test_a_worker_loop_change_refreshes_the_boards_naming_it():
    heard: list[str] = []
    crew_work.set_listener(heard.append)
    crew_work.remember("chat-1", {"items": [_item("a")]})
    crew_work.remember("chat-2", {"items": [_item("b", worker_session_key="chat-other")]})
    crew_work.worker_loop_moved("dashboard:chat-w")
    assert heard == ["chat-1"]


def test_a_person_s_steer_after_the_ask_answers_it():
    asked = _item(
        "b",
        status="question",
        reason="needs_human",
        events=[{"ts": _stamp(1), "kind": "steer", "text": "use the blue one"}],
    )
    assert _summary([asked])["person"] == 0
    older = dict(asked, events=[{"ts": _stamp(30), "kind": "steer", "text": "old"}])
    assert _summary([older])["person"] == 1


def test_an_older_publish_never_replaces_newer_items():
    assert crew_work.remember("chat-7", {"items": [_item("a")]}, 5) == "chat-7"
    stale = {"items": [_item("a", status="question", reason="needs_human")]}
    assert crew_work.remember("chat-7", stale, 4) is None
    assert crew_work.summary_for_state("chat-7", SimpleNamespace(_slots={}))["person"] == 0


def test_a_peer_report_stamp_with_no_offset_does_not_break_the_answer_check():
    remote = _item(
        "p",
        status="blocked",
        reason="needs_human",
        worker_instance_id="peer-1",
        last_report_at="2026-11-01T05:50:00",
        events=[{"ts": _stamp(2), "kind": "decision", "text": "go"}],
    )
    summary = _summary([remote])
    assert summary["open"] == 1


def test_a_steer_before_a_new_question_in_the_same_second_does_not_answer_it():
    same = _stamp(1)
    asked = _item(
        "b",
        status="question",
        reason="needs_human",
        last_report_at=same,
        events=[
            {"ts": same, "kind": "steer", "text": "earlier"},
            {"ts": same, "kind": "report", "status": "question", "text": "which one?"},
        ],
    )
    assert _summary([asked])["person"] == 1
    answered = dict(asked, events=[*asked["events"], {"ts": same, "kind": "steer", "text": "blue"}])
    assert _summary([answered])["person"] == 0
