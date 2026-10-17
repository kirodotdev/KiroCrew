"""What a whole dashboard turn does, in order, for each canonical script.

Each test drives ``chat_runner._run_chat`` end to end through
``turn_harness.run_turn`` and reads only the :class:`~turn_harness.TurnRecord` and
what the turn's collaborators saw while it ran, never the runner's source, so a
change that keeps the turn's behaviour keeps them green and a reordering a reader
of the turn could notice turns them red.

What is pinned per canonical turn (landed text; a tool call approved; a prompt
nobody answers; an empty reply that requeues; a chunk then an error; a refused
session; a local slash command; a warm second turn):

* the order of the frames a client renders the turn from, as an ordered
  subsequence (a frame added between two of them changes nothing a client
  orders by);
* the persisted rows, the provider calls, the hand-offs and the stop reason;
* the crew-log entries and the session-manager calls as ordered subsequences,
  with exactly one turn closer and it last, and ``release`` last of the
  session calls;
* the end of the cycle: one ``slots`` push reporting the slot idle, then exactly
  one ``chat_done``, with no content frame after it, by which point the turn has
  handed back its lock, its identity and its in-flight marker.

Status frames (``activity_event``, ``chat_status``, ``context_usage``,
``refresh``) and the wall-clock ``heartbeat`` are never recorded: they carry no
order contract, and ids, timestamps and durations are never compared.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from turn_harness import (
    APPROVED,
    Raise,
    ScriptedProvider,
    TurnContext,
    TurnRecord,
    TurnScript,
    run_turn,
)

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.constants import TOOL_APPROVAL_TIMEOUT
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.llm_helpers import slot_switch_session_lock

_REPLY = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
_CLOSERS = ("on_turn_completed", "on_turn_failed")
#: Frames whose relative order is what a client renders the turn from.
_CONTENT_KINDS = frozenset(
    {
        "chat_chunk",
        "chat_segment",
        "chat_message",
        "tool_call",
        "tool_result",
        "approval_resolved",
        "queue_pop",
        "chat_done",
    }
)
#: The session-manager calls whose order brackets a turn's use of its session.
_SESSION_BRACKET = frozenset(
    {"get_or_create", "begin_turn", "record_success", "record_failure", "reset", "release"}
)
#: The crew-log entries every turn that opens writes before its first event.
_OPENING = ["on_session_opened", "on_message_received", "on_turn_started", "on_step_started"]


def _permission(request_id: str = "req-1", tool_call_id: str = "") -> AcpEvent:
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=request_id,
        tool_call_id=tool_call_id,
        title="fs_write",
        tool_kind="edit",
    )


class _SecondSessionProvider(ScriptedProvider):
    """A provider on a different ACP session from the first turn's."""

    @property
    def session_id(self) -> str:
        return "acp-session-2"


