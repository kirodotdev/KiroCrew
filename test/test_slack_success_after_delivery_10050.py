"""Slack turn-success accounting must run only after the end-of-turn answer
delivery finishes.

On both Slack paths a failure in the answer-carrying send means the reader
received no answer, so the turn is a failure. These tests assert that a failed
answer-carrying send books a failure (not a success) on each path, and that a
successful delivery still books exactly one success.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
)
from kiro_crew.slack import transport_dispatch

_test_dir = Path(__file__).parent
if str(_test_dir) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(_test_dir))
_golden = importlib.import_module("test_slack_golden_transcript")

FakeSessions = _golden.FakeSessions
RecordingSlackClient = _golden.RecordingSlackClient
ScriptedProvider = _golden.ScriptedProvider
make_event = _golden.make_event

_MSG_TS = "1700000000.000100"


class _TrackSessions(FakeSessions):
    """Records how many successes vs failures the turn booked."""

    def __init__(self, provider):
        super().__init__(provider)
        self.calls = {"success": 0, "failure": 0}

    def record_success(self, session_key):
        self.calls["success"] += 1

    async def record_failure(self, session_key):
        self.calls["failure"] += 1


class _DeliveryFailsSlackClient(RecordingSlackClient):
    """No-stream client whose answer-carrying send (the placeholder update)
    fails. ``start_stream`` returns None so the renderer falls back to a
    ``_THINKING`` placeholder and finalizes it via ``update_message`` — which is
    THE delivery of the answer on this path."""

    def __init__(self) -> None:
        super().__init__()
        self.stream_disabled = True

    async def update_message(self, channel, ts, text="", blocks=None) -> None:
        self._rec("update_message", channel=channel, ts=ts, text=text)
        raise RuntimeError("slack chat.update failed — answer never delivered")


def _run(monkeypatch, slack, sessions):
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


def test_transport_failed_answer_delivery_records_failure_not_success(monkeypatch):
    slack = _DeliveryFailsSlackClient()
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="the answer"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _TrackSessions(provider)

    _run(monkeypatch, slack, sessions)

    # The reader received no answer, so the turn is a failure and MUST NOT be
    # booked a success.
    assert sessions.calls["success"] == 0, "booked a success for an undelivered answer"
    assert sessions.calls["failure"] == 1


def test_transport_successful_delivery_still_records_success(monkeypatch):
    slack = RecordingSlackClient()  # streaming enabled, all sends succeed
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="the answer"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _TrackSessions(provider)

    _run(monkeypatch, slack, sessions)

    assert sessions.calls == {"success": 1, "failure": 0}


# ── Native path (handler.handle_message) ──

from conftest import MockSlackClient  # noqa: E402
from kiro_crew.providers.base import LLMEvent  # noqa: E402
from kiro_crew.slack.handler import handle_message  # noqa: E402

_handler_tests = importlib.import_module("test_slack_handler")
FakeSessionManager = _handler_tests.FakeSessionManager
FakeProvider = _handler_tests.FakeProvider


class _TrackNativeSessions(FakeSessionManager):
    def __init__(self, provider):
        super().__init__(provider)
        self.calls = {"success": 0, "failure": 0}
        self.releases = 0

    def record_success(self, key):
        self.calls["success"] += 1

    async def record_failure(self, key):
        self.calls["failure"] += 1
        return False

    def release(self, key):
        self.releases += 1


class _NativeDeliveryFailsSlack(MockSlackClient):
    """Streaming disabled (MockSlackClient default), so the final answer is
    delivered via ``_safe_final_update`` -> ``update_message`` on the posted
    ``_THINKING`` placeholder. That update fails: the reader never gets the
    answer."""

    async def update_message(self, channel, ts, text=""):
        self.actions.append(("update", {"channel": channel, "ts": ts, "text": text}))
        # The FIRST update after the answer text is the answer-carrying final
        # render. Fail every update: the placeholder cursor edits are best-effort
        # and the final one is the one that must surface as a failed delivery.
        raise RuntimeError("slack chat.update failed — answer never delivered")


def test_native_failed_answer_delivery_records_failure_not_success(monkeypatch):
    slack = _NativeDeliveryFailsSlack()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the answer is 42")])
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert sessions.calls["success"] == 0, "booked a success for an undelivered answer"
    assert sessions.calls["failure"] == 1


def test_native_successful_delivery_records_success(monkeypatch):
    slack = MockSlackClient()  # stream disabled but update_message succeeds
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the answer is 42")])
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert sessions.calls == {"success": 1, "failure": 0}


# ── F1: the permit is held through accounting (breaker-state window closed) ──


class _OrderedSessions(FakeSessionManager):
    """Records the order of release vs record_success so a test can assert the
    permit outlives accounting."""

    def __init__(self, provider):
        super().__init__(provider)
        self.events: list[str] = []

    def record_success(self, key):
        self.events.append("record_success")

    async def record_failure(self, key):
        self.events.append("record_failure")
        return False

    def release(self, key):
        self.events.append("release")


def test_native_permit_released_only_after_accounting(monkeypatch):
    # The window the finding names is between release and accounting: if release
    # ran first, a queued same-key turn could mutate breaker state mid-verdict.
    # So release MUST NOT precede record_success.
    slack = MockSlackClient()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the answer is 42")])
    sessions = _OrderedSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert "record_success" in sessions.events
    assert "release" in sessions.events
    assert sessions.events.index("record_success") < sessions.events.index(
        "release"
    ), f"permit released before accounting: {sessions.events}"


# ── F2: a model error plus a failed error-post books exactly one failure ──

from kiro_crew.acp.client import AcpError  # noqa: E402


class _RaisingProvider:
    """Provider whose stream raises a model-level error before any text."""

    working_directory = None

    async def stream(self, message, timeout=120.0):
        raise AcpError("auth expired")
        yield  # pragma: no cover — make it an async generator

    async def approve_tool(self, request_id, option_id="allow_once"):
        pass

    async def reject_tool(self, request_id):
        pass

    async def start(self):
        pass

    async def shutdown(self):
        pass

    def context_usage_pct(self):
        return 0.0


class _ErrorPostFailsSlack(MockSlackClient):
    """Every message/update send fails, so the error-text post in the delivery
    block also raises after the model error already booked its failure."""

    async def update_message(self, channel, ts, text=""):
        self.actions.append(("update", {"channel": channel, "ts": ts, "text": text}))
        raise RuntimeError("slack down")

    async def post_message(
        self, channel, text, thread_ts=None, unfurl_links=None, unfurl_media=None
    ):
        self.actions.append(("post_message", {"channel": channel, "text": text}))
        raise RuntimeError("slack down")


def test_native_model_error_plus_failed_error_post_books_one_failure(monkeypatch):
    slack = _ErrorPostFailsSlack()
    sessions = _TrackNativeSessions(_RaisingProvider())

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    # The AcpError handler booked one failure; the failed error-post in the
    # delivery block must NOT book a second one for the same turn.
    assert sessions.calls["failure"] == 1, f"double-counted: {sessions.calls}"
    assert sessions.calls["success"] == 0


# ── F3: a footer post failure leaves a delivered turn recorded as success ──


class _FooterFailsSlackClient(RecordingSlackClient):
    """Streaming succeeds (answer delivered), but the final footer post_blocks
    fails. A delivered turn must stay a success."""

    async def post_blocks(
        self, channel, blocks, text, thread_ts=None, unfurl_links=None, unfurl_media=None
    ):
        self._rec("post_blocks", channel=channel, text=text)
        raise RuntimeError("slack post_blocks failed at footer")


def test_transport_footer_failure_keeps_delivered_turn_a_success(monkeypatch):
    slack = _FooterFailsSlackClient()
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="the answer [OPTIONS: a | b]"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _TrackSessions(provider)

    _run(monkeypatch, slack, sessions)

    # The answer streamed and sealed; only the footer failed. That is decoration
    # after finalization and must not turn a received turn into a failure.
    assert sessions.calls == {"success": 1, "failure": 0}


# ── Structural release: an unlisted raise in the delivery/accounting region must
#    still free the session permit (else the per-session semaphore wedges) ──

import asyncio as _asyncio  # noqa: E402

from kiro_crew.slack.handler import ACTIVATION_REVIEW  # noqa: E402


class _ReviewEphemeralRaisesSlack(MockSlackClient):
    """Review path: the ephemeral draft post raises (user_not_in_channel / rate
    limit / any SlackApiError propagating from chat_postEphemeral)."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True  # stream so the answer is delivered first

    async def post_ephemeral(self, channel, user_id, text, blocks=None, thread_ts=None):
        self.actions.append(("ephemeral", {"channel": channel, "text": text}))
        raise RuntimeError("slack_api_error: user_not_in_channel")


