"""The ``kirocrew/status`` extension (version 1) and its origin rule.

RFC §14.5: a structured status is trusted only from the EXECUTION LAYER of the
session it names. The shape + version gate lives in
``StructuredStatus.from_meta`` (``acp/types.py``); provenance is decided by
``AcpSessionHandle._handle_update`` on the frame — routed to this session, not
fanned out, not a model-text frame. Model-authored "I am waiting" prose must
never create a wait, whatever it contains.
"""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.session_handle import AcpSessionHandle, _watchdog_evidence_class
from kiro_crew.acp.types import (
    EVENT_STRUCTURED_STATUS,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    METHOD_SESSION_UPDATE,
    STATUS_EXTENSION_KEY,
    STATUS_EXTENSION_VERSION,
    STATUS_ORIGIN_EXECUTION_LAYER,
    STATUS_PHASE_RUNNING,
    STATUS_PHASE_WAITING,
    WAIT_REASON_DEPENDENCY,
    WAIT_REASON_INPUT,
    JsonRpcMessage,
    StructuredStatus,
)

SESSION = "sA"


# ── from_meta: shape + version gate ──────────────────────────────────────────


def _meta(**fields):
    body = {"version": STATUS_EXTENSION_VERSION}
    body.update(fields)
    return {STATUS_EXTENSION_KEY: body}


def test_from_meta_parses_every_field():
    status, reason = StructuredStatus.from_meta(
        _meta(
            task_id="t1",
            session_id=SESSION,
            parent_id="p0",
            tool_call_id="c9",
            generation=3,
            phase=STATUS_PHASE_WAITING,
            wait_reason=WAIT_REASON_DEPENDENCY,
            dependency_scope="github:api.github.com:core",
            retry_at=1_700_000_000.5,
            progress_source="tool_output",
            cancellable=True,
            resumable=True,
            safe_retry=False,
            checkpoint_ref="ckpt:42",
        )
    )
    assert reason == ""
    assert status is not None
    assert status.version == 1
    assert (status.task_id, status.session_id, status.parent_id) == ("t1", SESSION, "p0")
    assert status.tool_call_id == "c9"
    assert status.generation == 3
    assert status.phase == STATUS_PHASE_WAITING
    assert status.wait_reason == WAIT_REASON_DEPENDENCY
    assert status.dependency_scope == "github:api.github.com:core"
    assert status.retry_at == 1_700_000_000.5
    assert status.progress_source == "tool_output"
    assert (status.cancellable, status.resumable, status.safe_retry) == (True, True, False)
    assert status.checkpoint_ref == "ckpt:42"
    assert status.origin == STATUS_ORIGIN_EXECUTION_LAYER
    assert status.is_waiting


def test_from_meta_absent_is_silent_not_an_error():
    assert StructuredStatus.from_meta({}) == (None, "absent")
    assert StructuredStatus.from_meta(None) == (None, "absent")
    assert StructuredStatus.from_meta({"kiro": {"toolName": "x"}}) == (None, "absent")


def test_from_meta_rejects_a_non_object_extension():
    assert StructuredStatus.from_meta({STATUS_EXTENSION_KEY: "waiting"}) == (None, "not_object")
    assert StructuredStatus.from_meta({STATUS_EXTENSION_KEY: ["v1"]}) == (None, "not_object")


@pytest.mark.parametrize("version", [0, 2, "1", 1.0, True, None])
def test_from_meta_version_gate_rejects_the_whole_frame(version):
    """A newer or malformed schema is never half-applied."""
    meta = {
        STATUS_EXTENSION_KEY: {
            "version": version,
            "phase": STATUS_PHASE_WAITING,
            "wait_reason": WAIT_REASON_INPUT,
        }
    }
    assert StructuredStatus.from_meta(meta) == (None, "version_unsupported")


def test_from_meta_rejects_an_unknown_phase_or_wait_reason():
    assert StructuredStatus.from_meta(_meta(phase="sleeping")) == (None, "phase_invalid")
    assert StructuredStatus.from_meta(
        _meta(phase=STATUS_PHASE_WAITING, wait_reason="waiting_for_godot")
    ) == (None, "wait_reason_invalid")
    assert StructuredStatus.from_meta(_meta(phase=STATUS_PHASE_WAITING)) == (
        None,
        "wait_reason_invalid",
    )


def test_from_meta_drops_a_wait_reason_outside_the_waiting_phase():
    status, _ = StructuredStatus.from_meta(
        _meta(phase=STATUS_PHASE_RUNNING, wait_reason=WAIT_REASON_INPUT)
    )
    assert status is not None
    assert status.wait_reason == ""
    assert not status.is_waiting


def test_from_meta_defaults_are_fail_closed():
    """Saying nothing grants nothing: no capability, no identity, running."""
    status, _ = StructuredStatus.from_meta(_meta())
    assert status is not None
    assert status.phase == STATUS_PHASE_RUNNING
    assert (status.cancellable, status.resumable, status.safe_retry) == (False, False, False)
    assert status.generation == 0
    assert status.retry_at is None
    assert status.task_id == "" and status.tool_call_id == ""


