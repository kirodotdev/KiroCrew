"""How a dashboard turn ends when it is cancelled, wherever the cancel lands.

``chat_runner._run_chat`` treats a bare ``CancelledError`` as one of two ends: the
operator's Stop press, labelled the non-fault ``cancelled``, or an involuntary
cancel (a closed tab, an idle sweep, shutdown, the turn ceiling), labelled
``error: cancelled``. Either way it keeps the text the user watched stream,
records the turn's spend, closes the turn in the crew log, hands the session
back and ends the cycle once. These tests drive real turns through
``turn_harness.run_turn``, cancel them mid-stream, mid-prompt and inside a cold
session claim, and assert on the record and the slot's end state only, never on
the runner's source.

The usage rows are the one wall-clock-free trace of the cancel label, so the
turns here run on a provider that reports billing (an ``AcpPromptStats`` with
credits) and a ``client`` handle, with the usage shard directory moved to the
test's own ``tmp_path``. The reply's footer is not asserted: it is measured on
the wall clock and is omitted when the turn took under a millisecond.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from turn_harness import (
    CANCEL_TURN,
    STOP,
    Do,
    ScriptedProvider,
    TurnContext,
    TurnRecord,
    TurnScript,
    Wait,
    run_turn,
)

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    AcpEvent,
    AcpPromptStats,
)
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers import usage as usage_handlers
from kiro_crew.llm_helpers import slot_switch_session_lock

_PARTIAL = AcpEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
_REPLY = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
_CREDITS = 2.5
_PROMPT = AcpEvent(
    kind=EVENT_PERMISSION_REQUEST, request_id="req-1", title="fs_write", tool_kind="edit"
)


class _BilledProvider(ScriptedProvider):
    """A provider with an inner client handle whose prompt bills ``_CREDITS``.

    The handle is what the turn publishes as ``slot._acp_client``; the abnormal-end
    usage row is written only while it is live. Fresh stats are installed as each
    prompt starts, as the ACP runner does, so the turn's spend is its own.
    """

    client = object()

    def stream(self, message: str, *, allow_image: bool = True) -> Any:
        self.last_prompt_stats = AcpPromptStats(credits=_CREDITS)
        return super().stream(message, allow_image=allow_image)


@pytest.fixture
def usage_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(usage_handlers, "_TOKEN_USAGE_DIR", tmp_path)
    return tmp_path


def _usage_rows(shard_dir: Path) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    for shard in sorted(shard_dir.glob("*.jsonl")):
        for line in shard.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rows.append((row["stop_reason"], row["credits"]))
    return rows


def _session_names(record: TurnRecord) -> list[str]:
    return [call.name for call in record.session_calls]


class _Seen:
    """What a ``Do`` or ``setup`` captured from inside the turn."""

    def __init__(self) -> None:
        self.task: Any = None
        self.ctx: TurnContext | None = None
        self.claims: list[tuple[bool, Any]] = []
        self.lock: Any = None

    def capture(self, ctx: TurnContext) -> None:
        self.ctx = ctx
        self.task = ctx.slot.task

    def cancel(self, ctx: TurnContext) -> None:
        self.capture(ctx)
        ctx.slot.task.cancel()

    def stop(self, ctx: TurnContext) -> None:
        # The Stop press: the flag ``_stop_pressed`` reads, then the task cancel.
        self.capture(ctx)
        ctx.slot._stopping = True
        ctx.slot.task.cancel()

    def close_tab(self, ctx: TurnContext) -> None:
        # Closing the chat fences the slot before it cancels the turn.
        self.capture(ctx)
        ctx.slot.begin_close()
        ctx.slot.task.cancel()


def _assert_cancelled_mid_stream(record: TurnRecord, seen: _Seen) -> None:
    """The duties every mid-stream cancel performs, whoever caused it."""
    assert [(row["role"], row["content"]) for row in record.history_rows] == [
        ("user", "hello"),
        ("assistant", "partial answer"),
    ]
    names = [call.name for call in record.crew_log]
    [sent] = record.crew("on_message_sent")
    assert sent.kwargs["interrupted"] is True
    assert names.index("on_message_sent") < names.index("on_turn_failed")
    assert names[-2:] == ["on_step_completed", "on_turn_failed"]
    assert record.stop_reason == "failed: CancelledError"
    # Neither a success nor a breaker failure: a cancel says nothing about the session.
    assert "record_success" not in _session_names(record)
    assert "record_failure" not in _session_names(record)
    assert _session_names(record).count("release") == 1
    # The first-turn history debt stays owed: kiro-cli discards a cancelled prompt.
    assert "consume_first_turn_history_owed" not in _session_names(record)
    assert [frame.kind for frame in record.ws_frames].count("chat_done") == 1
    assert record.successors == ()
    assert seen.ctx is not None
    slot = seen.ctx.slot
    assert (slot._active_turn_session_key, slot._acp_client, slot.task) == ("", None, None)
    # The guarded turn task ends cancelled, after its finally and tail have run.
    assert seen.task.done() and seen.task.cancelled()


class TestCancelMidStream:
    @pytest.mark.asyncio
    async def test_turn_cancel_involuntary_mid_stream_records_error_cancelled(
        self, usage_dir: Path
    ) -> None:
        seen = _Seen()
        record = await run_turn(
            TurnScript(events=[_PARTIAL, Do(seen.cancel), Wait(1), _DONE], provider=_BilledProvider)
        )

        _assert_cancelled_mid_stream(record, seen)
        [failed] = record.crew("on_turn_failed")
        assert failed.kwargs["error"] == "CancelledError"
        assert _usage_rows(usage_dir) == [("error: cancelled", _CREDITS)]
        assert seen.ctx is not None and not seen.ctx.slot.is_closing

    @pytest.mark.asyncio
    async def test_turn_cancel_stop_press_mid_stream_records_the_cancelled_label(
        self, usage_dir: Path
    ) -> None:
        seen = _Seen()
        record = await run_turn(
            TurnScript(events=[_PARTIAL, Do(seen.stop), Wait(1), _DONE], provider=_BilledProvider)
        )

        _assert_cancelled_mid_stream(record, seen)
        assert _usage_rows(usage_dir) == [(STOP_REASON_CANCELLED, _CREDITS)]
        assert [row["role"] for row in record.window] == ["user", "assistant", "done"]

    @pytest.mark.asyncio
    async def test_turn_cancel_closing_tab_lands_the_close_interruption_row(
        self, usage_dir: Path
    ) -> None:
        seen = _Seen()
        record = await run_turn(TurnScript(events=[_PARTIAL, Do(seen.close_tab), Wait(1), _DONE]))

        kinds = [(row["role"], (row.get("meta") or {}).get("kind")) for row in record.window]
        assert kinds == [
            ("user", None),
            ("assistant", None),
            ("error", "chat_close_interruption"),
            ("done", None),
        ]
        assert record.stop_reason == "failed: CancelledError"
        assert _session_names(record).count("release") == 1

    @pytest.mark.asyncio
    async def test_turn_cancel_without_a_close_lands_no_interruption_row(self) -> None:
        seen = _Seen()
        record = await run_turn(TurnScript(events=[_PARTIAL, Do(seen.cancel), Wait(1), _DONE]))

        assert [row["role"] for row in record.window] == ["user", "assistant", "done"]

    @pytest.mark.asyncio
    async def test_turn_cancel_after_text_was_streamed_saves_the_reply_once(self) -> None:
        seen = _Seen()
        record = await run_turn(
            TurnScript(events=[_REPLY, _PARTIAL, Do(seen.cancel), Wait(1), _DONE])
        )

        assert [row["content"] for row in record.rows("assistant")] == ["the replypartial answer"]
        assert len(record.crew("on_message_sent")) == 1


class TestCancelBeforeTheStream:
    @pytest.mark.asyncio
    async def test_turn_cancel_inside_cold_claim_frees_the_lock_and_the_identity(self) -> None:
        seen = _Seen()

        def _cancel_inside_the_claim(ctx: TurnContext) -> None:
            seen.capture(ctx)
            lock = slot_switch_session_lock(effective_session_key(ctx.slot))
            answer = ctx.state.sessions.get_or_create.side_effect

            async def _claim(*args: Any, **kwargs: Any) -> Any:
                seen.claims.append((lock.locked(), kwargs.get("wait_if_busy")))
                ctx.slot.task.cancel()
                # The claim's own await: a cold start yields while the runtime
                # spawns, and the cancel lands here.
                await asyncio.sleep(0)
                return await answer(*args, **kwargs)

            ctx.state.sessions.get_or_create.side_effect = _claim
            seen.lock = lock

        record = await run_turn(TurnScript(events=[_REPLY, _DONE], setup=_cancel_inside_the_claim))

        # Cancelled while the dispatch lock was held for the cold claim...
        assert seen.claims == [(True, False)]
        # ...and the turn's teardown still handed it back and retired the identity.
        assert seen.lock.locked() is False
        assert seen.ctx is not None
        assert seen.ctx.slot._active_turn_session_key == ""
        assert seen.ctx.slot._turn_in_flight_generation == 0
        assert record.calls("stream") == []
        assert record.crew_log == ()
        assert record.stop_reason == ""
        assert "release" not in _session_names(record)
        assert record.successors == ()
        assert [frame.kind for frame in record.ws_frames].count("chat_done") == 1


class TestCancelMidPrompt:
    @pytest.mark.asyncio
    async def test_turn_cancel_mid_prompt_retires_the_card_without_a_wire_answer(
        self, usage_dir: Path
    ) -> None:
        seen = _Seen()
        record = await run_turn(
            TurnScript(
                events=[Do(seen.capture), _PROMPT, _REPLY, _DONE],
                answers={"req-1": CANCEL_TURN},
                provider=_BilledProvider,
            )
        )

        [card] = [row for row in record.window if row["role"] == "permission"]
        assert json.loads(card["cls"])["resolved"] == "rejected"
        assert [frame.kind for frame in record.ws_frames].count("approval_resolved") == 1
        # The cancel ends the prompt from this side only: nothing is sent back.
        assert [call.name for call in record.provider_calls] == ["stream"]
        assert len(record.approval_decisions) == 1
        assert record.rows("assistant") == []
        assert record.stop_reason == "failed: CancelledError"
        assert _session_names(record).count("release") == 1
        assert _usage_rows(usage_dir) == [("error: cancelled", _CREDITS)]
        assert seen.ctx is not None
        assert seen.ctx.slot._approval_futures == {}
        assert seen.task.cancelled()

    @pytest.mark.asyncio
    async def test_turn_cancel_stop_mid_prompt_unblocks_the_wait_by_declining_the_call(
        self, usage_dir: Path
    ) -> None:
        seen = _Seen()
        record = await run_turn(
            TurnScript(
                events=[
                    Do(seen.capture),
                    _PROMPT,
                    AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
                ],
                answers={"req-1": STOP},
                provider=_BilledProvider,
            )
        )

        [card] = [row for row in record.window if row["role"] == "permission"]
        assert json.loads(card["cls"])["resolved"] == "rejected"
        # The pending wait is released by declining the call on the wire, at once.
        assert [(call.name, call.at) for call in record.provider_calls] == [
            ("stream", 0.0),
            ("reject_tool", 0.0),
        ]
        assert record.calls("reject_tool")[0].args == ("req-1",)
        assert record.calls("stream")[0].args[0].endswith("hello")
        assert [(d["approval_id"], d["decision"]) for d in record.approval_decisions] == [
            ("req-1", "rejected")
        ]
        assert [audit["outcome"] for audit in record.audits()] == ["rejected"]
        assert _session_names(record).count("release") == 1
        assert seen.ctx is not None
        assert seen.ctx.slot._approval_futures == {}