def test_review_ephemeral_raise_still_releases_permit(monkeypatch):
    slack = _ReviewEphemeralRaisesSlack()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the draft answer")])
    sessions = _TrackNativeSessions(provider)

    # A raising ephemeral post must NOT strand the permit — the key stays
    # acquirable. Design's stated clearance condition.
    try:
        asyncio.run(
            handle_message(
                slack,
                sessions,
                "C1",
                "q?",
                None,
                "msg1",
                "U1",
                channel_activation=ACTIVATION_REVIEW,
            )
        )
    except RuntimeError:
        # The raise propagates after the finally releases; that is acceptable.
        pass

    assert sessions.releases >= 1, "permit stranded after a raising ephemeral post"


class _CancelDuringDeliverySlack(MockSlackClient):
    """A send during the delivery block raises CancelledError, modelling a
    stop/shutdown cancelling the turn after the model completed."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True

    async def stop_stream(self, channel, ts, final_text=None):
        self.actions.append(("stop_stream", {"channel": channel, "ts": ts}))
        raise _asyncio.CancelledError()


def test_cancelled_after_completion_still_releases_permit(monkeypatch):
    slack = _CancelDuringDeliverySlack()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the answer is 42")])
    sessions = _TrackNativeSessions(provider)

    # CancelledError takes the deferred branch in the finally then propagates;
    # the permit must still be released on the way out.
    try:
        asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))
    except _asyncio.CancelledError:
        pass

    assert sessions.releases >= 1, "permit stranded after a cancel mid-delivery"


# ── One invariant, three regressions it settles ──


def test_native_review_ephemeral_raise_books_one_failure(monkeypatch):
    slack = _ReviewEphemeralRaisesSlack()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the draft answer")])
    sessions = _TrackNativeSessions(provider)

    try:
        asyncio.run(
            handle_message(
                slack,
                sessions,
                "C1",
                "q?",
                None,
                "msg1",
                "U1",
                channel_activation=ACTIVATION_REVIEW,
            )
        )
    except RuntimeError:
        pass

    # The draft never reached the reader, so exactly one failure is booked — the
    # breaker must not be left with no verdict at all.
    assert (
        sessions.calls["failure"] == 1
    ), f"review ephemeral raise left no verdict: {sessions.calls}"
    assert sessions.calls["success"] == 0


class _OptionsFooterFailsSlack(MockSlackClient):
    """Answer streams fine, but the footer post_blocks (which carries the
    [OPTIONS] choices) fails. The choices must be re-sent as fallback text so
    they are not silently dropped, and the turn stays a success."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True
        self.fallback_posts = []

    async def post_blocks(
        self, channel, blocks, text, thread_ts=None, unfurl_links=None, unfurl_media=None
    ):
        self.actions.append(("post_blocks", {"channel": channel}))
        raise RuntimeError("footer post_blocks failed")

    async def post_message(
        self, channel, text, thread_ts=None, unfurl_links=None, unfurl_media=None
    ):
        self.actions.append(("post_message", {"channel": channel, "text": text}))
        if text.startswith("*Options:*"):
            self.fallback_posts.append(text)
        return await super().post_message(channel, text, thread_ts)


