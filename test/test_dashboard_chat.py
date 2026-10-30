"""Dashboard chat: transient backend retry, throttle model fallback and the ACP-death redaction
of partial text in ``_run_chat``.

``TestRunChatTransientRetry`` keeps its thinking-only retry tests and the shared stream
harness (``_make_state``, ``_wire_sessions``, ``_client``, ``_drain_bg``) that the
``test_dashboard_chat_transient_*`` siblings, ``TestFallbackServedTurnAttribution``
and ``test_turn_stats.py`` reuse; ``TestAcpProcessDiedRecovery`` keeps its two redaction
tests and the harness ``test_dashboard_chat_provider_recovery.py`` reuses.
``TestFallbackServedTurnAttribution`` pins the model a fallback-served turn's usage row
names.

The other dashboard chat tests live in the sibling ``test_dashboard_chat_*.py``
files, one topic each; shared fakes are in ``dashboard_chat_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state
from dashboard_chat_helpers import _provider_mock

from kiro_crew.testing.wait import await_until, until_parked

# ── Tests: AcpProcessDied handler in _run_chat ──


class TestAcpProcessDiedRecovery:
    """Verify _run_chat handles AcpProcessDied with retry logic, redaction, and session reset."""

    def _make_state_and_slot(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import _run_chat

        state = _make_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        slot = state.get_or_create_slot("pipe-death-slot")
        slot.append("user", "hello", "msg msg-u")

        mock_client = state.sessions.get_or_create.return_value[0]
        mock_client.shutdown = AsyncMock()
        return state, slot, mock_client, _run_chat

    def _make_stream_raise(self, mock_client, exc):
        async def _raise(msg):
            raise exc
            yield  # noqa: E501

        mock_client.stream = _raise
        mock_client.stream_command = _raise

    @pytest.mark.asyncio
    async def test_partial_assistant_text_redacted(self, tmp_path: Path) -> None:
        """Pipe death mid-stream preserves redacted output and queues a continuation."""
        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.dashboard.chat_utils import (
            _CONN_RECOVER_MSG,
            SYNTHETIC_RECOVERY_KIND,
            RecoveryPayload,
        )
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._titled = True

        async def _stream_then_die(msg):
            yield LLMEvent(
                kind=EVENT_TEXT_CHUNK, text="partial output with AKIA1234567890ABCDEF secret"
            )
            raise AcpProcessDied("pipe broken")

        client.stream = _stream_then_die
        client.stream_command = _stream_then_die

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await _run_chat(state, slot, "test message")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert assistant_msgs, "Expected at least one assistant message with redacted content"
        for m in assistant_msgs:
            assert "AKIA1234567890ABCDEF" not in m.get("content", "")
        assert slot._queue == [
            {
                "id": slot._queue[0]["id"],
                "content": _CONN_RECOVER_MSG,
                "kind": SYNTHETIC_RECOVERY_KIND,
                # A continuation, not the user's request: the turn had emitted, so
                # this text is the runner's and must not mirror as user speech.
                "payload": RecoveryPayload.CONTINUATION,
                # Admission stamp: recovery requeues record the containment
                # that held at requeue so the drain can re-validate the retry.
                "meta": slot._queue[0]["meta"],
            }
        ]

    @pytest.mark.asyncio
    async def test_cancelled_error_redacts_partial_text(self, tmp_path: Path) -> None:
        """CancelledError mid-stream → partial output redacted before display."""
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _stream_then_cancel(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial with AKIA1234567890ABCDEF key")
            raise asyncio.CancelledError()

        client.stream = _stream_then_cancel
        client.stream_command = _stream_then_cancel

        await _run_chat(state, slot, "test message")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert assistant_msgs, "Expected at least one assistant message with redacted content"
        for m in assistant_msgs:
            assert "AKIA1234567890ABCDEF" not in m.get("content", "")


# ── Transient backend 5xx retry on the interactive chat path ──


class TestRunChatTransientRetry:
    """The interactive _run_chat stream loop reuses the llm_helpers transient
    classifier + backoff to retry a transient
    backend 5xx WITHOUT resetting the live session, guarded so a partial
    (already-streamed) response is never re-run."""

    _TRANSIENT = "Prompt error: {'message': 'Internal error: API Error: Internal server error'}"
    _AUTH = "Bedrock authentication failed. Run 'ada credentials update'"

    @staticmethod
    def _make_state(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.push_refresh = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @staticmethod
    def _wire_sessions(state, client):
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()
        state.sessions.release = MagicMock()
        # reset is an AsyncMock so we can assert it is NEVER awaited — a
        # transient 5xx must NOT reset the (still-alive) session.
        state.sessions.reset = AsyncMock()
        # discard_conversation is the poisoned-conversation escalation (clears
        # the resume sid, keeps the session-map entry with its channel
        # linkage); mocked so tests can assert exactly when it fires.
        state.sessions.discard_conversation = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))

    @staticmethod
    def _client(stream):
        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        # These are sync accessors on the real provider client; _run_chat
        # calls them without awaiting, so a bare AsyncMock attribute here
        # would leave an unawaited coroutine per turn.
        client.context_window_tokens = MagicMock(return_value=0)
        client.context_used_tokens = MagicMock(return_value=0)
        client.mcp_session_report = MagicMock(return_value=None)
        client.pop_pending_oauth_requests = MagicMock(return_value=[])
        client.available_models = MagicMock(return_value=[])
        # _drain_session_init_oauth_requests reaches pop_pending_oauth_requests
        # via client.client (the nested ACP client), not the front client
        # itself; leaving client.client as a bare AsyncMock attribute would
        # make that nested lookup resolve to another never-awaited coroutine.
        client.client = None
        client.stream = stream
        client.stream_command = stream
        # The poisoned-conversation canary reads the session's served model
        # via the provider's PUBLIC served_model accessor — mock that public
        # seam, not the provider internals it happens to be backed by.
        client.served_model = "claude-test-model"
        return client

    @staticmethod
    async def _drain_bg(state, limit=30):
        """Drive the finally-block re-queue cascade (each retry spawns a
        background _run_chat task) to completion. Bounded by the retry budget."""
        for _ in range(limit):
            pending = [t for t in list(state._background_tasks) if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def _err_texts(self, slot):
        return [m["content"] for m in slot.messages if m.get("role") == "error"]

    def _assistant_texts(self, slot):
        return [m["content"] for m in slot.messages if m.get("role") == "assistant"]

    @staticmethod
    def _record_queue_inserts(monkeypatch, slot) -> list[dict]:
        inserts: list[dict] = []
        repo = slot._queue_repository
        real_insert = repo.queue_insert

        def _record(owner, index, content, kind="", *args, **kw):
            inserts.append({"content": content, "kind": kind})
            return real_insert(owner, index, content, kind, *args, **kw)

        monkeypatch.setattr(repo, "queue_insert", _record)
        return inserts

    _BINDING_MISMATCH = (
        "Invalid `signature` in `thinking` block. "
        "The block is bound to a different conversation."
    )

    @staticmethod
    def _binding_failure():
        from kiro_crew.acp.client import _raise_acp_error

        _raise_acp_error(
            {
                "code": -32603,
                "message": "Prompt failed",
                "data": TestRunChatTransientRetry._BINDING_MISMATCH,
            }
        )

    @pytest.mark.asyncio
    async def test_transient_after_thinking_only_retries(self, tmp_path, monkeypatch):
        """A transient 5xx that lands AFTER reasoning streamed but before any
        answer token or tool call is still retried: thinking is ephemeral,
        broadcast-only output, so it does NOT flip _turn_emitted. This pins the
        deliberate decision in the EVENT_THINKING_CHUNK branch — if thinking
        ever starts being persisted, this guard must become a turn-emit to
        avoid a double-emit."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_THINKING_CHUNK,
            LLMEvent,
        )

        call_count = 0
        thinking_seen = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            # Reasoning streams on BOTH attempts (cosmetic re-stream is the
            # accepted cost of retrying a thinking-only turn).
            yield LLMEvent(kind=EVENT_THINKING_CHUNK, text="let me think...")
            if call_count == 1:
                # Transient 5xx after thinking, before any answer token.
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)

        # Count chat_thinking broadcasts to prove reasoning re-streamed.
        _orig_broadcast = state.broadcast_ws

        def _count_thinking(event, payload, *a, **kw):
            nonlocal thinking_seen
            if event == "chat_thinking":
                thinking_seen += 1
            return _orig_broadcast(event, payload, *a, **kw)

        state.broadcast_ws = MagicMock(side_effect=_count_thinking)

        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # Thinking did NOT block the retry: initial transient + one retry.
        assert call_count == 2
        # Reasoning re-streamed on the retry. The streaming redaction buffer
        # (issue 3) may split/coalesce a thinking chunk across broadcasts, so
        # assert "streamed on both attempts" rather than an exact per-chunk count.
        assert thinking_seen >= 2
        # Recovered cleanly on the live session (no reset, no ❌).
        assert any("ok-result" in t for t in self._assistant_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))
        assert any("Connection unstable" in t for t in self._err_texts(slot))
        state.sessions.reset.assert_not_awaited()
        assert slot._transient_5xx_retries == 0

    @pytest.mark.asyncio
    async def test_thinking_only_failures_never_accrue_discard_streak(self, tmp_path, monkeypatch):
        """A turn that streamed REASONING before dying is a mid-generation
        failure — the backend demonstrably serves this conversation — not the
        poisoned pre-stream signature. Repeated thinking-then-transient-death
        turns must never accrue the discard streak (and so can never reach the
        canary/discard), even though they leave _turn_emitted False for retry
        purposes. Locks in the _turn_thought gate on _prestream_exhausted."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_THINKING_CHUNK, LLMEvent

        async def _think_then_die(msg):
            yield LLMEvent(kind=EVENT_THINKING_CHUNK, text="reasoning...")
            raise AcpError(self._TRANSIENT)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_think_then_die)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            # Well past POISONED_SESSION_CYCLES worth of exhausted ladders.
            for i in range(3):
                await _run_chat(state, slot, f"msg-{i}")
                await self._drain_bg(state)

        # Thinking activity keeps the streak at zero every cycle: never
        # eligible, so no canary probe and no discard — the conversation is
        # being served, just dying mid-generation.
        assert slot._prestream_exhausted_cycles == 0
        assert not slot._poisoned_reset_used
        state.sessions.discard_conversation.assert_not_awaited()


class TestRunChatModelFallback:
    """Throttle-exhaustion model fallback (agent.fallback_model) on the
    interactive path: budget exhausts → substitute set_model → visible notice
    card → re-queue on the SAME live session; empty chain is byte-for-byte
    today's terminal error; the sticky swap is restored at the next genuine
    turn start."""

    _TRANSIENT = "Prompt error: {'message': 'Internal error: API Error: Internal server error'}"

    @pytest.mark.asyncio
    async def test_fallback_swap_refreshes_the_served_model_cache(self, tmp_path, monkeypatch):
        """The swap moves the LIVE session onto the candidate without a spawn,
        so the spawn-time cache would keep naming the primary; it must follow
        the provider's served model instead."""
        from kiro_crew.dashboard.chat_runner import _fallback_swap_for_turn

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.record_served_model("primary-model")

        class _Client:
            served_model = "primary-model"

            async def set_model(self, target):
                self._model = target
                self.served_model = target

        client = _Client()
        candidate = await _fallback_swap_for_turn(slot, client)
        assert candidate == "fallback-model"
        assert slot.served_model == "fallback-model"

    @pytest.mark.asyncio
    async def test_fallback_swap_serialises_on_the_pick_lock(self, tmp_path, monkeypatch):
        """REGRESSION (GPT finding on c97f2f2d): the swap awaits set_model, and
        an explicit pick landing during that await could be overwritten and
        snapshotted as fallback state. The swap must hold _model_pick_lock —
        with the lock pre-acquired, the swap may not advance until release.
        This closes the LAST writer: explicit pick, bulk pick, restore probe,
        and swap all serialise on the same per-slot lock."""
        import asyncio

        from kiro_crew.dashboard.chat_runner import _fallback_swap_for_turn

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        entered = asyncio.Event()

        class _Client:
            async def set_model(self, target):
                entered.set()
                self._model = target

        client = _Client()

        await slot._model_pick_lock.acquire()
        try:
            task = asyncio.create_task(_fallback_swap_for_turn(slot, client))
            # The swap takes the session lock, then parks on the held pick lock;
            # wait until it is parked there rather than guessing with a sleep.
            await until_parked(slot._model_pick_lock, count=1)
            # Lock held ⇒ the swap must not have reached set_model yet.
            assert not entered.is_set()
            assert not task.done()
        finally:
            slot._model_pick_lock.release()
        candidate = await await_until(task, what="the fallback swap finishing")
        assert candidate == "fallback-model"
        assert slot._active_fallback_model == "fallback-model"

    @pytest.mark.asyncio
    async def test_fallback_swap_serialises_on_the_session_lock(self, tmp_path, monkeypatch):
        """The swap awaits set_model and then snapshots the shared client pick
        epoch. The per-slot pick lock is disjoint across aliases, so a pick made
        through a DIFFERENT alias of the same wire session could land in that
        await and be absorbed into the snapshot, after which the restore reads
        not-stale and silently overwrites the user's choice. The swap must hold
        the session-scoped switch lock too — with it pre-acquired, the swap may
        not advance until release. Mirrors the restore probe and refusal path.

        Mutation guard: drop the slot_switch_session_lock acquisition around the
        swap and this test reddens, because the swap reaches set_model while the
        session lock is held elsewhere."""
        import asyncio

        from kiro_crew.dashboard.chat_runner import _fallback_swap_for_turn
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.llm_helpers import slot_switch_session_lock

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        entered = asyncio.Event()

        class _Client:
            async def set_model(self, target):
                entered.set()
                self._model = target

        client = _Client()

        _session_lock = slot_switch_session_lock(effective_session_key(slot))
        await _session_lock.acquire()
        try:
            task = asyncio.create_task(_fallback_swap_for_turn(slot, client))
            # The swap takes the session lock first, so it parks on the held one;
            # wait until it is parked there rather than guessing with a sleep.
            await until_parked(_session_lock, count=1)
            # Session lock held ⇒ the swap must not have reached set_model yet.
            assert not entered.is_set()
            assert not task.done()
        finally:
            _session_lock.release()
        candidate = await await_until(task, what="the fallback swap finishing")
        assert candidate == "fallback-model"
        assert slot._active_fallback_model == "fallback-model"

    @pytest.mark.asyncio
    async def test_stop_during_fallback_backoff_drops_the_requeue(self, tmp_path, monkeypatch):
        """A Stop pressed during the
        fallback backoff sleep resolves while no prompt is active; without the
        post-sleep guard the cancelled prompt is requeued and executes on the
        fallback anyway."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        async def _stop_during_sleep(_secs):
            # Simulate the user pressing Stop while the backoff sleeps: the
            # monotonic generation increments even though the stop resolves
            # back to "idle" before the sleep returns. Scoped to the backoff this
            # test is about, because the same-model arm re-reads the stop signals
            # after ITS wait too: an unscoped press lands on the FIRST backoff,
            # drops the replay there, and the fallback arm this test measures is
            # then never reached at all.
            if slot._fallback_walked:
                slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1

        with patch("asyncio.sleep", new=AsyncMock(side_effect=_stop_during_sleep)):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # Only the primary's budget ran; the cancelled prompt was NOT replayed
        # on the fallback (unfixed code replays it: +FALLBACK_CANDIDATE_ATTEMPTS).
        assert call_count == TRANSIENT_RETRIES + 1
        assert not any(q for q in getattr(slot, "_queue", []) if "hello" in str(q))
        # The drop arm ends the turn, so it must mirror the landed/terminal
        # per-turn resets: a fresh transient budget and a cleared walk (which
        # re-enables the restore probe). Sticky _active_fallback_model
        # deliberately survives for the probe to heal.
        assert slot._transient_5xx_retries == 0
        assert slot._fallback_candidate_idx == 0
        assert slot._fallback_walked == []

    _make_state = staticmethod(TestRunChatTransientRetry._make_state)
    _wire_sessions = staticmethod(TestRunChatTransientRetry._wire_sessions)
    _drain_bg = staticmethod(TestRunChatTransientRetry._drain_bg)

    def _err_texts(self, slot):
        return [m["content"] for m in slot.messages if m.get("role") == "error"]

    def _notice_texts(self, slot):
        return [m["content"] for m in slot.messages if m.get("role") == "notice"]

    def _assistant_texts(self, slot):
        return [m["content"] for m in slot.messages if m.get("role") == "assistant"]

    @staticmethod
    def _client(stream, advertised=("fallback-model",)):
        client = TestRunChatTransientRetry._client(stream)
        client._model = "claude-test-model"
        client.available_models = MagicMock(return_value=[{"modelId": m} for m in advertised])

        async def _set_model(model_id):
            # Mimic the real substitute path's bookkeeping so read_turn_model
            # (and therefore meta.turn_stats.model) reports the candidate.
            client._model = model_id
            client.served_model = model_id

        client.set_model = AsyncMock(side_effect=_set_model)
        return client

    @pytest.mark.asyncio
    async def test_budget_exhaustion_swaps_and_recovers(self, tmp_path, monkeypatch):
        """After the same-model budget exhausts, the session is moved onto the
        chain's candidate, the swap is ANNOUNCED as a persisted notice card,
        the turn recovers, and turn stats record the candidate."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= TRANSIENT_RETRIES + 1:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="fb-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # 4 attempts on the primary, 1 on the candidate (success).
        assert call_count == TRANSIENT_RETRIES + 2
        client.set_model.assert_awaited_once_with("fallback-model")
        # The swap is announced — a persisted notice card, never silent.
        notices = self._notice_texts(slot)
        assert any(
            "claude-test-model" in t and "fallback-model" in t and "throttled" in t for t in notices
        ), f"expected a fallback notice card, got {notices!r}"
        # Recovered on the candidate; no terminal ❌.
        assert any("fb-result" in t for t in self._assistant_texts(slot))
        assert not any(t.startswith("❌") for t in self._err_texts(slot))
        # The live session was never reset — same-session recovery.
        state.sessions.reset.assert_not_awaited()
        # Sticky fallback state armed for the next turn's restore probe...
        assert slot._active_fallback_model == "fallback-model"
        assert slot._fallback_primary_model == "claude-test-model"
        # ...while the per-cycle walk state reset with the budgets.
        assert slot._transient_5xx_retries == 0
        assert slot._fallback_candidate_idx == 0
        assert slot._fallback_walked == []
        # Turn stats carry the model that ACTUALLY served the turn: the
        # substitute set_model updated the provider's bookkeeping (mimicked by
        # this mock exactly as AcpSessionHandle/AcpClient.set_model do), so the
        # read_turn_model seam — the sole source for meta.turn_stats.model
        # (see _attach_turn_stats, pinned by test_turn_stats.py) — reports the
        # candidate. The mocked provider yields no duration, so the stats dict
        # itself is not attached here; the model field's attach contract is
        # already pinned by test_turn_stats.py::test_model_included_when_resolved.
        from kiro_crew.dashboard.handlers.usage import read_turn_model

        assert read_turn_model(client) == "fallback-model"

    @pytest.mark.asyncio
    async def test_empty_chain_is_todays_terminal_error(self, tmp_path, monkeypatch):
        """REGRESSION PIN: with no chain configured, budget exhaustion surfaces
        the terminal ❌ exactly as before — no swap, no notice, no extra
        attempts."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert call_count == TRANSIENT_RETRIES + 1
        client.set_model.assert_not_awaited()
        assert not self._notice_texts(slot)
        assert any(t.startswith("❌") for t in self._err_texts(slot))
        assert slot._active_fallback_model == ""

    @pytest.mark.asyncio
    async def test_chain_exhaustion_surfaces_error_with_story(self, tmp_path, monkeypatch):
        """Every candidate also fails: the terminal ❌ carries the chain's
        story (primary throttled + which fallbacks were tried), and each
        candidate got exactly two attempts."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # 4 on the primary + 2 on the candidate (initial + one retry).
        assert call_count == TRANSIENT_RETRIES + 3
        client.set_model.assert_awaited_once_with("fallback-model")
        errs = self._err_texts(slot)
        assert any(
            "fallback-model" in t and "also unavailable" in t for t in errs
        ), f"expected the chain story on the terminal error, got {errs!r}"
        # Per-cycle walk state refreshed for the next user-initiated cycle.
        assert slot._fallback_candidate_idx == 0
        assert slot._fallback_walked == []

    @pytest.mark.asyncio
    async def test_candidate_budget_routes_through_shared_rewind_body(self, tmp_path, monkeypatch):
        """DRIFT PIN: the post-swap counter rewind must come
        from llm_helpers.fallback_rewound_transient_budget — not a re-encoded
        local constant. Patching the shared body to grant no extra pass drops
        the candidate to a single attempt."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        # Rewind to the exhaustion threshold: the re-queued attempt runs, and
        # its failure immediately re-enters the fallback branch (no same-model
        # retry pass) — one attempt on the candidate instead of two.
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.fallback_rewound_transient_budget",
            lambda: TRANSIENT_RETRIES,
        )
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # 4 on the primary + only 1 on the candidate (vs 2 unpatched — see
        # test_chain_exhaustion_surfaces_error_with_story).
        assert call_count == TRANSIENT_RETRIES + 2
        client.set_model.assert_awaited_once_with("fallback-model")

    @pytest.mark.asyncio
    async def test_restore_probe_fires_on_next_genuine_turn(self, tmp_path, monkeypatch):
        """A sticky fallback from an earlier cycle is probed back to the
        primary at the start of the next genuine user turn — quietly (no
        chat card), clearing the sticky state on success."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        client._model = "fallback-model"
        client.served_model = "fallback-model"
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = "claude-test-model"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello again")
            await self._drain_bg(state)

        client.set_model.assert_awaited_once_with("claude-test-model")
        assert slot._active_fallback_model == ""
        assert slot._fallback_primary_model == ""
        # Quiet restore: no notice card for recovery.
        assert not self._notice_texts(slot)
        assert any("ok-result" in t for t in self._assistant_texts(slot))

    @pytest.mark.asyncio
    async def test_restore_probe_skipped_when_session_moved_on(self, tmp_path, monkeypatch):
        """If the session is not on our fallback (explicit user pick),
        the stale sticky state is dropped WITHOUT touching the model."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        client._model = "user-picked-model"
        client.served_model = "user-picked-model"
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = "claude-test-model"
        # A stale provider marker from the abandoned fallback must be dropped
        # WITH the slot state — a surviving marker would re-seed the long-dead
        # primary into a later, unrelated fallback walk.
        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

        setattr(client, TURN_FALLBACK_ATTR, ("claude-test-model", "fallback-model"))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello again")
            await self._drain_bg(state)

        client.set_model.assert_not_awaited()
        assert slot._active_fallback_model == ""
        assert getattr(client, TURN_FALLBACK_ATTR) is None

    @pytest.mark.asyncio
    async def test_restore_probe_honors_explicit_pick_of_the_fallback_itself(
        self, tmp_path, monkeypatch
    ):
        """A user who EXPLICITLY picks the very
        model the session fell back to changes slot.model but not the served
        model — the probe must drop the sticky state without restoring the
        old primary over their pick."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        client._model = "fallback-model"
        client.served_model = "fallback-model"
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        # Fallback activated while the slot was configured on the primary...
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = "claude-test-model"
        slot._fallback_slot_model = "claude-test-model"
        slot._fallback_pick_gen = slot._model_pick_gen
        # ...then the user explicitly picked the fallback model themselves
        # (the pick surface writes slot.model AND bumps the pick generation).
        slot.model = "fallback-model"
        slot._model_pick_gen += 1

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello again")
            await self._drain_bg(state)

        # No restore fired — the explicit pick wins — and the sticky state is gone.
        client.set_model.assert_not_awaited()
        assert slot._active_fallback_model == ""
        assert slot._fallback_slot_model == ""
        # The explicit pick itself is untouched.
        assert slot.model == "fallback-model"

    @pytest.mark.asyncio
    async def test_restore_probe_heals_automatic_backfill(self, tmp_path, monkeypatch):
        """REGRESSION (verifier finding): on an UNPINNED slot, a served-
        fallback value that reached slot.model without a pick-generation bump
        (pre-guard pollution) must NOT be misread as an explicit pick: the
        probe restores the primary, drops the sticky state, and heals
        slot.model back to the activation snapshot (else the fallback id would
        be re-sent as a pin on resume)."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        client._model = "fallback-model"
        client.served_model = "fallback-model"
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        # Fallback activated on an UNPINNED slot (empty snapshot, no pick)...
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = "claude-test-model"
        slot._fallback_slot_model = ""
        slot._fallback_pick_gen = slot._model_pick_gen
        # ...then the automatic backfill wrote the served fallback into
        # slot.model WITHOUT bumping the pick generation.
        slot.model = "fallback-model"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello again")
            await self._drain_bg(state)

        # The restore fired (backfill is not a pick) and healed the slot pin.
        client.set_model.assert_awaited_once_with("claude-test-model")
        assert slot._active_fallback_model == ""
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_backfill_never_persists_the_fallback_candidate(self, tmp_path, monkeypatch):
        """The span invariant: while a fallback is
        ACTIVE, the automatic model backfill must not write the served
        candidate into slot.model — slot.model is PERSISTED, and the in-memory
        sticky state is not, so a gateway restart would turn the temporary
        fallback into a permanent pin. An unpinned slot stays unpinned for the
        fallback's duration."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        # The provider resolves to the FALLBACK candidate (it is serving).
        client._model = "fallback-model"
        client.served_model = "fallback-model"
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot.model = ""  # unpinned
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = ""  # primary unknown -> no restore
        slot._fallback_pick_gen = slot._model_pick_gen

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # The turn ran, but the persisted pin was never written with the
        # candidate. (The unknown-primary probe clears the sticky state — the
        # pin staying empty is the property under test.)
        assert slot.model == ""