class _Journal:
    """One ordered log across the collaborators a turn talks to while it runs.

    The record keeps each collaborator's calls in its own list; this keeps them
    in ONE list, so an order between a session call, a content frame and a
    ``slots`` push can be asserted. Each entry also notes the slot's dispatch
    lock, its active session key and its in-flight marker at that moment. Only
    content frames are logged: status frames and the wall-clock heartbeat carry
    no order.
    """

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict[str, Any]]] = []

    def setup(self, ctx: TurnContext) -> None:
        state, slot = ctx.state, ctx.slot
        # Read on the turn's own loop: the lock cache is per loop.
        lock = slot_switch_session_lock(effective_session_key(slot))

        def _facts() -> dict[str, Any]:
            return {
                "lock_held": lock.locked(),
                "active_key": slot._active_turn_session_key,
                "marker": slot._turn_in_flight_generation,
            }

        frames, notes = state.broadcast_ws, state._broadcast

        def _frame(kind: str, payload: Any = None) -> Any:
            if kind in _CONTENT_KINDS:
                self.entries.append(("frame", kind, _facts()))
            return frames(kind, payload)

        def _note(note: dict[str, Any]) -> Any:
            if note.get("_type") in _CONTENT_KINDS:
                label = f"{note.get('_type')}:{note.get('role')}"
                self.entries.append(("frame", label, _facts()))
            return notes(note)

        state.broadcast_ws, state._broadcast = _frame, _note
        for name in sorted(_SESSION_BRACKET):
            self._wrap_session_call(getattr(state.sessions, name), name, _facts)

        def _wrap_slots_push() -> None:
            answer = state.push_slots_update

            def _push(*args: Any, **kwargs: Any) -> Any:
                self.entries.append(("slots", "", {"running": slot.turn_running}))
                return answer(*args, **kwargs)

            state.push_slots_update = _push

        # The harness installs its prompt-answering ``push_slots_update`` after
        # this setup returns and before the turn's first step; a callback queued
        # here runs in that gap, so it wraps the harness's own and calls through.
        asyncio.get_running_loop().call_soon(_wrap_slots_push)

    def _wrap_session_call(self, method: Any, name: str, facts: Any) -> None:
        answer = method.side_effect

        def _note(kwargs: dict[str, Any]) -> None:
            seen = {"wait_if_busy": kwargs["wait_if_busy"]} if "wait_if_busy" in kwargs else {}
            self.entries.append(("session", name, {**seen, **facts()}))

        if inspect.iscoroutinefunction(answer):

            async def _async_call(*args: Any, **kwargs: Any) -> Any:
                _note(kwargs)
                return await answer(*args, **kwargs)

            method.side_effect = _async_call
        else:

            def _call(*args: Any, **kwargs: Any) -> Any:
                _note(kwargs)
                return answer(*args, **kwargs)

            method.side_effect = _call

    def index(self, kind: str, name: str) -> int:
        return next(i for i, entry in enumerate(self.entries) if entry[:2] == (kind, name))

    def named(self, kind: str, name: str) -> list[dict[str, Any]]:
        return [facts for seen, label, facts in self.entries if (seen, label) == (kind, name)]


def _content(record: TurnRecord) -> list[str]:
    """The content frames in order; a run of one label is counted once."""
    labels: list[str] = []
    for frame in record.ws_frames:
        if frame.kind not in _CONTENT_KINDS:
            continue
        label = frame.kind
        if frame.kind == "chat_message":
            label = f"chat_message:{frame.payload.get('role')}"
        if not labels or labels[-1] != label:
            labels.append(label)
    return labels


def _roles(record: TurnRecord) -> list[str]:
    return [row["role"] for row in record.history_rows]


def _crew(record: TurnRecord) -> list[str]:
    return [call.name for call in record.crew_log]


def _bracket(record: TurnRecord) -> list[str]:
    return [call.name for call in record.session_calls if call.name in _SESSION_BRACKET]


def _in_order(seen: Sequence[str], expected: Sequence[str]) -> bool:
    """Whether *expected* occurs in *seen* in this order (other entries between)."""
    remaining = iter(seen)
    return all(name in remaining for name in expected)


def _prompt(record: TurnRecord) -> str:
    [(prompt,)] = [call.args for call in record.calls("stream")]
    return prompt


def _assert_one_closer_last(record: TurnRecord, closer: str) -> None:
    names = _crew(record)
    assert [name for name in names if name in _CLOSERS] == [closer], names
    assert names[-2:] == ["on_step_completed", closer], names


def _assert_cycle_ends_once(record: TurnRecord, journal: _Journal) -> None:
    """``slots`` reports the slot idle, then one ``chat_done``, then no content."""
    kinds = [frame.kind for frame in record.ws_frames]
    assert kinds.count("chat_done") == 1, kinds
    done_at = journal.index("frame", "chat_done")
    pushes = [entry for entry in journal.entries[:done_at] if entry[0] == "slots"]
    assert pushes and pushes[-1][2] == {"running": False}, journal.entries
    content = _content(record)
    assert content[content.index("chat_done") + 1 :] == [], content
    assert record.window[-1]["role"] == "done"


