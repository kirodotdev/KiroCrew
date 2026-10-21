"""Telegram dispatcher: how a turn ends, and what it publishes and reports.

Covers the turn's exit paths (what is charged, finalized, released, cleaned and drained),
the user-safe failure reason, the session identity a turn publishes for managed MCP
tools, and the context-threshold notices.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import FakeProvider, _dispatcher, _dm, _Ev

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_STALE_RECOVER
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.telegram.renderer import TelegramApprovalDecider, TelegramRenderer
from kiro_crew.telegram.transport_dispatch import _user_safe_failure_reason
from kiro_crew.testing.wait import await_until


class TestTelegramSessionPidPublish:
    """#232: a Telegram turn must publish its session identity so managed MCP
    tools (learn_add, cron management, ...) can resolve ``X-Session-Key`` from
    a Telegram-originated turn. Publication is centralized in
    ``messaging.identity.publish_turn_identity``; this asserts Telegram dispatch
    delegates to that shared writer (DM + forum). Regression guard for the
    ``missing X-Session-Key`` 400. The publish semantics themselves (pid guard,
    executor offload) are covered in test_messaging_identity.py.
    """

    def test_telegram_turn_delegates_identity_publish(self) -> None:
        d, _cli, sess = _dispatcher({7})
        sess._pid = 4242  # SessionManager.get_pid -> kiro-cli host PID

        async def _go() -> None:
            with patch("kiro_crew.telegram.transport_dispatch.publish_turn_identity") as pub:
                await d.handle_message(
                    InboundMessage(
                        channel_type="telegram",
                        user_id="7",
                        conversation_id="7",
                        text="hi",
                    )
                )
                pub.assert_awaited_once_with(sess, "telegram:kirocrew:direct:7")

        asyncio.run(_go())


class TestUserSafeFailureReason:
    def test_user_worded_acp_error_yields_bounded_single_line(self) -> None:
        exc = AcpError("line one\nline two\ttail", transient=False, user_worded=True)
        assert _user_safe_failure_reason(exc) == "⚠️ line one line two tail"

    def test_local_paths_are_redacted(self) -> None:
        exc = AcpError(
            "failed reading /home/alice/.kiro/crew/creds", transient=False, user_worded=True
        )
        out = _user_safe_failure_reason(exc)
        assert out is not None
        assert "/home/alice" not in out

    def test_length_hard_cap(self) -> None:
        out = _user_safe_failure_reason(AcpError("x" * 5000, transient=False, user_worded=True))
        assert out is not None
        # "⚠️ " prefix + capped body (ellipsis included in the cap).
        assert len(out) <= 500 + len("⚠️ ")
        assert out.endswith("…")

    def test_non_retryable_provider_fallback_returns_none(self) -> None:
        # The formatter's fallback passes the provider's own text through and
        # still marks it non-retryable; only Kiro Crew's own wording may reach chat.
        exc = AcpError("provider said: {'data': 'opaque'}", transient=False)
        assert _user_safe_failure_reason(exc) is None

    def test_transient_returns_none(self) -> None:
        exc = AcpError("5xx", transient=True, user_worded=True)
        assert _user_safe_failure_reason(exc) is None

    def test_unclassified_returns_none(self) -> None:
        assert _user_safe_failure_reason(AcpError("unknown", transient=None)) is None

    def test_non_acp_returns_none(self) -> None:
        assert _user_safe_failure_reason(ValueError("boom")) is None

    def test_empty_message_returns_none(self) -> None:
        exc = AcpError("   \n ", transient=False, user_worded=True)
        assert _user_safe_failure_reason(exc) is None


class TestContextThresholdNotices:
    def test_soft_threshold_nudges(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess.check_context_usage = lambda key, provider: 85.0  # >= soft (80)

        asyncio.run(d._maybe_notice(7, ("direct", "7"), "key", object()))

        assert any("/compact" in s[0] for s in cli.sent)

    def test_soft_nudge_suppressed_on_auto_managed_backend(self) -> None:
        # The nudge advises /compact, which this backend refuses — it compacts
        # on its own, so there is nothing for the user to act on.
        d, cli, sess = _dispatcher({7})
        sess.check_context_usage = lambda key, provider: 85.0
        provider = SimpleNamespace(manual_compact_unsupported_backend="kas")

        asyncio.run(d._maybe_notice(7, ("direct", "7"), "key", provider))

        assert cli.sent == []

    def test_below_soft_threshold_stays_silent(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess.check_context_usage = lambda key, provider: 10.0

        asyncio.run(d._maybe_notice(7, ("direct", "7"), "key", object()))

        assert cli.sent == []


class TestTurnLifecycleCharacterization:
    """The turn's exit paths: what is charged, finalized, released, cleaned and drained."""

    _KEY = "telegram:kirocrew:direct:7"

    def test_a_failure_reason_is_bounded_and_scrubbed(self) -> None:
        from kiro_crew.memory_stores import UnknownMemoryStore

        assert (
            _user_safe_failure_reason(AcpError("x" * 500, transient=False, user_worded=True))
            == "⚠️ " + "x" * 500
        )
        assert _user_safe_failure_reason(
            AcpError("x" * 501, transient=False, user_worded=True)
        ) == ("⚠️ " + "x" * 499 + "…")
        assert _user_safe_failure_reason(
            AcpError("a" * 498 + " " + "b" * 10, transient=False, user_worded=True)
        ) == ("⚠️ " + "a" * 498 + "…")
        scrubbed = _user_safe_failure_reason(
            AcpError("failed reading /srv/op/.kiro/crew/creds", transient=False, user_worded=True)
        )
        assert scrubbed is not None and "/srv/op" not in scrubbed
        assert scrubbed.startswith("⚠️ failed reading ")
        secret = _user_safe_failure_reason(
            AcpError("token AKIAIOSFODNN7EXAMPLE rejected", transient=False, user_worded=True)
        )
        assert secret is not None and "AKIAIOSFODNN7EXAMPLE" not in secret
        assert _user_safe_failure_reason(
            UnknownMemoryStore("private memory 'm' is unavailable")
        ) == ("⚠️ private memory 'm' is unavailable")

        class _Unprintable(AcpError):
            def __str__(self) -> str:
                raise RuntimeError("boom")

        assert (
            _user_safe_failure_reason(_Unprintable("x", transient=False, user_worded=True)) is None
        )

    def _instrument(self, monkeypatch: Any, d: Any, sess: Any, stream: Any = None) -> list:
        from kiro_crew.messaging import auto_title
        from kiro_crew.telegram import transport_dispatch as td

        events: list[tuple[Any, ...]] = []
        monkeypatch.setattr(auto_title, "try_claim", lambda _key: False)
        real_discard = TelegramApprovalDecider.discard_session
        monkeypatch.setattr(
            TelegramApprovalDecider,
            "discard_session",
            classmethod(lambda cls, key: (events.append(("discard", key)), real_discard(key))[1]),
        )
        sess.consume_needs_reinjection = lambda key: True
        sess.mark_needs_reinjection = lambda key: events.append(("rearm", key))
        real_close = TelegramRenderer.close

        async def _close(self: Any, failure_reason: Any = None) -> None:
            events.append(("close", failure_reason, self._session_key in d._active_renderers))
            await real_close(self, failure_reason=failure_reason)

        monkeypatch.setattr(TelegramRenderer, "close", _close)
        real_release = sess.release

        def _release(key: str) -> None:
            events.append(("release", key, key in d._active_renderers))
            real_release(key)

        sess.release = _release

        async def _record_failure(key: str) -> None:
            events.append(("charge", key))

        sess.record_failure = _record_failure
        monkeypatch.setattr(
            td,
            "cleanup_attachments",
            lambda paths: events.append(
                ("cleanup", list(paths), threading.current_thread() is not threading.main_thread())
            ),
        )

        async def _drain(key: str) -> None:
            events.append(("drain", key))

        d._drain_queue = _drain
        if stream is not None:
            provider = FakeProvider()
            provider.stream = stream  # type: ignore[method-assign]

            async def _acquire(key: str, **kw: Any) -> Any:
                return provider, True, False

            sess.get_or_create = _acquire
        return events

    def test_the_finally_order_on_a_provider_failure(self, monkeypatch: Any) -> None:
        d, _cli, sess = _dispatcher({7})

        async def _boom(message: str) -> Any:
            raise RuntimeError("stream died")
            yield  # pragma: no cover

        events = self._instrument(monkeypatch, d, sess, stream=_boom)
        asyncio.run(d.handle_message(_dm("hi")))
        k = self._KEY
        assert events == [
            ("charge", k),
            ("discard", k),
            ("rearm", k),
            ("close", None, True),
            ("release", k, False),
            ("cleanup", [], True),
            ("drain", k),
        ]

    def test_the_finally_order_on_success(self, monkeypatch: Any) -> None:
        d, _cli, sess = _dispatcher({7})
        events = self._instrument(monkeypatch, d, sess)
        asyncio.run(d.handle_message(_dm("hi")))
        k = self._KEY
        assert events == [
            ("discard", k),
            ("close", None, True),
            ("release", k, False),
            ("cleanup", [], True),
            ("drain", k),
        ]

    def test_the_finally_order_on_a_cold_start_failure(self, monkeypatch: Any) -> None:
        d, _cli, sess = _dispatcher({7}, raise_on_get=True)
        events = self._instrument(monkeypatch, d, sess)
        asyncio.run(d.handle_message(_dm("hi")))
        k = self._KEY
        assert events == [
            ("discard", k),
            ("close", None, True),
            ("cleanup", [], True),
            ("drain", k),
        ]

    def test_drain_false_never_drains(self, monkeypatch: Any) -> None:
        d, _cli, sess = _dispatcher({7})
        events = self._instrument(monkeypatch, d, sess)
        asyncio.run(d.handle_message(_dm("hi"), drain=False))
        assert not [e for e in events if e[0] == "drain"]
        assert sess.successes == [self._KEY]

    def test_an_empty_turn_returns_inside_the_try_and_skips_the_drain(
        self, monkeypatch: Any
    ) -> None:
        d, _cli, sess = _dispatcher({7})
        events = self._instrument(monkeypatch, d, sess)
        asyncio.run(
            d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="")
            )
        )
        k = self._KEY
        assert sess.successes == [] and d.ctx_builder.build_calls == []
        assert events == [
            ("discard", k),
            ("close", None, True),
            ("release", k, False),
            ("cleanup", [], True),
        ]

    def test_a_cancelled_turn_finalizes_and_releases_but_does_not_drain(
        self, monkeypatch: Any
    ) -> None:
        d, cli, sess = _dispatcher({7})
        started = asyncio.Event()

        async def _hang(message: str) -> Any:
            started.set()
            # flake-ok: pre-existing never-set event in a hang stub the test cancels, moved verbatim; tracked for a de-flake pass
            await asyncio.Event().wait()
            yield  # pragma: no cover

        events = self._instrument(monkeypatch, d, sess, stream=_hang)
        k = self._KEY

        async def _go() -> None:
            task = asyncio.create_task(d.handle_message(_dm("hi")))
            await await_until(started, timeout=5, what="the stream starting")
            assert isinstance(d._active_renderers.get(k), TelegramRenderer)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                # flake-ok: pre-existing await of the cancelled turn task, moved verbatim; tracked for a de-flake pass
                await task

        asyncio.run(_go())
        assert events == [
            ("discard", k),
            ("rearm", k),
            ("close", None, True),
            ("release", k, False),
            ("cleanup", [], True),
        ]
        assert k not in d._active_renderers
        assert sess.failures == [] and sess.successes == []

    def test_close_gets_the_failure_reason_the_turn_classified(self, monkeypatch: Any) -> None:
        from kiro_crew.memory_stores import UnknownMemoryStore

        cases = [
            (
                AcpError("model not in your plan", transient=False, user_worded=True),
                "⚠️ model not in your plan",
            ),
            # Non-retryable, but the provider's own fallback text: generic reply.
            (AcpError("provider said: {'data': 'opaque'}", transient=False), None),
            (AcpError("5xx", transient=True), None),
            (RuntimeError("boom"), None),
            (
                UnknownMemoryStore("private memory 'm' is unavailable"),
                "⚠️ private memory 'm' is unavailable",
            ),
        ]
        for exc, reason in cases:
            d, cli, sess = _dispatcher({7})

            async def _raise(message: str, exc: BaseException = exc) -> Any:
                raise exc
                yield  # pragma: no cover

            events = self._instrument(monkeypatch, d, sess, stream=_raise)
            asyncio.run(d.handle_message(_dm("hi")))
            assert [e[1] for e in events if e[0] == "close"] == [reason], exc
            assert ("charge", self._KEY) in events, exc

    def test_a_landed_turn_clears_the_shared_death_streak_after_record_success(
        self, monkeypatch: Any
    ) -> None:
        from kiro_crew import runtime_death

        runtime_death._reset_for_tests()
        try:
            d, _cli, sess = _dispatcher({7})
            runtime_death.note_shared_death(self._KEY)
            runtime_death.note_shared_death(self._KEY)
            at_success: list[int] = []
            real_success = sess.record_success

            def _success(key: str) -> None:
                at_success.append(runtime_death.shared_deaths(key))
                real_success(key)

            sess.record_success = _success  # type: ignore[method-assign]
            counted: list[int] = []

            class _Stats:
                def inc_message_received(self) -> None:
                    pass

                def inc_message_success(self) -> None:
                    counted.append(
                        runtime_death.shared_deaths(TestTurnLifecycleCharacterization._KEY)
                    )

            monkeypatch.setattr("kiro_crew.telegram.transport_dispatch.Stats", _Stats)
            asyncio.run(d.handle_message(_dm("hi")))
            assert at_success == [2] and counted == [0]
            assert runtime_death.shared_deaths(self._KEY) == 0
        finally:
            runtime_death._reset_for_tests()

    def _run_turn_ending(self, monkeypatch: Any, stop_reason: str) -> tuple[list, Any, list]:
        d, _cli, sess = _dispatcher({7})

        async def _stream(message: str) -> Any:
            yield _Ev(EVENT_TEXT_CHUNK, text="half an answer")
            yield _Ev(EVENT_COMPLETE, stop_reason=stop_reason)

        events = self._instrument(monkeypatch, d, sess, stream=_stream)
        resets: list[str] = []

        async def _reset(key: str) -> None:
            resets.append(key)

        sess.reset = _reset
        asyncio.run(d.handle_message(_dm("hi")))
        return events, sess, resets

    @pytest.mark.parametrize("stop_reason", ["timeout", STOP_REASON_STALE_RECOVER])
    def test_an_unfinished_turn_is_a_failure_and_resets_the_session(
        self, monkeypatch: Any, stop_reason: str
    ) -> None:
        events, sess, resets = self._run_turn_ending(monkeypatch, stop_reason)
        assert sess.successes == []
        assert ("charge", self._KEY) in events
        assert resets == [self._KEY]

    @pytest.mark.parametrize("stop_reason", ["timeout", STOP_REASON_STALE_RECOVER])
    def test_at_the_breaker_threshold_an_unfinished_turn_resets_once(
        self, monkeypatch: Any, stop_reason: str
    ) -> None:
        """The failure that trips the breaker resets the session inside
        ``record_failure`` and answers True. A successor a same-key message started
        during that reset is the live runtime afterwards, so the turn must not reset
        it again."""
        d, _cli, sess = _dispatcher({7})

        async def _stream(message: str) -> Any:
            yield _Ev(EVENT_TEXT_CHUNK, text="half an answer")
            yield _Ev(EVENT_COMPLETE, stop_reason=stop_reason)

        events = self._instrument(monkeypatch, d, sess, stream=_stream)
        runtimes = ["first"]
        killed: list[str] = []

        async def _reset(key: str) -> None:
            killed.append(runtimes[-1])
            runtimes.append(f"successor {len(runtimes)}")

        async def _breaker_at_threshold(key: str) -> bool:
            events.append(("charge", key))
            await sess.reset(key)
            return True

        sess.reset = _reset
        sess.record_failure = _breaker_at_threshold
        asyncio.run(d.handle_message(_dm("hi")))
        assert ("charge", self._KEY) in events and sess.successes == []
        assert killed == ["first"], f"the dispatcher reset again after the breaker did: {killed}"

    def test_a_finished_turn_is_a_success_and_keeps_the_session(self, monkeypatch: Any) -> None:
        events, sess, resets = self._run_turn_ending(monkeypatch, "end_turn")
        assert sess.successes == [self._KEY]
        assert ("charge", self._KEY) not in events
        assert resets == []