def test_native_options_footer_failure_delivers_choices_as_fallback(monkeypatch):
    slack = _OptionsFooterFailsSlack()
    provider = FakeProvider(
        [LLMEvent(kind="text_chunk", text="pick one [OPTIONS: Apple | Banana]")]
    )
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    # The answer reached the reader (stream), so the turn is a success; the
    # options-carrying footer failed, so the choices are re-sent as text rather
    # than silently dropped.
    assert sessions.calls == {"success": 1, "failure": 0}
    assert (
        slack.fallback_posts
    ), "options footer failed but choices were not re-sent as fallback text"
    joined = "\n".join(slack.fallback_posts)
    assert "Apple" in joined and "Banana" in joined


# ── F3: both the OPTIONS footer and its fallback fail -> book a failure ──


class _OptionsFooterAndFallbackFailSlack(MockSlackClient):
    """Answer streams fine, but BOTH the footer post_blocks and the plain-text
    fallback fail. The choices (answer-carrying) never reached the reader, so the
    turn must NOT record success."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True

    async def post_blocks(
        self, channel, blocks, text, thread_ts=None, unfurl_links=None, unfurl_media=None
    ):
        self.actions.append(("post_blocks", {"channel": channel}))
        raise RuntimeError("footer post_blocks failed")

    async def post_message(
        self, channel, text, thread_ts=None, unfurl_links=None, unfurl_media=None
    ):
        self.actions.append(("post_message", {"channel": channel, "text": text}))
        if text.startswith("*Options:*"):
            raise RuntimeError("fallback post_message failed too")
        return await super().post_message(channel, text, thread_ts)


def test_native_options_footer_and_fallback_both_fail_books_failure(monkeypatch):
    slack = _OptionsFooterAndFallbackFailSlack()
    provider = FakeProvider(
        [LLMEvent(kind="text_chunk", text="pick one [OPTIONS: Apple | Banana]")]
    )
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    # The choices are answer-carrying and neither delivery landed, so the single
    # verdict is a failure — not the success a body-only view would book.
    assert (
        sessions.calls["success"] == 0
    ), "booked success though OPTIONS choices never reached the reader"
    assert sessions.calls["failure"] == 1


# ── F2: the OPTIONS fallback text passes display-safe redaction ──


class _OptionsFooterFailsCapturingSlack(MockSlackClient):
    """Footer fails; the plain-text fallback is captured so the test can assert
    it was redacted before sending."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True
        self.fallback_text = None

    async def post_blocks(
        self, channel, blocks, text, thread_ts=None, unfurl_links=None, unfurl_media=None
    ):
        raise RuntimeError("footer failed")

    async def post_message(
        self, channel, text, thread_ts=None, unfurl_links=None, unfurl_media=None
    ):
        if text.startswith("*Options:*"):
            self.fallback_text = text
        return await super().post_message(channel, text, thread_ts)