def test_from_meta_type_coercion_is_strict():
    """A capability that is not literally True is False; a non-int generation is
    0; a non-finite or non-numeric retry_at is None; a non-string id is ""."""
    status, _ = StructuredStatus.from_meta(
        _meta(
            cancellable="yes",
            resumable=1,
            safe_retry="true",
            generation="7",
            retry_at="soon",
            task_id=12345,
            tool_call_id=["c1"],
        )
    )
    assert status is not None
    assert (status.cancellable, status.resumable, status.safe_retry) == (False, False, False)
    assert status.generation == 0
    assert status.retry_at is None
    assert status.task_id == "" and status.tool_call_id == ""
    nan, _ = StructuredStatus.from_meta(_meta(retry_at=float("nan")))
    assert nan is not None and nan.retry_at is None
    inf, _ = StructuredStatus.from_meta(_meta(retry_at=float("inf")))
    assert inf is not None and inf.retry_at is None


def test_from_meta_bounds_string_fields():
    status, _ = StructuredStatus.from_meta(_meta(task_id="x" * 5000))
    assert status is not None
    assert len(status.task_id) == 512


def test_from_meta_ignores_unknown_fields():
    status, reason = StructuredStatus.from_meta(_meta(future_field={"a": 1}))
    assert reason == "" and status is not None


# ── Origin rule on the session handle ────────────────────────────────────────


def _handle() -> AcpSessionHandle:
    rt = MagicMock()
    rt._last_activity = time.monotonic()
    rt.pid = None
    rt.acp_backend = "kiro"
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    return AcpSessionHandle(SESSION, asyncio.Queue(), rt)


def _update_msg(update: dict, *, session_id: str = SESSION, meta=None, fanout=False):
    params: dict = {"sessionId": session_id, "update": update}
    if meta is not None:
        params["_meta"] = meta
    msg = JsonRpcMessage(method=METHOD_SESSION_UPDATE, params=params)
    msg.fanout_no_owner = fanout
    return msg


def _tool_call(tool_call_id="c1", command="git log", **extra):
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": tool_call_id,
        "title": "bash",
        "kind": "execute",
        "rawInput": {"command": command},
        **extra,
    }


def _status_events(events):
    return [e for e in events if e.kind == EVENT_STRUCTURED_STATUS]


def test_routed_frame_status_is_accepted_as_a_typed_event():
    handle = _handle()
    events = handle._handle_update(
        _update_msg(
            _tool_call(),
            meta=_meta(
                phase=STATUS_PHASE_WAITING, wait_reason=WAIT_REASON_INPUT, tool_call_id="c1"
            ),
        )
    )
    kinds = [e.kind for e in events]
    assert kinds == [EVENT_TOOL_CALL, EVENT_STRUCTURED_STATUS]
    (status_ev,) = _status_events(events)
    assert status_ev.tool_call_id == "c1"
    assert status_ev.status is not None
    assert status_ev.status.is_waiting
    assert status_ev.status.wait_reason == WAIT_REASON_INPUT
    assert status_ev.status.origin == STATUS_ORIGIN_EXECUTION_LAYER
    assert handle.status_rejections == {}


def test_status_on_the_inner_update_meta_is_accepted_too():
    """kiro-cli carries ``_meta.kiro`` on the inner update; the extension may
    ride there as well as at the notification level."""
    handle = _handle()
    update = _tool_call()
    update["_meta"] = _meta(phase=STATUS_PHASE_WAITING, wait_reason=WAIT_REASON_DEPENDENCY)
    events = handle._handle_update(_update_msg(update))
    (status_ev,) = _status_events(events)
    assert status_ev.status is not None
    assert status_ev.status.wait_reason == WAIT_REASON_DEPENDENCY


def test_status_on_a_frame_that_matches_this_session_id_is_accepted():
    handle = _handle()
    events = handle._handle_update(
        _update_msg(_tool_call(), meta=_meta(session_id=SESSION, phase=STATUS_PHASE_RUNNING))
    )
    assert len(_status_events(events)) == 1


def test_fanout_frame_status_is_rejected(caplog):
    """An ownerless frame fanned out to several sessions names no owner."""
    handle = _handle()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.session_handle"):
        events = handle._handle_update(
            _update_msg(
                {"sessionUpdate": "plan", "entries": []},
                meta=_meta(phase=STATUS_PHASE_WAITING, wait_reason=WAIT_REASON_INPUT),
                fanout=True,
            )
        )
    assert _status_events(events) == []
    assert handle.status_rejections == {"fanout_no_owner": 1}
    assert "status_rejected" in caplog.text and "fanout_no_owner" in caplog.text


def test_child_routed_frame_status_is_rejected_and_not_reattributed():
    """A native sub-agent's frame carries the CHILD sessionId; its status is
    the child's (recovery boundary = parent session), never this handle's."""
    handle = _handle()
    events = handle._handle_update(
        _update_msg(
            _tool_call(tool_call_id="child-c1"),
            session_id="sub-1",
            meta=_meta(phase=STATUS_PHASE_WAITING, wait_reason=WAIT_REASON_INPUT),
        )
    )
    assert _status_events(events) == []
    assert handle.status_rejections == {"child_origin": 1}


