"""What an approval card of a KAS engine turn shows beyond any other card. Display only.

A dispatch's approval request names the call and not the task, so the card is given
the task the dispatch frame named. A child's call request names the sub-agent that
asked (``_meta.kiro.requester.subagent``) when the engine reports it, and the card
is given that name. Neither field is read by a gate, a grant or the audit, and
outside an engine turn, or when the engine reports neither, the card is as it was.

The request frames below are the shapes kiro-cli 3.5.0 sent in a real run, cut to
the fields read.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.acp import session_handle_native_children as children
from kiro_crew.dashboard import engine_subagent_cards as cards

CALL = "invoke_subagent_toolu_bdrk_019Hyz9XEeDzAmxkRpDxDj4q"
SUBTASK = "31c14746-6447-4300-8ee9-074e74540ae3"
PROMPT = "Run this exact shell command: sleep 20; echo one"

DISPATCH_REQUEST = {
    "sessionId": "sess_c08ed9e2",
    "toolCall": {"toolCallId": CALL, "status": "pending", "title": "Sub-agent: kirocrew-helper"},
    "_meta": {"kiro": {"toolId": "invoke_sub_agent", "requester": {"sessionId": "sess_c08ed9e2"}}},
}
CHILD_REQUEST = {
    "sessionId": "sess_c08ed9e2",
    "toolCall": {
        "toolCallId": "run_command_toolu_1",
        "status": "pending",
        "title": "sleep 20; echo one",
    },
    "_meta": {
        "kiro": {
            "toolId": "run_command",
            "requester": {
                "sessionId": "sess_c08ed9e2",
                "subagent": {"agentSubtaskId": SUBTASK, "agentName": "kirocrew-helper", "depth": 1},
            },
        }
    },
}


def _roster(status: str = "pending", prompt: Any = PROMPT) -> list[dict[str, Any]]:
    handle = SimpleNamespace(
        _prompt_or_tool_seen=False, _kas_subagent_roster={}, _note_native_child=lambda sid: True
    )
    frame = {
        "sessionUpdate": "tool_call",
        "toolCallId": CALL,
        "title": "Sub-agent: kirocrew-helper",
        "status": status,
        "rawInput": {"name": "kirocrew-helper", "prompt": prompt},
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": SUBTASK}},
    }
    [event] = children._handle_kas_subagent(handle, frame) or []
    return event.subagents


def _turn(*, engine: bool) -> Any:
    return SimpleNamespace(_engine_children=engine, _engine_dispatch_tasks={})


def _event(call: str = CALL, asker: str = "") -> Any:
    return SimpleNamespace(tool_call_id=call, requesting_subagent=asker)


def test_a_child_request_names_its_asker_and_a_dispatch_request_names_none() -> None:
    assert children.kas_requesting_subagent(CHILD_REQUEST) == "kirocrew-helper"
    assert children.kas_requesting_subagent(DISPATCH_REQUEST) == ""


def test_an_engine_that_does_not_report_the_asker_gives_nothing() -> None:
    for params in (
        {},
        {"_meta": {"kiro": {}}},
        {"_meta": {"kiro": {"requester": "x"}}},
        {"_meta": {"kiro": {"requester": {"subagent": {"agentName": 7}}}}},
        "not a mapping",
    ):
        assert children.kas_requesting_subagent(params) == ""


def test_the_asker_name_is_bounded() -> None:
    params = {"_meta": {"kiro": {"requester": {"subagent": {"agentName": "a" * 50_000}}}}}
    # Kept to what the card shows of it plus the reach of the card's own cut.
    held = children.kas_requesting_subagent(params)
    assert len(held) == children.REQUESTER_NAME_CAP + children.NATIVE_CHILD_SHOWN_LOOKAHEAD
    shown = cards.approval_card_fields(_turn(engine=True), _event("run_command_toolu_1", held))
    assert len(shown["asked_by_subagent"]) == cards.ASKER_MAX == children.REQUESTER_NAME_CAP


def test_the_roster_row_keeps_the_dispatch_call_id() -> None:
    [row] = _roster()
    assert row["dispatch_call"] == CALL


def test_a_dispatch_card_of_an_engine_turn_shows_the_task() -> None:
    turn = _turn(engine=True)
    cards.remember_dispatch_tasks(turn._engine_dispatch_tasks, _roster())
    assert cards.approval_card_fields(turn, _event()) == {"subagent_task": PROMPT}


def test_a_child_call_card_of_an_engine_turn_names_the_asker() -> None:
    turn = _turn(engine=True)
    assert cards.approval_card_fields(turn, _event("run_command_toolu_1", "kirocrew-helper")) == {
        "asked_by_subagent": "kirocrew-helper"
    }
    # The engine did not name the asker: the card is as it was.
    assert cards.approval_card_fields(turn, _event("run_command_toolu_1", "")) == {}


def test_outside_an_engine_turn_no_card_gains_a_field() -> None:
    turn = _turn(engine=False)
    cards.remember_dispatch_tasks(turn._engine_dispatch_tasks, _roster())
    assert cards.approval_card_fields(turn, _event()) == {}
    assert cards.approval_card_fields(turn, _event("run_command_toolu_1", "kirocrew-helper")) == {}


def test_the_card_builder_redacts_and_bounds_the_task() -> None:
    """The fields come back as the engine gave them; the card's own builder, its
    output boundary, redacts and cuts each before it lands on the row."""
    from kiro_crew.security import redact_and_truncate
    from kiro_crew.testing.synthetic_credentials import github_classic

    token = github_classic(seed=7)
    turn = _turn(engine=True)
    cards.remember_dispatch_tasks(
        turn._engine_dispatch_tasks, _roster(prompt=f"push {token} " + "x" * 5000)
    )
    raw = cards.approval_card_fields(turn, _event())["subagent_task"]
    shown = redact_and_truncate(raw, cards.APPROVAL_TASK_MAX)
    assert token not in shown
    assert len(shown) == cards.APPROVAL_TASK_MAX


@pytest.mark.asyncio
async def test_the_approval_card_a_turn_shows_carries_the_task_redacted_and_cut() -> None:
    """Through a real dashboard turn: a KAS engine dispatch waits on its approval in a
    session that mounted the engine's tool (the ``kas_subagents`` preview on), and the
    card frame the turn sends carries the dispatch's task with the credential redacted
    and the text cut to ``APPROVAL_TASK_MAX``. Redaction is on here because this test
    publishes the owner's setting as on (``redaction_seams.publish(False)``)."""
    from engine_turn_helpers import registered, session
    from turn_harness import APPROVED, TurnScript, run_turn

    from kiro_crew import redaction_seams
    from kiro_crew.acp.types import (
        EVENT_COMPLETE,
        EVENT_PERMISSION_REQUEST,
        EVENT_SUBAGENT_LIST,
        STOP_REASON_END_TURN,
        AcpEvent,
    )
    from kiro_crew.testing.synthetic_credentials import github_classic

    redaction_seams.publish(False)
    token = github_classic(seed=7)
    events = [
        AcpEvent(kind=EVENT_SUBAGENT_LIST, subagents=_roster(prompt=f"push {token} " + "x" * 5000)),
        AcpEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id="req-1",
            tool_call_id=CALL,
            title="Sub-agent: kirocrew-helper",
            tool_kind="other",
        ),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
    ]

    record = await run_turn(
        TurnScript(events=events, answers={"req-1": APPROVED}, provider=session(registered())),
        config={"dashboard": {"feature_previews": {"kas_subagents": True}}},
    )

    [card] = record.approval_cards
    task = card["meta"]["subagent_task"]
    assert token not in task
    assert len(task) == cards.APPROVAL_TASK_MAX