def test_native_options_fallback_is_redacted(monkeypatch):
    slack = _OptionsFooterFailsCapturingSlack()
    # An option containing a credential-shaped token must be redacted in the
    # fallback text, not rendered raw into Slack.
    secret = "AKIAIOSFODNN7EXAMPLE"
    provider = FakeProvider(
        [LLMEvent(kind="text_chunk", text=f"pick [OPTIONS: use {secret} | skip]")]
    )
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert slack.fallback_text is not None, "fallback was not posted"
    assert secret not in slack.fallback_text, "credential rendered raw into the OPTIONS fallback"


# ── F1: a cancellation at the pre-delivery yield must release the permit ──
# (the finally must not read a local bound only after that yield)


def test_native_cancel_at_predelivery_yield_releases_permit(monkeypatch):
    import kiro_crew.slack.handler as _h

    real_sleep = asyncio.sleep
    state = {"tripped": False}

    async def _sleep(delay, *a, **k):
        # Trip the FIRST zero-delay sleep (the post-finalize yield inside the
        # structural try) with a cancellation, exactly the stop/shutdown case.
        if delay == 0 and not state["tripped"]:
            state["tripped"] = True
            raise asyncio.CancelledError()
        return await real_sleep(delay, *a, **k)

    monkeypatch.setattr(_h.asyncio, "sleep", _sleep)

    slack = MockSlackClient()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the answer is 42")])
    sessions = _TrackNativeSessions(provider)

    try:
        asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))
    except asyncio.CancelledError:
        pass

    assert state["tripped"], "the pre-delivery yield was not exercised"
    # The permit is released on the way out — no UnboundLocalError in the finally
    # swallowing the release.
    assert sessions.releases >= 1, "permit stranded by a cancellation at the pre-delivery yield"


# ── F2: transport path — both OPTIONS deliveries failing records a failure ──


class _TransportOptionsBothFailSlack(RecordingSlackClient):
    """Answer streams fine, but the footer post_blocks AND the options fallback
    post_message both fail — the choices never reached the reader."""

    async def post_blocks(
        self, channel, blocks, text, thread_ts=None, unfurl_links=None, unfurl_media=None
    ):
        self._rec("post_blocks", channel=channel, text=text)
        raise RuntimeError("footer post_blocks failed")

    async def post_message(
        self, channel, text, thread_ts=None, unfurl_links=None, unfurl_media=None
    ):
        self._rec("post_message", channel=channel, text=text)
        if text.startswith("*Options:*"):
            raise RuntimeError("options fallback failed too")
        return self._next_ts()