async def _run(script: TurnScript) -> tuple[TurnRecord, _Journal]:
    journal = _Journal()
    own = script.setup

    def _setup(ctx: TurnContext) -> None:
        journal.setup(ctx)
        if own is not None:
            own(ctx)

    return await run_turn(dataclasses.replace(script, setup=_setup)), journal


class TestCanonicalTurnSequence:
    @pytest.mark.asyncio
    async def test_turn_sequence_landed_text_streams_persists_then_ends_the_cycle(self) -> None:
        record, journal = await _run(TurnScript(events=[_REPLY, _DONE]))

        assert _in_order(_content(record), ["chat_chunk", "chat_message:assistant", "chat_done"])
        assert _roles(record) == ["user", "assistant"]
        assert record.rows("assistant")[0]["content"] == "the reply"
        assert [call.name for call in record.provider_calls] == ["stream"]
        assert _prompt(record).endswith("hello")
        assert _in_order(_crew(record), [*_OPENING, "on_message_sent", "on_step_completed"])
        _assert_one_closer_last(record, "on_turn_completed")
        assert _in_order(_bracket(record), ["get_or_create", "begin_turn", "record_success"])
        assert _bracket(record)[-1] == "release"
        assert _bracket(record).count("release") == 1
        [claim] = journal.named("session", "get_or_create")
        assert claim["wait_if_busy"] is False
        assert record.successors == ()
        assert record.stop_reason == STOP_REASON_END_TURN
        _assert_cycle_ends_once(record, journal)

    @pytest.mark.asyncio
    async def test_turn_sequence_tool_call_approved_then_result_then_text(self) -> None:
        record, journal = await _run(
            TurnScript(
                events=[
                    AcpEvent(
                        kind=EVENT_TOOL_CALL, tool_call_id="t1", title="fs_write", tool_kind="edit"
                    ),
                    _permission(tool_call_id="t1"),
                    AcpEvent(
                        kind=EVENT_TOOL_RESULT,
                        tool_call_id="t1",
                        tool_output="ok",
                        tool_final=True,
                        tool_status="completed",
                    ),
                    _REPLY,
                    _DONE,
                ],
                answers={"req-1": APPROVED},
            )
        )

        assert _in_order(
            _content(record),
            [
                "tool_call",
                "chat_message:permission",
                "approval_resolved",
                "tool_result",
                "chat_message:assistant",
                "chat_done",
            ],
        )
        # One card for one prompt: a second card would be a second click.
        assert len(record.approval_cards) == 1
        assert _roles(record) == ["user", "tool", "tool", "assistant"]
        assert record.rows("assistant")[0]["content"] == "the reply"
        # Answered on the spot, not by the window expiring.
        assert [(call.name, call.at) for call in record.provider_calls] == [
            ("stream", 0.0),
            ("approve_tool", 0.0),
        ]
        assert record.calls("approve_tool")[0].args == ("req-1",)
        assert _prompt(record).endswith("hello")
        assert [(d["approval_id"], d["decision"]) for d in record.approval_decisions] == [
            ("req-1", "approved")
        ]
        assert [audit["outcome"] for audit in record.audits()] == ["invoked", "approved"]
        assert _in_order(
            _crew(record),
            [
                *_OPENING,
                "on_tool_called",
                "on_approval_requested",
                "on_approval_decided",
                "on_tool_completed",
                "on_step_completed",
                "on_step_started",
                "on_message_sent",
            ],
        )
        _assert_one_closer_last(record, "on_turn_completed")
        assert record.stop_reason == STOP_REASON_END_TURN
        _assert_cycle_ends_once(record, journal)

    @pytest.mark.asyncio
    async def test_turn_sequence_unanswered_prompt_expires_at_the_approval_window(self) -> None:
        record, journal = await _run(TurnScript(events=[_permission(), _REPLY, _DONE]))

        assert _in_order(
            _content(record),
            [
                "chat_message:permission",
                "chat_message:error",
                "approval_resolved",
                "chat_message:assistant",
                "chat_done",
            ],
        )
        assert len(record.approval_cards) == 1
        assert _roles(record) == ["user", "error", "tool", "assistant"]
        # The expiry declines the call on the wire and tells the model why, both at
        # exactly the window's end.
        assert [(call.name, call.at) for call in record.provider_calls] == [
            ("stream", 0.0),
            ("steer", TOOL_APPROVAL_TIMEOUT),
            ("reject_tool", TOOL_APPROVAL_TIMEOUT),
        ]
        assert [audit["outcome"] for audit in record.audits()] == ["rejected"]
        assert len(record.approval_decisions) == 1
        _assert_one_closer_last(record, "on_turn_completed")
        assert record.stop_reason == STOP_REASON_END_TURN
        _assert_cycle_ends_once(record, journal)

    @pytest.mark.asyncio
    async def test_turn_sequence_empty_reply_requeues_instead_of_ending_the_cycle(self) -> None:
        record, journal = await _run(TurnScript(events=[_DONE], message="again please"))

        assert _content(record)[-2:] == ["queue_pop", "chat_message:inject"]
        assert "chat_done" not in _content(record)
        assert _roles(record) == ["user"]
        [successor] = record.successors
        assert successor.args[0] == "again please"
        assert successor.kwargs["_synthetic_recovery_turn"] is True
        assert record.window[-1]["role"] == "inject"
        assert len(record.calls("stream")) == 1
        _assert_one_closer_last(record, "on_turn_completed")
        assert _bracket(record)[-1] == "release"
        # The session is handed back, and the turn's identity and in-flight marker
        # retired, before the successor is popped.
        assert journal.index("session", "release") < journal.index("frame", "queue_pop")
        [popped] = journal.named("frame", "queue_pop")
        assert (popped["active_key"], popped["marker"], popped["lock_held"]) == ("", 0, False)

    @pytest.mark.asyncio
    async def test_turn_sequence_chunk_then_error_keeps_the_text_above_the_card(self) -> None:
        record, journal = await _run(TurnScript(events=[_REPLY, Raise(RuntimeError("boom"))]))

        assert _in_order(
            _content(record),
            ["chat_chunk", "chat_message:assistant", "chat_message:error", "chat_done"],
        )
        assert _roles(record) == ["user", "assistant", "error"]
        assert record.rows("assistant")[0]["content"] == "the reply"
        assert "boom" in record.rows("error")[0]["content"]
        assert _in_order(_crew(record), [*_OPENING, "on_message_sent", "on_step_completed"])
        _assert_one_closer_last(record, "on_turn_failed")
        [failed] = record.crew("on_turn_failed")
        assert failed.kwargs["error"] == "RuntimeError"
        assert _in_order(_bracket(record), ["get_or_create", "begin_turn", "record_failure"])
        assert _bracket(record)[-1] == "release"
        assert "record_success" not in _bracket(record)
        assert record.successors == ()
        assert record.stop_reason == "failed: RuntimeError"
        _assert_cycle_ends_once(record, journal)

    @pytest.mark.asyncio
    async def test_turn_sequence_refused_session_shows_the_card_and_never_opens(self) -> None:
        record, journal = await _run(TurnScript(allocation_error=RuntimeError("spawn refused")))

        assert _in_order(_content(record), ["chat_message:error", "chat_done"])
        assert _roles(record) == ["user", "error"]
        assert "spawn refused" in record.rows("error")[0]["content"]
        assert record.provider_calls == ()
        # A turn that never reached its provider never opened in the crew log.
        assert record.crew_log == ()
        assert record.stop_reason == ""
        assert _bracket(record) == ["get_or_create", "record_failure"]
        assert record.successors == ()
        _assert_cycle_ends_once(record, journal)

    @pytest.mark.asyncio
    async def test_turn_sequence_local_slash_command_answers_without_a_session(self) -> None:
        record, journal = await _run(TurnScript(message="/goal ship the release"))

        assert _in_order(_content(record), ["chat_message:assistant", "chat_done"])
        assert [row["role"] for row in record.window] == ["user", "assistant", "done"]
        assert record.allocations == []
        assert _bracket(record) == []
        assert record.provider_calls == ()
        assert record.crew_log == ()
        assert record.successors == ()
        # The command's reply is shown while the slot still reads running.
        pushes = [facts["running"] for kind, _, facts in journal.entries if kind == "slots"]
        assert pushes[0] is True and pushes[-1] is False
        _assert_cycle_ends_once(record, journal)

    @pytest.mark.asyncio
    async def test_turn_sequence_warm_second_turn_claims_with_the_lease_wait(self) -> None:
        second = _Journal()

        def _registered(ctx: TurnContext) -> None:
            # The first turn's session is still registered when the next one starts.
            second.setup(ctx)
            ctx.state.sessions.has_session.side_effect = lambda _key: True

        record, first = await _run(
            TurnScript(
                events=[_REPLY, _DONE],
                message="first",
                then=TurnScript(
                    events=[_REPLY, _DONE],
                    message="second",
                    setup=_registered,
                    provider=_SecondSessionProvider,
                ),
            )
        )

        assert record.then is not None
        then = record.then
        assert _in_order(_content(then), ["chat_chunk", "chat_message:assistant", "chat_done"])
        assert [(row["role"], row["content"]) for row in then.history_rows] == [
            ("user", "first"),
            ("assistant", "the reply"),
            ("user", "second"),
            ("assistant", "the reply"),
        ]
        assert _prompt(then).endswith("second")
        [claim] = second.named("session", "get_or_create")
        assert (claim["wait_if_busy"], claim["lock_held"]) == (True, False)
        # The second session names the first one's id as its predecessor.
        [opened] = then.crew("on_session_opened")
        assert opened.kwargs["previous_sid"] == "acp-session-1"
        _assert_one_closer_last(then, "on_turn_completed")
        assert _bracket(then)[-1] == "release"
        _assert_cycle_ends_once(record, first)
        _assert_cycle_ends_once(then, second)


