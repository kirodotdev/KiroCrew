"""Keep a binding's channel name and archive state in step with Slack.

Slack tells the app when one of its channels is archived, unarchived or renamed
(``channel_*`` for a public channel, ``group_*`` for a private one; the manifest
subscribes to them only while the ``code_channels`` switch is on). For a channel
that is a code channel, the binding records the new fact and the dashboard
re-pushes its slot list, so the session's badge reads "Archived" -- or the new
``#name`` -- without a reload. A channel that is not bound is ignored, and so is
every event while the code-channels gate is shut (an existing install keeps its
subscriptions after the switch goes off).

The facts land on the binding through the store (:meth:`CodeChannelStore.update_binding`):
``archived_at`` is stamped on archive and cleared on unarchive, and
``channel_name`` follows a rename. Each applied change is recorded in the SEL as
``slack.code_channel.lifecycle``. An event with no workspace id, from a
workspace the live origin gate (``slack_gate.check_message_origin``) does not
admit, or not naming exactly the binding's recorded workspace (a binding with
none matches nothing), is refused and audited: the same origin rule the
agent-session events follow.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from kiro_crew.platform import current_context
from kiro_crew.sel import sel
from kiro_crew.slack.code_channels.store import Binding, store

logger = logging.getLogger(__name__)

ARCHIVE_EVENTS = frozenset({"channel_archive", "group_archive"})
UNARCHIVE_EVENTS = frozenset({"channel_unarchive", "group_unarchive"})
RENAME_EVENTS = frozenset({"channel_rename", "group_rename"})
LIFECYCLE_EVENTS = ARCHIVE_EVENTS | UNARCHIVE_EVENTS | RENAME_EVENTS

_OPERATION = "slack.code_channel.lifecycle"
_background: set[asyncio.Task[Any]] = set()


def _audit(caller: str, outcome: str, resources: str, error: str = "") -> None:
    sel().log_api_access(
        caller=caller or "slack",
        operation=_OPERATION,
        outcome=outcome,
        source="slack",
        resources=resources,
        error=error,
    )


def _channel_of(event: dict[str, Any]) -> tuple[str, str]:
    """``(channel_id, new_name)``; a rename carries the channel as an object."""
    raw = event.get("channel")
    if isinstance(raw, dict):
        return str(raw.get("id") or ""), str(raw.get("name") or "")
    return str(raw or ""), ""


def _changes(event_type: str, new_name: str) -> dict[str, Any]:
    if event_type in ARCHIVE_EVENTS:
        return {"archived_at": time.time()}
    if event_type in UNARCHIVE_EVENTS:
        return {"archived_at": 0.0}
    if event_type in RENAME_EVENTS and new_name.strip():
        return {"channel_name": new_name.strip()}
    return {}


def _apply(binding: Binding, changes: dict[str, Any]) -> Binding | None:
    """Write *changes* onto the stored binding; None when it is gone meanwhile."""
    return store().update_binding(binding.channel_id, **changes)


def _gate_open() -> bool:
    """The code-channels gate: with the feature off these events change nothing."""
    from kiro_crew.slack.code_channels.binding import code_channels_on, live_config

    return code_channels_on(live_config())


def _push_slots() -> None:
    try:
        from kiro_crew.slack.handler_runtime.access import get_dashboard_state

        state = get_dashboard_state()
        if state is not None and hasattr(state, "push_slots_update"):
            state.push_slots_update()
    except Exception:
        logger.debug("code channel lifecycle: slots push failed", exc_info=True)


async def handle_lifecycle_event(payload: dict[str, Any], event: dict[str, Any]) -> bool:
    """Apply one archive / unarchive / rename event; True when a binding changed.

    Events for one channel are applied one at a time, in the order they were
    handed over: each takes the channel's lock before its first await, and an
    asyncio lock wakes its waiters first-come first-served. Without that an
    archive whose store read is slow could land after the unarchive that
    followed it and leave a live channel marked Archived.
    """
    channel, _new_name = _channel_of(event)
    if not channel:
        return False
    lock = _channel_locks.get(channel)
    if lock is None:
        lock = _channel_locks[channel] = asyncio.Lock()
    _lock_users[channel] = _lock_users.get(channel, 0) + 1
    try:
        async with lock:
            return await _handle(payload, event)
    finally:
        _lock_users[channel] -= 1
        if not _lock_users[channel]:
            del _lock_users[channel]
            _channel_locks.pop(channel, None)


#: One lock per channel with events in flight; dropped when the last one ends.
_channel_locks: dict[str, asyncio.Lock] = {}
_lock_users: dict[str, int] = {}


async def _handle(payload: dict[str, Any], event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "")
    if event_type not in LIFECYCLE_EVENTS:
        return False
    channel, new_name = _channel_of(event)
    if not channel or not _gate_open():
        return False
    binding = await asyncio.to_thread(store().get_binding, channel)
    if binding is None:
        return False
    caller = str(event.get("user") or "")
    resources = f"channel={channel} event={event_type}"
    team = str(payload.get("team_id") or event.get("team") or "")
    if not team:
        _audit(caller, "denied", resources, "missing_team_id")
        return False
    if not current_context().slack_gate.check_message_origin(team):
        _audit(caller, "denied", resources, "workspace_not_allowed")
        return False
    if team != binding.team_id:
        # An unknown recorded workspace matches nothing: a binding without one
        # cannot be changed by an event from any workspace.
        _audit(caller, "denied", resources, "workspace_mismatch")
        return False
    changes = _changes(event_type, new_name)
    if not changes:
        return False
    try:
        stored = await asyncio.to_thread(_apply, binding, changes)
    except Exception as exc:
        logger.warning("code channel %s: %s not recorded: %s", channel, event_type, exc)
        _audit(caller, "error", resources, type(exc).__name__)
        return False
    if stored is None:
        return False
    _audit(caller, "allowed", resources)
    _push_slots()
    return True


def dispatch(payload: dict[str, Any], event: dict[str, Any]) -> None:
    """Handle a lifecycle event off the socket listener (fire and forget)."""
    task = asyncio.ensure_future(handle_lifecycle_event(payload, event))
    _background.add(task)
    task.add_done_callback(_background.discard)