def test_transport_options_footer_and_fallback_both_fail_records_failure(monkeypatch):
    slack = _TransportOptionsBothFailSlack()
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="pick one [OPTIONS: Apple | Banana]"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _TrackSessions(provider)

    _run(monkeypatch, slack, sessions)

    # The OPTIONS choices are answer-carrying; both deliveries failed, so the
    # renderer propagates and the dispatcher records a failure, not a success.
    assert sessions.calls["success"] == 0, "booked success though the OPTIONS choices never landed"
    assert sessions.calls["failure"] == 1


# ── F1: a stream on which EVERY append is refused delivered no answer ──
#    (used-stream stays a success as delivery-debt; wholly-refused stream fails)


class _AllAppendsRefusedSlack(MockSlackClient):
    """Streaming is enabled and stop_stream succeeds, but every append_stream is
    refused for the whole turn (a Slack append outage). The reader sees no answer
    text, so the turn is a failure — not a success booked from the mere fact that
    a stream was opened."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True

    async def append_stream(self, channel, ts, text):
        self.actions.append(("append_stream", {"channel": channel, "ts": ts, "text": text}))
        return False  # refused, and refused again after any rotation retry


def test_native_stream_all_appends_refused_books_failure(monkeypatch):
    slack = _AllAppendsRefusedSlack()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the answer is 42")])
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    # No append landed, so the answer never reached the reader. Booking a success
    # here is exactly the before-delivery accounting this change removes.
    assert (
        sessions.calls["success"] == 0
    ), "booked success for a stream that delivered no answer text"
    assert sessions.calls["failure"] == 1


class _OneAppendThenRefusedSlack(MockSlackClient):
    """First append lands, the rest are refused. The stream WAS used, so the
    refused remainder is recoverable delivery-debt and the turn stays a
    success — the change must not turn a used stream into a failure."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True
        self._appends = 0

    async def append_stream(self, channel, ts, text):
        self.actions.append(("append_stream", {"channel": channel, "ts": ts, "text": text}))
        self._appends += 1
        return self._appends == 1  # only the first append is confirmed


def test_native_stream_partial_delivery_stays_a_success(monkeypatch):
    slack = _OneAppendThenRefusedSlack()
    provider = FakeProvider(
        [
            LLMEvent(kind="text_chunk", text="first part "),
            LLMEvent(kind="text_chunk", text="second part"),
        ]
    )
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    # At least one real-text append was confirmed, so the answer reached the
    # reader; the refused remainder is delivery-debt, not a turn failure.
    assert sessions.calls == {"success": 1, "failure": 0}


# ── F2: the OPTIONS fallback must scrub against what Slack RENDERS, not just the
#    literal bytes — an emphasis-split credential the literal scan misses ──


