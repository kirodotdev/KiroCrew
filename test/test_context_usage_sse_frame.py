"""SSE-only consumers must see the context meter via ``broadcast_context_usage``.

A WebSocket client gets the context reading through the typed
``broadcast_ws("context_usage", ...)`` frame, but an SSE-only consumer (an API
client or the KAS soak harness) never opens a WebSocket. The single writer
``broadcast_context_usage`` therefore ALSO mirrors the same payload into the
slot's live stream queue as an ephemeral wire-only frame, under the same
``context_usage`` name — so every producer (end-of-turn, compaction, cron
injection, reset) feeds the SSE channel identically and it cannot drift.

The mirror must be ephemeral: it lands in ``_pending`` for the live reader to
drain, but is never persisted, never added to ``messages``, and never counted
in ``total_messages``.
"""

from __future__ import annotations

import json

from chat_test_helpers import _make_state

from kiro_crew.dashboard.state import _ChatSlot


def _register_slot(state, key="dashboard:chat-1", **kw):
    slot = _ChatSlot(key=key, **kw)
    state._slots[key] = slot
    return slot


def test_broadcast_pushes_context_usage_sse_frame(tmp_path):
    """A single broadcast enqueues one ephemeral ``context_usage`` frame."""
    state = _make_state(tmp_path)
    slot = _register_slot(state)
    payload = {"slot": slot.key, "pct": 42.5, "used_tokens": 85000, "window_tokens": 200000}

    state.broadcast_context_usage(slot.key, payload)

    drained = slot.drain()
    frames = [m for m in drained if m.get("cls") == "context_usage"]
    assert len(frames) == 1
    frame = frames[0]
    # Reuses the wire's existing name — NOT a second "context_window_update" spelling.
    assert frame["role"] == "context_usage"
    assert json.loads(frame["content"]) == payload


def test_sse_mirror_is_ephemeral_not_persisted(tmp_path):
    """The frame is live-only: transcript and lifetime count stay untouched."""
    state = _make_state(tmp_path)
    slot = _register_slot(state)

    state.broadcast_context_usage(slot.key, {"slot": slot.key, "pct": 10.0})

    assert slot.messages == []  # not appended to the transcript
    assert slot.total_messages == 0  # not counted toward lifetime messages


def test_every_producer_feeds_sse_via_single_writer(tmp_path):
    """Any call site reaching broadcast_context_usage emits the SSE frame.

    Guards the single-writer invariant: producers do NOT hand-roll their own
    ``_pending`` push, so covering the writer covers all of them.
    """
    state = _make_state(tmp_path)
    slot = _register_slot(state)

    # A reset frame (post-compaction) carries no token counts — still mirrored.
    state.broadcast_context_usage(slot.key, {"slot": slot.key, "pct": 0.0, "reset": True})
    drained = slot.drain()
    frames = [m for m in drained if m.get("cls") == "context_usage"]
    assert len(frames) == 1
    assert json.loads(frames[0]["content"])["reset"] is True


def test_missing_slot_is_a_noop(tmp_path):
    """An unknown slot key must not raise (WS broadcast still fires upstream)."""
    state = _make_state(tmp_path)
    state.broadcast_context_usage("dashboard:does-not-exist", {"slot": "x", "pct": 1.0})


def test_non_serializable_payload_skips_sse_mirror(tmp_path):
    """A non-JSON-serializable payload must not raise or enqueue a frame."""
    state = _make_state(tmp_path)
    slot = _register_slot(state)

    state.broadcast_context_usage(slot.key, {"slot": slot.key, "pct": object()})

    assert [m for m in slot.drain() if m.get("cls") == "context_usage"] == []


def test_push_wire_frame_is_live_only(tmp_path):
    """The slot-owned helper queues for SSE without persisting."""
    state = _make_state(tmp_path)
    slot = _register_slot(state)

    slot.push_wire_frame("context_usage", json.dumps({"pct": 5.0}))

    drained = slot.drain()
    assert len(drained) == 1
    assert drained[0]["cls"] == "context_usage"
    assert drained[0]["role"] == "context_usage"
    assert slot.messages == []
    assert slot.total_messages == 0
