"""A permission request answered with no row must not split the reply.

Codex sends a sub-agent's approval requests on the parent session in the middle
of the parent's message, with a tool call id no ``tool_call`` frame announced.
A grant that appends no row (YOLO, session trust) leaves the segment open: the
cut is owed and paid only before a row the decision lands, so the message stays
one assistant row even when it is split mid-word, inside bold text or inside a
diff hunk.
"""

from __future__ import annotations

import random

import pytest
from chat_test_helpers import _make_state
from turn_harness import APPROVED, SlotSpec, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard.chat_runner import _flush_segment
from kiro_crew.dashboard.state import _ChatSlot

_MESSAGE = (
    "The **358-method hosted suite and three source reviews are running**. "
    "All five synthetic test files retained. Only the download UI changed:\n\n"
    "@@ -0,0 +1 @@\n+export const ready = true;\n\n"
    'The config now reads {"name": "sample-report.json", "mode": "strict"}.'
)


def _chunks(text: str, rng: random.Random, pieces: int) -> list[str]:
    cuts = sorted(rng.sample(range(1, len(text)), pieces - 1))
    return [text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)], strict=True)]


def _approval(n: int) -> AcpEvent:
    # The shape codex-acp sends for a sub-agent's exec: the parent's session,
    # an unannounced call id, no sub-session tag.
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=f"req-{n}",
        tool_call_id=f"call_child_{n}",
        title="Run command",
        tool_kind="execute",
    )


def _stream(seed: int) -> tuple[list[AcpEvent], dict[str, str]]:
    rng = random.Random(seed)
    chunks = _chunks(_MESSAGE, rng, rng.randint(8, 40))
    at = set(rng.sample(range(1, len(chunks)), min(3, len(chunks) - 1)))
    events: list[AcpEvent] = []
    answers: dict[str, str] = {}
    for i, chunk in enumerate(chunks):
        if i in at:
            events.append(_approval(i))
            answers[f"req-{i}"] = APPROVED
            if i % 2:
                events.append(AcpEvent(kind=EVENT_TOOL_CALL_UPDATE, tool_call_id=f"call_child_{i}"))
        events.append(AcpEvent(kind=EVENT_TEXT_CHUNK, text=chunk, message_id="msg_1"))
    events.append(AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN))
    return events, answers


@pytest.mark.asyncio
@pytest.mark.parametrize("trust", ["yolo", "trust"])
@pytest.mark.parametrize("seed", range(12))
async def test_silent_grants_inside_a_message_keep_it_one_row(seed, trust):
    events, answers = _stream(seed)
    record = await run_turn(
        TurnScript(events=events, answers=answers),
        slot=SlotSpec(yolo=trust == "yolo", trust=trust == "trust"),
    )
    assert record.stop_reason == STOP_REASON_END_TURN
    # Persisted: exactly the native message, once, no loss and no duplication.
    assert [r.get("content") for r in record.rows("assistant")] == [_MESSAGE]
    assert [r.get("role") for r in record.history_rows] == ["user", "assistant"]
    # Reload serves the same single answer, with no stranded streaming rows.
    window = [r for r in record.window if r.get("role") in ("assistant", "chunk")]
    assert [(r.get("role"), r.get("content")) for r in window] == [("assistant", _MESSAGE)]
    # The crew log's copy of the body is one message, not fragments.
    sent = [c.kwargs.get("text") for c in record.crew("on_message_sent")]
    assert "".join(t for t in sent if t) == _MESSAGE
    assert len([t for t in sent if t]) == 1


@pytest.mark.asyncio
async def test_an_interactive_approval_card_still_cuts_after_prose():
    record = await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Writing the file now."),
                AcpEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    request_id="req-1",
                    title="fs_write",
                    tool_kind="edit",
                ),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Written."),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ],
            answers={"req-1": APPROVED},
        ),
    )
    roles = [(r.get("role"), r.get("content")) for r in record.history_rows]
    assert roles[1] == ("assistant", "Writing the file now.")
    assert roles[-1] == ("assistant", "Written.")
    assert any(role == "tool" for role, _ in roles[2:-1]), roles


@pytest.mark.asyncio
@pytest.mark.parametrize("yolo", [True, False])
async def test_a_tool_call_inside_the_reply_still_cuts(yolo):
    record = await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Running one command."),
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="c1", title="ls", tool_kind="execute"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="done"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ],
        ),
        slot=SlotSpec(yolo=yolo),
    )
    roles = [(r.get("role"), r.get("content")) for r in record.history_rows]
    assert roles[1:] == [
        ("assistant", "Running one command."),
        ("tool", "🔧 ls"),
        ("assistant", "done"),
    ]


def test_a_send_cut_during_the_decision_saves_the_segment_once(tmp_path):
    """A send that cuts the reply while the decision is pending settles the owed cut."""
    state = _make_state(tmp_path)
    slot = _ChatSlot(key="dashboard:chat-owed")
    state._slots[slot.key] = slot
    slot.append("chunk", "Checking the build", "chunk")
    # What the permission request arms: a cut of the same pending text.
    slot._owed_segment_cut = lambda: _flush_segment(state, slot, "Checking the build")

    _flush_segment(state, slot, "Checking the build", broadcast=False, quiet_persist=True)
    slot.append("user", "also run lint", "msg msg-u")

    assert [(m["role"], m["content"]) for m in slot.messages] == [
        ("assistant", "Checking the build"),
        ("user", "also run lint"),
    ]
    assert slot._owed_segment_cut is None


def test_append_pays_an_owed_cut_before_a_durable_row_only():
    slot = _ChatSlot("chat-owed")
    paid: list[int] = []

    def cut() -> None:
        paid.append(len(slot.messages))
        slot.append("assistant", "before", "msg msg-a")

    slot._owed_segment_cut = cut
    slot.append("chunk", "token", "chunk")
    assert paid == []
    slot.append("tool", "🔧 ls", "msg msg-tool")
    assert paid == [1]
    assert slot._owed_segment_cut is None
    assert [m["role"] for m in slot.messages] == ["chunk", "assistant", "tool"]