#: Scripts built fresh for each run, so no exception outlives the test that raised it.
_CYCLE_SCRIPTS: dict[str, Callable[[], TurnScript]] = {
    "landed": lambda: TurnScript(events=[_REPLY, _DONE]),
    "error": lambda: TurnScript(events=[_REPLY, Raise(RuntimeError("boom"))]),
    "refused": lambda: TurnScript(allocation_error=RuntimeError("spawn refused")),
    "local-command": lambda: TurnScript(message="/goal ship the release"),
}


class TestTheCycleEndsInOneOrder:
    """The frontend settles a turn on the idle ``slots`` frame, then ``chat_done``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", list(_CYCLE_SCRIPTS))
    async def test_turn_sequence_idle_slots_push_precedes_the_single_chat_done(
        self, name: str
    ) -> None:
        record, journal = await _run(_CYCLE_SCRIPTS[name]())

        _assert_cycle_ends_once(record, journal)
        [done] = journal.named("frame", "chat_done")
        # By chat_done the turn has handed back everything it held.
        assert (done["lock_held"], done["active_key"], done["marker"]) == (False, "", 0)

    @pytest.mark.asyncio
    async def test_turn_sequence_release_precedes_the_marker_clear_and_chat_done(self) -> None:
        record, journal = await _run(TurnScript(events=[_REPLY, Raise(RuntimeError("boom"))]))

        [released] = journal.named("session", "release")
        # The in-flight marker of a turn that did not land is retired only after the
        # session is handed back, and before the cycle ends.
        assert released["marker"] > 0
        assert released["lock_held"] is False
        assert journal.index("session", "release") < journal.index("frame", "chat_done")
        [done] = journal.named("frame", "chat_done")
        assert done["marker"] == 0
        assert record.stop_reason == "failed: RuntimeError"

    @pytest.mark.asyncio
    async def test_turn_sequence_same_script_gives_the_same_ordered_journal(self) -> None:
        script = TurnScript(
            events=[_permission(), _REPLY, _DONE], answers={"req-1": APPROVED}, message="twice"
        )
        first_record, first = await _run(script)
        second_record, second = await _run(script)

        assert [entry[:2] for entry in first.entries] == [entry[:2] for entry in second.entries]
        assert _content(first_record) == _content(second_record)
        assert _crew(first_record) == _crew(second_record)
