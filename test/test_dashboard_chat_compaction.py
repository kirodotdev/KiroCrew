"""Dashboard chat: compaction status broadcast backoff and the deferred-compaction wait."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from dashboard_chat_helpers import _provider_mock

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for


class TestBroadcastCompactionResultBackoff:
    """Streak/cooldown backoff for the per-turn EVENT_COMPACTION_STATUS
    failure path (Mesh compaction-spam fix). Distinct from the
    claude/kiro deferred-wait integration tests below — these exercise
    ``_broadcast_compaction_result`` directly for speed and isolation.
    """

    @staticmethod
    def _make_slot_and_state(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")
        return state, slot

    @staticmethod
    def _failed_event(title: str = ""):
        from kiro_crew.providers.base import LLMEvent

        return LLMEvent(kind="compaction_status", text="failed", title=title)

    @staticmethod
    def _completed_event(title: str = "did stuff"):
        from kiro_crew.providers.base import LLMEvent

        return LLMEvent(kind="compaction_status", text="completed", title=title)

    def test_first_n_failures_shown_as_is(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_utils import (
            _COMPACTION_NOTICE_SHOW_FIRST_N,
            _broadcast_compaction_result,
        )

        state, slot = self._make_slot_and_state(tmp_path, monkeypatch)

        for i in range(1, _COMPACTION_NOTICE_SHOW_FIRST_N + 1):
            msg = _broadcast_compaction_result(state, slot, self._failed_event())
            assert msg is not None, f"failure #{i} should still be shown"
            assert "unknown error" in msg
            # Streak-count wording only appears once we exceed the shown limit.
            assert f"{i}x in a row" not in msg

    def test_failures_beyond_limit_suppressed_within_cooldown(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_utils import (
            _COMPACTION_NOTICE_SHOW_FIRST_N,
            _broadcast_compaction_result,
        )

        state, slot = self._make_slot_and_state(tmp_path, monkeypatch)

        for _ in range(_COMPACTION_NOTICE_SHOW_FIRST_N):
            _broadcast_compaction_result(state, slot, self._failed_event())

        # One more failure while still within cooldown must return None
        # (nothing appended to the slot, no new broadcast).
        before = len(slot.messages)
        msg = _broadcast_compaction_result(state, slot, self._failed_event())
        assert msg is None
        assert len(slot.messages) == before

    def test_cooldown_elapsed_shows_collapsed_streak_message(self, tmp_path, monkeypatch):
        import kiro_crew.dashboard.chat_utils as chat_utils

        state, slot = self._make_slot_and_state(tmp_path, monkeypatch)

        fake_now = [1000.0]
        monkeypatch.setattr(chat_utils.time, "monotonic", lambda: fake_now[0])

        for _ in range(chat_utils._COMPACTION_NOTICE_SHOW_FIRST_N):
            chat_utils._broadcast_compaction_result(state, slot, self._failed_event())

        # Still within cooldown: suppressed.
        fake_now[0] += 1.0
        assert chat_utils._broadcast_compaction_result(state, slot, self._failed_event()) is None

        # Cooldown elapses: next failure collapses the streak into one message.
        fake_now[0] += chat_utils._COMPACTION_FAIL_COOLDOWN_SECS + 1.0
        msg = chat_utils._broadcast_compaction_result(state, slot, self._failed_event())
        assert msg is not None
        assert "x in a row" in msg
        assert "too large to" in msg or "unknown error" in msg

    def test_enriched_title_replaces_unknown_error(self, tmp_path, monkeypatch):
        """The notice reads the event title. kiro-cli sends no summary on
        failure, so the ACP layer now carries the notification's own reason
        there — the row must name it instead of collapsing to
        "unknown error"."""
        from kiro_crew.dashboard.chat_utils import _broadcast_compaction_result

        state, slot = self._make_slot_and_state(tmp_path, monkeypatch)

        msg = _broadcast_compaction_result(
            state, slot, self._failed_event("context window exceeded")
        )
        assert msg is not None
        assert "context window exceeded" in msg
        assert "unknown error" not in msg

    def test_success_resets_streak_and_cooldown(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_utils import (
            _COMPACTION_NOTICE_SHOW_FIRST_N,
            _broadcast_compaction_result,
        )

        state, slot = self._make_slot_and_state(tmp_path, monkeypatch)

        for _ in range(_COMPACTION_NOTICE_SHOW_FIRST_N):
            _broadcast_compaction_result(state, slot, self._failed_event())
        assert slot._compaction_fail_streak == _COMPACTION_NOTICE_SHOW_FIRST_N

        msg = _broadcast_compaction_result(state, slot, self._completed_event())
        assert msg is not None
        assert "compacted" in msg.lower()
        assert slot._compaction_fail_streak == 0
        assert slot._compaction_fail_cooldown_until == 0.0

        # A fresh failure right after a success must be shown (not suppressed),
        # proving the reset actually took effect.
        msg = _broadcast_compaction_result(state, slot, self._failed_event())
        assert msg is not None

    def test_completed_broadcasts_context_usage_reset(self, tmp_path, monkeypatch):
        """Regression: the in-turn compaction path posted the notice but never
        refreshed the context meter — the bar stayed at the pre-compaction
        value until the next turn."""
        from kiro_crew.dashboard.chat_utils import _broadcast_compaction_result

        state, slot = self._make_slot_and_state(tmp_path, monkeypatch)

        _broadcast_compaction_result(state, slot, self._completed_event())

        resets = [
            c for c in state.broadcast_ws.call_args_list if c.args and c.args[0] == "context_usage"
        ]
        assert resets, "completed compaction must broadcast a context_usage event"
        payload = resets[0].args[1]
        assert payload == {"slot": slot.key, "pct": 0.0, "reset": True}

    def test_failed_does_not_broadcast_context_usage(self, tmp_path, monkeypatch):
        """A failed compaction leaves usage unchanged — no meter reset."""
        from kiro_crew.dashboard.chat_utils import _broadcast_compaction_result

        state, slot = self._make_slot_and_state(tmp_path, monkeypatch)

        msg = _broadcast_compaction_result(state, slot, self._failed_event())

        assert msg is not None and msg.startswith("❌"), msg
        assert not [
            c for c in state.broadcast_ws.call_args_list if c.args and c.args[0] == "context_usage"
        ]

    def test_notice_prefixes_pin_the_frontend_parser_contract(self, tmp_path, monkeypatch):
        """The dashboard folds the compaction row by parsing its TEXT: the
        frontend (``website/src/pages/chat/CompactionCard.tsx``,
        ``parseCompactionNotice``) keys on the exact ``✅ Conversation compacted``
        lead (colon or period) for the folded card and on a ``❌`` lead for the
        error notice, and the row is tagged ``kind="compaction"`` so the registry
        can claim it. Nothing else pins the writer side, so a wording change here
        would silently unfold a multi-KB summary back into a plain notice. This
        test is that pin — change both sides together."""
        from kiro_crew.dashboard.chat_utils import _broadcast_compaction_result

        state, slot = self._make_slot_and_state(tmp_path, monkeypatch)

        with_summary = _broadcast_compaction_result(
            state, slot, self._completed_event("## Goal\nship")
        )
        assert with_summary == "\u2705 Conversation compacted: ## Goal\nship"
        without_summary = _broadcast_compaction_result(state, slot, self._completed_event(""))
        assert without_summary == "\u2705 Conversation compacted."
        failed = _broadcast_compaction_result(state, slot, self._failed_event("too large"))
        assert failed is not None and failed.startswith("\u274c Compaction failed: ")

        tagged = [m for m in slot.messages if m.get("role") == "assistant"]
        assert tagged and all((m.get("meta") or {}).get("kind") == "compaction" for m in tagged)

    def test_state_notice_glyphs_pin_the_frontend_status_regex(self):
        """The other half of the same contract: ``state_parts/session_events.py`` writes three more
        ``kind="compaction"`` shapes whose LEAD GLYPH the frontend strips
        (``STATUS_LEAD_RE`` in ``CompactionCard.tsx``: 🔄 U+1F504, ♻ U+267B,
        ⏳ U+23F3) and one ⚠-led failure it routes to the error surface. A glyph
        swap here (⏳ U+23F3 vs ⌛ U+231B was a real review-round slip) would
        leave a raw emoji beside the card's icon with nothing red on this side."""
        from kiro_crew.dashboard import state as st

        assert st._AUTO_COMPACT_NOTICE.startswith("\U0001f504 ")
        assert st._SESSION_RECYCLED_NOTICE.startswith("\u267b\ufe0f ")
        assert st.stuck_turn_notice(120).startswith("\u23f3 ")
        assert st._AUTO_COMPACT_FAILED_NOTICE.startswith("\u26a0 Auto-compact failed")


