"""Shared fixtures-as-helpers for the provider-budget-banner test modules.

Not a test module (no ``test_`` prefix, not collected): it carries the wiring
every banner-recovery end-to-end test reuses, as a mixin the topic test classes
inherit. Split out of ``test_provider_budget_banner.py`` so each topic module
stays under the file line cap while every helper lives exactly once.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from chat_test_helpers import _make_state

_LOST_RUN_SECS = 30.0


class BannerRecoveryHelpers:
    """Wiring shared by the provider-budget-banner recovery test classes."""

    @staticmethod
    def _state(tmp_path, monkeypatch):
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
    def _wire(state, client):
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.discard_conversation = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        client.context_window_tokens = MagicMock(return_value=0)
        client.context_used_tokens = MagicMock(return_value=0)
        client.mcp_session_report = MagicMock(return_value=None)
        client.client = MagicMock()
        client.client.pop_pending_oauth_requests = MagicMock(return_value=[])

    @staticmethod
    def _client(stream):
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = stream
        client.stream_command = stream
        client.served_model = "gpt-test-model"
        return client

    @staticmethod
    def _provider_recovery_row():
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.dashboard.chat_utils import (
            RECOVERY_PROVENANCE_META_KEY,
            RecoveryProvenance,
        )

        return {
            "role": "inject",
            "content": _POSTTOKEN_RECOVER_MSG,
            "meta": {
                RECOVERY_PROVENANCE_META_KEY: (RecoveryProvenance.PROVIDER_BUDGET_ARTIFACT.value)
            },
        }

    @staticmethod
    def _transient_recovery_row():
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.dashboard.chat_utils import (
            RECOVERY_PROVENANCE_META_KEY,
            RecoveryProvenance,
        )

        return {
            "role": "inject",
            "content": _POSTTOKEN_RECOVER_MSG,
            "meta": {RECOVERY_PROVENANCE_META_KEY: RecoveryProvenance.TRANSIENT_RETRY.value},
        }

    @staticmethod
    async def _drain_bg(state, limit=30):
        for _ in range(limit):
            pending = [task for task in list(state._background_tasks) if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    @staticmethod
    def _assistant_texts(slot) -> list[str]:
        return [m.get("content", "") for m in slot.messages if m.get("role") == "assistant"]

    @staticmethod
    def _steerable(client):
        """Publish a steer-capable inner client whose RPC acknowledges the write."""
        inner_client = MagicMock()
        inner_client.supports_steer = True
        inner_client.steer = AsyncMock(return_value=True)
        inner_client.pop_pending_oauth_requests = MagicMock(return_value=[])
        client.client = inner_client
        return inner_client
