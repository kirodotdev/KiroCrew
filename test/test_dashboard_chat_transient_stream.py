"""Dashboard chat: transient stream failures in ``_run_chat`` — retry budget, auth, connection drops."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from test_dashboard_chat import TestRunChatTransientRetry as _TransientSuite


class TestRunChatTransientRetryStreamFailure:
    """TestRunChatTransientRetry: pre- and post-token failures, the retry budget, connection drops."""

    _TRANSIENT = _TransientSuite._TRANSIENT
    _AUTH = _TransientSuite._AUTH
    _make_state = staticmethod(_TransientSuite._make_state)
    _wire_sessions = staticmethod(_TransientSuite._wire_sessions)
    _client = staticmethod(_TransientSuite._client)
    _drain_bg = staticmethod(_TransientSuite._drain_bg)
    _err_texts = _TransientSuite._err_texts
    _assistant_texts = _TransientSuite._assistant_texts

    @pytest.mark.asyncio
    async def test_transient_pre_token_retries_then_recovers_no_reset(self, tmp_path, monkeypatch):
        """A transient 5xx before any token streams is retried on the SAME live
        session (no reset); the second attempt succeeds."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True  # skip background auto-title

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert call_count == 2  # initial transient failure + one retry
        # Recovered: the response is persisted, no ❌ error surfaced.
        assert any("ok-result" in t for t in self._assistant_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))
        # The transient branch fired (status surfaced) ...
        assert any("Connection unstable" in t for t in self._err_texts(slot))
        # ... and the notice is TAGGED, which is the only thing telling the UI a
        # recovery is pending; without it a stale pill re-runs the queued choice.
        from kiro_crew.dashboard.chat_utils import TRANSIENT_RETRY_KIND

        _hiccups = [
            m
            for m in slot.messages
            if m.get("role") == "error" and "Connection unstable" in m.get("content", "")
        ]
        assert _hiccups, "no hiccup row to inspect"
        assert all((m.get("meta") or {}).get("kind") == TRANSIENT_RETRY_KIND for m in _hiccups)
        # Negative control: a TERMINAL error row must NOT carry the tag, or the
        # discriminator would classify every failure as a pending retry.
        slot.append("error", "❌ terminal for the control", "msg msg-err")
        assert (slot.messages[-1].get("meta") or {}).get("kind") != TRANSIENT_RETRY_KIND
        # ... and the live session was NOT reset.
        state.sessions.reset.assert_not_awaited()
        # Budget reset to 0 after the successful turn.
        assert slot._transient_5xx_retries == 0

    @pytest.mark.asyncio
    async def test_transient_post_token_textonly_retries_once(self, tmp_path, monkeypatch):
        """A transient 5xx AFTER text streamed (no tool call) is retried EXACTLY
        ONCE, APPEND-ONLY: the streamed partial is PRESERVED (finalized as a
        normal assistant message, exactly like the terminal else: branch), a
        recovery notice is surfaced, and a CONTINUE instruction — NOT the
        original prompt — is re-queued onto the same live session. The retry
        appends the continued answer as a NEW message below. The user sees an
        append-only sequence [partial] [recover notice] [continued answer] with
        nothing retracted. No chat_stream_reset event exists anymore."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        call_count = 0
        captured: list = []

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            captured.append(msg)
            if call_count == 1:
                # First attempt streams a partial, then hits a transient 5xx.
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial ")
                raise AcpError(self._TRANSIENT)
            # Retry streams a clean, continued answer.
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert call_count == 2  # one post-token retry
        # The recovery notice surfaced and no ❌ error was raised.
        assert any("Connection unstable" in t for t in self._err_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))
        # APPEND-ONLY: the partial is PRESERVED as a finalized assistant message,
        # AND the continued retry answer is appended below it. Both are present;
        # the live chunk placeholders are finalized away.
        assert not any(m.get("role") == "chunk" for m in slot.messages)
        assistant = self._assistant_texts(slot)
        assert any("partial" in t for t in assistant), "partial must be preserved"
        assert any("ok-result" in t for t in assistant), "continued answer must append below"
        # The RE-QUEUED prompt is the CONTINUE instruction, NOT the original.
        assert len(captured) == 2
        assert (
            "Continue from where it stopped" in captured[1]
        ), "the retry must re-queue the continue instruction, not the original prompt"
        assert "hello" not in captured[1], "the original prompt must NOT be re-queued"
        # No chat_stream_reset broadcast — that frontend reconcile event was
        # removed entirely by the append-only rework.
        assert not any(
            c.args and c.args[0] == "chat_stream_reset" for c in state.broadcast_ws.call_args_list
        ), "chat_stream_reset must no longer be broadcast"
        # Live session was NOT reset. The one-shot allowance stays consumed
        # (True) across the synthetic recovery turn — it is refreshed only at the
        # start of the NEXT genuine user turn, never on the recovery turn itself
        # (finding #3, prevents a recovery-turn re-failure from looping).
        state.sessions.reset.assert_not_awaited()
        assert slot._posttoken_retry_used is True

    @pytest.mark.asyncio
    async def test_transient_post_token_suppressed_preserves_partial(self, tmp_path, monkeypatch):
        """When Stop is active (_should_suppress_requeue True) during a post-token
        transient 5xx, the partial assistant text is PRESERVED (persisted as an
        assistant message) and the message is NOT re-queued. Under the
        append-only design the partial + a retry notice are shown regardless of
        eligibility; only the re-queue is gated."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            # Stream a partial, then hit a transient 5xx (post-token, no tool).
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
            raise AcpError(self._TRANSIENT)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        # Stop is active → suppress the re-queue for the whole handler.
        monkeypatch.setattr(chat_runner, "_should_suppress_requeue", lambda s: True)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # No re-queue: the stream ran exactly once (Stop is active). Finding #3:
        # a suppressed recovery does NOT consume the one-shot allowance — the
        # flag is only set when a recovery is actually enqueued, so it stays
        # False and a later genuine turn can still recover once.
        assert call_count == 1
        assert slot._posttoken_retry_used is False
        # HIGH: the partial survives as a persisted assistant message.
        assert any("partial answer" in t for t in self._assistant_texts(slot))
        # Chunk placeholders were finalized (not left as role "chunk").
        assert not any(m.get("role") == "chunk" for m in slot.messages)
        # The retry notice is shown (append-only: partial + notice regardless of
        # eligibility); no ❌ terminal error is raised here.
        assert any("Connection unstable" in t for t in self._err_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))
        # No chat_stream_reset broadcast — that event was removed entirely.
        assert not any(
            c.args and c.args[0] == "chat_stream_reset" for c in state.broadcast_ws.call_args_list
        )
        # Transient path never resets the (still-alive) session.
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transient_post_toolcall_recovers(self, tmp_path, monkeypatch):
        """A transient 5xx AFTER a TOOL CALL fired RECOVERS instead of failing
        fast. Because the retry re-queues a CONTINUE instruction onto the
        SAME live session — which still holds the completed tool results — the
        model resumes from where it stopped instead of blindly re-running the
        tool. Exactly one post-token recovery fires, the partial is preserved,
        and the continue instruction (not the original prompt) is re-queued."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        call_count = 0
        captured: list = []

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            captured.append(msg)
            if call_count == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="working ")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    text="",
                    title="fs_read",
                    tool_kind="read",
                    tool_call_id="tc-1",
                )
                raise AcpError(self._TRANSIENT)
            # Retry continues from the preserved context and finishes cleanly.
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert call_count == 2  # post-tool transient now RECOVERS via continue
        # Recovery notice surfaced, no ❌ terminal error.
        assert any("Connection unstable" in t for t in self._err_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))
        # The partial text is preserved and the continued answer appended below.
        assert any("working" in t for t in self._assistant_texts(slot)), "partial must be preserved"
        assert any(
            "ok-result" in t for t in self._assistant_texts(slot)
        ), "continued answer must append below"
        # The CONTINUE instruction — not the original prompt — was re-queued.
        assert len(captured) == 2
        assert "Continue from where it stopped" in captured[1]
        assert "hello" not in captured[1]
        # The one-shot allowance stays consumed (True) across the recovery turn;
        # it is refreshed only by the next genuine user turn. Live session never
        # reset.
        assert slot._posttoken_retry_used is True
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_posttoken_suppressed_leaves_allowance_for_later_turn(
        self, tmp_path, monkeypatch
    ):
        """Finding #3(a): a Stop-suppressed post-token transient must NOT consume
        the one-shot allowance (the flag is set only when a recovery is actually
        enqueued). A LATER genuine user turn on the same slot can therefore still
        recover once."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        # ── Turn 1: Stop active → suppressed post-token transient. ──
        async def _stream_suppressed(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial-1")
            raise AcpError(self._TRANSIENT)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream_suppressed)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        monkeypatch.setattr(chat_runner, "_should_suppress_requeue", lambda s: True)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "first question")
            await self._drain_bg(state)

        # Allowance untouched — the suppressed path never enqueued a recovery.
        assert slot._posttoken_retry_used is False
        # And the row says so: nothing resumes, so it is the TERMINAL notice
        # (give-up text + token, no retry kind) — never a pending "resuming…"
        # the dashboard would draw as a soft notice with no way to continue.
        from kiro_crew.dashboard.chat_utils import (
            TRANSIENT_GIVE_UP_TEXT,
            TRANSIENT_NOTICE_GIVE_UP,
            TRANSIENT_RETRY_KIND,
        )

        _terminal = [
            m
            for m in slot.messages
            if m.get("role") == "error" and "Connection unstable" in m.get("content", "")
        ]
        assert len(_terminal) == 1
        assert _terminal[0]["content"] == TRANSIENT_GIVE_UP_TEXT
        assert _terminal[0]["meta"].get("notice") == TRANSIENT_NOTICE_GIVE_UP
        assert _terminal[0]["meta"].get("kind") != TRANSIENT_RETRY_KIND

        # ── Turn 2: Stop cleared → a genuine turn recovers once. ──
        call_count = 0

        async def _stream_recover(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial-2")
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _stream_recover
        client.stream_command = _stream_recover
        monkeypatch.setattr(chat_runner, "_should_suppress_requeue", lambda s: False)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "second question")
            await self._drain_bg(state)

        # The later real turn recovered: it re-ran once and produced the answer.
        assert call_count == 2
        assert any("ok-result" in t for t in self._assistant_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))

    @pytest.mark.asyncio
    async def test_posttoken_recovery_turn_refailure_does_not_loop(self, tmp_path, monkeypatch):
        """Finding #3(b): a post-token transient that re-fires DURING the
        synthetic recovery turn must NOT recover again (no infinite re-queue).
        The recovery turn inherits the already-consumed allowance (it is not
        refreshed for the recover message), so the post-token branch is skipped
        and a clean terminal error surfaces instead of a second recovery."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        captured: list = []

        async def _stream(msg):
            captured.append(msg)
            # The recovery turn emits a partial then hits ANOTHER transient 5xx.
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="still-failing")
            raise AcpError(self._TRANSIENT)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        # Simulate the state left by the originating turn that enqueued recovery.
        slot._posttoken_retry_used = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            # Drive the recovery turn directly with the continue instruction.
            await _run_chat(state, slot, _POSTTOKEN_RECOVER_MSG)
            await self._drain_bg(state)

        # Ran exactly once — no second recovery was enqueued (no loop).
        assert len(captured) == 1
        # The recover instruction was NOT re-queued a second time.
        assert not any(m == _POSTTOKEN_RECOVER_MSG for m in captured[1:])
        # The flag was NOT refreshed by the recovery turn (stays consumed).
        assert slot._posttoken_retry_used is True
        # A clean terminal error surfaced (branch skipped, fell to else:).
        assert any(t.startswith("❌") for t in self._err_texts(slot))
        # The partial from the recovery turn is still preserved (append-only).
        assert any("still-failing" in t for t in self._assistant_texts(slot))
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auth_error_fails_fast_no_retry(self, tmp_path, monkeypatch):
        """An auth failure is excluded from the transient set — it fails fast
        with a clean error and is never retried (a retry can't fix a token)."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._AUTH)
            yield  # pragma: no cover — makes this an async generator

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert call_count == 1  # fail-fast, no retry
        assert slot._transient_5xx_retries == 0
        assert any(t.startswith("❌") for t in self._err_texts(slot))
        assert not any("Connection unstable" in t for t in self._err_texts(slot))
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("partial", ["", "Here is the first half"])
    async def test_codex_provider_failure_renders_as_an_error_row(
        self, tmp_path, monkeypatch, partial
    ):
        """codex-acp writes a terminal provider error as message text and answers
        ``end_turn``. The session handle turns that into the turn's AcpError
        (``test_codex_terminal_error_fidelity``); here the error the handle
        actually raises is fed to the chat loop, which must show an error row,
        keep any partial answer, never store the provider error as assistant
        text, and not retry a 400."""
        from test_codex_terminal_error_fidelity import (
            _PROVIDER_MESSAGE,
            END_TURN,
            ERROR_CHUNK_2_0,
            _handle,
            _run,
        )

        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        _events, failure = await _run(_handle([ERROR_CHUNK_2_0, END_TURN]))
        assert failure is not None
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if partial:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=partial)
            raise failure

        state = self._make_state(tmp_path, monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
        self._wire_sessions(state, self._client(_stream))
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert call_count == 1, "a deterministic 400 was retried"
        errors = self._err_texts(slot)
        assert any(_PROVIDER_MESSAGE in t for t in errors), errors
        assistant = self._assistant_texts(slot)
        assert not any(_PROVIDER_MESSAGE in t for t in assistant), assistant
        assert not any("invalid_request_error" in t for t in assistant), assistant
        if partial:
            assert any(partial in t for t in assistant), assistant
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transient_exhausts_budget_then_clean_error_resumable(
        self, tmp_path, monkeypatch
    ):
        """Persistent transient 5xx is retried up to the budget, then surfaces a
        clean ❌ error while leaving the session resumable (never reset)."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        # fallback_model="" (disabled): this test pins the PRE-FEATURE ladder
        # (the default is now "auto", which would walk the fallback branch —
        # that branch has its own class, TestRunChatModelFallback).
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # Initial attempt + TRANSIENT_RETRIES re-prompts, then it gives up.
        assert call_count == TRANSIENT_RETRIES + 1
        # Clean error surfaced; session left resumable (no reset).
        assert any(t.startswith("❌") for t in self._err_texts(slot))
        state.sessions.reset.assert_not_awaited()
        # The terminal ❌ ENDS the cycle, so the budget is refreshed for the
        # next one — the Continue press the error message invites must get the
        # full ladder again, not inherit an exhausted counter.
        assert slot._transient_5xx_retries == 0

    @staticmethod
    def _connection_drop():
        """A network-path drop raised through the real ACP raise path."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.acp.transport_errors import _raise_acp_error

        try:
            _raise_acp_error(
                {
                    "code": -32603,
                    "message": "Internal error",
                    "data": "dispatch failure: io error: connection reset by peer",
                }
            )
        except AcpError as exc:
            assert exc.connection_failure is True
            return exc
        raise AssertionError("_raise_acp_error did not raise")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("synthetic", [False, True], ids=["user-turn", "recovery-turn"])
    async def test_connection_drop_shorter_than_window_recovers(
        self, tmp_path, monkeypatch, synthetic
    ):
        """A network drop that outlasts the fixed TRANSIENT_RETRIES count but
        ends inside the recovery window resumes the turn on the SAME session,
        with one standing retry notice rather than one per extra attempt. A
        runner recovery turn replays as a different inject kind and gets the
        same single notice."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_utils import (
            _SYNTHETIC_RECOVERY_MSGS,
            TRANSIENT_NOTICE_META_KEY,
            TRANSIENT_NOTICE_RETRYING,
        )
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        drop_attempts = TRANSIENT_RETRIES + 3
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= drop_attempts:
                raise self._connection_drop()
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="back-online")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        sleeps: list[float] = []

        async def _fake_sleep(secs, *a, **k):
            sleeps.append(secs)

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            if synthetic:
                await _run_chat(
                    state,
                    slot,
                    _SYNTHETIC_RECOVERY_MSGS[0],
                    _synthetic_recovery_turn=True,
                    _synthetic_payload=True,
                )
            else:
                await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert call_count == drop_attempts + 1
        assert any("back-online" in t for t in self._assistant_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))
        state.sessions.reset.assert_not_awaited()
        retrying = [
            m
            for m in slot.messages
            if (m.get("meta") or {}).get(TRANSIENT_NOTICE_META_KEY) == TRANSIENT_NOTICE_RETRYING
        ]
        assert len(retrying) == TRANSIENT_RETRIES
        # The landed turn hands the whole ladder back.
        assert slot._transient_5xx_retries == 0

    @pytest.mark.asyncio
    async def test_connection_drop_longer_than_window_fails_clearly(self, tmp_path, monkeypatch):
        """A drop that outlasts the recovery window ends in the same clean,
        resumable ❌ as an exhausted provider-error ladder. Elapsed time comes
        from one fake monotonic clock that both modules read and each attempt
        advances, so the result does not depend on the host's uptime."""
        import types

        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import CONNECTION_RECOVERY_WINDOW_SECS, TRANSIENT_RETRIES

        step = CONNECTION_RECOVERY_WINDOW_SECS / 5
        clock = {"now": 10_000.0}
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                clock["now"] += step
            raise self._connection_drop()
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
        import kiro_crew.dashboard.chat_runner as runner_mod
        import kiro_crew.llm_helpers as helpers_mod

        for mod in (runner_mod, helpers_mod):
            fake = types.SimpleNamespace(
                **{k: getattr(mod.time, k) for k in dir(mod.time) if not k.startswith("__")}
            )
            fake.monotonic = lambda: clock["now"]
            monkeypatch.setattr(mod, "time", fake)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # Attempt N sees N-1 steps elapsed; the window closes at five steps,
        # so the sixth attempt is the last, two beyond the fixed count.
        assert call_count == 6
        assert call_count > TRANSIENT_RETRIES + 1
        # The terminal row names the network as the cause, not a retry notice.
        errs = self._err_texts(slot)
        assert errs and "Could not reach the model backend" in errs[-1]
        state.sessions.reset.assert_not_awaited()
        assert slot._transient_5xx_retries == 0

    @pytest.mark.asyncio
    async def test_provider_5xx_keeps_fixed_budget_inside_window(self, tmp_path, monkeypatch):
        """A provider 5xx answer is not a network drop: it keeps the fixed
        TRANSIENT_RETRIES count even while the recovery window is open."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            exc = AcpError(self._TRANSIENT, transient=True)
            exc.connection_failure = False
            raise exc
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert call_count == TRANSIENT_RETRIES + 1
        assert any(t.startswith("❌") for t in self._err_texts(slot))

    @pytest.mark.asyncio
    async def test_transient_budget_refreshed_after_terminal_error(self, tmp_path, monkeypatch):
        """Regression: a cycle that EXHAUSTS the transient budget and surfaces
        the terminal ❌ must refresh the budget for the NEXT cycle. The
        happy-path reset only runs when a cycle COMPLETES, so before the fix
        the exhausted counter leaked into every later cycle: the very next
        5xx — e.g. right after the Continue press the ❌ message itself
        invites ("retry in a moment") — failed instantly with ZERO retries
        until some turn happened to finish cleanly."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        # ── Cycle 1: persistent 5xx exhausts the budget → terminal ❌. ──
        async def _always_fail(msg):
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_always_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "first question")
            await self._drain_bg(state)

        assert any(t.startswith("❌") for t in self._err_texts(slot))
        assert slot._transient_5xx_retries == 0
        n_before = len(slot.messages)

        # ── Cycle 2: a 5xx on the next turn retries again (fresh budget). ──
        call_count = 0

        async def _fail_once_then_ok(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _fail_once_then_ok
        client.stream_command = _fail_once_then_ok

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "second question")
            await self._drain_bg(state)

        # Before the fix: 3 < TRANSIENT_RETRIES was False on the first 5xx, so
        # cycle 2 died instantly (call_count == 1, a second ❌, no answer).
        assert call_count == 2
        tail = slot.messages[n_before:]
        tail_errs = [m["content"] for m in tail if m.get("role") == "error"]
        tail_answers = [m["content"] for m in tail if m.get("role") == "assistant"]
        assert any("Connection unstable" in t for t in tail_errs)
        assert not any(t.startswith("❌") for t in tail_errs)
        assert any("ok-result" in t for t in tail_answers)
        # Completed cycle → budget back to 0 via the happy-path reset.
        assert slot._transient_5xx_retries == 0
        state.sessions.reset.assert_not_awaited()