class TestRunChatCompactDeferredWait:
    """The deferred-compaction wait in ``_turn_compact_command`` (slash_commands) is a
    kiro-cli-only protocol step. claude-agent-acp performs /compact synchronously inside
    session/prompt and never emits ``_kiro.dev/compaction/status``, so the handler must
    skip ``wait_for_compaction`` for that backend or it sits blocked for 30 minutes and
    finally surfaces "Compaction timed out."
    """

    @staticmethod
    def _make_mock_client(events):
        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.context_window_tokens = MagicMock(return_value=0)
        client.context_used_tokens = MagicMock(return_value=0)
        client.wait_for_compaction = AsyncMock(return_value={"type": "timeout"})

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_claude_backend_skips_wait_for_compaction(self, tmp_path, monkeypatch):
        """When the backend compacts INLINE, the dashboard must report success
        immediately and never call ``wait_for_compaction``."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE)]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        # The claude backend finishes compaction inside the prompt turn, so the
        # double carries that backend's capability record. Set on the client, not
        # patched at module scope: ``capabilities_of`` serves several call sites in the turn
        # (``_on_complete``, ``_turn_compact_command``), and patching it would answer for all.
        client.capabilities = capabilities_for(ACP_BACKEND_CLAUDE)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/compact")

        client.wait_for_compaction.assert_not_called()
        state.sessions.mark_needs_reinjection.assert_called_once_with("dashboard:s1")
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert any("Conversation compacted" in m["content"] for m in assistant_msgs)
        assert not any("timed out" in m["content"] for m in assistant_msgs)
        # The notice must be tagged kind="compaction" so it does not shadow the
        # follow-up [OPTIONS:] buttons of the turn it follows (persisted meta +
        # live broadcast). See chat_utils._append_compaction_notice.
        compaction_msgs = [m for m in assistant_msgs if "Conversation compacted" in m["content"]]
        assert compaction_msgs
        assert all(m.get("meta", {}).get("kind") == "compaction" for m in compaction_msgs)
        # The appended row carries a minted ``meta.mid``: the live copy is
        # delivered through append's own identity-carrying door (_on_message),
        # so no hand-built duplicate ``chat_message`` frame may fire — a
        # mid-less manual frame rendered the notice twice.
        assert all(m.get("meta", {}).get("mid") for m in compaction_msgs)
        assistant_broadcasts = [
            c
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "chat_message" and c.args[1].get("role") == "assistant"
        ]
        assert assistant_broadcasts == []
        # Updated context% must be broadcast so the dashboard bar refreshes.
        ws_kinds = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "context_usage" in ws_kinds

    @pytest.mark.asyncio
    async def test_kiro_backend_still_waits_for_compaction(self, tmp_path, monkeypatch):
        """kiro-cli backend keeps the original deferred-wait path."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE)]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        client.wait_for_compaction = AsyncMock(
            return_value={"type": "completed", "summary": "summary text"}
        )
        # A real client dropped its counts when the completed status arrived
        # (AcpPromptStats.reset_after_compaction) — model that so the
        # end-of-turn payload broadcast reflects the post-compaction state.
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/compact")

        client.wait_for_compaction.assert_awaited_once()
        state.sessions.mark_needs_reinjection.assert_called_once_with("dashboard:s1")
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert any("summary text" in m["content"] for m in assistant_msgs)
        # A completed deferred compaction must send the `reset` form — the
        # provider's counts were just dropped, so a payload broadcast would
        # claim "0 used" of a window nothing has re-measured yet.
        usage_calls = [
            c for c in state.broadcast_ws.call_args_list if c.args and c.args[0] == "context_usage"
        ]
        assert {"slot": slot.key, "pct": 0.0, "reset": True} in [c.args[1] for c in usage_calls]
        # No later broadcast may resurrect the stale pre-compaction meter.
        assert all(c.args[1].get("pct") == 0.0 for c in usage_calls)
        # Notice tagged kind="compaction" on the kiro deferred-wait path too.
        compaction_msgs = [m for m in assistant_msgs if "summary text" in m["content"]]
        assert compaction_msgs
        assert all(m.get("meta", {}).get("kind") == "compaction" for m in compaction_msgs)

    @pytest.mark.asyncio
    async def test_completed_status_arms_skills_context_reinjection(self, tmp_path, monkeypatch):
        """A provider-native completed status restores skills context once."""
        from kiro_crew.providers.base import EVENT_COMPACTION_STATUS, EVENT_COMPLETE, LLMEvent

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(
            [
                LLMEvent(kind=EVENT_COMPACTION_STATUS, text="completed"),
                LLMEvent(kind=EVENT_COMPLETE),
            ]
        )
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "continue after provider compaction")

        state.sessions.mark_needs_reinjection.assert_called_once_with("dashboard:s1")

    @pytest.mark.asyncio
    async def test_kiro_backend_broadcasts_real_post_compaction_usage(self, tmp_path, monkeypatch):
        """When the wait_for_compaction grace drain captured kiro's fresh
        post-compaction metadata, the broadcast must carry the REAL numbers
        (accurate pct + served window), not the reset/unknown fallback."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE)]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        client.wait_for_compaction = AsyncMock(return_value={"type": "completed", "summary": ""})
        # Post-drain provider state: metadata applied against the kept 1M
        # served window.
        client.context_usage_pct = MagicMock(return_value=5.0)
        client.context_window_tokens = MagicMock(return_value=1_000_000)
        client.context_used_tokens = MagicMock(return_value=50_000)
        client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/compact")

        state.sessions.mark_needs_reinjection.assert_called_once_with("dashboard:s1")
        usage_calls = [
            c for c in state.broadcast_ws.call_args_list if c.args and c.args[0] == "context_usage"
        ]
        assert usage_calls
        expected = {
            "slot": slot.key,
            "pct": 5.0,
            "used_tokens": 50_000,
            "window_tokens": 1_000_000,
        }
        assert expected in [c.args[1] for c in usage_calls]
        # The accurate numbers must not be wiped by a reset broadcast.
        assert not any(c.args[1].get("reset") for c in usage_calls)

    @pytest.mark.asyncio
    async def test_kiro_backend_failed_compaction_keeps_meter(self, tmp_path, monkeypatch):
        """A failed deferred compaction leaves usage unchanged: re-send the
        current counts, never the reset form (which would blank a still-valid
        meter)."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE)]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        client.wait_for_compaction = AsyncMock(return_value={"type": "failed"})
        client.context_window_tokens = MagicMock(return_value=200_000)
        client.context_used_tokens = MagicMock(return_value=150_000)
        client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/compact")

        state.sessions.mark_needs_reinjection.assert_not_called()
        usage_calls = [
            c for c in state.broadcast_ws.call_args_list if c.args and c.args[0] == "context_usage"
        ]
        assert usage_calls
        payload = usage_calls[-1].args[1]
        assert payload == {
            "slot": slot.key,
            "pct": 10.0,  # unchanged, from context_usage_pct()
            "used_tokens": 150_000,
            "window_tokens": 200_000,
        }

    @pytest.mark.asyncio
    async def test_replay_pending_compact_returns_without_waiting(self, tmp_path, monkeypatch):
        """A /compact that reaches a fresh session still owing replay ends now.

        The replayed history is delivered only with the next context-bearing
        prompt, so the provider session holds nothing: kiro-cli answers
        "Conversation too short to compact." with ``end_turn`` and never sends a
        compaction status. Waiting for one strands the chat for the whole
        compaction timeout and then reports a timeout for a compaction that was
        never going to start. The replay lease must stay armed for the next
        ordinary prompt, and no reinjection is armed because nothing was
        compacted.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(
            [
                LLMEvent(kind=EVENT_TEXT_CHUNK, text="Conversation too short to compact.\n"),
                LLMEvent(kind=EVENT_COMPLETE),
            ]
        )
        client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.provider_switch_replay_pending = MagicMock(return_value=True)
        state.sessions.consume_provider_switch_replay = MagicMock(return_value=True)
        state.sessions.commit_provider_switch_replay_sid = MagicMock(return_value=True)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/compact")

        client.wait_for_compaction.assert_not_called()
        state.sessions.mark_needs_reinjection.assert_not_called()
        state.sessions.consume_provider_switch_replay.assert_not_called()
        state.sessions.commit_provider_switch_replay_sid.assert_not_called()
        compacting_frames = [
            c
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "chat_message" and c.args[1].get("role") == "compacting"
        ]
        assert compacting_frames == []
        notices = [m for m in slot.messages if m.get("meta", {}).get("kind") == "compaction"]
        assert len(notices) == 1
        assert notices[0]["content"].startswith("Nothing to compact yet")
        assert not any("timed out" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_fresh_session_compact_returns_without_waiting(self, tmp_path, monkeypatch):
        """A /compact as the first message of a brand-new chat ends now.

        No ordinary prompt has reached the fresh provider session, so its
        first-turn history debt is still armed and it holds no conversation:
        kiro-cli answers "Conversation too short to compact." with ``end_turn``
        and never sends a compaction status. Waiting for one strands the chat
        for the whole compaction timeout and then reports a timeout. The debt
        must stay armed for the next ordinary prompt, and no reinjection is
        armed because nothing was compacted.
        """
        from kiro_crew.dashboard.chat_turn.recovery import COMPACT_FRESH_SESSION_NOTICE
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(
            [
                LLMEvent(kind=EVENT_TEXT_CHUNK, text="Conversation too short to compact.\n"),
                LLMEvent(kind=EVENT_COMPLETE),
            ]
        )
        client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.provider_switch_replay_pending = MagicMock(return_value=False)
        state.sessions.first_turn_history_owed_pending = MagicMock(return_value=True)
        state.sessions.consume_first_turn_history_owed = MagicMock(return_value=True)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/compact")

        client.wait_for_compaction.assert_not_called()
        state.sessions.mark_needs_reinjection.assert_not_called()
        state.sessions.consume_first_turn_history_owed.assert_not_called()
        compacting_frames = [
            c
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "chat_message" and c.args[1].get("role") == "compacting"
        ]
        assert compacting_frames == []
        notices = [m for m in slot.messages if m.get("meta", {}).get("kind") == "compaction"]
        assert [n["content"] for n in notices] == [COMPACT_FRESH_SESSION_NOTICE]
        assert not any("timed out" in m.get("content", "") for m in slot.messages)

    @staticmethod
    def _compaction_notice(slot) -> str:
        """The text of the compaction notice appended to the transcript."""
        notices = [
            m["content"]
            for m in slot.messages
            if m.get("meta", {}).get("kind") == "compaction"
            and "Compaction" in m.get("content", "")
        ]
        assert notices, "no compaction notice was appended"
        return notices[-1]

    async def _run_failed_compaction(self, tmp_path, monkeypatch, summary):
        """Drive /compact to a `failed` result carrying *summary*."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client([LLMEvent(kind=EVENT_COMPLETE)])
        result = {"type": "failed"}
        if summary is not None:
            result["summary"] = summary
        client.wait_for_compaction = AsyncMock(return_value=result)
        client.context_window_tokens = MagicMock(return_value=200_000)
        client.context_used_tokens = MagicMock(return_value=150_000)
        client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/compact")
        return slot

    @pytest.mark.asyncio
    async def test_failed_compaction_surfaces_the_reason(self, tmp_path, monkeypatch):
        """The provider's reason reaches the user instead of being discarded.

        Without it, a /compact that failed because the conversation is too large
        is indistinguishable from one that failed because the backend was
        unreachable — and those two call for different next moves. Every other
        surface (Slack/Telegram/Discord, and this dashboard's own auto-compact
        notice) already says which.
        """
        slot = await self._run_failed_compaction(
            tmp_path, monkeypatch, "context too large to summarize"
        )
        assert self._compaction_notice(slot) == (
            "❌ Compaction failed: context too large to summarize"
        )

    @pytest.mark.asyncio
    async def test_failed_compaction_without_a_reason_keeps_the_bare_notice(
        self, tmp_path, monkeypatch
    ):
        """No reason means no dangling colon — the old wording stands."""
        slot = await self._run_failed_compaction(tmp_path, monkeypatch, None)
        assert self._compaction_notice(slot) == "❌ Compaction failed."

    @pytest.mark.asyncio
    async def test_failed_compaction_blank_reason_keeps_the_bare_notice(
        self, tmp_path, monkeypatch
    ):
        """A whitespace-only reason is treated as absent, not printed."""
        slot = await self._run_failed_compaction(tmp_path, monkeypatch, "   ")
        assert self._compaction_notice(slot) == "❌ Compaction failed."

    @pytest.mark.asyncio
    async def test_failed_compaction_reason_is_redacted(self, tmp_path, monkeypatch):
        """A backend-echoed reason is not trusted to be credential-free.

        Same redact pair as the sibling `completed` branch, which already treats
        the provider's text as untrusted.
        """
        slot = await self._run_failed_compaction(
            tmp_path, monkeypatch, "auth failed for AKIAIOSFODNN7EXAMPLE key"
        )
        notice = self._compaction_notice(slot)
        assert "AKIAIOSFODNN7EXAMPLE" not in notice
        assert "Compaction failed:" in notice

    @pytest.mark.asyncio
    async def test_failed_compaction_reason_is_length_capped(self, tmp_path, monkeypatch):
        """A wall of provider text cannot scroll the transcript away."""
        from kiro_crew.dashboard.chat_runner import _COMPACT_FAIL_REASON_MAX_CHARS

        slot = await self._run_failed_compaction(tmp_path, monkeypatch, "x" * 5_000)
        notice = self._compaction_notice(slot)
        assert notice.endswith("…")
        assert len(notice) < _COMPACT_FAIL_REASON_MAX_CHARS + 60