class _OptionsFooterFailsCapturingSlack2(MockSlackClient):
    """Footer post_blocks fails; the plain-text OPTIONS fallback is captured so
    the test can assert it was display-safe redacted before sending."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True
        self.fallback_text = None

    async def post_blocks(
        self, channel, blocks, text, thread_ts=None, unfurl_links=None, unfurl_media=None
    ):
        raise RuntimeError("footer failed")

    async def post_message(
        self, channel, text, thread_ts=None, unfurl_links=None, unfurl_media=None
    ):
        # The display-safe scrub strips the ``*Options:*`` emphasis to
        # ``Options:`` when it downgrades a message carrying a canonical-form
        # credential (formatting is worth less than a leaked key), so match the
        # bare label rather than the emphasised prefix.
        if text.lstrip("*").startswith("Options:"):
            self.fallback_text = text
        return await super().post_message(channel, text, thread_ts)


def test_native_options_fallback_redacts_markup_obfuscated_credential(monkeypatch):
    slack = _OptionsFooterFailsCapturingSlack2()
    # An emphasis-split key: the literal bytes match no credential pattern, but
    # Slack renders the ``**`` away and shows the reader an intact key. Only a
    # display-safe scan against the rendered form catches it. A literal-only
    # exfil+credential scrub (the bypassed path) lets this through.
    obfuscated = "AKIAIOSF**ODNN7EXAMPLE**"
    rendered = "AKIAIOSFODNN7EXAMPLE"
    provider = FakeProvider(
        [LLMEvent(kind="text_chunk", text=f"pick [OPTIONS: use {obfuscated} | skip]")]
    )
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert slack.fallback_text is not None, "fallback was not posted"
    # Assert against what Slack RENDERS, not the raw bytes: canonicalizing the
    # fallback collapses the emphasis exactly as Slack does, so a key hidden by
    # ``**`` in the bytes but intact on screen is caught here. A literal-only
    # exfil+credential scrub (the bypassed path) leaves the key in the rendered
    # form; the display-safe scrub redacts it before it is ever sent.
    from kiro_crew.messaging.display_safety import canonicalize_display

    rendered_fallback = canonicalize_display(slack.fallback_text)
    assert (
        rendered not in rendered_fallback
    ), "markup-obfuscated credential rendered raw into the OPTIONS fallback"


# ── F1: transport/renderer streaming path — every append refused while the
#    stream stays live. The renderer's ``on_done`` seal is best-effort, so
#    without the guard the turn falls through to ``_finalized = True`` and books
#    success for an answer the reader never saw (renderer.py:1203). The guard
#    routes the undelivered answer through the propagating fallback instead. ──


class _TransportAllAppendsRefusedSlack(RecordingSlackClient):
    """Streaming stays enabled (``start_stream`` keeps succeeding so a rotation
    never flips ``_use_slack_stream`` False) but every ``append_stream`` is
    refused, and the fallback ``update_message`` ALSO fails — so the answer never
    reaches the reader on any sub-path. The turn is a failure."""

    def __init__(self) -> None:
        super().__init__()
        self.stream_disabled = False  # start_stream succeeds → stream stays live

    async def append_stream(self, channel, ts, text) -> bool:
        self._rec("append_stream", channel=channel, ts=ts, text=text)
        return False  # refused, and refused again after any rotation retry

    async def update_message(self, channel, ts, text="", blocks=None) -> None:
        # The renderer's F1 guard routes the undelivered answer here; this send
        # is the answer-carrying delivery on the recovery path, so its failure
        # must propagate and leave the turn a failure.
        self._rec("update_message", channel=channel, ts=ts, text=text)
        raise RuntimeError("slack chat.update failed — answer never delivered")


def test_transport_stream_all_appends_refused_books_failure(monkeypatch):
    slack = _TransportAllAppendsRefusedSlack()
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="the answer is 42"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _TrackSessions(provider)

    _run(monkeypatch, slack, sessions)

    # No append landed and the fallback update failed, so the reader saw no
    # answer. Booking a success here is exactly the before-delivery accounting
    # the F1 guard removes.
    assert sessions.calls["success"] == 0, "booked success for a stream that delivered no answer"
    assert sessions.calls["failure"] == 1


class _TransportAppendsRefusedFallbackRecoversSlack(RecordingSlackClient):
    """Every ``append_stream`` is refused, but the fallback ``update_message``
    SUCCEEDS — the final chat.update delivers the answer. A transient
    all-appends failure the fallback recovers must stay a success; the guard
    must not turn a recovered delivery into a failure."""

    def __init__(self) -> None:
        super().__init__()
        self.stream_disabled = False

    async def append_stream(self, channel, ts, text) -> bool:
        self._rec("append_stream", channel=channel, ts=ts, text=text)
        return False

    # update_message inherits the recording success default → fallback delivers.


def test_transport_stream_all_appends_refused_but_fallback_delivers_stays_success(monkeypatch):
    slack = _TransportAppendsRefusedFallbackRecoversSlack()
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="the answer is 42"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _TrackSessions(provider)

    _run(monkeypatch, slack, sessions)

    # The fallback chat.update delivered the answer, so the turn is a success.
    assert sessions.calls == {"success": 1, "failure": 0}
    # The guard's fallback ran: an update_message carrying the answer was sent.
    assert any(m == "update_message" for m, _ in slack.transcript), "fallback update never ran"


# ── F1 (native, handler.py): a tool-only / reasoning-only streaming turn opens
#    the stream but delivers no answer text, so ``_stream_delivered`` stays False.
#    It REACHED its (empty) answer and must book a success — not a failure from
#    the wholly-refused-stream predicate, which only applies when there WAS answer
#    text to send. A false failure here increments the consecutive-failure breaker
#    toward a session-resetting trip on an ordinary turn. ──


class _StreamEnabledNoAnswerSlack(MockSlackClient):
    """Streaming enabled; start/stop succeed. The turn opens the stream but its
    only text is reasoning that strips to empty, so no real answer text is ever
    confirmed on the stream (``_stream_delivered`` stays False)."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True