# ── A fallback-served turn's usage row names the model that ran ──
#
# The served id is on the slot as ``_active_fallback_model``, written only after
# ``advance_fallback_candidate`` witnessed the ``set_model``. Passing it as the
# caller-side model makes the row correct by construction, rather than relying on
# ``model_source``'s wrapper-chain walk reaching a deeply wrapped provider.


def _deep_chain_client(stream, *, primary="primary-model", advertised=("fallback-model",)):
    """A provider whose model state sits six wrapper layers below the surface.

    Mirrors the nesting ``_wrapper_chain``'s docstring documents (``AcpProvider``
    -> ``client`` -> ``AcpSessionProvider`` -> ``_handle`` -> ``AcpSessionHandle``
    -> ``_runtime``) with the extra wrapper layers a long-lived shared session
    accumulates. ``set_model`` writes the handle's ``_model``/``_resolved_model_id``
    exactly as ``AcpSessionHandle.set_model`` does. The depth-first walk reaches
    them, so this shape exercises the caller-side path with the walk as an
    independent second witness rather than as the row's only source.
    """

    class _Runtime:
        def __init__(self):
            self._model = "process-level-arg"

    class _Handle:
        def __init__(self):
            self._model = primary
            self._resolved_model_id = primary
            self._runtime = _Runtime()

    class _SessionProvider:
        def __init__(self, handle):
            self._handle = handle
            self._runtime = handle._runtime

    class _Wrapper:
        def __init__(self, inner):
            self._client = inner
            self.client = inner

    handle = _Handle()
    node = _SessionProvider(handle)
    for _ in range(6):
        node = _Wrapper(node)

    class _Provider:
        def __init__(self, inner):
            self._client = inner
            self.client = inner
            self.stream = stream
            self.stream_command = stream
            # This deliberately non-ABC wrapper still models a provider seam;
            # capabilities it does not implement must take the ABC-safe default.
            self.is_kiro_backend = False
            # The public served_model seam the poisoned-conversation canary and
            # the fallback witness read; delegates to the handle like the real
            # AcpProvider.served_model property does.
            self._handle_ref = handle

        @property
        def served_model(self):
            return self._handle_ref._model or self._handle_ref._resolved_model_id

        def available_models(self):
            return [{"modelId": m} for m in advertised]

        def context_usage_pct(self):
            return 0.0

        def context_used_tokens(self):
            return 0

        def context_window_tokens(self):
            return 0

        async def set_model(self, model_id):
            handle._model = model_id
            handle._resolved_model_id = model_id

    return _Provider(node), handle


