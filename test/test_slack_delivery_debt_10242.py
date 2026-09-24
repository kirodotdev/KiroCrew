"""A Slack stream that loses real answer text for good must say so.

An append Slack refuses is retried once on a fresh message. When both the append
and that retry fail, those characters are on no message at all, and the reader is
looking at an answer with a hole in it that nothing marks.

Both stream paths now carry a per-turn delivery-debt flag and disclose the loss
at finalize. These tests pin the disclosure on each path, pin that a turn which
lost nothing stays silent, and pin that the notice is not answer text: it must
not make a stream that delivered no answer look like a delivered one.

The repair the disclosure replaces is unreachable, and that is measured rather
than assumed: a refused append always attempts a rotation, so a for-good loss
arrives at finalize with the answer already spread over two messages. Restating
the whole text in the message the reader is watching would repeat the abandoned
one, so the notice is the only honest move left. When the rotation itself fails
the stream is demoted instead, and the demoted path re-sends that segment's
complete text on its own -- ``test_native_rotation_failure_resends_the_lost_text``
pins that, because it is the reason the repair branch is absent.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    STOP_REASON_END_TURN,
)
from kiro_crew.slack import transport_dispatch
from kiro_crew.slack.handler import DELIVERY_DEBT_NOTICE

_test_dir = Path(__file__).parent
if str(_test_dir) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(_test_dir))

from conftest import MockSlackClient  # noqa: E402
from kiro_crew.providers.base import LLMEvent  # noqa: E402
from kiro_crew.slack.handler import handle_message  # noqa: E402

_handler_tests = importlib.import_module("test_slack_handler")
_golden = importlib.import_module("test_slack_golden_transcript")

FakeSessionManager = _handler_tests.FakeSessionManager
FakeProvider = _handler_tests.FakeProvider
FakeSessions = _golden.FakeSessions
RecordingSlackClient = _golden.RecordingSlackClient
ScriptedProvider = _golden.ScriptedProvider
make_event = _golden.make_event

_MSG_TS = "1700000000.000100"

#: A fragment of the notice distinctive enough to find in a transcript without
#: pinning its exact wording, which is free to be reworded.
_NOTICE_MARK = "did not reach Slack"

_LOST = "BRAVO-LOST"


def _assert_notice_shape() -> None:
    """The fragment the assertions look for has to be in the real notice.

    Without this the searches below pass vacuously the moment the notice is
    reworded past the fragment: every ``notice in transcript`` check would go on
    reporting absence, which is what a missing disclosure also looks like.
    """
    assert _NOTICE_MARK in DELIVERY_DEBT_NOTICE


class _TrackNativeSessions(FakeSessionManager):
    """Counts the successes and failures the native turn books."""

    def __init__(self, provider):
        super().__init__(provider)
        self.calls = {"success": 0, "failure": 0}

    def record_success(self, key):
        self.calls["success"] += 1

    async def record_failure(self, key):
        self.calls["failure"] += 1
        return False


class _TrackTransportSessions(FakeSessions):
    """Counts the successes and failures the transport turn books."""

    def __init__(self, provider):
        super().__init__(provider)
        self.calls = {"success": 0, "failure": 0}

    def record_success(self, session_key):
        self.calls["success"] += 1

    async def record_failure(self, session_key):
        self.calls["failure"] += 1


# ── native path ──


class _NativeLosesOneDeltaSlack(MockSlackClient):
    """The first append lands, every later one is refused, rotation succeeds.

    That is the shape a for-good loss actually takes: the retry on the fresh
    message is refused too, so the middle chunk is on no message, and the answer
    now spans the abandoned message and the replacement.

    The refusal window never closes here, so the notice's own append is refused
    as well. That makes this the harness for the fallback, not for the
    on-the-stream disclosure.
    """

    def __init__(self):
        super().__init__()
        self._stream_enabled = True
        self._appends = 0

    async def append_stream(self, channel, ts, text):
        self.actions.append(("append_stream", {"channel": channel, "ts": ts, "text": text}))
        self._appends += 1
        return self._appends == 1


class _NativeRecoversAfterTheLossSlack(MockSlackClient):
    """One delta is refused twice, then Slack accepts again.

    A refusal window that closes is the common case, and it is the one where the
    notice can go on the stream the reader is watching. Appends two and three are
    the lost delta and its retry on the fresh message; everything after lands.
    """

    def __init__(self):
        super().__init__()
        self._stream_enabled = True
        self._appends = 0

    async def append_stream(self, channel, ts, text):
        self.actions.append(("append_stream", {"channel": channel, "ts": ts, "text": text}))
        self._appends += 1
        return self._appends not in (2, 3)


def _native_texts(slack) -> str:
    """Every string the native path put on the wire, joined."""
    return "\n".join(str(payload) for _name, payload in slack.actions)


def _native_notice_sinks(slack) -> tuple[bool, bool]:
    """Where the notice was sent: (attempted on the stream, posted as a message).

    Attempting an append is not delivering one -- ``append_stream`` refuses by
    returning False -- so the two sinks are counted separately and no assertion
    can mistake one for the other.
    """
    on_stream = any(
        name == "append_stream" and _NOTICE_MARK in str(payload.get("text", ""))
        for name, payload in slack.actions
    )
    as_message = any(
        name == "post" and _NOTICE_MARK in str(payload.get("text", ""))
        for name, payload in slack.actions
    )
    return on_stream, as_message


def test_native_for_good_loss_is_disclosed_on_the_stream():
    _assert_notice_shape()
    slack = _NativeRecoversAfterTheLossSlack()
    provider = FakeProvider(
        [
            LLMEvent(kind="text_chunk", text="alpha "),
            LLMEvent(kind="text_chunk", text=f"{_LOST} "),
            LLMEvent(kind="text_chunk", text="charlie"),
        ]
    )
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    # The reader is told the answer is incomplete.
    assert _NOTICE_MARK in _native_texts(slack), "a for-good loss was dropped without a word"
    # And it is on the stream the reader is watching, because Slack accepted it.
    # No separate message is needed, so none is sent.
    on_stream, as_message = _native_notice_sinks(slack)
    assert on_stream, "the notice never reached the stream"
    assert not as_message, "posted a fallback message for a notice the stream accepted"
    # And the turn is still a success: an answer did reach the reader, which is
    # what the accounting records. Disclosing a hole is not a failed turn.
    assert sessions.calls == {"success": 1, "failure": 0}


def test_native_refused_notice_reaches_a_separate_message():
    """The notice's own append can be refused, and unread that re-hides the loss.

    ``append_stream`` reports a refusal by returning False rather than raising, so
    a notice whose return value is ignored is as absent as the text it exists to
    disclose -- and the two refusals it takes fall inside one Slack rate-limit or
    outage window, which is correlated rather than independent. The disclosure has
    to land on a sink that confirms it.
    """
    _assert_notice_shape()
    slack = _NativeLosesOneDeltaSlack()
    provider = FakeProvider(
        [
            LLMEvent(kind="text_chunk", text="alpha "),
            LLMEvent(kind="text_chunk", text=f"{_LOST} "),
            LLMEvent(kind="text_chunk", text="charlie"),
        ]
    )
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    on_stream, as_message = _native_notice_sinks(slack)
    assert on_stream, "the notice was never even attempted on the stream"
    assert as_message, "the stream refused the notice and nothing re-sent it"


def test_native_turn_that_lost_nothing_stays_silent():
    """The other direction: no debt, no notice.

    A disclosure that fires on a clean turn would tell every reader their answer
    is broken, so the gate has to be tested for not firing as well as for firing.
    """
    _assert_notice_shape()
    slack = MockSlackClient()
    slack._stream_enabled = True
    provider = FakeProvider(
        [
            LLMEvent(kind="text_chunk", text="alpha "),
            LLMEvent(kind="text_chunk", text="charlie"),
        ]
    )
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert _NOTICE_MARK not in _native_texts(slack), "cried loss on a turn that lost nothing"
    assert sessions.calls == {"success": 1, "failure": 0}


class _NativeRefusesEveryAppendSlack(MockSlackClient):
    """Every append is refused for the whole turn, rotation succeeds.

    No real text is ever confirmed, so this turn delivered no answer.
    """

    def __init__(self):
        super().__init__()
        self._stream_enabled = True

    async def append_stream(self, channel, ts, text):
        self.actions.append(("append_stream", {"channel": channel, "ts": ts, "text": text}))
        return False


def test_native_notice_does_not_count_as_a_delivered_answer():
    """The notice must not be mistaken for the answer arriving.

    It is sent straight at the client rather than through the append helper, so
    it never enters the delivery ledger. If it did, a turn whose every real
    append was refused would book a success on the strength of its own apology.
    """
    slack = _NativeRefusesEveryAppendSlack()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the answer is 42")])
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert (
        sessions.calls["success"] == 0
    ), "the notice was counted as the answer reaching the reader"
    assert sessions.calls["failure"] == 1


class _NativeRotationAlsoFailsSlack(MockSlackClient):
    """The first append lands, later appends are refused, and rotation fails.

    A failed rotation demotes the stream, which is the branch that re-sends the
    segment's whole text instead of disclosing anything.
    """

    def __init__(self):
        super().__init__()
        self._stream_enabled = True
        self._appends = 0

    async def start_stream(self, channel, thread_ts, **kwargs):
        if self._appends:
            return None
        return await super().start_stream(channel, thread_ts, **kwargs)

    async def append_stream(self, channel, ts, text):
        self.actions.append(("append_stream", {"channel": channel, "ts": ts, "text": text}))
        self._appends += 1
        return self._appends == 1


def test_native_rotation_failure_resends_the_lost_text():
    """This case needs no notice, and that is why the repair branch is absent.

    A refused append always tries a rotation. When the rotation fails the stream
    is demoted and the end-of-turn update re-sends the segment's complete text,
    lost characters included -- so there is nothing to disclose, and the only
    other outcome is the rotation succeeding, where restating is what would
    duplicate. No reachable state is left for an overwrite to repair.
    """
    slack = _NativeRotationAlsoFailsSlack()
    provider = FakeProvider(
        [
            LLMEvent(kind="text_chunk", text="alpha "),
            LLMEvent(kind="text_chunk", text=f"{_LOST} "),
            LLMEvent(kind="text_chunk", text="charlie"),
        ]
    )
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    wire = _native_texts(slack)
    assert _LOST in wire, "the demoted path did not re-send the refused text"
    assert sessions.calls == {"success": 1, "failure": 0}


# ── transport path ──


def _run_transport(monkeypatch, slack, sessions):
    monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
    monkeypatch.setattr(
        transport_dispatch, "_hydrate_thread_overrides", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
    monkeypatch.setattr(transport_dispatch, "_thread_agents", {})
    asyncio.run(
        transport_dispatch.handle_message_transport(
            slack=slack,
            sessions=sessions,
            channel="C1",
            text="hello",
            thread_ts=None,
            msg_ts=_MSG_TS,
            user_id="U_OWNER",
            context_builder=None,
            conversation_log=None,
        )
    )


class _TransportLosesOneDeltaSlack(RecordingSlackClient):
    """The first append lands, every later one is refused, rotation succeeds.

    The refusal window never closes, so the notice's own append is refused too.
    That makes this the harness for the fallback, not for the on-the-stream
    disclosure.
    """

    def __init__(self) -> None:
        super().__init__()
        self._appends = 0

    async def append_stream(self, channel, ts, text) -> bool:
        self._rec("append_stream", channel=channel, ts=ts, text=text)
        self._appends += 1
        return self._appends == 1


class _TransportRecoversAfterTheLossSlack(RecordingSlackClient):
    """One delta is refused twice, then Slack accepts again.

    Appends two and three are the lost delta and its retry on the fresh message;
    everything after lands, so the notice can go on the stream.
    """

    def __init__(self) -> None:
        super().__init__()
        self._appends = 0

    async def append_stream(self, channel, ts, text) -> bool:
        self._rec("append_stream", channel=channel, ts=ts, text=text)
        self._appends += 1
        return self._appends not in (2, 3)


def _transport_texts(slack) -> str:
    """Every string the transport path put on the wire, joined."""
    return "\n".join(str(payload) for _name, payload in slack.transcript)


def _transport_notice_sinks(slack) -> tuple[bool, bool]:
    """Where the notice was sent: (attempted on the stream, posted as a message)."""
    on_stream = any(
        name == "append_stream" and _NOTICE_MARK in str(payload.get("text", ""))
        for name, payload in slack.transcript
    )
    as_message = any(
        name == "post_message" and _NOTICE_MARK in str(payload.get("text", ""))
        for name, payload in slack.transcript
    )
    return on_stream, as_message


def test_transport_for_good_loss_is_disclosed_on_the_stream(monkeypatch):
    _assert_notice_shape()
    slack = _TransportRecoversAfterTheLossSlack()
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="alpha "),
            make_event(EVENT_TEXT_CHUNK, text=f"{_LOST} "),
            make_event(EVENT_TEXT_CHUNK, text="charlie"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _TrackTransportSessions(provider)

    _run_transport(monkeypatch, slack, sessions)

    assert _NOTICE_MARK in _transport_texts(slack), "a for-good loss was dropped without a word"
    on_stream, as_message = _transport_notice_sinks(slack)
    assert on_stream, "the notice never reached the stream"
    assert not as_message, "posted a fallback message for a notice the stream accepted"
    assert sessions.calls == {"success": 1, "failure": 0}


def test_transport_refused_notice_reaches_a_separate_message(monkeypatch):
    """Same refusal on this path: the notice's own append returns False.

    The fallback message is sent directly rather than through the append helper,
    so it discloses the loss without entering the delivery ledger the rescue
    replays.
    """
    _assert_notice_shape()
    slack = _TransportLosesOneDeltaSlack()
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="alpha "),
            make_event(EVENT_TEXT_CHUNK, text=f"{_LOST} "),
            make_event(EVENT_TEXT_CHUNK, text="charlie"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _TrackTransportSessions(provider)

    _run_transport(monkeypatch, slack, sessions)

    on_stream, as_message = _transport_notice_sinks(slack)
    assert on_stream, "the notice was never even attempted on the stream"
    assert as_message, "the stream refused the notice and nothing re-sent it"


def test_transport_turn_that_lost_nothing_stays_silent(monkeypatch):
    """The other direction on this path: no debt, no notice."""
    _assert_notice_shape()
    slack = RecordingSlackClient()
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="alpha "),
            make_event(EVENT_TEXT_CHUNK, text="charlie"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _TrackTransportSessions(provider)

    _run_transport(monkeypatch, slack, sessions)

    assert _NOTICE_MARK not in _transport_texts(slack), "cried loss on a turn that lost nothing"
    assert sessions.calls == {"success": 1, "failure": 0}


class _NativeRefusesTheLostDeltaSlack(MockSlackClient):
    """Refuses every append carrying the lost delta; everything else lands.

    Keyed on the text rather than on a counter so the harness does not depend on
    how many cards a tool boundary happens to add to the stream. The delta and its
    post-rotation retry both carry the marker, so both are refused and the debt is
    set, while the notice and any later answer text are accepted.
    """

    def __init__(self):
        super().__init__()
        self._stream_enabled = True

    async def append_stream(self, channel, ts, text):
        self.actions.append(("append_stream", {"channel": channel, "ts": ts, "text": text}))
        return _LOST not in text


def _native_notice_count(slack) -> int:
    """How many notices the native path put on the wire, counting both sinks."""
    return sum(
        1
        for name, payload in slack.actions
        if name in ("append_stream", "post") and _NOTICE_MARK in str(payload.get("text", ""))
    )


def _wait_call() -> LLMEvent:
    """A ``wait`` tool call, the event that makes the handler abandon its stream."""
    return LLMEvent(kind=EVENT_TOOL_CALL, title="wait", tool_name="wait", tool_call_id="t-wait")


def test_native_wait_boundary_discloses_before_it_drops_the_stream():
    """A turn whose lost text precedes a ``wait`` still tells the reader.

    The boundary seals that message and clears both the stream handle and the
    accumulated source. A turn ending with no post-wait text opens no replacement
    stream, so the disclosure at finalize is gated out, and the lost characters are
    absent from the text any later message could restate. The boundary itself is
    the last point the reader can be told, and the message the gap is in is the
    right place to tell them.
    """
    _assert_notice_shape()
    slack = _NativeRefusesTheLostDeltaSlack()
    provider = FakeProvider(
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="alpha "),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"{_LOST} "),
            _wait_call(),
        ]
    )
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert _NOTICE_MARK in _native_texts(
        slack
    ), "a wait boundary dropped the stream and took the loss with it"
    on_stream, _as_message = _native_notice_sinks(slack)
    assert on_stream, "the notice did not reach the message the gap is in"


def test_native_debt_settled_at_a_wait_is_not_reported_twice():
    """Post-wait text opens a fresh stream, and finalize must not repeat the notice.

    Settling clears the debt, so the reader is told once, on the message that lost
    the text. A second notice on the replacement message would report a gap they
    have already been shown and point it at text that is not missing there.
    """
    _assert_notice_shape()
    slack = _NativeRefusesTheLostDeltaSlack()
    provider = FakeProvider(
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="alpha "),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"{_LOST} "),
            _wait_call(),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="charlie"),
        ]
    )
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert _native_notice_count(slack) == 1, "the same loss was disclosed more than once"


class _TransportRefusesTheLostDeltaSlack(RecordingSlackClient):
    """Refuses every append carrying the lost delta; everything else lands."""

    async def append_stream(self, channel, ts, text) -> bool:
        self._rec("append_stream", channel=channel, ts=ts, text=text)
        return _LOST not in text


def _transport_notice_count(slack) -> int:
    """How many notices the transport path put on the wire, counting both sinks."""
    return sum(
        1
        for name, payload in slack.transcript
        if name in ("append_stream", "post_message")
        and _NOTICE_MARK in str(payload.get("text", ""))
    )


def test_transport_wait_boundary_discloses_before_it_drops_the_stream(monkeypatch):
    """Same boundary on the transport path, same last chance to say so."""
    _assert_notice_shape()
    slack = _TransportRefusesTheLostDeltaSlack()
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="alpha "),
            make_event(EVENT_TEXT_CHUNK, text=f"{_LOST} "),
            make_event(EVENT_TOOL_CALL, title="wait", tool_name="wait", tool_call_id="t-wait"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _TrackTransportSessions(provider)

    _run_transport(monkeypatch, slack, sessions)

    assert _NOTICE_MARK in _transport_texts(
        slack
    ), "a wait boundary dropped the stream and took the loss with it"
    on_stream, _as_message = _transport_notice_sinks(slack)
    assert on_stream, "the notice did not reach the message the gap is in"


def test_transport_debt_settled_at_a_wait_is_not_reported_twice(monkeypatch):
    """Post-wait text opens a fresh stream, and on_done must not repeat the notice."""
    _assert_notice_shape()
    slack = _TransportRefusesTheLostDeltaSlack()
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="alpha "),
            make_event(EVENT_TEXT_CHUNK, text=f"{_LOST} "),
            make_event(EVENT_TOOL_CALL, title="wait", tool_name="wait", tool_call_id="t-wait"),
            make_event(EVENT_TEXT_CHUNK, text="charlie"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _TrackTransportSessions(provider)

    _run_transport(monkeypatch, slack, sessions)

    assert _transport_notice_count(slack) == 1, "the same loss was disclosed more than once"
