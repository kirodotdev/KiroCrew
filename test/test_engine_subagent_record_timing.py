"""What the engine sub-agent record keeps beyond the engine's words: time and the turn's credits.

The KAS engine reports no credits, model or time for one sub-agent: the recorded turn
in ``fixtures/kas_native_subagents/two-helpers-turn.jsonl`` carries none on any child
frame, and its one credit total covers the whole turn. So the record keeps the card's
own clock (``started_at`` / ``ended_at``) and the turn's total (``turn_credits``), and
nothing per child that the engine did not send.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import kiro_crew.dashboard.chat_runner as cr
from kiro_crew.acp import kas_wire
from kiro_crew.dashboard import engine_subagent_record as record

FIXTURES = Path(__file__).parent / "fixtures" / "kas_native_subagents"


def _updates(name: str) -> list[dict[str, Any]]:
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["params"]["update"] for line in lines[1:]]


def _tracker(**info: Any) -> dict[str, dict[str, Any]]:
    card = {"id": "native:a", "engine": True, "agent": "kirocrew-helper", "task": "t"}
    card.update(info)
    return {"a": card}


def test_the_recorded_turn_reports_no_credits_model_or_time_for_a_child() -> None:
    children = [
        u for u in _updates("two-helpers-turn.jsonl") if kas_wire.kiro_meta(u).get("agentSubtaskId")
    ]
    assert children, "the fixture holds the children's frames"
    for update in children:
        kiro = kas_wire.kiro_meta(update)
        assert set(kiro) <= {
            "kind",
            "agentSubtaskId",
            "toolOrigin",
            "executionId",
            "turnId",
            "replayId",
        }
        raw = update.get("rawInput")
        assert not isinstance(raw, dict) or "model" not in raw


def test_the_recorded_turns_one_credit_total_includes_its_children() -> None:
    [done] = [
        u
        for u in _updates("two-helpers-turn.jsonl")
        if kas_wire.kiro_meta(u).get("kind") == "turn_completion"
    ]
    kiro = kas_wire.kiro_meta(done)
    [summary] = kiro["promptTurnSummaries"]
    # The children's shell calls are listed in the same summary as the dispatch.
    assert summary["usedTools"].count("execute_bash") == 2
    assert kas_wire.turn_credits(kiro) == summary["usage"]


def test_the_card_clock_is_kept() -> None:
    got = record.engine_record(_tracker(started=1000.12345, done_at=1012.5, engine_end="completed"))
    assert got is not None
    [entry] = got["children"]
    assert (entry["started_at"], entry["ended_at"]) == (1000.123, 1012.5)


def test_a_card_with_no_close_or_a_clock_running_backwards_keeps_no_time() -> None:
    for info in (
        {"started": 1000.0},
        {"started": 1000.0, "done_at": 999.0},
        {"started": "x", "done_at": 2.0},
    ):
        [entry] = record.engine_record(_tracker(**info))["children"]
        assert "started_at" not in entry and "ended_at" not in entry


def test_no_child_entry_carries_a_credit_or_model_figure() -> None:
    tracker = _tracker(
        started=1.0, done_at=2.0, credits=3.0, model="some-model", engine_end="completed"
    )
    [entry] = record.engine_record(tracker)["children"]
    assert not {"credits", "model"} & set(entry)


def _slot(tracker: dict[str, dict[str, Any]]) -> Any:
    from unittest.mock import MagicMock

    slot = MagicMock()
    slot._native_subagent_tracker = tracker
    slot.messages = [{"role": "assistant", "content": "answer", "meta": {"mid": "m1"}}]
    slot._turn_reply_mids = ["m1"]
    return slot


def test_the_turns_credits_ride_on_the_record_from_the_footer() -> None:
    slot = _slot(_tracker(started=1.0, done_at=2.0, engine_end="completed"))
    assert cr._attach_turn_stats(slot, 1200, 0.1874, 0.0, turn_boundary=0) is True
    meta = slot.messages[0]["meta"]
    assert meta[record.RECORD_META_KEY]["turn_credits"] == meta["turn_stats"]["credits"] == 0.1874


def test_a_turn_with_no_credits_keeps_no_turn_credits() -> None:
    slot = _slot(_tracker(engine_end="completed"))
    assert cr._attach_turn_stats(slot, 1200, 0.0, 0.0, turn_boundary=0) is True
    assert "turn_credits" not in slot.messages[0]["meta"][record.RECORD_META_KEY]


def test_each_turn_gets_its_own_opaque_key_and_keeps_it() -> None:
    from types import SimpleNamespace

    from kiro_crew.dashboard import engine_subagent_cards as cards

    one, two = SimpleNamespace(_engine_turn_key=""), SimpleNamespace(_engine_turn_key="")
    key = cards.engine_turn_key(one)
    assert key == cards.engine_turn_key(one)
    assert key != cards.engine_turn_key(two)
    assert len(key) == 16 and int(key, 16) >= 0


def test_the_turns_key_rides_on_its_cards_frames_and_record() -> None:
    from unittest.mock import MagicMock

    from kiro_crew.dashboard import engine_subagent_cards as cards
    from kiro_crew.dashboard.chat_turn import native_cards

    row = {
        "sessionId": "s1",
        "agentName": "kirocrew-helper",
        "initialQuery": "Sub-agent: kirocrew-helper",
        "prompt": "echo one",
        "target": "kirocrew-helper",
        "status": {"type": "in_progress", "message": ""},
    }
    tracker: dict[str, dict[str, Any]] = {}
    state = MagicMock()
    state._native_cards = {}
    slot = MagicMock()
    slot.key = "chat-1"
    rows = cards.engine_rows([row], tracker, "00ff00ff00ff00ff")
    native_cards._native_subagent_sync(state, slot, rows, tracker, {})
    spawn = state.broadcast_ws.call_args_list[0].args
    assert spawn[0] == "subagent_spawn"
    assert spawn[1]["engine"] is True and spawn[1]["engine_turn"] == "00ff00ff00ff00ff"
    done = {**row, "status": {"type": "completed", "message": ""}}
    native_cards._native_subagent_sync(
        state, slot, cards.engine_rows([done], tracker, "00ff00ff00ff00ff"), tracker, {}
    )
    assert state.broadcast_ws.call_args_list[-1].args[1]["engine_turn"] == "00ff00ff00ff00ff"
    got = record.engine_record(tracker)
    assert got is not None and got["turn"] == "00ff00ff00ff00ff"


def test_a_card_of_no_engine_turn_carries_no_key() -> None:
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.chat_turn import native_cards

    row = {
        "sessionId": "s1",
        "agentName": "a",
        "initialQuery": "task",
        "status": {"type": "working"},
    }
    state = MagicMock()
    state._native_cards = {}
    slot = MagicMock()
    slot.key = "chat-1"
    native_cards._native_subagent_sync(state, slot, [row], {}, {})
    frame = state.broadcast_ws.call_args_list[0].args[1]
    assert "engine" not in frame and "engine_turn" not in frame


def test_a_replayed_engine_card_keeps_its_turns_key() -> None:
    from types import SimpleNamespace

    from kiro_crew.dashboard.state import DashboardState

    card = {
        "id": "native:s1",
        "engine": True,
        "engine_turn": "ab12",
        "task": "t",
        "agent": "a",
        "started": 1.0,
    }
    slot = SimpleNamespace(
        key="chat-1", _native_subagent_tracker={"s1": card}, _native_subagent_output={}
    )
    state = SimpleNamespace(_slots={"chat-1": slot})
    [running] = DashboardState.native_subagent_snapshots(state)  # type: ignore[arg-type]
    assert running["frame_extras"] == {"engine": True, "engine_turn": "ab12", "started": 1.0}


def test_the_batch_totals_and_span_count_the_children_left_out_of_the_list() -> None:
    tracker: dict[str, dict[str, Any]] = {}
    for i in range(record.RECORD_CHILDREN_MAX):
        tracker[f"s{i}"] = {
            "id": f"native:{i}",
            "engine": True,
            "agent": "a",
            "task": "t",
            "started": 1000.0,
            "done": True,
            "done_at": 1002.0,
            "engine_end": "completed",
        }
    tracker["last"] = {
        "id": "native:last",
        "engine": True,
        "agent": "a",
        "task": "t",
        "started": 1000.0,
        "done": True,
        "done_at": 1100.0,
        "engine_end": "failed",
    }
    got = record.engine_record(tracker)
    assert got is not None
    assert len(got["children"]) == record.RECORD_CHILDREN_MAX and got["more"] == 1
    assert got["ends"] == {"completed": record.RECORD_CHILDREN_MAX, "failed": 1, "not_reported": 0}
    assert (got["started_at"], got["ended_at"]) == (1000.0, 1100.0)


def test_a_card_still_open_when_the_record_is_made_counts_to_that_moment() -> None:
    tracker = {
        "a": {
            "id": "native:a",
            "engine": True,
            "agent": "a",
            "task": "t",
            "started": 1000.0,
            "done": True,
            "done_at": 1005.0,
            "engine_end": "completed",
        },
        "b": {
            "id": "native:b",
            "engine": True,
            "agent": "a",
            "task": "t",
            "started": 1000.0,
            "done": False,
        },
    }
    got = record.engine_record(tracker, now=1060.0)
    assert got is not None
    assert (got["started_at"], got["ended_at"]) == (1000.0, 1060.0)
    assert got["ends"] == {"completed": 1, "failed": 0, "not_reported": 1}