class TestFallbackServedTurnAttribution:
    _TRANSIENT = TestRunChatModelFallback._TRANSIENT
    _make_state = staticmethod(TestRunChatTransientRetry._make_state)
    _wire_sessions = staticmethod(TestRunChatTransientRetry._wire_sessions)
    _drain_bg = staticmethod(TestRunChatTransientRetry._drain_bg)

    @staticmethod
    def _rows(shard_dir):
        if not shard_dir.exists():
            return []
        return [
            json.loads(line)
            for shard in sorted(shard_dir.glob("*.jsonl"))
            for line in shard.read_text().splitlines()
            if line.strip()
        ]

    @pytest.mark.asyncio
    async def test_row_names_the_fallback_that_served_when_the_chain_is_capped(
        self, tmp_path, monkeypatch
    ):
        """A fallback-served turn on a deeply-wrapped provider still attributes
        its credits to the model that ran, instead of landing in ``unknown``."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.handlers import usage as usage_mod
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        shard_dir = tmp_path / "usage" / "tokens"
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )

        calls = 0

        async def _stream(msg):
            nonlocal calls
            calls += 1
            if calls <= TRANSIENT_RETRIES + 1:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="fb-result")
            yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=12.5))

        state = self._make_state(tmp_path, monkeypatch)
        client, handle = _deep_chain_client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        # The depth-first walk reaches this handle on its own, so left live it
        # would name the same model and the row would be right for TWO reasons.
        # Blind it for the run: the row's value must then come from the
        # caller-side model alone (`_resolve_model` short-circuits on a
        # non-blank caller-side model and never consults `model_source`). The
        # real walk is asserted separately below as the independent second witness.
        real_read_turn_model = usage_mod.read_turn_model
        monkeypatch.setattr(usage_mod, "read_turn_model", lambda source: "")

        with patch("asyncio.sleep", new_callable=AsyncMock):  # flake-ok: drain sleeps
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # Preconditions: the fallback really served the turn, the blinded walk
        # really reports nothing, and the real walk reaches the deep handle.
        assert slot._active_fallback_model == "fallback-model"
        assert handle._model == "fallback-model"
        assert usage_mod.read_turn_model(client) == "", (
            "precondition: the wrapper-chain walk must be blind for this run, "
            "otherwise the row is not exercising the caller-side path alone"
        )
        assert real_read_turn_model(client) == "fallback-model", (
            "the depth-first wrapper-chain walk must reach the deep handle here; "
            "a blank means a wrapper stack of this depth hides the model again"
        )

        rows = self._rows(shard_dir)
        assert len(rows) == 1, rows
        assert rows[0]["credits"] == 12.5
        assert rows[0]["model"] == "fallback-model", (
            "a fallback-served turn's credits must name the model that ran; "
            f"got {rows[0]['model']!r} (read time renders a blank as 'unknown')"
        )

    @pytest.mark.asyncio
    async def test_non_fallback_turn_still_records_the_slot_model(self, tmp_path, monkeypatch):
        """With no fallback active the row keeps recording the slot's own model."""
        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.handlers import usage as usage_mod
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        shard_dir = tmp_path / "usage" / "tokens"
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=4.0))

        state = self._make_state(tmp_path, monkeypatch)
        client, _handle = _deep_chain_client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot.model = "pinned-model"

        with patch("asyncio.sleep", new_callable=AsyncMock):  # flake-ok: drain sleeps
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert slot._active_fallback_model == ""
        rows = self._rows(shard_dir)
        assert len(rows) == 1, rows
        assert rows[0]["model"] == "pinned-model", rows[0]

    @pytest.mark.asyncio
    async def test_a_deferred_mid_turn_pick_is_not_billed_to_the_running_turn(
        self, tmp_path, monkeypatch
    ):
        """A model pick taken WHILE this turn streams lands on slot.model at once
        (api_chat_slot_model's deferred branch), but the live session was never
        switched, so the turn ran on what the provider serves. The row must name
        the served model, and the committed pick must survive — the backfill must
        not overwrite an Auto pick's "" with the served id."""
        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.handlers import usage as usage_mod
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        shard_dir = tmp_path / "usage" / "tokens"
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)

        state = self._make_state(tmp_path, monkeypatch)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="served by the primary")
            # The user picks "" (Auto) WHILE the turn streams: the deferred
            # branch commits it to slot.model and marks the boundary. The live
            # session is NOT switched, so the turn still runs on primary-model.
            slot.model = ""
            slot._deferred_model_switch_key = "s1"
            yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=9.0))

        # The live session serves primary-model (the deep handle's _model).
        client, handle = _deep_chain_client(_stream, primary="primary-model")
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot.model = "primary-model"

        # The tail runs apply_deferred_model_switch, which on this non-ACP fake
        # arms a reset; stub the consume so the fake harness is not asked to
        # tear down and rebuild a session it does not model. The completion's
        # attribution (the behaviour under test) has already run by then.
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._consume_pending_reset",
            AsyncMock(return_value=False),
        )

        # Suppresses the background drain's real sleeps like the three sibling
        # tests; the test waits on _drain_bg, never on a clock.
        with patch("asyncio.sleep", new_callable=AsyncMock):  # flake-ok: drain sleeps
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        rows = self._rows(shard_dir)
        assert len(rows) == 1, rows
        assert rows[0]["model"] == "primary-model", (
            "a turn running while a deferred pick waits must bill the served "
            f"model, not the pending pin; got {rows[0]['model']!r}"
        )
        # The Auto pick survives: the backfill must not write the served id over
        # the committed "" while the boundary marker is set.
        assert slot.model == "", (
            "the deferred pick must not be overwritten by the completion backfill; "
            f"slot.model is {slot.model!r}"
        )

    @pytest.mark.asyncio
    async def test_stale_sticky_fallback_is_not_billed_after_a_resume(self, tmp_path, monkeypatch):
        """The sticky candidate can OUTLIVE the swap it records.

        It deliberately survives a landed turn (the session stays on the fallback
        until the start-of-turn restore probe succeeds), that probe is skipped for
        a synthetic recovery message (`_turn_dispatch_gates`), and a reset resumes
        the session by re-sending `slot.model` as a set_model override
        (`model_fallback`). So a recovery turn can run on the PRIMARY with
        `_active_fallback_model` still set. The row must name what the live
        provider is serving, not the stale candidate.
        """
        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.handlers import usage as usage_mod
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        shard_dir = tmp_path / "usage" / "tokens"
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="recovered")
            yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=7.0))

        state = self._make_state(tmp_path, monkeypatch)
        client, handle = _deep_chain_client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot.model = "primary-model"
        # The post-resume state: the live session is back on the primary while
        # the sticky candidate still names the fallback, and the restore probe
        # has not run to clear it.
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = "primary-model"
        slot._fallback_candidate_idx = 1  # non-zero: keeps the restore probe off
        assert handle._model == "primary-model", "precondition: session is on the primary"

        with patch("asyncio.sleep", new_callable=AsyncMock):  # flake-ok: drain sleeps
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        rows = self._rows(shard_dir)
        assert len(rows) == 1, rows
        assert rows[0]["model"] == "primary-model", (
            "a stale sticky fallback must not be billed: the live provider is "
            f"serving 'primary-model', got {rows[0]['model']!r}"
        )