def test_the_remembered_tasks_are_bounded() -> None:
    tasks: dict[str, str] = {}
    rows = [
        {"dispatch_call": f"c{n}", "prompt": "p"} for n in range(cards.DISPATCH_TASKS_KEPT + 10)
    ]
    cards.remember_dispatch_tasks(tasks, rows)
    assert len(tasks) == cards.DISPATCH_TASKS_KEPT
    # A known call's task can still change; rows without both fields are skipped.
    cards.remember_dispatch_tasks(
        tasks, [{"dispatch_call": "c0", "prompt": "q"}, {"prompt": "z"}, 7]
    )
    assert tasks["c0"] == "q"


def test_a_turn_keeps_a_task_for_every_dispatch_the_roster_admits() -> None:
    """The bound is the roster's own: the 257th dispatch's card still shows its task."""
    turn = _turn(engine=True)
    rows = [{"dispatch_call": f"c{n}", "prompt": f"task {n}"} for n in range(257)]
    cards.remember_dispatch_tasks(turn._engine_dispatch_tasks, rows)
    assert cards.approval_card_fields(turn, _event("c256")) == {"subagent_task": "task 256"}
    # The row's own string is kept, not a copy cut at store.
    assert turn._engine_dispatch_tasks["c256"] is rows[256]["prompt"]
    assert cards.DISPATCH_TASKS_KEPT == children.NATIVE_CHILD_ROSTER_CAP


def test_the_card_fields_are_only_the_two_display_keys() -> None:
    """``perm_meta.update`` takes these, so they must never name a key a decide reads."""
    turn = _turn(engine=True)
    cards.remember_dispatch_tasks(turn._engine_dispatch_tasks, _roster())
    got = cards.approval_card_fields(turn, _event(CALL, "kirocrew-helper"))
    assert set(got) == {"subagent_task", "asked_by_subagent"}
