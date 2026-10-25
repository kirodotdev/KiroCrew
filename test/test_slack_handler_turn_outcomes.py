"""Slack handler tests: how a turn that ends abnormally is booked and shown.

Compaction failure and retry, unfinished and cancelled turns, and presentation-side
failures that must not fail a live turn.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from slack_handler_helpers import _clean_approval_state  # noqa: F401
from slack_handler_helpers import FakeProvider, FakeSessionManager, _visible_texts, _within_lost_run

from conftest import MockSlackClient
from kiro_crew.acp.client import AcpError
from kiro_crew.hooks import HOOK_REPLY
from kiro_crew.messaging.display_safety import canonicalize_display
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack.handler import (
    _THINKING,
    ACTIVATION_REVIEW,
    handle_message,
    set_allowed_users,
    set_owner_id,
)


class TestStopReasonCompactionFailed:
    """A COMPACTION_FAILED terminal is synthetic — the backend abandoned the
    turn after a failed auto-compaction and never sent end_turn, so it still
    counts the prompt as in progress. The handler must reset the session or
    the NEXT Slack message collides with "prompt already in progress"."""

    @pytest.mark.asyncio
    async def test_compaction_failed_resets_the_abandoned_session(self):
        from kiro_crew.acp.types import STOP_REASON_COMPACTION_FAILED

        slack = MockSlackClient()
        provider = FakeProvider(
            [
                LLMEvent(kind="text_chunk", text="partial"),
                LLMEvent(kind="complete", stop_reason=STOP_REASON_COMPACTION_FAILED),
            ]
        )
        sessions = FakeSessionManager(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        assert any(
            r.startswith("reset:") for r in sessions.removed
        ), f"no session reset after COMPACTION_FAILED: {sessions.removed}"


class _BookingSessions(FakeSessionManager):
    """Records the success and failure bookings the handler makes."""

    def __init__(self, provider):
        super().__init__(provider)
        self.booked: list[str] = []

    def record_success(self, key):
        self.booked.append(f"success:{key}")

    async def record_failure(self, key):
        self.booked.append(f"failure:{key}")
        return False


def _thread_text(slack) -> str:
    """The last text the thread shows for the reply message."""
    texts = [a[1].get("text", "") for a in slack.actions if a[0] in ("post", "update")]
    return texts[-1] if texts else ""


class TestStopReasonUnfinishedTurn:
    """The session handle ends a turn the backend never finished with
    ``stop_reason="timeout"`` (its turn ceiling) or ``STOP_REASON_STALE_RECOVER``.
    The runtime still reads alive, so the turn must not be posted and booked as
    a finished reply: the thread is told it timed out, the turn is a failure,
    and the session is reset so the next message starts a fresh runtime."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop_reason", ["timeout", "stale_recover"])
    async def test_an_unfinished_turn_is_failed_told_and_reset(self, stop_reason):
        slack = MockSlackClient()
        provider = FakeProvider(
            [
                LLMEvent(kind="text_chunk", text="Here is the first half of the pl"),
                LLMEvent(kind="complete", stop_reason=stop_reason),
            ]
        )
        sessions = _BookingSessions(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        final = _thread_text(slack)
        assert "Here is the first half of the pl" in final, final
        assert "Request timed out" in final, final
        assert any(r.startswith("reset:") for r in sessions.removed), sessions.removed
        assert any(b.startswith("failure:") for b in sessions.booked), sessions.booked
        assert not any(b.startswith("success:") for b in sessions.booked), sessions.booked

    @pytest.mark.asyncio
    async def test_an_unfinished_turn_with_no_text_says_it_timed_out(self):
        slack = MockSlackClient()
        provider = FakeProvider([LLMEvent(kind="complete", stop_reason="timeout")])
        sessions = _BookingSessions(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        assert "Request timed out" in _thread_text(slack)
        assert any(r.startswith("reset:") for r in sessions.removed), sessions.removed

    @pytest.mark.asyncio
    async def test_a_finished_turn_posts_its_reply_and_keeps_the_session(self):
        slack = MockSlackClient()
        provider = FakeProvider(
            [
                LLMEvent(kind="text_chunk", text="done"),
                LLMEvent(kind="complete", stop_reason="end_turn"),
            ]
        )
        sessions = _BookingSessions(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        final = _thread_text(slack)
        assert "done" in final and "timed out" not in final, final
        assert not any(r.startswith("reset:") for r in sessions.removed), sessions.removed
        assert not any(b.startswith("failure:") for b in sessions.booked), sessions.booked

    @pytest.mark.asyncio
    async def test_a_users_stop_keeps_the_session(self):
        from kiro_crew.acp.types import STOP_REASON_CANCELLED

        slack = MockSlackClient()
        provider = FakeProvider(
            [
                LLMEvent(kind="text_chunk", text="partial"),
                LLMEvent(kind="complete", stop_reason=STOP_REASON_CANCELLED),
            ]
        )
        sessions = _BookingSessions(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        assert not any(r.startswith("reset:") for r in sessions.removed), sessions.removed
        assert "timed out" not in _thread_text(slack)

    # The per-process client ends a turn at its prompt deadline by RAISING
    # AcpTimeoutError instead of yielding a terminal, and leaves its process
    # running. The thread must read the same as for the stop-reason ending.

    @pytest.mark.asyncio
    async def test_a_raised_timeout_keeps_the_streamed_text_and_adds_the_notice(self):
        from kiro_crew.acp.client import AcpTimeoutError

        slack = MockSlackClient()
        provider = _RaisingProvider(
            [LLMEvent(kind="text_chunk", text="Here is the first half of the pl")],
            AcpTimeoutError(),
        )
        sessions = _BookingSessions(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        final = _thread_text(slack)
        assert "Here is the first half of the pl" in final, final
        assert "Request timed out" in final, final
        assert final.index("Here is the first half") < final.index("Request timed out")

    @pytest.mark.asyncio
    async def test_a_raised_timeout_with_partial_output_adds_the_notice(self):
        from kiro_crew.acp.client import AcpTimeoutError

        slack = MockSlackClient()
        provider = _RaisingProvider([], AcpTimeoutError(partial_output="half an answer"))
        sessions = _BookingSessions(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        final = _thread_text(slack)
        assert "half an answer" in final and "Request timed out" in final, final

    @pytest.mark.asyncio
    async def test_a_raised_timeout_resets_the_session_it_held(self):
        from kiro_crew.acp.client import AcpTimeoutError

        slack = MockSlackClient()
        provider = _RaisingProvider([], AcpTimeoutError())
        sessions = _BookingSessions(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        assert "Request timed out" in _thread_text(slack)
        assert any(r.startswith("reset:") for r in sessions.removed), sessions.removed
        assert any(b.startswith("failure:") for b in sessions.booked), sessions.booked
        assert not any(b.startswith("success:") for b in sessions.booked), sessions.booked

    # At its threshold the breaker resets the session inside ``record_failure`` and
    # answers True (``SessionAllocation.record_failure``). A same-key message can claim
    # a successor runtime while that reset is awaited, so the turn must not reset the
    # session a second time: that would end the successor mid-turn.

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop_reason", ["timeout", "stale_recover"])
    async def test_at_the_breaker_threshold_an_unfinished_turn_resets_once(self, stop_reason):
        slack = MockSlackClient()
        provider = FakeProvider(
            [
                LLMEvent(kind="text_chunk", text="half an answer"),
                LLMEvent(kind="complete", stop_reason=stop_reason),
            ]
        )
        sessions = _BreakerAtThreshold(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        assert "Request timed out" in _thread_text(slack)
        assert sessions.booked == ["failure:" + sessions.keys_seen[0]], sessions.booked
        ended = sessions.ended
        assert ended == ["first"], f"the handler reset again after the breaker did: {ended}"

    @pytest.mark.asyncio
    async def test_at_the_breaker_threshold_a_raised_timeout_resets_once(self):
        from kiro_crew.acp.client import AcpTimeoutError

        slack = MockSlackClient()
        provider = _RaisingProvider(
            [LLMEvent(kind="text_chunk", text="half an answer")], AcpTimeoutError()
        )
        sessions = _BreakerAtThreshold(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        assert "Request timed out" in _thread_text(slack)
        assert sessions.booked == ["failure:" + sessions.keys_seen[0]], sessions.booked
        ended = sessions.ended
        assert ended == ["first"], f"the handler reset again after the breaker did: {ended}"


class _BreakerAtThreshold(_BookingSessions):
    """``record_failure`` as the breaker answers at its threshold: it books the failure,
    resets the session itself and returns True. Each reset ends the runtime that is live,
    and while it is awaited a same-key message claims a successor runtime."""

    def __init__(self, provider):
        super().__init__(provider)
        self.runtimes = ["first"]
        self.ended: list[str] = []

    async def record_failure(self, key):
        self.booked.append(f"failure:{key}")
        await self.reset(key)
        return True

    async def reset(self, key):
        await super().reset(key)
        self.ended.append(self.runtimes[-1])
        self.runtimes.append(f"successor {len(self.runtimes)}")


class _RaisingProvider(FakeProvider):
    """Streams *events*, then raises *error* instead of yielding a terminal."""

    def __init__(self, events, error):
        super().__init__(events)
        self._error = error

    async def stream(self, message, timeout=120.0):
        for event in self._events:
            yield event
        raise self._error


class _SequencedProvider(FakeProvider):
    """A provider whose successive ``stream`` calls play different scripts.

    The first script ends in the synthetic COMPACTION_FAILED completion; what
    the next one does is the test's choice. ``transient`` is the ACP layer's
    verdict on WHY compaction failed (``None`` models a provider that predates
    the attribute).
    """

    def __init__(self, scripts, *, transient):
        super().__init__()
        self._scripts = list(scripts)
        #: Turns of the USER's message only. The handler also streams a
        #: session-naming prompt on a new session (auto-title); that is not an
        #: attempt and plays no script.
        self.turns = 0
        self.turn_prompts: list[str] = []
        if transient is not None:
            self.last_compaction_transient = transient

    #: Prompts that are the user's turn: the bare text (no context builder) or a
    #: built prompt carrying it -- never the auto-title prompt, which quotes it.
    def _is_turn(self, message: str) -> bool:
        return "hello" in message and not message.startswith("You are a session naming agent")

    async def stream(self, message, timeout=120.0):
        if not self._is_turn(message):
            async for event in super().stream(message, timeout):
                yield event
            return
        self.turn_prompts.append(message)
        script = self._scripts[min(self.turns, len(self._scripts) - 1)]
        self.turns += 1
        for event in script:
            yield event


def _abandoned():
    from kiro_crew.acp.types import STOP_REASON_COMPACTION_FAILED

    return [LLMEvent(kind="complete", stop_reason=STOP_REASON_COMPACTION_FAILED)]


def _answered():
    return [LLMEvent(kind="text_chunk", text="The answer is 42"), LLMEvent(kind="complete")]


class TestTransientCompactionRetry:
    """A turn abandoned after a TRANSIENT compaction failure -- a throttled or
    5xx'd summarization call, nothing wrong with the message -- is replayed
    instead of dropped. Slack's replay is a nested ``handle_message`` call with
    every argument unchanged, running inside the original task: it resolves the
    same session, keeps the same activation and pinning, can still read the
    attachment files the text refers to, and needs no queue drain from whoever
    dispatched the original. A permanent verdict keeps the give-up behaviour."""

    @pytest.mark.asyncio
    async def test_a_transient_failure_replays_the_message(self):
        from kiro_crew.slack.handler import _COMPACTION_RETRY_NOTICE, _NO_RESPONSE

        slack = MockSlackClient()
        provider = _SequencedProvider([_abandoned(), _answered()], transient=True)
        sessions = FakeSessionManager(provider)
        log = MagicMock()

        await handle_message(
            slack, sessions, "C1", "hello", "thread1", "msg1", "U1", conversation_log=log
        )

        assert provider.turns == 2, "the same message is sent again"
        assert "reset:thread1" in sessions.removed
        assert sessions.keys_seen.count("thread1") == 2, "the replay acquires a fresh session"
        texts = _visible_texts(slack)
        assert any(_COMPACTION_RETRY_NOTICE in t for t in texts), texts
        assert any("The answer is 42" in t for t in texts), texts
        assert not any(_NO_RESPONSE in t for t in texts), texts
        # The abandoned attempt persists nothing: the log carries the user's
        # message exactly once, with the reply the replay produced.
        user_rows = [c for c in log.append.call_args_list if c.args[1] == "user"]
        assert len(user_rows) == 1, log.append.call_args_list
        assert not any(
            _COMPACTION_RETRY_NOTICE in str(c.args) for c in log.append.call_args_list
        ), "the retry notice is shown, never recorded"
        # ONE gap spans the whole replay, closed by the outer attempt once the
        # nested call has settled and released its permit.
        assert sessions.replay_gaps == [("open", "thread1"), ("close", "thread1")]

    @pytest.mark.asyncio
    async def test_a_compaction_the_abandoned_attempt_reported_is_not_armed_after_it(self):
        """The reset popped the session that compacted and the replay cold-starts a
        new one, so arming the flag would make the next turn inject it twice."""
        compacted_then_abandoned = [
            LLMEvent(kind="compaction_status", text="completed"),
            *_abandoned(),
        ]
        provider = _SequencedProvider([compacted_then_abandoned, _answered()], transient=True)
        sessions = FakeSessionManager(provider)
        marks: list[str] = []
        sessions.consume_needs_reinjection = lambda key: False
        sessions.mark_needs_reinjection = marks.append

        await handle_message(MockSlackClient(), sessions, "C1", "hello", "thread1", "msg1", "U1")

        assert provider.turns == 2, "the message was replayed"
        assert marks == []

    @pytest.mark.asyncio
    async def test_a_permanent_failure_keeps_the_give_up_behaviour(self):
        slack = MockSlackClient()
        provider = _SequencedProvider([_abandoned(), _answered()], transient=False)
        sessions = FakeSessionManager(provider)

        await handle_message(slack, sessions, "C1", "hello", "thread1", "msg1", "U1")

        assert provider.turns == 1, "a compaction that overflowed the window is not replayed"
        assert "reset:thread1" in sessions.removed
        assert sessions.keys_seen.count("thread1") == 1
        assert sessions.replay_gaps == [("open", "thread1"), ("close", "thread1")]

    @pytest.mark.asyncio
    async def test_a_provider_without_a_verdict_is_not_read_as_transient(self):
        slack = MockSlackClient()
        provider = _SequencedProvider([_abandoned(), _answered()], transient=None)
        sessions = FakeSessionManager(provider)

        await handle_message(slack, sessions, "C1", "hello", "thread1", "msg1", "U1")

        assert provider.turns == 1

    @pytest.mark.asyncio
    async def test_an_emitted_turn_is_not_replayed_even_when_transient(self):
        """Verbatim replay is only safe before anything landed in the thread."""
        slack = MockSlackClient()
        provider = _SequencedProvider(
            [[LLMEvent(kind="text_chunk", text="partial"), *_abandoned()], _answered()],
            transient=True,
        )
        sessions = FakeSessionManager(provider)

        await handle_message(slack, sessions, "C1", "hello", "thread1", "msg1", "U1")

        assert provider.turns == 1
        assert "reset:thread1" in sessions.removed

    @pytest.mark.asyncio
    async def test_the_budget_is_per_message_and_bounded(self):
        """A throttle that keeps firing gets exactly ``_COMPACTION_FAILED_RETRIES``
        replays; the attempt after the last one gives up and posts as the old
        code did."""
        import kiro_crew.slack.handler as handler_mod

        budget = handler_mod._COMPACTION_FAILED_RETRIES
        slack = MockSlackClient()
        provider = _SequencedProvider([_abandoned()], transient=True)  # never recovers
        sessions = FakeSessionManager(provider)

        await handle_message(slack, sessions, "C1", "hello", "thread1", "msg1", "U1")

        assert provider.turns == budget + 1
        assert sessions.removed.count("reset:thread1") == budget + 1
        assert sessions.replay_gaps[-1] == ("close", "thread1"), "the gap never outlives the turn"
        from kiro_crew.slack.handler import _NO_RESPONSE

        assert any(_NO_RESPONSE in t for t in _visible_texts(slack)), "the last attempt posts"

    @pytest.mark.asyncio
    async def test_a_cancellation_during_the_reset_still_closes_the_gap(self):
        """``!stop`` cancels the handler task; landing in the reset await, that
        skips every close inside the try. A gap left open would make every later
        claim on this key wait forever, so the outer ``finally`` closes it."""

        class _CancelInReset(FakeSessionManager):
            async def reset(self, key):
                await super().reset(key)
                raise asyncio.CancelledError()

        slack = MockSlackClient()
        provider = _SequencedProvider([_abandoned(), _answered()], transient=True)
        sessions = _CancelInReset(provider)

        with pytest.raises(asyncio.CancelledError):
            await handle_message(slack, sessions, "C1", "hello", "thread1", "msg1", "U1")

        assert sessions.replay_gaps[0] == ("open", "thread1")
        assert sessions.replay_gaps[-1] == ("close", "thread1")

    @pytest.mark.asyncio
    async def test_a_stop_during_the_reset_gap_keeps_the_message_dropped(self):
        """A ``!stop`` landing while the abandoned session is being reset finds
        no session to cancel. The manager records it inside the replay gap the
        handler opens before the reset; the replay re-reads the count right
        before it would open a prompt and ends without one."""

        class _StopInGap(FakeSessionManager):
            async def reset(self, key):
                await super().reset(key)
                assert ("open", key) in getattr(self, "replay_gaps", []), "gap opens before reset"
                self._stop_gen = getattr(self, "_stop_gen", 0) + 1

        slack = MockSlackClient()
        provider = _SequencedProvider([_abandoned(), _answered()], transient=True)
        sessions = _StopInGap(provider)

        await handle_message(slack, sessions, "C1", "hello", "thread1", "msg1", "U1")

        assert provider.turns == 1, "the stopped message must not run again"
        assert sessions.keys_seen.count("thread1") == 2, "the replay acquired, then bailed"
        assert not any("The answer is 42" in t for t in _visible_texts(slack))
        assert sessions.replay_gaps[0] == ("open", "thread1")
        assert sessions.replay_gaps[-1] == ("close", "thread1")

    @pytest.mark.asyncio
    async def test_a_hook_reply_persists_only_after_the_replay_gap_resolves(self):
        """A hook auto-reply acquires no session, so ``get_or_create``'s wait on
        the replay gap never fences it. Posted into a thread whose older message
        sits between its reset and its replay, its record would file AHEAD of
        the replayed turn the thread saw first. The canned reply still posts at
        once; the conversation log is written only once the gap owner settled."""

        class _GapSessions(FakeSessionManager):
            def __init__(self, provider):
                super().__init__(provider)
                self.gap = asyncio.Event()
                self.parked = asyncio.Event()
                self.waited: list[str] = []

            async def await_replay_gap(self, key):
                self.waited.append(key)
                self.parked.set()
                await self.gap.wait()

        slack = MockSlackClient()
        sessions = _GapSessions(FakeProvider())
        log = MagicMock()
        builder = MagicMock()
        builder.hooks.on_message = MagicMock(
            return_value=MagicMock(action=HOOK_REPLY, text="canned answer")
        )

        task = asyncio.create_task(
            handle_message(
                slack,
                sessions,
                "C1",
                "ping",
                "thread1",
                "msg1",
                "U1",
                context_builder=builder,
                conversation_log=log,
            )
        )
        # The turn parks on the gap (real thread hops precede the hook check),
        # so wait for the park itself rather than for a guessed number of polls.
        try:
            await _within_lost_run(sessions.parked.wait(), "the park on the replay gap")
        finally:
            if not sessions.parked.is_set():
                task.cancel()
        assert sessions.waited == ["thread1"]
        assert any("canned answer" in t for t in _visible_texts(slack)), "the reply posts at once"
        assert log.append.call_count == 0, "the record must wait for the replay to settle"
        assert not task.done()

        sessions.gap.set()
        await _within_lost_run(task, "the turn after the gap closed")
        user_rows = [c for c in log.append.call_args_list if c.args[1] == "user"]
        assert len(user_rows) == 1, log.append.call_args_list
        assert sessions.keys_seen == [], "a hook reply acquires no session"

    @pytest.mark.asyncio
    async def test_a_stop_with_no_session_is_still_recorded_on_the_manager(self):
        """The handler's ``!stop`` fallback answers "Nothing running." when the
        key has no session -- which is exactly the state of a turn between its
        abandoned attempt and its compaction replay. The Stop is recorded on
        the manager BEFORE that check, so the replay can see it."""

        class _NotingSessions(FakeSessionManager):
            def __init__(self, provider, owner=None):
                super().__init__(provider)
                self.noted: list[str] = []
                self.owner = owner

            def note_stop(self, key):
                self.noted.append(key)
                return True

            def get_session_for_thread(self, thread_ts):
                return self.owner

        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER"})
        slack = MockSlackClient()
        sessions = _NotingSessions(FakeProvider())

        await handle_message(slack, sessions, "C1", "!stop", "thread1", "msg1", "U_OWNER")

        assert sessions.noted == ["thread1"]
        assert any("Nothing running" in t for t in _visible_texts(slack))

        # A linked thread records against the session that OWNS the thread --
        # the key its turns and their replay actually run under.
        slack = MockSlackClient()
        sessions = _NotingSessions(FakeProvider(), owner="dashboard:chat-7")
        await handle_message(slack, sessions, "C1", "!stop", "thread1", "msg1", "U_OWNER")
        assert sessions.noted == ["dashboard:chat-7"]

    @pytest.mark.asyncio
    async def test_a_thinking_only_attempt_takes_its_placeholder_down_before_replaying(
        self, monkeypatch
    ):
        """Reasoning is not output that a replay could duplicate, so a
        thinking-only abandoned attempt IS replayed -- but the 💭 placeholder it
        posted above where its answer would have gone must not stay behind, and
        its reaction ladder / stall watchdog must be finalized before the nested
        call takes over the Slack message."""
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig

        _real_cfg = KiroCrewConfig.load()
        # The real ``agent`` section, not a stub of it: ``handler._get_default_agent``
        # reads ``load().agent.default_agent`` and caches it in a module global, so a
        # config object carrying only ``slack`` raises unless some earlier test in the
        # same process happened to warm that cache first.
        monkeypatch.setattr(
            "kiro_crew.slack.handler.KiroCrewConfig.load",
            lambda: dataclasses.replace(
                _real_cfg,
                slack=dataclasses.replace(
                    _real_cfg.slack, show_thinking=True, reactions_enabled=False
                ),
            ),
        )
        slack = MockSlackClient()
        provider = _SequencedProvider(
            [[LLMEvent(kind="thinking_chunk", text="hmm"), *_abandoned()], _answered()],
            transient=True,
        )
        sessions = FakeSessionManager(provider)

        await handle_message(slack, sessions, "C1", "hello", "thread1", "msg1", "U1")

        assert provider.turns == 2, "a thinking-only attempt is still replayable"
        posted_thinking = [
            a[1]["ts"] for a in slack.actions if a[0] == "post" and "💭" in a[1]["text"]
        ]
        deleted = [a[1]["ts"] for a in slack.actions if a[0] == "delete"]
        assert posted_thinking, "the abandoned attempt reserved a reasoning slot"
        assert posted_thinking[0] in deleted, "and the replay took it down"

    @pytest.mark.asyncio
    async def test_the_replay_reruns_the_users_text_not_the_cancelled_turn_preamble(self):
        """After a soft-cancel the first attempt folds a cancelled-turn preamble
        into the prompt it sends the model. The replay must re-run what the user
        TYPED: the nested call derives its own preamble from its own one-shot
        gate (already consumed here, so none), and the conversation log records
        the user's text, never the preamble a previous attempt prepended."""
        from types import SimpleNamespace

        from kiro_crew.context import ContextBuilder

        log = MagicMock()
        log.get_metadata.return_value = {}
        log.get_metadata_status.return_value = ({}, True)
        log.recent.return_value = [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier partial answer"},
        ]
        ctx = ContextBuilder(conversation_log=log)
        slack = MockSlackClient()
        provider = _SequencedProvider([_abandoned(), _answered()], transient=True)
        sessions = FakeSessionManager(provider)
        sessions._sessions = {
            "thread1": SimpleNamespace(prev_turn_cancelled=True, provider=provider)
        }

        await handle_message(
            slack,
            sessions,
            "C1",
            "hello",
            "thread1",
            "msg1",
            "U1",
            context_builder=ctx,
            conversation_log=log,
        )

        assert provider.turns == 2
        first, replay = provider.turn_prompts
        assert "earlier question" in first, "the abandoned attempt carried the preamble"
        assert "earlier question" not in replay, "the replay re-ran the user's text alone"
        user_rows = [c.args[2] for c in log.append.call_args_list if c.args[1] == "user"]
        assert user_rows == ["hello"], user_rows

    @pytest.mark.asyncio
    async def test_the_replay_carries_every_argument_of_the_original(self, monkeypatch):
        """Pinning, activation, asker and action context all travel with the
        replay -- that is what makes a nested call Slack's own replay rather than
        a re-dispatch that forgets how the message arrived."""
        import kiro_crew.slack.handler as handler_mod

        seen: list[dict] = []
        real = handler_mod.handle_message

        async def _recording(*args, **kwargs):
            if kwargs.get("_compaction_replay") is not None:
                seen.append({"args": args, **kwargs})
                return None
            return await real(*args, **kwargs)

        monkeypatch.setattr(handler_mod, "handle_message", _recording)
        slack = MockSlackClient()
        provider = _SequencedProvider([_abandoned()], transient=True)
        sessions = FakeSessionManager(provider)

        await real(
            slack,
            sessions,
            "C1",
            "hello",
            "thread1",
            "msg1",
            "U1",
            team_id="T1",
            channel_agent="ops",
            user_display_name="Alice",
            action_context="clicked a button",
            channel_activation="review",
            from_trusted_bot=True,
            had_voice_input=True,
        )

        assert len(seen) == 1
        call = seen[0]
        assert call["args"] == (slack, sessions, "C1", "hello", "thread1", "msg1", "U1")
        assert call["team_id"] == "T1"
        assert call["channel_agent"] == "ops"
        assert call["user_display_name"] == "Alice"
        assert call["action_context"] == "clicked a button"
        assert call["channel_activation"] == "review"
        assert call["from_trusted_bot"] is True
        assert call["had_voice_input"] is True
        assert call["_compaction_replay"].attempt == 1


class TestStopReasonCancelled:
    """Phase 4: handler response to stopReason='cancelled'."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _ensure_reactions_enabled(self, monkeypatch):
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig

        _real_load = KiroCrewConfig.load

        def _patched_load():
            cfg = _real_load()
            return dataclasses.replace(
                cfg, slack=dataclasses.replace(cfg.slack, reactions_enabled=True)
            )

        monkeypatch.setattr(KiroCrewConfig, "load", _patched_load)

    @pytest.mark.asyncio
    async def test_handler_stop_reason_cancelled_skips_record_success(self):
        """When EVENT_COMPLETE carries stop_reason='cancelled', neither
        record_success nor record_failure should be called."""
        from kiro_crew.acp.types import STOP_REASON_CANCELLED

        slack = MockSlackClient()
        provider = FakeProvider(
            [
                LLMEvent(kind="text_chunk", text="partial"),
                LLMEvent(kind="complete", stop_reason=STOP_REASON_CANCELLED),
            ]
        )
        sessions = FakeSessionManager(provider)
        sessions._success_calls: list[str] = []
        sessions._failure_calls: list[str] = []
        _orig_success = sessions.record_success
        _orig_failure = sessions.record_failure

        def _track_success(key):
            sessions._success_calls.append(key)
            return _orig_success(key)

        async def _track_failure(key):
            sessions._failure_calls.append(key)
            return await _orig_failure(key)

        sessions.record_success = _track_success
        sessions.record_failure = _track_failure

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        assert sessions._success_calls == []
        assert sessions._failure_calls == []

    @pytest.mark.asyncio
    async def test_handler_stop_reason_cancelled_skips_consolidation(self, monkeypatch):
        """When cancelled, maybe_consolidate must not be called."""
        from unittest.mock import MagicMock

        from kiro_crew.acp.types import STOP_REASON_CANCELLED

        slack = MockSlackClient()
        provider = FakeProvider(
            [
                LLMEvent(kind="text_chunk", text="partial"),
                LLMEvent(kind="complete", stop_reason=STOP_REASON_CANCELLED),
            ]
        )
        sessions = FakeSessionManager(provider)

        mock_consolidator = MagicMock()

        await handle_message(
            slack,
            sessions,
            "C1",
            "hello",
            None,
            "msg1",
            "U1",
            consolidator=mock_consolidator,
        )

        mock_consolidator.maybe_consolidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_handler_stop_reason_end_turn_preserves_existing_behavior(self):
        """When stop_reason='end_turn', record_success and maybe_consolidate fire."""
        from unittest.mock import MagicMock

        from kiro_crew.acp.types import STOP_REASON_END_TURN

        slack = MockSlackClient()
        provider = FakeProvider(
            [
                LLMEvent(kind="text_chunk", text="done"),
                LLMEvent(kind="complete", stop_reason=STOP_REASON_END_TURN),
            ]
        )
        sessions = FakeSessionManager(provider)
        sessions._success_calls: list[str] = []
        _orig = sessions.record_success

        def _track(key):
            sessions._success_calls.append(key)
            return _orig(key)

        sessions.record_success = _track

        mock_consolidator = MagicMock()
        mock_conversation_log = MagicMock()

        await handle_message(
            slack,
            sessions,
            "C1",
            "hello",
            None,
            "msg1",
            "U1",
            conversation_log=mock_conversation_log,
            consolidator=mock_consolidator,
        )

        assert len(sessions._success_calls) == 1
        mock_consolidator.maybe_consolidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_handler_stop_reason_cancelled_flushes_partial_text(self):
        """Partial text chunks before cancel must be flushed, not dropped."""
        from kiro_crew.acp.types import STOP_REASON_CANCELLED

        slack = MockSlackClient()
        provider = FakeProvider(
            [
                LLMEvent(kind="text_chunk", text="partial output here"),
                LLMEvent(kind="complete", stop_reason=STOP_REASON_CANCELLED),
            ]
        )
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        # The partial text should appear in the final posted/updated message
        all_text = " ".join(
            a[1].get("text", "") for a in slack.actions if a[0] in ("update", "post", "stop_stream")
        )
        assert "partial output here" in all_text


class TestLiveTurnPresentationFailure:
    """A presentation-side Slack call that raises mid-run must not flip the
    placeholder to the terminal "🔧 Something went wrong" message nor record a
    session failure while the ACP turn is still live and will finish with the
    correct, complete reply.

    The exposed calls run on the first ``tool_call`` event, before any text has
    streamed, so ``accumulated`` is empty. A non-ACP raise there is not caught by
    the typed ``except`` arms (all ``kiro_crew.acp.client`` errors) and would
    reach the generic ``except Exception`` catch-all, which renders the terminal
    error and records a failure. These tests assert the raise is swallowed and
    the turn still delivers.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _ensure_reactions_enabled(self, monkeypatch):
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig

        _real_load = KiroCrewConfig.load

        def _patched_load():
            cfg = _real_load()
            return dataclasses.replace(
                cfg, slack=dataclasses.replace(cfg.slack, reactions_enabled=True)
            )

        monkeypatch.setattr(KiroCrewConfig, "load", _patched_load)

    @pytest.mark.asyncio
    async def test_progress_card_raise_does_not_render_terminal_error(self):
        # A slack client whose progress-card call raises like a real Slack API
        # refusal / rate limit — NOT swallowed internally, so it propagates into
        # the handler's loop exactly as a non-swallowing client would.
        class RaisingCardSlack(MockSlackClient):
            def __init__(self):
                super().__init__()
                self._stream_enabled = True  # take the Slack streaming path

            async def append_task(self, *a, **kw):
                raise RuntimeError("ratelimited: chat.appendStream")

        # A FakeSessionManager that records whether the turn was marked failed.
        class TrackingSessions(FakeSessionManager):
            def __init__(self, provider):
                super().__init__(provider)
                self.record_failure_calls = 0
                self.record_success_calls = 0

            async def record_failure(self, key):
                self.record_failure_calls += 1
                return False

            def record_success(self, key):
                self.record_success_calls += 1

        slack = RaisingCardSlack()
        # tool_call FIRST (accumulated empty at the raise), then the real reply.
        provider = FakeProvider(
            [
                LLMEvent(
                    kind="tool_call",
                    title="Running: grep",
                    tool_kind="execute",
                    tool_purpose="searching the repo",
                ),
                LLMEvent(kind="text_chunk", text="The answer is 42"),
            ]
        )
        sessions = TrackingSessions(provider)

        await handle_message(slack, sessions, "C1", "do something slow", None, "msg1", "U1")

        # 1. The turn was NOT recorded as a failure — a cosmetic card refusal is
        #    not the turn dying.
        assert sessions.record_failure_calls == 0
        # 2. The terminal error string was never sent to Slack on any surface.
        all_text = " ".join(
            str(a[1].get("text") or "")
            for a in slack.actions
            if a[0] in ("post", "update", "append_stream", "stop_stream")
        )
        assert "Something went wrong" not in all_text, slack.actions
        # 3. The real reply still reached the user.
        assert "The answer is 42" in all_text, slack.actions
        # 4. The turn completed successfully.
        assert sessions.record_success_calls == 1

    @pytest.mark.asyncio
    async def test_stream_start_and_fallback_both_fail_still_posts_reply(self):
        """The one sub-path the card test does not reach: streaming is on, but
        ``start_stream`` demotes (returns None) AND the ``chat.update`` fallback
        ``post_message`` raises. ``_ensure_stream_started`` must leave
        ``stream_ts`` falsy so end of turn posts the accumulated reply directly —
        NOT route through the placeholder-edit branch against a ts that does not
        exist (which loses a single-part reply silently while recording success).
        """

        class DemotedThenRaisingSlack(MockSlackClient):
            def __init__(self):
                super().__init__()
                self._stream_enabled = True
                self._real_ts: set[str] = set()

            async def start_stream(self, *a, **kw):
                # Demote: chat.startStream unavailable → non-streaming fallback.
                return None

            async def post_message(self, channel, text, thread_ts=None, **kw):
                # The ``chat.update`` placeholder fallback goes through the base
                # ``post_message`` and raises on a Slack refusal. The FINAL answer
                # post is a separate call with the real reply text and must still
                # go through, so only the placeholder text is refused.
                if text == _THINKING:
                    raise RuntimeError("channel_not_found: chat.postMessage")
                ts = await super().post_message(channel, text, thread_ts=thread_ts, **kw)
                self._real_ts.add(ts)
                return ts

            async def update_message(self, channel, ts, text):
                # Editing a message that was never posted fails, exactly as the
                # Slack API rejects chat.update against a non-existent ts. The
                # handler swallows that at debug, so a reply routed here instead
                # of to a fresh post_message is lost silently — which is the
                # data-loss defect the buggy truthy sentinel introduced.
                if ts not in self._real_ts:
                    raise RuntimeError(f"message_not_found: chat.update ts={ts}")
                return await super().update_message(channel, ts, text)

        class TrackingSessions(FakeSessionManager):
            def __init__(self, provider):
                super().__init__(provider)
                self.record_failure_calls = 0
                self.record_success_calls = 0

            async def record_failure(self, key):
                self.record_failure_calls += 1
                return False

            def record_success(self, key):
                self.record_success_calls += 1

        slack = DemotedThenRaisingSlack()
        # tool_call FIRST so _ensure_stream_started runs with accumulated empty,
        # then a single-part reply — the part that must not be lost.
        provider = FakeProvider(
            [
                LLMEvent(
                    kind="tool_call",
                    title="Running: grep",
                    tool_kind="execute",
                    tool_purpose="searching the repo",
                ),
                LLMEvent(kind="text_chunk", text="The answer is 42"),
            ]
        )
        sessions = TrackingSessions(provider)

        await handle_message(slack, sessions, "C1", "do something slow", None, "msg1", "U1")

        # The reply must arrive via a real ``post`` (the end-of-turn else branch),
        # NOT an ``update`` against a bogus placeholder ts — an update there is
        # swallowed and the single-part reply is lost. Asserting specifically on
        # ``post`` is what makes the truthy-sentinel regression fail this test.
        reply_posts = [str(a[1].get("text") or "") for a in slack.actions if a[0] == "post"]
        assert any("The answer is 42" in p for p in reply_posts), slack.actions
        # No terminal error, and the turn is not falsely failed.
        all_text = " ".join(
            str(a[1].get("text") or "")
            for a in slack.actions
            if a[0] in ("post", "update", "stop_stream")
        )
        assert "Something went wrong" not in all_text, slack.actions
        assert sessions.record_failure_calls == 0


class TestStreamingLoopPresentationGuards:
    """Presentation-guard sweep: every remaining presentation-side Slack
    call inside the streaming loop must be best-effort. The real ``SlackClient``
    swallows its own API errors, but a client or transport that raises instead
    would escape into the generic ``except Exception`` catch-all (the typed arms
    are all ``kiro_crew.acp.client`` errors), rendering the terminal
    "🔧 Something went wrong" message and recording a session failure on a turn
    that is still live. Each test drives one guarded site with a raising client
    and asserts: no failure recorded, no terminal placeholder, reply delivered.
    """

    class _TrackingSessions(FakeSessionManager):
        def __init__(self, provider):
            super().__init__(provider)
            self.record_failure_calls = 0
            self.record_success_calls = 0

        async def record_failure(self, key):
            self.record_failure_calls += 1
            return False

        def record_success(self, key):
            self.record_success_calls += 1

    @staticmethod
    def _all_text(slack) -> str:
        return " ".join(
            str(a[1].get("text") or "")
            for a in slack.actions
            if a[0] in ("post", "update", "append_stream", "stop_stream")
        )

    @pytest.mark.asyncio
    async def test_set_thread_status_raise_in_tool_and_text_branches(self):
        """``set_thread_status`` raises in the TOOL_CALL branch (tool status) and
        again in the TEXT_CHUNK branch (the ``_status_dirty`` reset): both are
        decoration and must be swallowed."""

        class RaisingStatusSlack(MockSlackClient):
            def __init__(self):
                super().__init__()
                self._stream_enabled = True
                self.status_raises = 0

            async def set_thread_status(self, *a, **kw):
                # Out-of-scope calls for this sweep: the pre-loop base-status
                # call (before the stream opens) and the end-of-turn/error-path
                # status CLEARS (empty status). Raise only on the in-loop
                # TOOL_CALL / TEXT_CHUNK branch calls this sweep guards —
                # non-empty statuses sent after the stream opened.
                status = a[2] if len(a) > 2 else kw.get("status", "")
                if not status or not any(m == "start_stream" for m, _ in self.actions):
                    return await super().set_thread_status(*a, **kw)
                self.status_raises += 1
                raise RuntimeError("ratelimited: assistant.threads.setStatus")

        slack = RaisingStatusSlack()
        # tool_call hits the TOOL_CALL-branch status; the following text chunk
        # (with _status_dirty=True) hits the TEXT_CHUNK-branch reset.
        provider = FakeProvider(
            [
                LLMEvent(
                    kind="tool_call",
                    title="Running: grep",
                    tool_kind="execute",
                    tool_purpose="searching the repo",
                ),
                LLMEvent(kind="text_chunk", text="The answer is 42"),
            ]
        )
        sessions = self._TrackingSessions(provider)

        await handle_message(slack, sessions, "C1", "do something slow", None, "msg1", "U1")

        assert slack.status_raises >= 2, slack.actions  # both branches actually fired
        assert sessions.record_failure_calls == 0
        all_text = self._all_text(slack)
        assert "Something went wrong" not in all_text, slack.actions
        assert "The answer is 42" in all_text, slack.actions
        assert sessions.record_success_calls == 1

    @pytest.mark.asyncio
    async def test_wait_finalize_stop_stream_raise(self):
        """``stop_stream`` raising at the wait-tool finalize must not fail the
        turn: the stream is abandoned either way and the next chunk reopens a
        fresh one. Only the finalize call (no ``final_text``) raises — the
        end-of-turn delivery call carries the reply and stays live."""

        class RaisingFinalizeSlack(MockSlackClient):
            def __init__(self):
                super().__init__()
                self._stream_enabled = True
                self.finalize_raises = 0

            async def stop_stream(self, channel, ts, final_text=None):
                if final_text is None:
                    self.finalize_raises += 1
                    raise RuntimeError("message_not_found: chat.stopStream")
                return await super().stop_stream(channel, ts, final_text)

        slack = RaisingFinalizeSlack()
        provider = FakeProvider(
            [
                LLMEvent(
                    kind="tool_call",
                    title="Running: wait",
                    tool_kind="execute",
                    tool_purpose="waiting for CI",
                ),
                LLMEvent(kind="text_chunk", text="The answer is 42"),
            ]
        )
        sessions = self._TrackingSessions(provider)

        await handle_message(slack, sessions, "C1", "babysit the build", None, "msg1", "U1")

        assert slack.finalize_raises >= 1, slack.actions  # the finalize path actually fired
        assert sessions.record_failure_calls == 0
        all_text = self._all_text(slack)
        assert "Something went wrong" not in all_text, slack.actions
        assert "The answer is 42" in all_text, slack.actions
        assert sessions.record_success_calls == 1

    @pytest.mark.asyncio
    async def test_append_stream_raise_with_failed_rotation(self):
        """``append_stream`` raising (instead of returning False) must route into
        the existing rotation path, and the rotation's own ``start_stream``
        raising must demote to chat.update — never escape to the catch-all. The
        reply is still delivered from ``accumulated`` at end of turn."""

        class RaisingAppendSlack(MockSlackClient):
            def __init__(self):
                super().__init__()
                self._stream_enabled = True
                self._started_once = False
                self.append_raises = 0

            async def start_stream(self, *a, **kw):
                # First open succeeds (streaming path taken); the rotation's
                # reopen raises like a transport failure.
                if self._started_once:
                    raise RuntimeError("fatal_error: chat.startStream")
                self._started_once = True
                return await super().start_stream(*a, **kw)

            async def append_stream(self, *a, **kw):
                self.append_raises += 1
                raise RuntimeError("ratelimited: chat.appendStream")

        slack = RaisingAppendSlack()
        provider = FakeProvider([LLMEvent(kind="text_chunk", text="The answer is 42")])
        sessions = self._TrackingSessions(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        assert slack.append_raises >= 1, slack.actions  # the guarded site actually fired
        assert sessions.record_failure_calls == 0
        all_text = self._all_text(slack)
        assert "Something went wrong" not in all_text, slack.actions
        assert "The answer is 42" in all_text, slack.actions
        assert sessions.record_success_calls == 1

    @pytest.mark.asyncio
    async def test_append_retry_raise_after_successful_rotation_still_delivers(self):
        """Rotation SUCCEEDS but the retry append raises too, and EVERY append
        on the turn is refused: the raise still maps onto the refused-append
        outcome — the turn stays live and shows no terminal error — but a stream
        that delivered no text at all did not reach the reader, so the verdict is
        a failure, not a success booked from the mere fact a stream was opened.
        (Re-delivering a dropped delta on a stream that DID land text is the
        delivery-debt follow-up, deliberately out of this sweep's scope.)"""

        class AlwaysRaisingAppendSlack(MockSlackClient):
            def __init__(self):
                super().__init__()
                self._stream_enabled = True
                self.append_raises = 0

            async def append_stream(self, *a, **kw):
                self.append_raises += 1
                raise RuntimeError("ratelimited: chat.appendStream")

        slack = AlwaysRaisingAppendSlack()
        provider = FakeProvider([LLMEvent(kind="text_chunk", text="The answer is 42")])
        sessions = self._TrackingSessions(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        # Both the first append and the post-rotation retry fired and raised.
        assert slack.append_raises >= 2, slack.actions
        # No terminal error surfaces: the appends are swallowed, not escalated to
        # the catch-all, so the turn completes cleanly.
        all_text = self._all_text(slack)
        assert "Something went wrong" not in all_text, slack.actions
        # But nothing was delivered, so the single verdict is a failure.
        assert sessions.record_success_calls == 0, slack.actions
        assert sessions.record_failure_calls == 1

    @pytest.mark.asyncio
    async def test_initial_start_stream_raise_demotes_to_fallback(self):
        """The lazy first ``start_stream`` in ``_ensure_stream_started`` raising
        must demote to the chat.update path exactly like a ``None`` return —
        not escape to the catch-all from the first event."""

        class RaisingStartSlack(MockSlackClient):
            def __init__(self):
                super().__init__()
                self._stream_enabled = True
                self.start_raises = 0

            async def start_stream(self, *a, **kw):
                self.start_raises += 1
                raise RuntimeError("fatal_error: chat.startStream")

        slack = RaisingStartSlack()
        provider = FakeProvider(
            [
                LLMEvent(
                    kind="tool_call",
                    title="Running: grep",
                    tool_kind="execute",
                    tool_purpose="searching the repo",
                ),
                LLMEvent(kind="text_chunk", text="The answer is 42"),
            ]
        )
        sessions = self._TrackingSessions(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        assert slack.start_raises >= 1, slack.actions  # the guarded site actually fired
        assert sessions.record_failure_calls == 0
        all_text = self._all_text(slack)
        assert "Something went wrong" not in all_text, slack.actions
        assert "The answer is 42" in all_text, slack.actions
        assert sessions.record_success_calls == 1


# ── Turn verdict after delivery ──
# A turn is booked a success only once its answer-carrying send has landed: a
# failed final send means the reader got no answer, so the turn is a failure.


class _TrackNativeSessions(FakeSessionManager):
    """Counts the successes, failures and permit releases the turn books."""

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
    """Stream disabled, so the answer is delivered by updating the ``_THINKING``
    placeholder. Every update fails: the reader never gets the answer."""

    async def update_message(self, channel, ts, text=""):
        self.actions.append(("update", {"channel": channel, "ts": ts, "text": text}))
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


class _OrderedSessions(FakeSessionManager):
    """Records the order of release vs record_success."""

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
    # If release ran first, a queued same-key turn could mutate breaker state
    # mid-verdict, so the permit must outlive accounting.
    slack = MockSlackClient()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the answer is 42")])
    sessions = _OrderedSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert "record_success" in sessions.events
    assert "release" in sessions.events
    assert sessions.events.index("record_success") < sessions.events.index(
        "release"
    ), f"permit released before accounting: {sessions.events}"


class _AcpErrorProvider:
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
    """Every message/update send fails, so the error-text post also raises after
    the model error already booked its failure."""

    async def update_message(self, channel, ts, text=""):
        self.actions.append(("update", {"channel": channel, "ts": ts, "text": text}))
        raise RuntimeError("slack down")

    async def post_message(self, channel, text, thread_ts=None):
        self.actions.append(("post_message", {"channel": channel, "text": text}))
        raise RuntimeError("slack down")


def test_native_model_error_plus_failed_error_post_books_one_failure(monkeypatch):
    slack = _ErrorPostFailsSlack()
    sessions = _TrackNativeSessions(_AcpErrorProvider())

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    # The failed error-post must not book a second failure for the same turn.
    assert sessions.calls["failure"] == 1, f"double-counted: {sessions.calls}"
    assert sessions.calls["success"] == 0


class _ReviewEphemeralRaisesSlack(MockSlackClient):
    """Review path: the ephemeral draft post raises (user_not_in_channel, rate
    limit, or any SlackApiError from chat_postEphemeral)."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True

    async def post_ephemeral(self, channel, user_id, text, blocks=None, thread_ts=None):
        self.actions.append(("ephemeral", {"channel": channel, "text": text}))
        raise RuntimeError("slack_api_error: user_not_in_channel")


def _run_review_turn(slack, sessions) -> None:
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
        # The raise may propagate after the finally releases; that is acceptable.
        pass


def test_review_ephemeral_raise_still_releases_permit(monkeypatch):
    # A raising ephemeral post must not strand the per-session permit.
    slack = _ReviewEphemeralRaisesSlack()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the draft answer")])
    sessions = _TrackNativeSessions(provider)

    _run_review_turn(slack, sessions)

    assert sessions.releases == 1, "permit stranded (or double-released) after a raising post"


class _CancelDuringDeliverySlack(MockSlackClient):
    """``stop_stream`` raises CancelledError: a stop/shutdown cancelling the turn
    after the model completed."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True

    async def stop_stream(self, channel, ts, final_text=None):
        self.actions.append(("stop_stream", {"channel": channel, "ts": ts}))
        raise asyncio.CancelledError()


def test_cancelled_after_completion_still_releases_permit(monkeypatch):
    slack = _CancelDuringDeliverySlack()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the answer is 42")])
    sessions = _TrackNativeSessions(provider)

    # The cancellation propagates, and the permit is released on the way out.
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert sessions.releases == 1, "permit stranded after a cancel mid-delivery"


def test_native_review_ephemeral_raise_books_one_failure(monkeypatch):
    slack = _ReviewEphemeralRaisesSlack()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the draft answer")])
    sessions = _TrackNativeSessions(provider)

    _run_review_turn(slack, sessions)

    # The draft never reached the reader: exactly one failure, never no verdict.
    assert (
        sessions.calls["failure"] == 1
    ), f"review ephemeral raise left no verdict: {sessions.calls}"
    assert sessions.calls["success"] == 0


class _OptionsFooterFailsSlack(MockSlackClient):
    """Answer streams fine, but the footer post_blocks carrying the [OPTIONS]
    choices fails. ``fallback_posts`` collects the plain-text re-send."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True
        self.fallback_posts = []

    async def post_blocks(self, channel, blocks, text, thread_ts=None):
        self.actions.append(("post_blocks", {"channel": channel}))
        raise RuntimeError("footer post_blocks failed")

    async def post_message(self, channel, text, thread_ts=None):
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

    # The answer streamed, so the turn is a success; the failed footer's choices
    # are re-sent as text rather than silently dropped.
    assert sessions.calls == {"success": 1, "failure": 0}
    assert (
        slack.fallback_posts
    ), "options footer failed but choices were not re-sent as fallback text"
    joined = "\n".join(slack.fallback_posts)
    assert "Apple" in joined and "Banana" in joined


class _OptionsFooterAndFallbackFailSlack(MockSlackClient):
    """Answer streams fine, but both the footer post_blocks and the plain-text
    fallback fail, so the choices never reach the reader."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True

    async def post_blocks(self, channel, blocks, text, thread_ts=None):
        self.actions.append(("post_blocks", {"channel": channel}))
        raise RuntimeError("footer post_blocks failed")

    async def post_message(self, channel, text, thread_ts=None):
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

    # The choices are answer-carrying and neither delivery landed.
    assert (
        sessions.calls["success"] == 0
    ), "booked success though OPTIONS choices never reached the reader"
    assert sessions.calls["failure"] == 1


class _OptionsFallbackCapturingSlack(MockSlackClient):
    """Footer post_blocks fails; the plain-text OPTIONS fallback is captured.

    The display-safe scrub strips the ``*Options:*`` emphasis to ``Options:`` when
    it downgrades a message carrying a credential, so the bare label is matched.
    """

    def __init__(self):
        super().__init__()
        self._stream_enabled = True
        self.fallback_text = None

    async def post_blocks(self, channel, blocks, text, thread_ts=None):
        raise RuntimeError("footer failed")

    async def post_message(self, channel, text, thread_ts=None):
        if text.lstrip("*").startswith("Options:"):
            self.fallback_text = text
        return await super().post_message(channel, text, thread_ts)


#: An AWS-documentation example key, built from parts so no scanner matches it here.
_FAKE_AWS_KEY = "AKIA" + "IOSFODNN7" + "EXAMPLE"


def test_native_options_fallback_is_redacted(monkeypatch):
    slack = _OptionsFallbackCapturingSlack()
    provider = FakeProvider(
        [LLMEvent(kind="text_chunk", text=f"pick [OPTIONS: use {_FAKE_AWS_KEY} | skip]")]
    )
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert slack.fallback_text is not None, "fallback was not posted"
    assert (
        _FAKE_AWS_KEY not in slack.fallback_text
    ), "credential rendered raw into the OPTIONS fallback"


def test_native_options_fallback_redacts_markup_obfuscated_credential(monkeypatch):
    # The literal bytes match no credential pattern, but Slack renders the ``**``
    # away and shows an intact key; only a scan of the rendered form catches it.
    slack = _OptionsFallbackCapturingSlack()
    obfuscated = _FAKE_AWS_KEY[:8] + "**" + _FAKE_AWS_KEY[8:] + "**"
    provider = FakeProvider(
        [LLMEvent(kind="text_chunk", text=f"pick [OPTIONS: use {obfuscated} | skip]")]
    )
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert slack.fallback_text is not None, "fallback was not posted"
    rendered_fallback = canonicalize_display(slack.fallback_text)
    assert (
        _FAKE_AWS_KEY not in rendered_fallback
    ), "markup-obfuscated credential rendered raw into the OPTIONS fallback"


class _HandlerAsyncio:
    """``asyncio`` as the handler's namespace sees it, with *overrides* swapped in.

    Patching ``asyncio.sleep`` itself would reach every coroutine and thread in the
    worker. The handler reads ``asyncio`` from its own globals, so rebinding that
    name reaches exactly the yields the turn makes.
    """

    def __init__(self, **overrides):
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(asyncio, name)


def test_native_cancel_at_predelivery_yield_releases_permit(monkeypatch):
    # The finally must not read a local bound only after the pre-delivery yield.
    import kiro_crew.slack.handler as _h

    real_sleep = asyncio.sleep
    state = {"tripped": False}

    async def _sleep(delay, *a, **k):
        # Cancel at the first zero-delay sleep: the post-finalize yield.
        if delay == 0 and not state["tripped"]:
            state["tripped"] = True
            raise asyncio.CancelledError()
        return await real_sleep(delay, *a, **k)

    monkeypatch.setattr(_h, "asyncio", _HandlerAsyncio(sleep=_sleep))

    slack = MockSlackClient()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the answer is 42")])
    sessions = _TrackNativeSessions(provider)

    try:
        asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))
    except asyncio.CancelledError:
        pass

    assert state["tripped"], "the pre-delivery yield was not exercised"
    assert sessions.releases == 1, "permit stranded by a cancellation at the pre-delivery yield"


class _AllAppendsRefusedSlack(MockSlackClient):
    """Streaming enabled, but every append_stream is refused for the whole turn."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True

    async def append_stream(self, channel, ts, text):
        self.actions.append(("append_stream", {"channel": channel, "ts": ts, "text": text}))
        return False


def test_native_stream_all_appends_refused_books_failure(monkeypatch):
    # Opening a stream is not delivering an answer: no append landed.
    slack = _AllAppendsRefusedSlack()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the answer is 42")])
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert (
        sessions.calls["success"] == 0
    ), "booked success for a stream that delivered no answer text"
    assert sessions.calls["failure"] == 1


class _OneAppendThenRefusedSlack(MockSlackClient):
    """First append lands, the rest are refused: the stream was used."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True
        self._appends = 0

    async def append_stream(self, channel, ts, text):
        self.actions.append(("append_stream", {"channel": channel, "ts": ts, "text": text}))
        self._appends += 1
        return self._appends == 1


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

    # The refused remainder is delivery debt, not a turn failure.
    assert sessions.calls == {"success": 1, "failure": 0}


class _StreamingSlack(MockSlackClient):
    """Streaming enabled; start, append and stop all succeed."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True


def test_native_stream_reasoning_only_turn_books_success_not_failure(monkeypatch):
    # A turn with no answer text reached its (empty) answer. A false failure here
    # would push the consecutive-failure breaker toward a session reset.
    slack = _StreamingSlack()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="<thinking>working</thinking>")])
    sessions = _TrackNativeSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert sessions.calls == {"success": 1, "failure": 0}


class _StopStreamCancelsSlack(MockSlackClient):
    """Streaming enabled; ``stop_stream`` raises CancelledError, after delivery
    and before the verdict step."""

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

    # A real-text append landed before the cancel, so the finally books the
    # decided success rather than leaving no verdict.
    assert sessions.calls == {"success": 1, "failure": 0}


def test_native_cancel_in_stop_stream_options_turn_books_no_success(monkeypatch):
    # The choices ride only in the footer, which the cancellation pre-empts.
    slack = _StopStreamCancelsSlack()
    provider = FakeProvider(
        [LLMEvent(kind="text_chunk", text="here are choices [OPTIONS: alpha | beta]")]
    )
    sessions = _TrackNativeSessions(provider)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert (
        sessions.calls["success"] == 0
    ), "booked success for an OPTIONS turn whose choices never delivered"


class _ContextProbeRaisesSessions(_TrackNativeSessions):
    """The post-completion context-usage probe raises a transient error."""

    def check_context_usage(self, key, provider):
        raise RuntimeError("transient context probe failure")


def test_native_context_probe_failure_books_exactly_one_verdict(monkeypatch):
    # The probe is advisory: its failure must not add a failure on top of the
    # completed turn's success.
    slack = MockSlackClient()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the answer is 42")])
    sessions = _ContextProbeRaisesSessions(provider)

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert sessions.calls == {"success": 1, "failure": 0}


def test_native_cancel_at_sleep0_after_stream_delivered_books_success(monkeypatch):
    # A cancel at the post-stream yield reaches the finally before the answer
    # verdict is recomputed; the confirmed stream append must still book success.
    slack = _StreamingSlack()
    provider = FakeProvider([LLMEvent(kind="text_chunk", text="the answer is 42")])
    sessions = _TrackNativeSessions(provider)

    import kiro_crew.slack.handler as _h

    real_sleep = asyncio.sleep
    state = {"fired": False}

    async def _sleep(secs, *a, **k):
        if secs == 0 and not state["fired"] and getattr(slack, "_saw_append", False):
            state["fired"] = True
            raise asyncio.CancelledError()
        return await real_sleep(secs, *a, **k)

    # Only cancel once a real-text append has landed: the post-stream yield.
    _orig_append = slack.append_stream

    async def _append(channel, ts, text):
        if text and text.strip():
            slack._saw_append = True
        return await _orig_append(channel, ts, text)

    slack.append_stream = _append  # type: ignore[method-assign]
    monkeypatch.setattr(_h, "asyncio", _HandlerAsyncio(sleep=_sleep))

    try:
        asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))
    except asyncio.CancelledError:
        pass

    assert state["fired"], "test did not hit the post-stream sleep(0) cancellation"
    assert sessions.calls["success"] == 1 and sessions.calls["failure"] == 0
