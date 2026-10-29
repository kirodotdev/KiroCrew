"""Wire tokens for the status rows the chat gateway appends to a transcript.

A gateway status row (a recovery ``error`` card, an empty-turn ``notice``, a
model-fallback ``notice``) keeps its English sentence as ``content``: channel
mirrors, SSE readers, an older frontend and the model itself read that text
verbatim. The dashboard does not. It reads ``meta["notice"]`` and renders
localized copy keyed on the token (``website/src/pages/chat/gatewayNotice.ts``),
and ``meta["notice_args"]`` carries the values that copy interpolates (a model
id, an exit code, a recovery count).

One namespace covers every family. The transient-5xx tokens
(``TRANSIENT_NOTICE_*`` in ``chat_utils``) ride the same ``meta["notice"]`` field
and join the same frontend table. ``test/test_transient_notice_parity.py``
discovers every ``*_NOTICE_*`` token in both modules and pins it against that
table, so a token added on one side only fails there rather than silently
rendering raw English. The compaction cards' own ``meta["notice"]`` markers
(``session_recycled``, ``stuck_turn``) ride the same field on assistant rows and
are drawn by the compaction card instead, so they are not in this table.

A row appended with literal English and no token is copy the dashboard cannot
localize. Give it a token here and a row in the frontend table.
"""

from __future__ import annotations

#: The row ``meta`` field the token rides in. Equal to
#: ``chat_utils.TRANSIENT_NOTICE_META_KEY``: one field for every family.
GATEWAY_NOTICE_META_KEY = "notice"
#: The row ``meta`` field holding the values the localized copy interpolates.
GATEWAY_NOTICE_ARGS_KEY = "notice_args"

# Recovery ``error`` rows. A ``*_retrying`` / ``*_recovering`` row is a pending
# link (the gateway already re-queued the turn); every other token is terminal.
GATEWAY_NOTICE_CONNECTION_LOST_RETRYING = "connection_lost_retrying"
GATEWAY_NOTICE_CONNECTION_LOST_GIVE_UP = "connection_lost_give_up"
GATEWAY_NOTICE_SESSION_BUSY_RETRYING = "session_busy_retrying"
GATEWAY_NOTICE_SESSION_BUSY_GIVE_UP = "session_busy_give_up"
GATEWAY_NOTICE_TURN_STALLED_RECOVERING = "turn_stalled_recovering"
GATEWAY_NOTICE_TURN_STALLED_GIVE_UP = "turn_stalled_give_up"
GATEWAY_NOTICE_TOOL_STALLED_RECOVERING = "tool_stalled_recovering"
GATEWAY_NOTICE_TOOL_STALLED_GIVE_UP = "tool_stalled_give_up"
GATEWAY_NOTICE_SESSION_STUCK = "session_stuck"
GATEWAY_NOTICE_SHARED_RUNTIME_RESTARTING = "shared_runtime_restarting"

# Empty-response ``notice`` rows. Their English text is
# ``messaging.empty_turn_copy``, shared with the channel driver.
GATEWAY_NOTICE_EMPTY_TURN = "empty_turn_give_up"
GATEWAY_NOTICE_EMPTY_TURN_AFTER_RECOVERY = "empty_turn_after_recovery"
GATEWAY_NOTICE_EMPTY_TURN_AFTER_WORK = "empty_turn_after_work"
GATEWAY_NOTICE_EMPTY_TURN_CONTINUING = "empty_turn_continuing"
GATEWAY_NOTICE_EMPTY_TURN_CONTINUING_AFTER_WORK = "empty_turn_continuing_after_work"
GATEWAY_NOTICE_COMPACTION_CONTINUING = "compaction_continuing"

# Model-fallback ``notice`` rows.
GATEWAY_NOTICE_MODEL_ACCESS_FALLBACK = "model_access_fallback"
GATEWAY_NOTICE_MODEL_UNAVAILABLE = "model_unavailable"
GATEWAY_NOTICE_MODEL_THROTTLED_FALLBACK = "model_throttled_fallback"

# Session-reconnect, credential-reload and model-session rebuild ``error`` rows.
# Each keeps today's red card; only its copy is localized.
GATEWAY_NOTICE_SESSION_RECONNECTING = "session_reconnecting"
GATEWAY_NOTICE_SESSION_RECONNECT_GIVE_UP = "session_reconnect_give_up"
GATEWAY_NOTICE_CREDENTIALS_RELOADING = "credentials_reloading"
GATEWAY_NOTICE_REASONING_MISMATCH_REBUILDING = "reasoning_mismatch_rebuilding"
GATEWAY_NOTICE_IMAGE_HISTORY_RESTARTING = "image_history_restarting"
GATEWAY_NOTICE_CONVERSATION_REJECTED_RESTARTING = "conversation_rejected_restarting"
GATEWAY_NOTICE_COMPACTION_RETRYING = "compaction_retrying"
GATEWAY_NOTICE_SESSION_RESET_CONTINUING = "session_reset_continuing"

