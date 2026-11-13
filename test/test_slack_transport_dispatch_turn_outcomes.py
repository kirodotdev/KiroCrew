"""Slack transport dispatch: what a turn leaves behind once it ends.

Covers the agent the session file's metadata header records (from the user-turn
receipt, the options-stamp fallback and a hook auto-reply), the auto-title that
follows a successful turn, the error reply a failed turn posts and holds back
for a trusted bot, what a turn that fails partway through persists of the
reply it streamed, and the voice reply a finished turn speaks. The message
timestamp and the capturing session and context-builder stand-ins live in
``slack_transport_dispatch_helpers.py``, which also binds the golden-transcript
fakes from ``test_slack_golden_transcript``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from slack_transport_dispatch_helpers import (
    _MSG_TS,
    FakeSessions,
    RecordingSlackClient,
    ScriptedProvider,
    _CapturingCtxBuilder,
    _CapturingSessions,
    make_event,
)

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TIMEOUT,
)
from kiro_crew.messaging.link import canonical_key
from kiro_crew.slack import handler as _handler
from kiro_crew.slack import transport_dispatch
from kiro_crew.testing.wait import await_until

#: These tests start background helper turns, which are skipped while the host's
#: memory posture is critical (see ``ample_host_free_memory``).
pytestmark = pytest.mark.usefixtures("ample_host_free_memory")


class TestConversationLogAgentMetadata:
    """The transport's user-turn write creates the session file — its metadata
    header records the agent only when that creating append supplies it.
    Pre-fix, the dashboard listed every Slack-spawned session as the "default"
    agent even though the turn ran under the resolved agent (issue: agent chip
    shows "default" for Slack sessions)."""

    def test_transport_turn_records_agent_in_session_metadata(self, monkeypatch, tmp_path):
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "sales-agent")
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

        slack = RecordingSlackClient()
        provider = ScriptedProvider(
            [
                make_event(EVENT_TEXT_CHUNK, text="hi"),
                make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )
        sessions = _CapturingSessions(provider)
        log = ConversationLog(base_dir=tmp_path)

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
                conversation_log=log,
                agent_override=None,
            )
        )

        listed = log.list_sessions()
        assert listed, "transport turn should have persisted a session file"
        # The session the dashboard lists must carry the agent the turn ran
        # under — not fall back to "default" because the header omitted it.
        assert listed[0].get("agent") == "sales-agent"

    def test_options_stamp_fallback_records_agent_when_receipt_write_failed(
        self, monkeypatch, tmp_path
    ):
        """The options-stamp fallback write is file-creating exactly when the
        user-turn receipt write failed — it must supply the agent for the same
        reason the receipt write does, or the session is pinned to "default"."""
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "sales-agent")
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

        slack = RecordingSlackClient()
        provider = ScriptedProvider(
            [
                # An [OPTIONS:] trailer makes the renderer invoke stamp_options,
                # routing persistence through _persist_and_stamp.
                make_event(EVENT_TEXT_CHUNK, text="Pick one.\n\n[OPTIONS: A | B]"),
                make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )
        sessions = _CapturingSessions(provider)
        log = ConversationLog(base_dir=tmp_path)

        # Fail the FIRST append (the user-turn receipt) so the stamp fallback
        # becomes the write that creates the session file.
        real_append = log.append
        state = {"failed": False}

        def _flaky_append(*args, **kwargs):
            if not state["failed"]:
                state["failed"] = True
                raise OSError("simulated receipt-write failure")
            return real_append(*args, **kwargs)

        monkeypatch.setattr(log, "append", _flaky_append)

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
                conversation_log=log,
                agent_override=None,
            )
        )

        listed = log.list_sessions()
        assert listed, "options-stamp fallback should have persisted a session file"
        assert listed[0].get("agent") == "sales-agent"

    def test_hook_reply_write_records_agent_in_session_metadata(self, monkeypatch, tmp_path):
        """A hook-answered FIRST message is the write that creates the session
        file — it runs before session acquisition, so the agent must be
        resolved early (native handler parity) or the session is pinned to
        "default" forever."""
        from kiro_crew.history import ConversationLog
        from kiro_crew.hooks import HookResult

        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "sales-agent")
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

        slack = RecordingSlackClient()
        sessions = _CapturingSessions(self._noop_provider())
        cb = _CapturingCtxBuilder(hook_result=HookResult.reply("canned answer"))
        log = ConversationLog(base_dir=tmp_path)

        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="ping",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=cb,
                conversation_log=log,
                agent_override=None,
            )
        )

        # The hook short-circuited the turn — no LLM session was spawned.
        assert sessions.agents == []
        listed = log.list_sessions()
        assert listed, "hook auto-reply should have persisted a session file"
        assert listed[0].get("agent") == "sales-agent"

    @staticmethod
    def _noop_provider():
        return ScriptedProvider(
            [
                make_event(EVENT_TEXT_CHUNK, text="hi"),
                make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )


# ── Auto-title on the transport path ───────────────────────────────────


class _TitlingProvider(ScriptedProvider):
    """The turn's events on the first stream, a title on the naming turn.

    ``ScriptedProvider`` deliberately returns an EMPTY stream for every call after
    the first, which is exactly why the missing auto-title never showed up in this
    file: an empty naming stream reads as SKIP, and SKIP is silent.
    """

    def __init__(self, events, title="Deploy the gateway"):
        super().__init__(events)
        self._title = title

    async def stream(self, message: str):
        self.stream_calls += 1
        if self.stream_calls == 1:
            for ev in self._events:
                yield ev
            return
        self.title_prompt = message
        yield make_event(EVENT_TEXT_CHUNK, text=self._title)
        yield make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


class _TitleSessions(_CapturingSessions):
    """Adds the two calls ``background_turn`` makes around the naming turn."""

    def __init__(self, provider):
        super().__init__(provider)
        self.released: list = []
        self.recycled = 0

    def release(self, key):
        self.released.append(key)

    async def recycle_background(self):
        self.recycled += 1


def _run_transport_titling(monkeypatch, *, restricted=False, conversation_log=None):
    """Drive one successful transport turn and drain the fire-and-forget tasks."""
    monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
    monkeypatch.setattr(
        transport_dispatch,
        "_hydrate_thread_overrides",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
    monkeypatch.setattr(transport_dispatch, "_thread_agents", {})
    monkeypatch.setattr(transport_dispatch, "_is_slack_restricted", lambda _key: restricted)

    slack = RecordingSlackClient()
    provider = _TitlingProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="hi"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _TitleSessions(provider)

    async def _drive():
        await transport_dispatch.handle_message_transport(
            slack=slack,
            sessions=sessions,
            channel="C1",
            text="deploy the gateway please",
            thread_ts=None,
            msg_ts=_MSG_TS,
            user_id="U_OWNER",
            context_builder=None,
            conversation_log=conversation_log,
        )
        # The auto-title is fire-and-forget through handler's tracked-task set,
        # so draining that set is the deterministic join point.
        #
        # Scoped to THIS loop's tasks. The set is a process global and every test
        # gets a fresh loop, so an earlier test in the same xdist worker can leave
        # a task behind whose loop is already closed — gathering it raises
        # "The future belongs to a different loop", which passes under ``-n0`` and
        # fails only under ``-n auto``.
        loop = asyncio.get_running_loop()
        pending = [t for t in list(_handler._background_tasks) if t.get_loop() is loop]
        if pending:
            # The join is bounded, so a title task that never finishes fails
            # this test by name.
            await await_until(
                asyncio.gather(*pending, return_exceptions=True),
                what="this loop's auto-title tasks to finish",
            )

    asyncio.run(_drive())
    return slack, sessions, provider


def _titles(slack):
    return [kw["title"] for (m, kw) in slack.transcript if m == "set_thread_title"]


class TestTransportAutoTitle:
    """The transport path titles a conversation after its first successful turn.

    It never did, while ``messaging.use_transport`` defaults True — so on a
    default install NO Slack session was ever LLM-titled and every surface fell
    back to a deterministic truncation.
    """

    def test_a_successful_turn_titles_the_thread(self, monkeypatch):
        """Mutation: delete the auto-title block in ``handle_message_transport``
        — red, which is the state this path shipped in."""
        slack, _sessions, provider = _run_transport_titling(monkeypatch)
        assert _titles(slack) == ["Deploy the gateway"]
        assert provider.stream_calls == 2  # the turn, then the naming turn

    def test_the_naming_turn_sees_the_exchange(self, monkeypatch):
        _slack, _sessions, provider = _run_transport_titling(monkeypatch)
        assert "deploy the gateway please" in provider.title_prompt
        assert "hi" in provider.title_prompt

    def test_a_restricted_session_is_not_titled(self, monkeypatch):
        """A temporary/incognito session persists nothing, so there is nothing to
        name and no background turn to spend on it.

        Mutation: drop the ``not _is_slack_restricted(...)`` clause — red.
        """
        slack, _sessions, provider = _run_transport_titling(monkeypatch, restricted=True)
        assert _titles(slack) == []
        assert provider.stream_calls == 1

    def test_a_second_turn_does_not_retitle(self, monkeypatch):
        """The claim is shared, so the conversation is named once.

        Mutation: replace ``auto_title.try_claim`` with ``mark_titled`` — red.
        """
        slack, _sessions, provider = _run_transport_titling(monkeypatch)
        slack2, _s2, provider2 = _run_transport_titling(monkeypatch)
        assert _titles(slack) == ["Deploy the gateway"]
        assert _titles(slack2) == []
        assert provider2.stream_calls == 1

    def test_the_native_claim_suppresses_the_transport_one(self, monkeypatch):
        """One tracker across both paths: a thread the native loop already
        claimed is not titled again here.

        Mutation: give ``auto_title`` a per-module tracker per channel — red.
        """
        _handler._mark_titled(canonical_key(_MSG_TS), "manual")
        slack, _sessions, provider = _run_transport_titling(monkeypatch)
        assert _titles(slack) == []
        assert provider.stream_calls == 1

    def test_an_auto_title_dispatch_failure_does_not_record_a_failure(self, monkeypatch):
        """Bookkeeping isolation, same contract as every other step here.

        Mutation: remove the ``try/except`` around the auto-title dispatch — red,
        because the raise falls through to the outer handler and the
        already-successful turn is re-recorded as a failure.
        """
        calls = {"success": 0, "failure": 0}

        class _TrackSessions(_TitleSessions):
            def record_success(self, key):
                calls["success"] += 1

            async def record_failure(self, key):
                calls["failure"] += 1

        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

        def _boom(_key):
            raise RuntimeError("tracker exploded")

        monkeypatch.setattr(transport_dispatch.auto_title, "try_claim", _boom)

        slack = RecordingSlackClient()
        sessions = _TrackSessions(
            ScriptedProvider(
                [
                    make_event(EVENT_TEXT_CHUNK, text="hi"),
                    make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
                ]
            )
        )
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
        assert calls == {"success": 1, "failure": 0}


class TestTransportTrustedBotErrorSuppression:
    """Echo-loop guard parity with native handle_message.

    A failed turn on a trusted-bot message must NOT post the transport error
    reply: in a mutual-mesh setup the reply is itself a bot-authored event the
    peer admits, so replying opens an unbounded error-reply ping-pong.
    """

    def _run_failing_turn(self, monkeypatch, *, from_trusted_bot: bool):
        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "")
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

        # Deterministic failure inside the outer try: renderer construction
        # raises before any session work.
        def _boom(*a, **k):
            raise RuntimeError("forced turn failure")

        monkeypatch.setattr(transport_dispatch, "SlackRenderer", _boom)

        slack = RecordingSlackClient()
        provider = ScriptedProvider([])
        sessions = _CapturingSessions(provider)
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="hello",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="B_TRUSTED" if from_trusted_bot else "U_OWNER",
                context_builder=None,
                conversation_log=None,
                from_trusted_bot=from_trusted_bot,
            )
        )
        errors = [
            kw["text"]
            for method, kw in slack.transcript
            if method == "post_message" and "Something went wrong" in kw.get("text", "")
        ]
        status_clears = [kw for method, kw in slack.transcript if method == "set_thread_status"]
        return errors, status_clears

    def test_error_reply_suppressed_for_trusted_bot(self, monkeypatch):
        errors, status_clears = self._run_failing_turn(monkeypatch, from_trusted_bot=True)
        assert errors == []
        # The thread status must still be cleared: suppression covers only the
        # error MESSAGE, or a stale "working" status pins to the thread forever.
        assert any(kw.get("status") == "" for kw in status_clears)

    def test_error_reply_posted_for_human_sender(self, monkeypatch):
        errors, status_clears = self._run_failing_turn(monkeypatch, from_trusted_bot=False)
        assert len(errors) == 1
        assert any(kw.get("status") == "" for kw in status_clears)


class TestTransportPartialProgressRescue:
    """A turn killed mid-flight must persist what the model already produced.

    Otherwise the partial output lives only in the renderer, and every retry
    re-reads a transcript that ends at the question and starts over.
    """

    def _run_dying_turn(
        self,
        monkeypatch,
        tmp_path,
        *,
        streamed: str,
        break_user_row: bool = False,
        break_user_row_after_write: bool = False,
        finish_first: bool = False,
    ):
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "sales-agent")
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

        # Stream some text, THEN die — the shape of a real transient fault, and
        # the one the old code discarded. TurnDriver takes the renderer as its
        # second positional arg.
        #
        # The stand-in keeps NO buffer of its own on purpose. The rescue reads the
        # renderer's delivery ledger, so the only way these tests can pass is if
        # the text actually went out through ``_append_stream`` and Slack
        # acknowledged it — a seam wired to nothing, or wired to text that was
        # merely produced, fails here.
        class _DyingDriver:
            def __init__(self, client, renderer, *a, **k):
                self._renderer = renderer

            async def run(self, message):
                if streamed:
                    await self._renderer.on_text_chunk(streamed)
                if finish_first:
                    # The reply COMPLETED, then something downstream faulted (a
                    # footer post that 4xxs). Same ``except`` branch, but the
                    # transcript is whole.
                    await self._renderer.on_done()
                raise RuntimeError("backend died mid-stream")

        monkeypatch.setattr(transport_dispatch, "TurnDriver", _DyingDriver)

        slack = RecordingSlackClient()
        sessions = _CapturingSessions(ScriptedProvider([]))
        log = ConversationLog(base_dir=tmp_path)
        if break_user_row:
            # Reproduce the real shape of the lost receipt: the FIRST user-row
            # append raises (a history lock timeout held by a concurrent
            # writer), so ``_logged_user_turn`` stays False. One-shot on
            # purpose — the lock is transient, and the rescue's own write has to
            # be allowed to succeed or the test would prove nothing about it.
            _real_append = log.append
            _broke: list[bool] = []

            def _append(key, role, content, **kw):
                if role == "user" and not _broke:
                    _broke.append(True)
                    raise RuntimeError("history lock timeout")
                return _real_append(key, role, content, **kw)

            monkeypatch.setattr(log, "append", _append)
        if break_user_row_after_write:
            # The other half of the same ambiguity, and the one that makes a
            # two-row rescue corrupting: the row IS written and the append then
            # raises. ``ConversationLog.append`` really does work in this order —
            # write, invalidate caches, ``_maybe_rotate`` — and only the ``stat``
            # inside the rotation is guarded, so an oversized transcript whose
            # rewrite faults lands here. ``_logged_user_turn`` stays False even
            # though the question is durable.
            _real_append_2 = log.append
            _broke_2: list[bool] = []

            def _append_after(key, role, content, **kw):
                result = _real_append_2(key, role, content, **kw)
                if role == "user" and not _broke_2:
                    _broke_2.append(True)
                    raise RuntimeError("transcript rotation failed after append")
                return result

            monkeypatch.setattr(log, "append", _append_after)
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="reconcile the two ledgers",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=None,
                conversation_log=log,
            )
        )
        rows = []
        for path in sorted(Path(tmp_path).rglob("*.jsonl")):
            for line in path.read_text().splitlines():
                if line.strip():
                    rows.append(__import__("json").loads(line))
        return rows, slack

    def test_partial_reply_is_persisted_with_continue_marker(self, monkeypatch, tmp_path):
        from kiro_crew.slack.renderer import PARTIAL_TURN_MARKER

        rows, slack = self._run_dying_turn(
            monkeypatch, tmp_path, streamed="Analysis so far: the first two checks passed."
        )
        assistant = [r for r in rows if r.get("role") == "assistant"]
        assert len(assistant) == 1, "the partial reply should be rescued exactly once"
        content = assistant[0]["content"]
        # The established finding survives for the retry to build on...
        assert "the first two checks passed" in content
        # ...and is unambiguously flagged as cut off, so the next turn continues
        # instead of treating the work as already reported.
        assert content.endswith(PARTIAL_TURN_MARKER)
        # The failure is still surfaced to the user — rescue is additive.
        assert any(
            "Something went wrong" in kw.get("text", "")
            for method, kw in slack.transcript
            if method == "post_message"
        )

    def test_no_assistant_row_when_nothing_was_streamed(self, monkeypatch, tmp_path):
        """A turn that died before producing any text must not persist an empty
        assistant row — that would fabricate a reply the model never made."""
        rows, _ = self._run_dying_turn(monkeypatch, tmp_path, streamed="")
        assert [r for r in rows if r.get("role") == "assistant"] == []
        # The question itself is still durable (unchanged pre-existing behavior).
        assert [r for r in rows if r.get("role") == "user"]

    def test_no_rescue_when_the_user_receipt_outcome_is_unknown(self, monkeypatch, tmp_path):
        """A receipt append that RAISED leaves the row's fate unknowable, so the
        rescue must write nothing rather than guess.

        A rotation fault in ``ConversationLog.append`` raises with the row durable;
        the lock timeout simulated here raises with nothing written. Writing both
        rows would duplicate the question; the assistant row alone would orphan it.
        """
        rows, _ = self._run_dying_turn(
            monkeypatch,
            tmp_path,
            streamed="Checked the first two ledgers; both reconcile.",
            break_user_row=True,
        )
        assert [r for r in rows if r.get("role") == "assistant"] == [], (
            "an assistant row here would either follow a duplicated question or "
            "have no question at all"
        )
        # And the rescue must not have invented a second copy of the question.
        assert [r.get("role") for r in rows].count("user") <= 1

    def test_rescue_never_duplicates_a_question_the_receipt_already_wrote(
        self, monkeypatch, tmp_path
    ):
        """The receipt append can raise with the row ALREADY on disk.

        ``append`` rotates after writing, so a rotation fault raises after the
        question is durable and ``_logged_user_turn`` stays False. Reading that flag
        as "no question on disk" would append a second copy of the prompt.
        """
        rows, _ = self._run_dying_turn(
            monkeypatch,
            tmp_path,
            streamed="Checked the first two ledgers; both reconcile.",
            break_user_row_after_write=True,
        )
        user_rows = [r for r in rows if r.get("role") == "user"]
        assert len(user_rows) == 1, "the question must appear exactly once"
        assert user_rows[0]["content"] == "reconcile the two ledgers"
        assert [r for r in rows if r.get("role") == "assistant"] == []

    def test_rescued_text_keeps_its_leading_indentation(self, monkeypatch, tmp_path):
        """Indentation is content in a transcript, so the rescue persists raw.

        Stripping is only how emptiness is decided. A fenced block or a nested
        list that loses its left margin changes meaning, and neither
        success-path write trims it, so the failure path must not either.
        """
        streamed = "    indented line\n\ttabbed line"
        rows, _ = self._run_dying_turn(monkeypatch, tmp_path, streamed=streamed)
        assistant = [r for r in rows if r.get("role") == "assistant"]
        assert len(assistant) == 1
        assert assistant[0]["content"].startswith(streamed)

    def test_a_completed_reply_is_not_persisted_as_partial(self, monkeypatch, tmp_path):
        """A fault AFTER the stream finished must not stamp the cutoff marker.

        A later failure (a footer post that 4xxs) still unwinds through the rescue.
        The marker tells the next turn to resume, so it would redo finished work.
        """
        from kiro_crew.slack.renderer import PARTIAL_TURN_MARKER

        rows, _ = self._run_dying_turn(
            monkeypatch,
            tmp_path,
            streamed="Both ledgers reconcile; totals match.",
            finish_first=True,
        )
        assert not [
            r
            for r in rows
            if r.get("role") == "assistant" and PARTIAL_TURN_MARKER in r.get("content", "")
        ], "a finished reply must never be flagged as cut off"

    def test_a_completed_reply_gets_no_error_reply(self, monkeypatch, tmp_path):
        """A fault AFTER the answer was delivered must not post the error line.

        The reader already has the whole reply, so "Something went wrong" next to
        it reports a failure that never reached them.
        """
        _, slack = self._run_dying_turn(
            monkeypatch,
            tmp_path,
            streamed="Verdict: the fix is good to ship.",
            finish_first=True,
        )
        assert not [
            kw
            for method, kw in slack.transcript
            if method == "post_message" and "Something went wrong" in kw.get("text", "")
        ]

    def test_the_rescued_row_keeps_its_slack_user_provenance(self, monkeypatch, tmp_path):
        """The rescue must record WHO asked, like every success-path write.

        ``source_user`` is half of ``history.PROVENANCE_FIELDS``; a row missing it
        loses its Slack attribution, so a later rewrite credits the row to the
        dashboard rather than the person who sent the message.
        """
        rows, _ = self._run_dying_turn(
            monkeypatch, tmp_path, streamed="Partial finding worth keeping."
        )
        assistant = [r for r in rows if r.get("role") == "assistant"]
        assert len(assistant) == 1
        assert assistant[0].get("source_user") == "U_OWNER"


# ── Turn verdict after delivery ──
# A turn is booked a success only once its answer-carrying send has landed: a
# failed final send means the reader got no answer, so the turn is a failure.


class _VerdictSessions(FakeSessions):
    """Counts the successes and failures the turn books."""

    def __init__(self, provider):
        super().__init__(provider)
        self.calls = {"success": 0, "failure": 0}

    def record_success(self, session_key):
        self.calls["success"] += 1

    async def record_failure(self, session_key):
        self.calls["failure"] += 1


def _run_verdict_turn(monkeypatch, slack, sessions):
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


def _answer_events(text: str = "the answer") -> list:
    return [
        make_event(EVENT_TEXT_CHUNK, text=text),
        make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
    ]


class _DeliveryFailsSlackClient(RecordingSlackClient):
    """No-stream client: ``start_stream`` returns None, so the renderer finalizes
    a ``_THINKING`` placeholder via ``update_message``, which is the answer's
    delivery on this path. That update fails."""

    def __init__(self) -> None:
        super().__init__()
        self.stream_disabled = True

    async def update_message(self, channel, ts, text="", blocks=None) -> None:
        self._rec("update_message", channel=channel, ts=ts, text=text)
        raise RuntimeError("slack chat.update failed — answer never delivered")


def test_transport_failed_answer_delivery_records_failure_not_success(monkeypatch):
    slack = _DeliveryFailsSlackClient()
    sessions = _VerdictSessions(ScriptedProvider(_answer_events()))

    _run_verdict_turn(monkeypatch, slack, sessions)

    assert sessions.calls["success"] == 0, "booked a success for an undelivered answer"
    assert sessions.calls["failure"] == 1


def test_transport_successful_delivery_still_records_success(monkeypatch):
    slack = RecordingSlackClient()  # streaming enabled, all sends succeed
    sessions = _VerdictSessions(ScriptedProvider(_answer_events()))

    _run_verdict_turn(monkeypatch, slack, sessions)

    assert sessions.calls == {"success": 1, "failure": 0}


class _FooterFailsSlackClient(RecordingSlackClient):
    """Streaming delivers the answer, but the final footer post_blocks fails."""

    async def post_blocks(self, channel, blocks, text, thread_ts=None):
        self._rec("post_blocks", channel=channel, text=text)
        raise RuntimeError("slack post_blocks failed at footer")


def test_transport_footer_failure_keeps_delivered_turn_a_success(monkeypatch):
    # The footer is decoration after finalization; its failure must not turn a
    # received answer into a failed turn.
    slack = _FooterFailsSlackClient()
    sessions = _VerdictSessions(ScriptedProvider(_answer_events("the answer [OPTIONS: a | b]")))

    _run_verdict_turn(monkeypatch, slack, sessions)

    assert sessions.calls == {"success": 1, "failure": 0}


class _TransportOptionsBothFailSlack(RecordingSlackClient):
    """Answer streams fine, but the footer post_blocks and the options fallback
    post_message both fail, so the choices never reach the reader."""

    async def post_blocks(self, channel, blocks, text, thread_ts=None):
        self._rec("post_blocks", channel=channel, text=text)
        raise RuntimeError("footer post_blocks failed")

    async def post_message(self, channel, text, thread_ts=None):
        self._rec("post_message", channel=channel, text=text)
        if text.startswith("*Options:*"):
            raise RuntimeError("options fallback failed too")
        return self._next_ts()


def test_transport_options_footer_and_fallback_both_fail_records_failure(monkeypatch):
    slack = _TransportOptionsBothFailSlack()
    sessions = _VerdictSessions(
        ScriptedProvider(_answer_events("pick one [OPTIONS: Apple | Banana]"))
    )

    _run_verdict_turn(monkeypatch, slack, sessions)

    # The OPTIONS choices are answer-carrying; both deliveries failed.
    assert sessions.calls["success"] == 0, "booked success though the OPTIONS choices never landed"
    assert sessions.calls["failure"] == 1


class _TransportAllAppendsRefusedSlack(RecordingSlackClient):
    """The stream stays live (``start_stream`` keeps succeeding, so a rotation
    never demotes it) but every append is refused, and the fallback
    ``update_message`` fails too: the answer reaches the reader on no sub-path."""

    def __init__(self) -> None:
        super().__init__()
        self.stream_disabled = False

    async def append_stream(self, channel, ts, text) -> bool:
        self._rec("append_stream", channel=channel, ts=ts, text=text)
        return False

    async def update_message(self, channel, ts, text="", blocks=None) -> None:
        # The renderer routes the undelivered answer here; this is the
        # answer-carrying send on the recovery path, so its failure propagates.
        self._rec("update_message", channel=channel, ts=ts, text=text)
        raise RuntimeError("slack chat.update failed — answer never delivered")


def test_transport_stream_all_appends_refused_books_failure(monkeypatch):
    # The renderer's ``on_done`` seal is best-effort, so without the guard the
    # turn would finalize and book success for an answer nobody saw.
    slack = _TransportAllAppendsRefusedSlack()
    sessions = _VerdictSessions(ScriptedProvider(_answer_events("the answer is 42")))

    _run_verdict_turn(monkeypatch, slack, sessions)

    assert sessions.calls["success"] == 0, "booked success for a stream that delivered no answer"
    assert sessions.calls["failure"] == 1


class _TransportAppendsRefusedFallbackRecoversSlack(RecordingSlackClient):
    """Every append is refused, but the fallback ``update_message`` (inherited
    recording default) succeeds and delivers the answer."""

    def __init__(self) -> None:
        super().__init__()
        self.stream_disabled = False

    async def append_stream(self, channel, ts, text) -> bool:
        self._rec("append_stream", channel=channel, ts=ts, text=text)
        return False


def test_transport_stream_all_appends_refused_but_fallback_delivers_stays_success(monkeypatch):
    slack = _TransportAppendsRefusedFallbackRecoversSlack()
    sessions = _VerdictSessions(ScriptedProvider(_answer_events("the answer is 42")))

    _run_verdict_turn(monkeypatch, slack, sessions)

    assert sessions.calls == {"success": 1, "failure": 0}
    fallback_texts = [p.get("text", "") for m, p in slack.transcript if m == "update_message"]
    assert any(
        "the answer is 42" in t for t in fallback_texts
    ), f"fallback update never carried the answer: {fallback_texts}"


class _OptionsFooterCancelsSlackClient(RecordingSlackClient):
    """The answer body streams, but the OPTIONS footer ``post_blocks`` raises
    CancelledError."""

    async def post_blocks(self, channel, blocks, text, thread_ts=None):
        raise asyncio.CancelledError()


def test_transport_options_footer_cancellation_books_failure(monkeypatch):
    # CancelledError skips ``except Exception``; the dispatcher still books a
    # failure, matching the native handler's teardown, so the turn has a verdict.
    slack = _OptionsFooterCancelsSlackClient()
    sessions = _VerdictSessions(
        ScriptedProvider(_answer_events("pick one [OPTIONS: alpha | beta]"))
    )

    with pytest.raises(asyncio.CancelledError):
        _run_verdict_turn(monkeypatch, slack, sessions)

    assert (
        sessions.calls["success"] == 0
    ), "booked success for an OPTIONS turn whose choices never delivered"
    assert sessions.calls["failure"] == 1


class _CancelledMidAnswerProvider(ScriptedProvider):
    """Writes part of an answer, then the turn task is cancelled (``!stop``)."""

    async def stream(self, message: str):
        self.stream_calls += 1
        yield make_event(EVENT_TEXT_CHUNK, text="The first half of the answer.")
        raise asyncio.CancelledError()


def test_a_cancelled_turn_still_shows_the_text_it_was_holding(monkeypatch):
    # The renderer holds the reply until the turn ends, and a cancelled turn
    # never reaches ``on_done``. What the model wrote goes out before the
    # cancellation propagates, as it did when it streamed live.
    slack = RecordingSlackClient()
    sessions = _VerdictSessions(_CancelledMidAnswerProvider([]))

    with pytest.raises(asyncio.CancelledError):
        _run_verdict_turn(monkeypatch, slack, sessions)

    shown = " ".join(
        p.get("text", "") for m, p in slack.transcript if m in ("append_stream", "update_message")
    )
    # Everything the driver released; its redactor's last held run ("answer.")
    # dies with the driver, on this path as on the exception path.
    assert "The first half of the" in shown, slack.transcript
    assert sessions.calls["failure"] == 1


class _NoTerminalProvider(ScriptedProvider):
    """Writes an answer, then the stream ends with no completion event."""

    async def stream(self, message: str):
        self.stream_calls += 1
        yield make_event(EVENT_TEXT_CHUNK, text="Here is the full answer you asked for.")


def test_a_stream_with_no_terminal_still_shows_its_reply(monkeypatch):
    # No terminal means no ``on_done``, so the reply the renderer holds is
    # released after the run: the thread shows what the transcript records.
    slack = RecordingSlackClient()
    sessions = _VerdictSessions(_NoTerminalProvider([]))

    _run_verdict_turn(monkeypatch, slack, sessions)

    shown = " ".join(
        p.get("text", "") for m, p in slack.transcript if m in ("append_stream", "update_message")
    )
    assert "Here is the full answer you asked for." in shown, slack.transcript


# ── Voice reply on the transport path ──────────────────────────────────

#: Past the fifty-character floor below which ``_reply_by_voice`` stays silent.
_SPOKEN_ANSWER = "The gateway restarted cleanly and every channel reconnected within a minute."


def _run_transport_voice(
    monkeypatch,
    voice_config,
    *,
    answer=_SPOKEN_ANSWER,
    stop_reason=STOP_REASON_END_TURN,
    channel="C1",
    **turn_kwargs,
):
    """Drive one transport turn under *voice_config* and drain its voice task.

    Synthesis is stubbed at ``voice_reply``'s entry point: the stub records what it
    was asked to speak and where, then uploads a clip through the real Slack
    uploader, so the recorded transcript shows whether a clip reached Slack.
    """
    from kiro_crew import voice_reply as vr

    monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
    monkeypatch.setattr(
        transport_dispatch,
        "_hydrate_thread_overrides",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
    monkeypatch.setattr(transport_dispatch, "_thread_agents", {})
    monkeypatch.setattr(_handler, "_vc", voice_config)
    monkeypatch.setattr(_handler, "_tts_available", lambda **kw: True)
    spoken: list[dict] = []

    async def _synthesize_and_upload(slack, channel, thread_ts, text, **kw):
        spoken.append({"channel": channel, "thread_ts": thread_ts, "text": text})
        return await vr.upload_voice_to_slack(slack, channel, thread_ts, "reply.wav")

    monkeypatch.setattr(_handler, "_voice_reply_fn", _synthesize_and_upload)

    slack = RecordingSlackClient()
    sessions = _TitleSessions(
        ScriptedProvider(
            [
                make_event(EVENT_TEXT_CHUNK, text=answer),
                make_event(EVENT_COMPLETE, stop_reason=stop_reason),
            ]
        )
    )

    async def _drive():
        await transport_dispatch.handle_message_transport(
            slack=slack,
            sessions=sessions,
            channel=channel,
            text="how did the restart go?",
            thread_ts=None,
            msg_ts=_MSG_TS,
            user_id="U_OWNER",
            context_builder=None,
            conversation_log=None,
            **turn_kwargs,
        )
        # The voice reply is fire-and-forget through handler's tracked-task set,
        # so draining this loop's tasks there is the deterministic join point.
        loop = asyncio.get_running_loop()
        pending = [t for t in list(_handler._background_tasks) if t.get_loop() is loop]
        if pending:
            await await_until(
                asyncio.gather(*pending, return_exceptions=True),
                what="this loop's voice reply tasks to finish",
            )

    asyncio.run(_drive())
    return slack, spoken


def _clips(slack):
    return [kw["filename"] for (m, kw) in slack.transcript if m == "upload_file"]


class TestTransportVoiceReply:
    """A finished transport turn is spoken when a voice reply is wanted.

    ``messaging.use_transport`` defaults True, so this is the path a default
    install speaks through.
    """

    @pytest.mark.parametrize(
        ("voice_settings", "turn_kwargs"),
        [
            pytest.param({"global_enabled": True}, {}, id="test_global_voice_reply"),
            pytest.param(
                {"sessions": {canonical_key(_MSG_TS)}}, {}, id="test_a_thread_with_voice_on"
            ),
            pytest.param(
                {"auto_reply_to_voice": True},
                {"had_voice_input": True},
                id="test_a_voice_memo_with_auto_reply_to_voice",
            ),
        ],
    )
    def test_each_opt_in_speaks_the_answer_into_its_thread(
        self, monkeypatch, voice_settings, turn_kwargs
    ):
        """Mutation: delete the voice step in ``handle_message_transport`` -- red."""
        voice_config = _handler._VoiceConfig(provider="polly", **voice_settings)
        slack, spoken = _run_transport_voice(monkeypatch, voice_config, **turn_kwargs)
        assert spoken == [{"channel": "C1", "thread_ts": _MSG_TS, "text": _SPOKEN_ANSWER}]
        assert _clips(slack) == ["voice-reply.wav"]
        # The clip follows the answer it speaks.
        methods = [m for (m, _kw) in slack.transcript]
        assert methods.index("append_stream") < methods.index("upload_file")

    def test_a_voice_memo_without_its_signal_is_not_spoken(self, monkeypatch):
        """``auto_reply_to_voice`` answers a voice memo only, so a typed message in
        the same configuration stays text."""
        slack, spoken = _run_transport_voice(
            monkeypatch, _handler._VoiceConfig(auto_reply_to_voice=True, provider="polly")
        )
        assert spoken == []
        assert _clips(slack) == []

    def test_the_clip_speaks_what_slack_showed(self, monkeypatch):
        """The OPTIONS trailer becomes buttons, never words the reader hears.

        Mutation: pass the driver's raw ``accumulated`` as the spoken text -- red.
        """
        _slack, spoken = _run_transport_voice(
            monkeypatch,
            _handler._VoiceConfig(global_enabled=True, provider="polly"),
            answer=f"{_SPOKEN_ANSWER}\n\n[OPTIONS: Restart again | Leave it]",
        )
        assert [s["text"] for s in spoken] == [_SPOKEN_ANSWER]

    def test_a_cancelled_turn_is_not_spoken(self, monkeypatch):
        """Mutation: drop the cancelled-turn clause from the voice gate -- red."""
        slack, spoken = _run_transport_voice(
            monkeypatch,
            _handler._VoiceConfig(global_enabled=True, provider="polly"),
            stop_reason=STOP_REASON_CANCELLED,
        )
        assert spoken == []
        assert _clips(slack) == []

    @pytest.mark.parametrize("stop_reason", [STOP_REASON_TIMEOUT, STOP_REASON_STALE_RECOVER])
    def test_an_unfinished_turn_is_not_spoken(self, monkeypatch, stop_reason):
        """A turn the backend never finished shows its partial answer and the
        unfinished-turn notice, which is not an answer to speak.

        Mutation: drop the unfinished-turn clause from the voice gate -- red.
        """
        slack, spoken = _run_transport_voice(
            monkeypatch,
            _handler._VoiceConfig(global_enabled=True, provider="polly"),
            stop_reason=stop_reason,
        )
        shown = "\n".join(str(v) for (_m, kw) in slack.transcript for v in kw.values())
        assert "did not finish its answer" in shown, shown
        assert spoken == []
        assert _clips(slack) == []

    def test_a_top_level_flat_dm_clip_lands_at_channel_root(self, monkeypatch):
        """A single-session DM answers a top-level message at channel root, so its
        clip goes there too rather than into a thread the answer is not in.

        Mutation: pass ``reply_ts`` instead of the posting thread -- red.
        """
        slack, spoken = _run_transport_voice(
            monkeypatch,
            _handler._VoiceConfig(global_enabled=True, provider="polly"),
            channel="D0AP0870FFH",
            dm_single_session=True,
        )
        assert spoken == [{"channel": "D0AP0870FFH", "thread_ts": "", "text": _SPOKEN_ANSWER}]
        assert _clips(slack) == ["voice-reply.wav"]

    def test_a_voice_reply_failure_does_not_record_a_failure(self, monkeypatch):
        """Bookkeeping isolation, same contract as every other step after the run.

        Mutation: remove the ``try/except`` around the voice step -- red, because
        the raise reaches the outer handler and re-records the turn as a failure.
        """
        calls = {"success": 0, "failure": 0}

        class _TrackSessions(_TitleSessions):
            def record_success(self, key):
                calls["success"] += 1

            async def record_failure(self, key):
                calls["failure"] += 1

        async def _boom(*a, **k):
            raise RuntimeError("tts exploded")

        # Resolved here, not at module top, so a missing module fails this test
        # alone instead of the whole file's collection.
        from kiro_crew.slack import transport_voice

        monkeypatch.setattr(transport_voice, "_reply_by_voice", _boom)
        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})
        sessions = _TrackSessions(
            ScriptedProvider(
                [
                    make_event(EVENT_TEXT_CHUNK, text=_SPOKEN_ANSWER),
                    make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
                ]
            )
        )
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=RecordingSlackClient(),
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
        assert calls == {"success": 1, "failure": 0}
