"""Redaction runs on across a tool call or an approval prompt inside one reply.

A tool call or a permission request is a DISPLAY boundary inside one reply, not the
end of it: the model's text before the boundary and its text after it are one run
of characters, so a credential can start in the first and finish in the second.
Each half alone matches no credential pattern, so a redaction window that ends at
the boundary streams and saves both halves raw, and a reader who joins the two rows
(or the two runs of ``chat_chunk`` frames) has the whole secret.

The boundary still splits the reply for display: narration written before a tool
call stays on the row before it, and the reply a linked channel receives is the
text after the last boundary only. So harmless text is pinned as well: every word
lands on the row it was written in, unchanged, while the credential case comes out
as one placeholder.

These drive the real ``_run_chat`` through ``turn_harness.run_turn`` and read what
the turn sent on the WebSocket, what it persisted, and what it queued for the
slot's stream readers (the /api/chat SSE stream and the OpenAI-compatible
replies). The key is the documented AWS example key, split so no literal sits in
the repo.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from turn_harness import APPROVED, Do, Raise, TurnContext, TurnRecord, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.constants import reflow_and_label_glued_option_marker
from kiro_crew.dashboard import chat_runner, openai_compat
from kiro_crew.dashboard.chat_utils import _build_stream_chunk
from kiro_crew.security import REDACTED_CREDENTIAL_TAG, redact

#: The AWS documentation's example access key id, split so no literal sits here.
_HEAD, _TAIL = "AKIA", "IOSFODNN7EXAMPLE"
_KEY = _HEAD + _TAIL
_TAG = REDACTED_CREDENTIAL_TAG
#: A made-up URL credential parameter: the name, then a value no other pass reads
#: as a secret, so only the name in front of it makes it one.
_PARAM, _VALUE = "?token=", "fakeParamValue123"

_KINDS = ("permission_request", "tool_call")
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


def _boundary(kind: str, n: int) -> AcpEvent:
    """The *n*-th display boundary of a turn: a tool call, or an approval prompt."""
    if kind == "tool_call":
        return AcpEvent(
            kind=EVENT_TOOL_CALL, tool_call_id=f"tc-{n}", title="Reading a file", tool_kind="read"
        )
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=f"req-{n}",
        tool_call_id=f"tc-{n}",
        title="Reading a file",
        tool_kind="read",
    )


async def _turn(
    kind: str,
    *segments: str | tuple[str, ...],
    setup: Callable[[TurnContext], Any] | None = None,
) -> TurnRecord:
    """Run one turn whose text is *segments*, a *kind* boundary between each two.

    A segment is one text chunk, or a tuple of the chunks it streams as; an empty
    last segment ends the turn right after its last boundary. Every prompt is
    approved.
    """
    events: list[AcpEvent] = []
    for n, segment in enumerate(segments):
        if n:
            events.append(_boundary(kind, n))
        for chunk in (segment,) if isinstance(segment, str) else segment:
            if chunk:
                events.append(AcpEvent(kind=EVENT_TEXT_CHUNK, text=chunk))
    events.append(_DONE)
    answers = {f"req-{n}": APPROVED for n in range(1, len(segments))}
    return await run_turn(TurnScript(events=events, answers=answers, setup=setup))


async def _turn_ended_by_shutdown(kind: str) -> TurnRecord:
    """Run a turn that writes "Let me check the config" before its first *kind*
    boundary and that the gateway's shutdown ends while that tool runs or that
    approval waits: the gateway's graceful save, then the drain that cancels the
    turn."""

    async def _shutting_down(ctx: TurnContext) -> None:
        await chat_runner.save_slot_off_loop(ctx.state, ctx.slot, force=True)
        ctx.state.sessions.final_drain_started = True

    events = [
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="Let me check the config"),
        _boundary(kind, 1),
        Do(_shutting_down),
        Raise(asyncio.CancelledError()),
    ]
    return await run_turn(TurnScript(events=events, answers={"req-1": APPROVED}))


class _Queue(list):
    """``slot._pending`` that keeps a copy of each row as it is queued, which is
    what a reader draining it as rows arrive sends on."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[dict[str, Any]] = []
        self.reader: Any = None

    def append(self, item: Any) -> None:  # type: ignore[override]
        self.seen.append(copy.deepcopy(item))
        super().append(item)