def test_native_stream_reasoning_only_turn_books_success_not_failure(monkeypatch):
    slack = _StreamEnabledNoAnswerSlack()
    # A chunk that opens the stream but reduces to empty ``clean_text`` after the
    # thinking strip: a reasoning-only / tool-only turn with no answer to deliver.
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="<thinking>working</thinking>")])
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    # No answer text to deliver → the (empty) answer reached → success. Booking a
    # failure here would drive the consecutive-failure breaker toward a session
    # reset on an ordinary reasoning-only turn.
    assert sessions.calls == {"success": 1, "failure": 0}


# ── F2 (native, handler.py): a CancelledError raised inside the best-effort
#    ``stop_stream`` await — after the answer reached the reader but before the
#    verdict step — is not caught by ``except Exception`` and propagates past the
#    verdict block. Without the finally booking the decided success, the turn is
#    a verdict hole: delivery succeeded yet no success is recorded, leaving the
#    consecutive-failure counter's reset stale. ──


class _StopStreamCancelsSlack(MockSlackClient):
    """Streaming enabled; a real-text append lands (answer reached), then
    ``stop_stream`` raises CancelledError — a cancellation in the finalize seal
    window, after delivery and before the verdict step."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True

    async def stop_stream(self, channel, ts, final_text=None):
        raise asyncio.CancelledError()


def test_native_cancel_in_stop_stream_after_delivery_books_success(monkeypatch):
    slack = _StopStreamCancelsSlack()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the answer is 42")])
    sessions = _TrackNativeSessions(provider)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    # The answer reached the reader (a real-text append landed) before the
    # cancellation in the best-effort seal. The finally books the decided success
    # so the delivered turn is not a verdict hole.
    assert sessions.calls == {"success": 1, "failure": 0}


# ── F2 (OPTIONS variant): a cancellation in the stop_stream await on a turn
#    whose reply carries an [OPTIONS] control must NOT book success — the choices
#    ride only in the footer, which the cancellation pre-empts, so the reader
#    never got them. The finally's success booking is gated on ``_options_present``
#    (bound before the await) precisely so this window is not falsely booked. ──


class _StopStreamCancelsOptionsSlack(MockSlackClient):
    """Streaming enabled; the answer body is delivered, then ``stop_stream``
    raises CancelledError — but the turn carries an [OPTIONS] control, so its
    choices are owned by the footer that never runs."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True

    async def stop_stream(self, channel, ts, final_text=None):
        raise asyncio.CancelledError()


def test_native_cancel_in_stop_stream_options_turn_books_no_success(monkeypatch):
    slack = _StopStreamCancelsOptionsSlack()
    provider = FakeProvider(
        [LLMEvent(kind="text_chunk", text="here are choices [OPTIONS: alpha | beta]")]
    )
    sessions = _TrackNativeSessions(provider)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    # The OPTIONS choices ride only in the footer, which the cancellation in the
    # stop_stream seal pre-empted, so the reader never got the actionable part of
    # the turn. Booking success here (via the F2 finally) would be exactly the
    # undelivered-OPTIONS hole the _options_present gate closes.
    assert (
        sessions.calls["success"] == 0
    ), "booked success for an OPTIONS turn whose choices never delivered"


# ── Context-probe double-book: ``check_context_usage`` runs AFTER the model
#    completes (``_turn_completed_ok`` already True) but inside the model ``try``.
#    A raise there must NOT enter the turn-failure ``except`` chain — that books a
#    raw failure while ``_turn_completed_ok`` stays True, so the delivery-verdict
#    block books a SECOND time. The probe is advisory, so its failure is swallowed
#    and the completed turn books exactly one verdict. ──


class _ContextProbeRaisesSessions(_TrackNativeSessions):
    """A session manager whose post-completion context-usage probe raises a
    transient error, as an overloaded provider probe would."""

    def check_context_usage(self, key, provider):
        raise RuntimeError("transient context probe failure")


