"""Slot, state and error builders shared by the ``test_recovery_l1_chat_runner*.py`` family.

``_RecordingSlot`` is a real ``_ChatSlot`` that records each recovery entry queued on it, a
continuation or a verbatim replay; ``_l1_state`` returns a ``DashboardState`` and its
client, which ends every turn on an infra refusal; ``_transient_error`` builds a backend
5xx the classifier calls transient. ``test_recovery_l1_chat_runner.py`` and
``test_recovery_l1_chat_runner_first_turn_history.py`` import all three.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.acp.client import AcpError
from kiro_crew.dashboard.chat_utils import SYNTHETIC_RECOVERY_KIND
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.hooks import ToolHookResult
from kiro_crew.recovery import ladder as lad


class _RecordingSlot(_ChatSlot):
    """A real slot that records every recovery continuation queued on it.

    ``_queue_recovery`` reaches the queue through ``slot.queue_insert``, and the
    entry it inserts is drained (or blocked) before the turn returns, so reading
    the queue afterwards cannot tell "never queued" from "queued and dispatched".
    """

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.recovery_inserts: list[str] = []
        # The whole queue immediately after each recovery insert, so a test can
        # read where the entry landed relative to anything queued during the wait.
        self.queue_after_recovery: list[list[str]] = []

    def queue_insert(self, index, content, **kw):  # type: ignore[override]
        queue_id = super().queue_insert(index, content, **kw)
        if kw.get("kind") == SYNTHETIC_RECOVERY_KIND:
            self.recovery_inserts.append(content)
            self.queue_after_recovery.append([q["content"] for q in self._queue])
        return queue_id


def _l1_state(tmp_path):
    """A DashboardState whose client ends every turn on an infra refusal."""
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    client = AsyncMock()
    sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    # Every claim here is a fresh session, so no provider is live before it; the
    # slash-command gates peek liveness with an await.
    sessions.is_provider_alive = AsyncMock(return_value=False)
    sessions.record_failure = AsyncMock()
    # Awaited by a turn that arms the FRESH first-turn debt.
    sessions.aflush = AsyncMock(return_value=None)
    sessions.check_context_usage = MagicMock()
    sessions.stop_generation = MagicMock(return_value=0)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    cb = MagicMock()
    cb.hooks.on_tool_call.return_value = ToolHookResult.allow()
    cb.build_message.return_value = ("hello", None)
    state.context_builder = cb
    hook_store = MagicMock()
    hook_store.fire = AsyncMock(return_value=[])
    state._hook_store = hook_store
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    client.context_usage_pct = MagicMock(return_value=0.0)
    client.context_window_tokens = MagicMock(return_value=0)
    client._client = client
    client.last_prompt_stats = None
    # The handle's verdict, on the real type the runner isinstance-checks.
    client.last_infra_error = lad.InfraError(lad.CLASS_CAPACITY, retry_after_secs=0.01)
    return state, client


def _transient_error() -> Exception:
    """A backend 5xx the classifier calls transient, via the structured flag."""
    exc = AcpError("Prompt error: {'message': 'Internal error: API Error: Internal server error'}")
    exc.transient = True
    return exc
