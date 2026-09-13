"""Tests for the generic pending-context drain + the thread->session persist map.

Ported from the upstream project — but ONLY the generic half. The
headline Slack thread-backfill (``_backfill_thread_context``) fires exclusively
on the challenge-and-redirect auto-link flow, which this fork DELIBERATELY
removes (external Slack messages reach the agent inline; there is no
``send_channel_challenge`` / ``_CHALLENGE_REDIRECT_ENABLED`` gate). So the
backfill helper, its ``chat_slack`` imports, and the auth/security/guard test
classes are dropped. What remains is the ungated refactor:

* ``chat_runner.drain_pending_context`` — the shared drain contract every
  producer (app-kit context inject) feeds; pinned here by driving a plain
  entry through it directly (not via the absent backfill helper).
* ``session_map`` thread->session reverse index survives a reload.
"""

from __future__ import annotations

from kiro_crew.dashboard.chat_runner import commit_drained_context, drain_pending_context
from kiro_crew.session_map import SessionMap

_BOUND_THREAD = "1700.42"
_BOUND_CHANNEL = "C999"


class TestPendingContextDrainContract:
    def test_entry_drains_into_background_context_frame(self, tmp_path, monkeypatch):
        """Drives the REAL drain (chat_runner.drain_pending_context) over a
        directly-injected entry, pinning injection → consumption end-to-end: the
        'content'/'source' keys and the [Background context …] frame the model
        actually receives. A future rename of either key breaks this test
        instead of silently breaking the feature."""
        from unittest.mock import MagicMock

        # A minimal slot stand-in carrying just the queue the drain reads.
        slot = MagicMock()
        slot._pending_context = [
            {"source": "slack-thread", "content": "the earlier thread discussion"},
        ]

        prefix = drain_pending_context(slot)
        assert '[Background context from "slack-thread"]' in prefix
        assert "[End of background context]" in prefix
        assert "the earlier thread discussion" in prefix
        # Drain is one-shot ONCE DELIVERY IS CONFIRMED: the queue is cleared so a later
        # turn isn't re-fed. Before the commit the entries are only IN FLIGHT, and a
        # second drain deliberately re-feeds them -- a drain whose turn died before the
        # prompt reached the provider must not have destroyed acknowledged content.
        assert slot._pending_context == []
        commit_drained_context(slot)
        assert drain_pending_context(slot) == ""


# ── Path A: persist-map reconnect (already implemented upstream) ─────────────


class TestPersistMapReconnect:
    def test_persist_map_survives_reload(self, tmp_path, monkeypatch):
        """The thread->session reverse index is rebuilt from persisted _data on
        load, so a thread reply resolves to its session after a restart."""
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        sm = SessionMap()
        sm.set_slack_link("dashboard:chat-3-99", _BOUND_THREAD, _BOUND_CHANNEL)
        assert sm.get_session_for_thread(_BOUND_THREAD) == "dashboard:chat-3-99"

        sm2 = SessionMap()
        assert sm2.get_session_for_thread(_BOUND_THREAD) == "dashboard:chat-3-99"
        ts, ch = sm2.get_slack_link("dashboard:chat-3-99")
        assert ts == _BOUND_THREAD
        assert ch == _BOUND_CHANNEL
