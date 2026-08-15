"""KAS session/update discriminant → Crew display mapping (Group A).

KAS folds signals that kiro-cli sends as separate top-level ``_kiro.dev/*``
methods (agent switch, per-turn metadata, compaction status) into
``session/update`` discriminants. These tests pin that the KAS-gated branch in
``AcpSessionHandle._handle_update`` restores each display, and that the kiro
path is untouched (the same discriminants are dropped on the kiro backend,
since kiro-cli never emits them).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    EVENT_AGENT_SWITCHED,
    EVENT_COMPACTION_STATUS,
    EVENT_STEER_CLEARED,
    EVENT_STEER_CONSUMED,
    EVENT_STEER_QUEUED,
    EVENT_TEXT_CHUNK,
    JsonRpcMessage,
)


def _handle(backend: str) -> AcpSessionHandle:
    """A handle over a fake runtime pinned to the given backend."""
    rt = MagicMock()
    rt.acp_backend = backend
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    return AcpSessionHandle("sA", asyncio.Queue(), rt)


def _update(handle: AcpSessionHandle, update: dict) -> list:
    return handle._handle_update(JsonRpcMessage(method="session/update", params={"update": update}))


# ── current_mode_update → agent_switched ─────────────────────────────────────


def test_current_mode_update_emits_on_first_and_on_change() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    # The session's initial current_mode_update is drained pre-prompt, so the
    # first frame reaching the handler is a real switch and MUST emit.
    events = _update(handle, {"sessionUpdate": "current_mode_update", "currentModeId": "kirocrew"})
    assert len(events) == 1 and events[0].kind == EVENT_AGENT_SWITCHED and events[0].text == "kirocrew"
    # A change to a different mode emits again.
    events = _update(handle, {"sessionUpdate": "current_mode_update", "currentModeId": "researcher"})
    assert len(events) == 1 and events[0].text == "researcher"


def test_current_mode_update_reassert_is_noop() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    first = _update(handle, {"sessionUpdate": "current_mode_update", "currentModeId": "kirocrew"})
    assert len(first) == 1  # first frame emits
    # A re-assert of the already-current mode must not echo a spurious switch.
    assert _update(handle, {"sessionUpdate": "current_mode_update", "currentModeId": "kirocrew"}) == []


def test_current_mode_update_missing_id_emits_nothing() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    assert _update(handle, {"sessionUpdate": "current_mode_update"}) == []
    assert _update(handle, {"sessionUpdate": "current_mode_update", "currentModeId": ""}) == []


# ── session_info_update / context_usage → context meter ──────────────────────


def test_context_usage_sets_context_pct() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(
        handle,
        {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": "context_usage", "usagePercentage": 42.5}}},
    )
    assert events == []  # metadata is state, not an event
    assert handle.last_prompt_stats.context_pct == 42.5


def test_context_usage_does_not_clobber_authoritative_usage() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    handle.last_prompt_stats.context_tokens_from_usage = True
    handle.last_prompt_stats.context_pct = 10.0
    _update(
        handle,
        {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": "context_usage", "usagePercentage": 99.0}}},
    )
    assert handle.last_prompt_stats.context_pct == 10.0


def test_context_usage_sanitizes_out_of_range() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    _update(
        handle,
        {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": "context_usage", "usagePercentage": 250.0}}},
    )
    assert handle.last_prompt_stats.context_pct == 100.0


def test_context_usage_nan_becomes_zero() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    _update(
        handle,
        {
            "sessionUpdate": "session_info_update",
            "_meta": {"kiro": {"kind": "context_usage", "usagePercentage": float("nan")}},
        },
    )
    assert handle.last_prompt_stats.context_pct == 0.0


def test_context_usage_missing_or_nonnumeric_pct_is_safe() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    handle.last_prompt_stats.context_pct = 5.0
    # absent usagePercentage (KAS emits session_info_update for many kinds)
    _update(handle, {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": "context_usage"}}})
    # non-numeric value
    _update(
        handle,
        {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": "context_usage", "usagePercentage": "n/a"}}},
    )
    assert handle.last_prompt_stats.context_pct == 5.0  # untouched, no raise


def test_context_usage_overflowing_int_is_safe() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    handle.last_prompt_stats.context_pct = 5.0
    # a JSON integer beyond float range must not raise OverflowError mid-dispatch.
    _update(
        handle,
        {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": "context_usage", "usagePercentage": 10**400}}},
    )
    assert handle.last_prompt_stats.context_pct == 5.0


# ── session_info_update / turn_completion → credits ──────────────────────────


def test_turn_completion_sums_credit_entries() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    _update(
        handle,
        {
            "sessionUpdate": "session_info_update",
            "_meta": {
                "kiro": {
                    "kind": "turn_completion",
                    "promptTurnSummaries": [
                        {"usage": 17, "unit": "credit", "unitPlural": "credits"},
                        {"usage": 3, "unit": "credit", "unitPlural": "credits"},
                        {"usage": 999, "unit": "token", "unitPlural": "tokens"},
                    ],
                }
            },
        },
    )
    assert handle.last_prompt_stats.credits == 20.0


def test_turn_completion_is_idempotent_not_accumulated() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    frame = {
        "sessionUpdate": "session_info_update",
        "_meta": {"kiro": {"kind": "turn_completion", "promptTurnSummaries": [{"usage": 12, "unit": "credit"}]}},
    }
    _update(handle, frame)
    _update(handle, frame)  # a duplicate / resume-replayed frame must not double the cost
    assert handle.last_prompt_stats.credits == 12.0


def test_turn_completion_ignores_malformed_usage() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    _update(
        handle,
        {
            "sessionUpdate": "session_info_update",
            "_meta": {
                "kiro": {
                    "kind": "turn_completion",
                    "promptTurnSummaries": [
                        {"usage": True, "unit": "credit"},  # bool is not a number
                        {"usage": "5", "unit": "credit"},  # str is not a number
                        {"usage": float("inf"), "unit": "credit"},  # non-finite
                    ],
                }
            },
        },
    )
    assert handle.last_prompt_stats.credits == 0.0


def test_turn_completion_non_list_summaries_is_safe() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    for bad in ({"kind": "turn_completion"}, {"kind": "turn_completion", "promptTurnSummaries": {"usage": 1}}):
        _update(handle, {"sessionUpdate": "session_info_update", "_meta": {"kiro": bad}})
    assert handle.last_prompt_stats.credits == 0.0


def test_turn_completion_overflowing_credit_is_safe() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    # math.isfinite(10**400) raises OverflowError (int->float); the guard must
    # swallow it, contribute nothing, and not abort the dispatch.
    _update(
        handle,
        {
            "sessionUpdate": "session_info_update",
            "_meta": {"kiro": {"kind": "turn_completion", "promptTurnSummaries": [{"usage": 10**400, "unit": "credit"}]}},
        },
    )
    assert handle.last_prompt_stats.credits == 0.0


# ── session_info_update / summarization_* → compaction status ────────────────


def test_summarization_completed_emits_compaction_and_resets() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    handle.last_prompt_stats.context_pct = 88.0
    handle.last_prompt_stats.context_tokens_from_usage = True
    events = _update(
        handle,
        {
            "sessionUpdate": "session_info_update",
            "_meta": {"kiro": {"kind": "summarization_completed", "conversationSummary": "did stuff", "truncated": False}},
        },
    )
    assert len(events) == 1
    assert events[0].kind == EVENT_COMPACTION_STATUS
    assert events[0].text == "completed"
    assert events[0].title == "did stuff"
    # reset_after_compaction dropped the stale authoritative counts.
    assert handle.last_prompt_stats.context_tokens_from_usage is False


def test_context_usage_reapplies_after_summarization_completed() -> None:
    # Sequencing (the KAS analog of the kiro-cli post-compaction metadata
    # re-apply): summarization_completed resets and clears the authoritative
    # flag, so a FOLLOWING context_usage frame must re-derive the meter. Were the
    # reset missing, context_tokens_from_usage would stay True and the fresh
    # percentage would be ignored — the dashboard bar frozen at the
    # pre-compaction value forever.
    handle = _handle(ACP_BACKEND_KAS)
    handle.last_prompt_stats.context_pct = 80.0
    handle.last_prompt_stats.context_tokens_from_usage = True
    _update(
        handle,
        {
            "sessionUpdate": "session_info_update",
            "_meta": {"kiro": {"kind": "summarization_completed", "conversationSummary": ""}},
        },
    )
    assert handle.last_prompt_stats.context_tokens_from_usage is False
    assert handle.last_prompt_stats.context_pct == 0.0
    _update(
        handle,
        {
            "sessionUpdate": "session_info_update",
            "_meta": {"kiro": {"kind": "context_usage", "usagePercentage": 12.0}},
        },
    )
    assert handle.last_prompt_stats.context_pct == 12.0


def test_summarization_started_emits_started_status() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(handle, {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": "summarization_started"}}})
    assert len(events) == 1
    assert events[0].kind == EVENT_COMPACTION_STATUS
    assert events[0].text == "started"


def test_summarization_failed_emits_failed_and_does_not_reset() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    handle.last_prompt_stats.context_tokens_from_usage = True
    events = _update(
        handle,
        {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": "summarization_failed", "reason": "error"}}},
    )
    assert len(events) == 1
    assert events[0].kind == EVENT_COMPACTION_STATUS
    assert events[0].text == "failed"
    # only a completed summarization resets — a failed one leaves stats intact.
    assert handle.last_prompt_stats.context_tokens_from_usage is True


def test_session_info_missing_or_malformed_meta_is_safe() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    assert _update(handle, {"sessionUpdate": "session_info_update"}) == []
    assert _update(handle, {"sessionUpdate": "session_info_update", "_meta": {}}) == []
    assert _update(handle, {"sessionUpdate": "session_info_update", "_meta": {"kiro": "notadict"}}) == []


def test_session_info_unhandled_kind_is_dropped() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    # recap/focus_update/etc. are session_info kinds Group A does not map — they
    # must drop cleanly (no event, no crash), not fall through to the parser.
    events = _update(
        handle,
        {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": "recap", "text": "so far..."}}},
    )
    assert events == []


# ── available_commands_update → recognized-and-dropped ───────────────────────


def test_available_commands_update_dropped_no_event() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(
        handle,
        {"sessionUpdate": "available_commands_update", "availableCommands": [{"name": "spec", "description": "x"}]},
    )
    assert events == []


# ── ordinary discriminants still parse on KAS ────────────────────────────────


def test_agent_message_chunk_still_parses_on_kas() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(handle, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}})
    assert len(events) == 1
    assert events[0].kind == EVENT_TEXT_CHUNK
    assert events[0].text == "hi"


# ── kiro-path parity: the KAS discriminants are NOT routed on kiro ───────────


def test_current_mode_update_not_routed_on_kiro_backend() -> None:
    handle = _handle(ACP_BACKEND_KIRO)
    # kiro-cli never emits current_mode_update; the shared parser has no branch
    # for it, so it is dropped (no agent_switched event) — proving the gate.
    assert _update(handle, {"sessionUpdate": "current_mode_update", "currentModeId": "kirocrew"}) == []


def test_context_usage_not_applied_on_kiro_backend() -> None:
    handle = _handle(ACP_BACKEND_KIRO)
    _update(
        handle,
        {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": "context_usage", "usagePercentage": 42.5}}},
    )
    assert handle.last_prompt_stats.context_pct != 42.5


# ── session_info_update / steering_* → steer events ──────────────────────────


def test_steering_injected_maps_to_consumed() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(
        handle,
        {
            "sessionUpdate": "session_info_update",
            "_meta": {"kiro": {"kind": "steering_injected", "messageId": "m1", "content": "focus on tests"}},
        },
    )
    # injected is the settling signal that _settle_consumed_steers consumes.
    assert len(events) == 1
    assert events[0].kind == EVENT_STEER_CONSUMED
    assert events[0].text == "focus on tests"


def test_steering_queued_maps_to_queued() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(
        handle,
        {
            "sessionUpdate": "session_info_update",
            "_meta": {"kiro": {"kind": "steering_queued", "messageId": "m1", "content": "also update docs"}},
        },
    )
    assert len(events) == 1
    assert events[0].kind == EVENT_STEER_QUEUED
    assert events[0].text == "also update docs"


def test_steering_cleared_maps_to_cleared_no_text() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(
        handle,
        {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": "steering_cleared", "messageIds": ["m1"]}}},
    )
    assert len(events) == 1
    assert events[0].kind == EVENT_STEER_CLEARED


def test_steering_not_routed_on_kiro_backend() -> None:
    handle = _handle(ACP_BACKEND_KIRO)
    # kiro-cli delivers steer as session/update discriminants (the "steer"
    # action), never as a session_info_update _meta.kiro kind, so this KAS-shaped
    # frame must NOT produce a steer event on the kiro path.
    assert _update(
        handle,
        {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": "steering_injected", "content": "x"}}},
    ) == []