async def _read_turn(
    kind: str, *segments: str | tuple[str, ...]
) -> tuple[TurnRecord, list[dict[str, Any]]]:
    """Run :func:`_turn` with a stream reader holding the slot's delivery queue,
    as the /api/chat SSE stream and the OpenAI-compatible replies hold it; return
    the record and every row the turn queued, in order."""
    queue = _Queue()

    def _attach(ctx: TurnContext) -> None:
        ctx.slot._pending = queue
        # Held for the whole turn: a released queue drops its chunk rows.
        queue.reader = ctx.slot.pending_consumer()
        queue.reader.__enter__()

    record = await _turn(kind, *segments, setup=_attach)
    return record, queue.seen


def _sse_reply(queued: list[dict[str, Any]]) -> str:
    """The reply an /api/chat SSE client joins from the stream's ``chunk`` frames."""
    frames = [json.loads(_build_stream_chunk(row)) for row in queued]
    return "".join(frame["content"] for frame in frames if frame["type"] == "chunk")


async def _openai_reply(queued: list[dict[str, Any]]) -> str:
    """The OpenAI-compatible (non-streaming) reply built from the queued rows."""
    slot = SimpleNamespace(
        _pending=[*copy.deepcopy(queued), {"cls": "done"}], event=asyncio.Event(), task=None
    )

    def _drain() -> list[dict[str, Any]]:
        rows = slot._pending[:]
        slot._pending.clear()
        return rows

    slot.drain = _drain
    resp = await openai_compat._blocking_response(
        SimpleNamespace(_slots={}), slot, "chatcmpl-1", "model", 0, False
    )
    return json.loads(resp.text)["choices"][0]["message"]["content"]


def _wire(record: TurnRecord) -> str:
    """Every ``chat_chunk`` the turn streamed, joined in order."""
    return "".join(frame.payload["content"] for frame in record.frames("chat_chunk"))


def _saved(record: TurnRecord) -> list[str]:
    """The assistant rows the turn persisted, in order."""
    return [row["content"] for row in record.rows("assistant")]


def _shown(record: TurnRecord) -> list[str]:
    """Each assistant row as an open tab holds it once every frame has landed: the
    row's ``chat_message``, then each content patch addressed to that row."""
    rows: dict[str, str] = {}
    for frame in record.ws_frames:
        payload: Any = frame.payload
        if frame.kind == "chat_message" and payload.get("role") == "assistant":
            rows[payload["meta"]["mid"]] = payload["content"]
        elif frame.kind == "chat_message_update" and payload.get("mid") in rows:
            rows[payload["mid"]] = payload["content"]
    return list(rows.values())


def _sent_text(record: TurnRecord) -> str:
    """Everything the turn put in a content frame: chunks, rows and row patches."""
    parts = []
    for frame in record.ws_frames:
        if frame.kind in ("chat_chunk", "chat_message", "chat_message_update"):
            parts.append(str(frame.payload.get("content", "")))
    return "\n".join(parts)


def _layout(record: TurnRecord) -> list[str]:
    """The window after the turn: assistant rows by content, and each boundary's
    rows (an approval card, the tool row it gates) as one ``|``."""
    out: list[str] = []
    for row in record.window:
        if row["role"] == "assistant":
            out.append(row["content"])
        elif row["role"] in ("tool", "permission") and out[-1:] != ["|"]:
            out.append("|")
    return out


def _chunks_after_boundary(record: TurnRecord) -> str:
    """The ``chat_chunk`` text streamed after the turn's first boundary was shown."""
    frames = list(record.ws_frames)
    first = next(
        i
        for i, frame in enumerate(frames)
        if frame.kind == "tool_call"
        or (frame.kind == "chat_message" and frame.payload.get("role") == "permission")
    )
    return "".join(f.payload["content"] for f in frames[first:] if f.kind == "chat_chunk")