def test_child_routed_frame_without_the_extension_is_not_a_rejection():
    handle = _handle()
    handle._handle_update(_update_msg(_tool_call(tool_call_id="child-c1"), session_id="sub-1"))
    assert handle.status_rejections == {}


def test_model_text_frame_never_carries_a_status():
    """Even a ``_meta`` on a text-chunk frame is refused: a wait is never
    created by anything that arrives alongside prose."""
    handle = _handle()
    events = handle._handle_update(
        _update_msg(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "I am waiting for input."},
            },
            meta=_meta(phase=STATUS_PHASE_WAITING, wait_reason=WAIT_REASON_INPUT),
        )
    )
    assert [e.kind for e in events] == [EVENT_TEXT_CHUNK]
    assert handle.status_rejections == {"model_text_frame": 1}


def test_model_text_that_looks_like_a_status_is_only_text():
    """The model writing the wire shape into its answer creates nothing."""
    handle = _handle()
    prose = (
        '{"kirocrew/status": {"version": 1, "phase": "waiting", '
        '"wait_reason": "waiting_input", "cancellable": true}}'
    )
    events = handle._handle_update(
        _update_msg(
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": prose}}
        )
    )
    assert [e.kind for e in events] == [EVENT_TEXT_CHUNK]
    assert events[0].status is None
    assert handle.status_rejections == {}


def test_session_mismatch_is_rejected():
    handle = _handle()
    events = handle._handle_update(_update_msg(_tool_call(), meta=_meta(session_id="someone-else")))
    assert _status_events(events) == []
    assert handle.status_rejections == {"session_mismatch": 1}


def test_version_gate_applies_on_the_handle_path():
    handle = _handle()
    events = handle._handle_update(
        _update_msg(
            _tool_call(),
            meta={
                STATUS_EXTENSION_KEY: {
                    "version": 2,
                    "phase": STATUS_PHASE_WAITING,
                    "wait_reason": WAIT_REASON_INPUT,
                }
            },
        )
    )
    assert _status_events(events) == []
    assert handle.status_rejections == {"version_unsupported": 1}


def test_rejection_logs_once_per_reason_per_turn(caplog):
    handle = _handle()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.session_handle"):
        for _ in range(3):
            handle._handle_update(_update_msg(_tool_call(), meta=_meta(session_id="someone-else")))
    assert handle.status_rejections == {"session_mismatch": 3}
    assert caplog.text.count("status_rejected") == 1


def test_rejection_counters_reset_per_turn():
    handle = _handle()
    handle._handle_update(_update_msg(_tool_call(), meta=_meta(session_id="someone-else")))
    assert handle.status_rejections == {"session_mismatch": 1}
    # The per-turn reset lives at the top of _run_turn; exercise the same
    # fields it clears rather than driving a whole prompt.
    handle._status_rejected.clear()
    assert handle.status_rejections == {}


def test_tool_call_frame_without_the_extension_emits_no_status():
    handle = _handle()
    events = handle._handle_update(_update_msg(_tool_call()))
    assert [e.kind for e in events] == [EVENT_TOOL_CALL]
    assert handle.status_rejections == {}


def test_platform_limited_has_its_own_metric_bucket():
    """The declared degradation is visible as a closed-enum evidence class."""
    assert (
        _watchdog_evidence_class(
            "platform_limited: shell child 42 alive, subtree flat (cpu +0ns); "
            "stdin-block evidence unavailable on this platform"
        )
        == "platform_limited"
    )
    assert (
        _watchdog_evidence_class("platform_limited: mcp subtree unobservable (no backend)")
        == "platform_limited"
    )
    # Ordering: the platform_limited text names a shell child / mcp subtree,
    # which must not fall into those buckets.
    assert _watchdog_evidence_class("shell child 42 alive") == "shell"
    assert _watchdog_evidence_class("mcp subtree flat (io +0B cpu +0t)") == "mcp_flat"


# ── Native-subtask counting at the parent recovery boundary ─────────────────


def test_child_routed_frames_are_counted_per_child_session():
    """RFC §14.8: a native child has identity and tool events but no cancel or
    resume of its own; the parent counts them so the recovery boundary has a
    number. Counting never mints a status for the child."""
    handle = _handle()
    assert handle.native_child_sessions == frozenset()
    handle._handle_update(_update_msg(_tool_call(tool_call_id="a"), session_id="sub-1"))
    handle._handle_update(_update_msg(_tool_call(tool_call_id="b"), session_id="sub-1"))
    handle._handle_update(_update_msg(_tool_call(tool_call_id="c"), session_id="sub-2"))
    assert handle.native_child_sessions == frozenset({"sub-1", "sub-2"})
    # The parent's own frames are never counted as children.
    handle._handle_update(_update_msg(_tool_call(tool_call_id="d")))
    assert handle.native_child_sessions == frozenset({"sub-1", "sub-2"})
    assert handle.status_rejections == {}
