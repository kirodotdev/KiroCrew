"""How the ``subscribe_subagents`` reconnect replay reaches one socket.

``ws.py`` builds the replay frames; this module filters them through the owner
and per-slot scope gates, packages them, and sends the end-of-replay marker.
``ws.py`` re-exports every name here, so callers keep importing from it.
"""

from __future__ import annotations

from typing import Any

# Reconnect replay: more subagent frames than this collapse into ONE
# subagent_snapshot_batch frame (scale plumbing — a per-agent burst at
# 60-100 agents saturates the socket the moment a client reconnects).
SUBAGENT_REPLAY_BATCH_THRESHOLD = 8


def _subagent_replay_has_owner(frame: object) -> bool:
    """Whether a replay frame names the chat that owns the subagent.

    ``spawn_run`` can continue in degraded mode when caller identity cannot be
    resolved, leaving ``parent_session_key == ""``. Such a run is visible in the
    global spawn inventory, but it has no session-scoped destination. Sending an
    empty ``slot`` to a chat client is unsafe: older reducers interpreted it as
    the currently active slot, so a fresh popout adopted unrelated agents.
    """
    if not isinstance(frame, dict):
        return False
    data = frame.get("data")
    if not isinstance(data, dict):
        return False
    slot = data.get("slot")
    return isinstance(slot, str) and bool(slot.strip())


async def _send_subagent_replay(ws: Any, state: Any, frames: list[dict]) -> None:
    """Send replay *frames* to *ws* through the same gates the live frames take.

    Per-slot scope gate on the reconnect replay. The broadcast chokepoint covers
    live events, but this replay writes to the socket directly, so it must apply
    the same check. Dashboard users pass through ``_ws_client_allowed``
    unconditionally.

    Ownership is a separate prerequisite: an unresolved parent produces
    ``slot: ''``. That run remains visible through the global spawn inventory, but
    there is no chat authorized to adopt it. Filtered before batching so even an
    older client with an empty-slot fallback never receives the orphan as
    session-scoped state.
    """
    frames = [
        _f
        for _f in frames
        if _subagent_replay_has_owner(_f)
        and state._ws_client_allowed(ws, str(_f.get("type", "")), _f.get("data", {}))
    ]
    try:
        if len(frames) > SUBAGENT_REPLAY_BATCH_THRESHOLD:
            # ``subagent_snapshot_batch`` is deliberately absent from every
            # ws_event_scope table: it is delivery packaging for frames THIS socket
            # is already cleared for (filtered item-by-item above), never a
            # broadcast. Routing it through the gate would reject it as an unknown
            # event and cost the app its whole replay, so keep this send and the
            # per-item filter together.
            await ws.send_json({"type": "subagent_snapshot_batch", "data": {"items": frames}})
        else:
            for _frame in frames:
                await ws.send_json(_frame)
    except Exception:
        pass


async def send_subagent_replay_done(ws: Any, ws_app: str | None) -> None:
    """Mark the end of the in-memory replay, for a dashboard socket only.

    The dashboard keeps the cards it held across the reconnect and reconciles
    each replayed run into its card; on this marker it drops the held cards the
    replay did not name (a run a restarted gateway does not know). Below the
    batch threshold nothing else marks the end. The durable fold runs later on
    the socket's replay queue and its ``subagent_done`` frames add cards of their
    own. An app socket (*ws_app* set) never receives it: no ws_event_scope table
    names it.
    """
    if ws_app:
        return
    try:
        await ws.send_json({"type": "subagent_replay_done", "data": {}})
    except Exception:
        pass