@pytest.fixture
def mirrored(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The final reply the turn hands its linked channel, recorded."""
    replies: list[str] = []

    async def _record(_state: Any, _key: str, text: str, **_kwargs: Any) -> None:
        replies.append(text)

    monkeypatch.setattr(chat_runner, "_deliver_cross_surface_reply", _record)
    return replies


@pytest.mark.parametrize("kind", _KINDS)
class TestCredentialSplitAcrossABoundary:
    @pytest.mark.asyncio
    async def test_the_wire_stream_redacts_the_whole_credential(self, kind: str) -> None:
        record = await _turn(kind, f"Before {_HEAD}", f"{_TAIL} after.")

        assert _wire(record) == f"Before {_TAG} after."
        sent = _sent_text(record)
        assert _TAIL not in sent and _HEAD not in sent, sent

    @pytest.mark.asyncio
    async def test_the_persisted_history_redacts_the_whole_credential(self, kind: str) -> None:
        record = await _turn(kind, f"Before {_HEAD}", f"{_TAIL} after.")

        # The narration stays on the row before the boundary; the placeholder
        # opens the row after it, where the credential was completed.
        assert _saved(record) == ["Before ", f"{_TAG} after."]
        assert _shown(record) == _saved(record)

    @pytest.mark.asyncio
    async def test_a_credential_split_over_two_boundaries_is_redacted(self, kind: str) -> None:
        record = await _turn(kind, f"Before {_HEAD}", _TAIL[:4], f"{_TAIL[4:]} after.")

        assert "".join(_saved(record)) == f"Before {_TAG} after."
        assert _wire(record) == f"Before {_TAG} after."
        sent = _sent_text(record)
        assert _TAIL[:4] not in sent and _TAIL[4:] not in sent, sent

    @pytest.mark.asyncio
    async def test_a_value_whose_name_survives_redaction_joins_the_name(
        self, kind: str, mirrored: list[str]
    ) -> None:
        """A URL parameter cut between its name and its value: redacting the two
        together keeps the name and replaces only the value, so the redacted output
        still starts with the held name. The name must still go with the value it
        names, everywhere the reply is saved or sent."""
        record, queued = await _read_turn(kind, f"Call /api{_PARAM}", f"{_VALUE} then")

        whole = redact(f"Call /api{_PARAM}{_VALUE} then")
        assert _VALUE not in whole, "the pass this test drives no longer redacts the value"
        assert "".join(_saved(record)) == whole
        assert "".join(_shown(record)) == whole
        assert _sse_reply(queued) == whole
        sent = _sent_text(record) + json.dumps(queued)
        assert _VALUE not in sent, sent
        [reply] = mirrored
        assert _VALUE not in redact(reply), reply

    @pytest.mark.asyncio
    @pytest.mark.parametrize("after", [" after.", ""], ids=["text-follows", "turn-ends"])
    async def test_a_credential_whose_halves_are_held_at_two_boundaries_is_redacted(
        self, kind: str, after: str
    ) -> None:
        """Each half of the key is held at its own boundary, so the halves meet
        only in the redactor. Whether text follows them or the turn ends there,
        every reader gets the placeholder and never a half."""
        record, queued = await _read_turn(kind, f"Before {_HEAD}", _TAIL, after)

        assert "".join(_saved(record)) == f"Before {_TAG}{after}"
        assert "".join(_shown(record)) == f"Before {_TAG}{after}"
        assert _sse_reply(queued) == f"Before {_TAG}{after}"
        sent = _sent_text(record) + json.dumps(queued)
        assert _TAIL not in sent and _HEAD not in sent, sent

    @pytest.mark.asyncio
    async def test_a_credential_whole_before_the_boundary_stays_redacted(self, kind: str) -> None:
        """The key is complete before the boundary, and the text after it starts
        with no space, so the two joined do not read as a key: the row before
        the boundary keeps the placeholder it saves on its own."""
        record = await _turn(kind, (f"Key {_HEAD}", _TAIL), "Done.")

        assert _saved(record) == [f"Key {_TAG}", "Done."]
        assert _KEY not in _sent_text(record)

    @pytest.mark.asyncio
    async def test_the_linked_channel_reply_sees_the_credential_whole(
        self, kind: str, mirrored: list[str]
    ) -> None:
        record = await _turn(kind, f"Before {_HEAD}", f"{_TAIL} after.")

        # The linked channel gets the turn's whole answer, its rows joined as the
        # transcript shows them, with the credential redacted whole on its row.
        [reply] = mirrored
        assert reply == "\n\n".join(_saved(record))
        assert redact(reply) == reply
        assert _HEAD not in reply and _TAIL not in reply, reply

    @pytest.mark.asyncio
    async def test_the_stream_readers_get_the_credential_redacted_whole(self, kind: str) -> None:
        """The run the boundary held opened the next segment, so it is never
        queued on its own: the SSE stream and the OpenAI reply see one placeholder."""
        _record, queued = await _read_turn(kind, f"Before {_HEAD}", f"{_TAIL} after.")

        assert _sse_reply(queued) == f"Before {_TAG} after."
        assert await _openai_reply(queued) == f"Before {_TAG} after."
        sent = json.dumps(queued)
        assert _TAIL not in sent and _HEAD not in sent, sent


def test_a_held_run_goes_back_only_on_the_variant_it_was_cut_from() -> None:
    """A run goes back on the row it was cut from, in the content the row holds
    then, and on the variant shown at the cut; a row showing another variant
    since is left as it is. Either way the run is queued for the slot's stream
    readers as the chunk it was held back from: it is part of what streamed."""
    frames: list[tuple[str, dict[str, Any]]] = []
    queued: list[tuple[str, str]] = []
    state = SimpleNamespace(broadcast_ws=lambda kind, payload: frames.append((kind, payload)))
    slot = SimpleNamespace(
        key="chat-1",
        workspace=None,
        _dirty=False,
        push_wire_frame=lambda cls, content: queued.append((cls, content)),
    )
    row: dict[str, Any] = {
        "role": "assistant",
        "content": "Let me ",
        "ts": "t1",
        "meta": {"mid": "m1"},
        "variants": [{"content": "An earlier reply"}, {"content": "Let me "}],
        "variant_idx": 1,
    }

    chat_runner._give_back_run(state, slot, row, "look.", 1)

    assert row["content"] == "Let me look." and row["variants"][1]["content"] == "Let me look."
    assert slot._dirty is True
    assert frames == [
        (
            "chat_message_update",
            {"slot": "chat-1", "ts": "t1", "mid": "m1", "content": "Let me look."},
        )
    ]
    row.update(content="An earlier reply", variant_idx=0)
    chat_runner._give_back_run(state, slot, row, " again", 1)
    assert row["content"] == "An earlier reply" and len(frames) == 1
    assert queued == [("chunk", "look."), ("chunk", " again")]
    # No text to give back changes nothing and queues nothing.
    chat_runner._give_back_run(state, slot, row, "", 0)
    assert row["content"] == "An earlier reply" and len(frames) == 1 and len(queued) == 2


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("Let me check", "Let me chec[REDACTED]"),
        ("Let me chec", "Let me check"),
        (None, "Let me check"),
    ],
    ids=["text-ends-otherwise", "copy-ends-otherwise", "no-copy"],
)
def test_a_run_the_copy_and_the_text_do_not_both_end_with_is_not_held(
    raw: str | None, text: str
) -> None:
    """A run is held off its segment only where the raw copy and the streamed
    text can be cut at the same place. Anywhere else both stay whole, and the
    cut saves the segment as it streamed."""
    slot = SimpleNamespace(segment_raw_text=raw, segment_shown_sha="as before")

    assert chat_runner._hold_segment_tail(slot, text, "check") is None
    assert (slot.segment_raw_text, slot.segment_shown_sha) == (raw, "as before")


def test_a_held_run_that_overflows_the_raw_copy_drops_the_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A held run put in front of a raw copy at its cap drops the copy, as an
    over-long streamed delta does; the streamed text still gets the run."""
    monkeypatch.setattr(chat_runner, "_SEGMENT_RAW_MAX_CHARS", 10)
    slot = SimpleNamespace(
        segment_raw_text="0123456789", segment_removed={"value": "tag"}, segment_shown_sha=None
    )

    assert chat_runner._open_segment_with(slot, "0123456789", "word") == "word0123456789"
    assert slot.segment_raw_text is None and slot.segment_removed == {}


@pytest.mark.parametrize("kind", _KINDS)
class TestHarmlessTextAcrossABoundary:
    @pytest.mark.asyncio
    async def test_a_word_split_by_the_boundary_stays_where_it_was_written(
        self, kind: str, mirrored: list[str]
    ) -> None:
        record = await _turn(kind, "Let me check the config", "uration file now.")

        assert _saved(record) == ["Let me check the config", "uration file now."]
        assert _shown(record) == _saved(record)
        assert _layout(record) == ["Let me check the config", "|", "uration file now."]
        # The bubble after the boundary streams only its own text.
        assert _chunks_after_boundary(record) == "uration file now."
        assert mirrored == ["\n\n".join(_saved(record))]

    @pytest.mark.asyncio
    async def test_narration_ending_a_sentence_stays_on_its_row(
        self, kind: str, mirrored: list[str]
    ) -> None:
        record = await _turn(kind, "Let me check the config.", "The config says hi.")

        assert _saved(record) == ["Let me check the config.", "The config says hi."]
        assert _shown(record) == _saved(record)
        assert _chunks_after_boundary(record) == "The config says hi."
        assert mirrored == ["Let me check the config.\n\nThe config says hi."]

    @pytest.mark.asyncio
    async def test_a_turn_ending_at_the_boundary_keeps_its_last_word(self, kind: str) -> None:
        record = await _turn(kind, "Let me check the config.", "")

        assert _saved(record) == ["Let me check the config."]
        assert _shown(record) == _saved(record)

    @pytest.mark.asyncio
    async def test_a_word_given_back_after_an_options_footer_is_repaired_as_one_save(
        self, kind: str
    ) -> None:
        """Prose glued to an ``[OPTIONS:]`` footer is moved to its own labelled line
        when a segment is saved. A word held at the boundary and given back after
        the footer is that glued prose, so the row ends as the whole segment's save
        would have left it, with the footer intact."""
        text = "Pick a path.\n[OPTIONS: A | B]Anytime."
        record = await _turn(kind, text, "")

        repaired, glued = reflow_and_label_glued_option_marker(text)
        assert glued, "the footer this test drives is no longer read as glued"
        assert _saved(record) == [repaired]
        assert "]Anytime." not in "".join(_shown(record))

    @pytest.mark.asyncio
    async def test_a_segment_held_whole_reaches_the_crew_log(self, kind: str) -> None:
        """A segment that is one held word saves an empty row at the boundary, and
        an empty body writes no ``message/sent``. The word given back is written
        then, under the model call that wrote it."""
        record = await _turn(kind, "OK.", "")

        assert _saved(record) == ["OK."]
        bodies = [call.kwargs.get("text") for call in record.crew("on_message_sent")]
        # The cut's call carries the empty row, which the emitter writes nothing for.
        assert [body for body in bodies if body] == ["OK."]

    @pytest.mark.asyncio
    async def test_a_word_given_back_as_shutdown_ends_the_turn_is_saved(self, kind: str) -> None:
        """The graceful save ran before the held word went back on its row, and the
        turn's teardown does not save while the process exits, so the turn's last
        seam saves the row: the next start shows it with its last word."""
        record = await _turn_ended_by_shutdown(kind)

        assert _saved(record) == ["Let me check the config"]

    @pytest.mark.asyncio
    async def test_a_save_failing_at_shutdown_is_logged_and_the_seam_goes_on(
        self, kind: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        real_save = chat_runner.save_slot_off_loop

        async def _save(state: Any, slot: Any, *args: Any, **kwargs: Any) -> bool:
            if state.sessions.final_drain_started:
                raise RuntimeError("the disk went away")
            return await real_save(state, slot, *args, **kwargs)

        monkeypatch.setattr(chat_runner, "save_slot_off_loop", _save)
        caplog.set_level(logging.DEBUG)
        record = await _turn_ended_by_shutdown(kind)

        # The graceful save's copy is what the next start reads.
        assert _saved(record) == ["Let me check the "]
        assert "Saving the partial reply at shutdown failed" in caplog.text

    @pytest.mark.asyncio
    async def test_a_word_alone_between_two_boundaries_stays_between_them(
        self, kind: str, mirrored: list[str]
    ) -> None:
        record = await _turn(kind, "Let me look.", "OK.", "Found it.")

        assert _saved(record) == ["Let me look.", "OK.", "Found it."]
        assert _shown(record) == _saved(record)
        assert _layout(record) == ["Let me look.", "|", "OK.", "|", "Found it."]
        assert mirrored == ["\n\n".join(_saved(record))]


@pytest.mark.parametrize("kind", _KINDS)
class TestStreamReadersAcrossABoundary:
    """The /api/chat SSE stream and the OpenAI-compatible replies build the reply
    from the chunks they drain off the slot and never see a row patch, so a run
    given back to its row reaches them as the chunk it was held back from: what
    they join is every word the model wrote, once and in order."""

    @pytest.mark.asyncio
    async def test_the_last_word_before_the_boundary_reaches_them(self, kind: str) -> None:
        record, queued = await _read_turn(kind, "Let me check the config", " file now.")

        assert _sse_reply(queued) == "Let me check the config file now."
        assert await _openai_reply(queued) == "Let me check the config file now."
        assert _saved(record) == ["Let me check the config", " file now."]

    @pytest.mark.asyncio
    async def test_a_word_split_by_the_boundary_reaches_them_whole(self, kind: str) -> None:
        _record, queued = await _read_turn(kind, "Let me check the config", "uration file now.")

        assert _sse_reply(queued) == "Let me check the configuration file now."
        assert await _openai_reply(queued) == "Let me check the configuration file now."

    @pytest.mark.asyncio
    async def test_runs_held_over_two_boundaries_reach_them_in_order(self, kind: str) -> None:
        _record, queued = await _read_turn(kind, "Let me look.", "OK.", "Found it.")

        assert _sse_reply(queued) == "Let me look.OK.Found it."
        assert await _openai_reply(queued) == "Let me look.OK.Found it."

    @pytest.mark.asyncio
    async def test_a_turn_ending_at_the_boundary_delivers_its_last_word(self, kind: str) -> None:
        _record, queued = await _read_turn(kind, "Let me check the config.", "")

        assert _sse_reply(queued) == "Let me check the config."
        assert await _openai_reply(queued) == "Let me check the config."