def test_native_context_probe_failure_books_exactly_one_verdict(monkeypatch):
    slack = MockSlackClient()  # stream disabled; update_message delivers the answer
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the answer is 42")])
    sessions = _ContextProbeRaisesSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    # The model completed and the answer was delivered, so the turn is one
    # success. The advisory probe's failure must not add a second (failure)
    # booking on top — that is the double-book the isolation fixes.
    assert sessions.calls == {"success": 1, "failure": 0}


# ── F1 structural (handler.py:4333): a CancelledError at the first
#    ``await asyncio.sleep(0)`` (after the model stream already delivered a
#    real-text append) reaches the release finally with ``_answer_reached`` still
#    at its default False — it is recomputed only later. Without reading
#    ``_stream_delivered`` (True from the first confirmed append, before that
#    yield) the finally would book NO verdict for a fully-streamed turn. The
#    ``_answer_reached or _stream_delivered`` guard closes that window. ──


class _StreamDeliversSlack(MockSlackClient):
    """Streaming enabled; a real-text append lands so _stream_delivered is True
    before the post-loop sleep(0) yield."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True


def test_native_cancel_at_sleep0_after_stream_delivered_books_success(monkeypatch):
    slack = _StreamDeliversSlack()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the answer is 42")])
    sessions = _TrackNativeSessions(provider)

    # Cancel exactly at the first asyncio.sleep(0) the handler awaits AFTER the
    # model stream loop (the release-region yield at handler.py:4367). By then a
    # real-text append has confirmed (_stream_delivered True) though _answer_reached
    # still defaults False.
    import kiro_crew.slack.handler as _h

    real_sleep = asyncio.sleep
    state = {"fired": False}

    async def _sleep(secs, *a, **k):
        if secs == 0 and not state["fired"] and getattr(slack, "_saw_append", False):
            state["fired"] = True
            raise asyncio.CancelledError()
        return await real_sleep(secs, *a, **k)

    # Mark once a real-text append has landed so we only cancel the POST-stream yield.
    _orig_append = slack.append_stream

    async def _append(channel, ts, text):
        if text and text.strip():
            slack._saw_append = True
        return await _orig_append(channel, ts, text)

    slack.append_stream = _append  # type: ignore[method-assign]
    monkeypatch.setattr(_h.asyncio, "sleep", _sleep)

    try:
        asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))
    except asyncio.CancelledError:
        pass

    # The stream delivered the answer before the cancellation, so the turn is a
    # success booked from _stream_delivered in the finally — not a verdict hole.
    assert state["fired"], "test did not hit the post-stream sleep(0) cancellation"
    assert sessions.calls["success"] == 1 and sessions.calls["failure"] == 0


# ── F2 companion (transport_dispatch): a CancelledError on the OPTIONS footer
#    delivery leaves the renderer un-finalized (the footer is answer-carrying) and
#    propagates past ``except Exception`` (it is a BaseException). The transport
#    dispatcher books a failure in that window — matching the native handler's
#    teardown — so an OPTIONS turn whose choices never reached the reader is not an
#    unrecorded verdict hole. ──


class _OptionsFooterCancelsSlackClient(RecordingSlackClient):
    """Streaming enabled; the answer body streams, but ``post_blocks`` (the
    OPTIONS footer) raises CancelledError — a mid-turn cancellation on the
    answer-carrying footer await."""

    async def post_blocks(
        self, channel, blocks, text, thread_ts=None, unfurl_links=None, unfurl_media=None
    ):
        raise asyncio.CancelledError()


def test_transport_options_footer_cancellation_books_failure(monkeypatch):
    slack = _OptionsFooterCancelsSlackClient()
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="pick one [OPTIONS: alpha | beta]"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _TrackSessions(provider)

    # The cancellation must propagate out of the dispatcher.
    with pytest.raises(asyncio.CancelledError):
        _run(monkeypatch, slack, sessions)

    # The OPTIONS choices never reached the reader (footer cancelled), and the
    # turn did not finalize, so the dispatcher books a failure — not a silent
    # verdict hole and not a success.
    assert (
        sessions.calls["success"] == 0
    ), "booked success for an OPTIONS turn whose choices never delivered"
    assert sessions.calls["failure"] == 1