# A cancelled recovery replay (``recovery_replays.cancel_notice``): one token per
# wording, so each reads as one sentence in every locale.
GATEWAY_NOTICE_AUTO_CONTINUE_CANCELLED_SUPERSEDED = "auto_continue_cancelled_superseded"
GATEWAY_NOTICE_AUTO_CONTINUE_CANCELLED_REBOUND = "auto_continue_cancelled_rebound"
GATEWAY_NOTICE_AUTO_CONTINUE_CANCELLED_STOPPED = "auto_continue_cancelled_stopped"
GATEWAY_NOTICE_FALLBACK_RETRY_CANCELLED_REBOUND = "fallback_retry_cancelled_rebound"
GATEWAY_NOTICE_FALLBACK_RETRY_CANCELLED_SUPERSEDED = "fallback_retry_cancelled_superseded"
GATEWAY_NOTICE_FALLBACK_RETRY_CANCELLED_STOPPED = "fallback_retry_cancelled_stopped"
GATEWAY_NOTICE_FILTER_RETRY_CANCELLED_REBOUND = "filter_retry_cancelled_rebound"
GATEWAY_NOTICE_FILTER_RETRY_CANCELLED_SUPERSEDED = "filter_retry_cancelled_superseded"
GATEWAY_NOTICE_FILTER_RETRY_CANCELLED_STOPPED = "filter_retry_cancelled_stopped"
GATEWAY_NOTICE_SESSION_RECONNECT_CANCELLED = "session_reconnect_cancelled"
GATEWAY_NOTICE_IMAGE_RECOVERY_CANCELLED = "image_recovery_cancelled"
GATEWAY_NOTICE_MODEL_SESSION_RECOVERY_CANCELLED = "model_session_recovery_cancelled"

# End-of-turn ``notice`` rows (``chat_turn/turn_ending``).
GATEWAY_NOTICE_TOOL_CALL_AS_TEXT = "tool_call_as_text"
GATEWAY_NOTICE_TOOL_CALL_AS_TEXT_PARTIAL = "tool_call_as_text_partial"
GATEWAY_NOTICE_TOOL_CALL_LEAKED_AT_COMPACTION = "tool_call_leaked_at_compaction"
GATEWAY_NOTICE_CAPACITY_RETRYING = "capacity_retrying"
GATEWAY_NOTICE_CAPACITY_RETRY_CANCELLED_SUPERSEDED = "capacity_retry_cancelled_superseded"
GATEWAY_NOTICE_CAPACITY_RETRY_CANCELLED_STOPPED = "capacity_retry_cancelled_stopped"
GATEWAY_NOTICE_CAPACITY_RETRIES_SPENT = "capacity_retries_spent"
GATEWAY_NOTICE_INTENT_WITHOUT_ACTION_AUTO_APPROVE = "intent_without_action_auto_approve"
GATEWAY_NOTICE_INTENT_WITHOUT_ACTION_CONTINUING = "intent_without_action_continuing"
GATEWAY_NOTICE_INTENT_WITHOUT_ACTION_SPENT = "intent_without_action_spent"
GATEWAY_NOTICE_READ_ONLY_STOP_RETRYING = "read_only_stop_retrying"
GATEWAY_NOTICE_WORK_IN_PROGRESS_ENDED = "work_in_progress_ended"
GATEWAY_NOTICE_DEFERRED_FOLLOWUP = "deferred_followup"

# Queue, agent-switch and admission rows.
GATEWAY_NOTICE_MEMORY_PREPARING = "memory_preparing"
GATEWAY_NOTICE_AGENT_SWITCH_BLOCKED_PINNED = "agent_switch_blocked_pinned"
GATEWAY_NOTICE_AGENT_SWITCH_BLOCKED_CREW_MODE = "agent_switch_blocked_crew_mode"
GATEWAY_NOTICE_CREW_NAME_UNDISPATCHABLE = "crew_name_undispatchable"
GATEWAY_NOTICE_REMOTE_CREW_READ_ONLY = "remote_crew_read_only"
GATEWAY_NOTICE_ARCHIVE_FAILED_TURN_CANCELLED = "archive_failed_turn_cancelled"

#: English fallback ``content`` for the recovery rows above. Plain language: no
#: "backend", no "hiccup" (the parity test lints every ``GATEWAY_*_TEXT``).
GATEWAY_CONNECTION_LOST_RETRYING_TEXT = "⟳ Connection lost — retrying…"
GATEWAY_CONNECTION_LOST_GIVE_UP_TEXT = "⟳ Connection lost — please retry."
GATEWAY_SESSION_BUSY_RETRYING_TEXT = "⟳ Session busy — retrying…"
GATEWAY_SESSION_BUSY_GIVE_UP_TEXT = "⟳ Session busy — please retry."
GATEWAY_TURN_STALLED_RECOVERING_TEXT = "⟳ Recovering a stalled turn…"
GATEWAY_TURN_STALLED_GIVE_UP_TEXT = "⟳ Turn stalled — please retry."
GATEWAY_TOOL_STALLED_RECOVERING_TEXT = "⟳ Tool appeared stalled — recovering…"
GATEWAY_TOOL_STALLED_GIVE_UP_TEXT = "⟳ Tool appeared stalled — please retry."
GATEWAY_SESSION_STUCK_TEXT = "Session stuck — please start a new chat."
GATEWAY_SHARED_RUNTIME_RESTARTING_TEXT = (
    "The agent process this chat shares kept restarting — please retry."
)


def gateway_notice_meta(token: str, **args: str | int) -> dict[str, object]:
    """The row ``meta`` that names *token*, plus its interpolation values.

    Merge it into a row's other meta (``{"kind": ..., **gateway_notice_meta(...)}``).
    Values are plain strings or ints; the frontend interpolates them as text.
    """
    meta: dict[str, object] = {GATEWAY_NOTICE_META_KEY: token}
    if args:
        meta[GATEWAY_NOTICE_ARGS_KEY] = dict(args)
    return meta
