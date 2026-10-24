"""Append-only session event log and runtime projections.

Kiro BYO 0.1.3 treats the typed event stream as the durable session truth.
Provider-facing messages, sticky routing pins and provider continuation metadata
are projections of that stream rather than separate persistence records.

The live ``Session.messages`` / ``Session.route_pins`` values remain in-memory
caches for compatibility with ACP/provider code. Persistence synchronizes legacy
or direct mutations back into explicit events before writing the session file.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Any, Iterable

SESSION_EVENT_SCHEMA_VERSION = 2

_MESSAGE_APPEND_EVENTS = frozenset({
    "user/message",
    "assistant/message",
    "assistant/attempt",
    "tool/result",
    "system/message",
})


def _clone(value: Any) -> Any:
    """Return a persistence-safe deep copy without requiring JSON round-trips."""
    return copy.deepcopy(value)


def _split_provider_context(message: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Separate provider-private continuation state from the visible message event."""
    row = _clone(message)
    raw = row.pop("_kiro_provider_context", None)
    return row, _clone(raw) if isinstance(raw, dict) else None


def event_type_for_message(message: dict[str, Any]) -> str:
    role = str(message.get("role") or "")
    if role == "user":
        return "user/message"
    if role == "assistant":
        return "assistant/attempt" if message.get("tool_calls") else "assistant/message"
    if role == "tool":
        return "tool/result"
    if role == "system":
        return "system/message"
    return "message/append"


def append_event(
    events: list[dict[str, Any]],
    event_type: str,
    data: dict[str, Any] | None = None,
    *,
    timestamp: float | None = None,
) -> dict[str, Any]:
    """Append one typed event and return the committed event envelope."""
    event = {
        "seq": len(events),
        "time": float(time.time() if timestamp is None else timestamp),
        "type": str(event_type),
        "data": _clone(dict(data or {})),
    }
    events.append(event)
    return event


def _valid_event(row: Any, expected_seq: int) -> bool:
    if not isinstance(row, dict):
        return False
    if int(row.get("seq", -1)) != expected_seq:
        return False
    if not isinstance(row.get("type"), str) or not str(row.get("type") or ""):
        return False
    if not isinstance(row.get("data", {}), dict):
        return False
    try:
        float(row.get("time", 0.0))
    except (TypeError, ValueError):
        return False
    return True


def validate_events(events: Any) -> bool:
    return isinstance(events, list) and all(_valid_event(row, i) for i, row in enumerate(events))


def derive_messages(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive the active provider transcript from an append-only event stream."""
    messages: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event_type in _MESSAGE_APPEND_EVENTS or event_type == "message/append":
            message = data.get("message")
            if isinstance(message, dict):
                messages.append(_clone(message))
            continue
        if event_type == "provider/context":
            context = data.get("context")
            try:
                index = int(data.get("message_index", -1))
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(messages) and isinstance(context, dict):
                messages[index]["_kiro_provider_context"] = _clone(context)
            continue
        if event_type == "transcript/truncate":
            try:
                length = max(0, int(data.get("length", 0)))
            except (TypeError, ValueError):
                continue
            del messages[length:]
            continue
        if event_type == "transcript/reset":
            raw = data.get("messages")
            if isinstance(raw, list):
                messages = [_clone(row) for row in raw if isinstance(row, dict)]
            continue
        if event_type == "compaction/summary":
            message = data.get("message")
            messages = [_clone(message)] if isinstance(message, dict) else []
            continue
        if event_type == "transcript/clear":
            messages.clear()
            continue
        if event_type == "assistant/revision":
            message = data.get("message")
            if not isinstance(message, dict):
                continue
            for idx in range(len(messages) - 1, -1, -1):
                if str(messages[idx].get("role") or "") == "assistant":
                    messages[idx] = _clone(message)
                    break
            else:
                messages.append(_clone(message))
    return messages


def derive_route_pins(events: Iterable[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """Project sticky routing state from semantic routing events."""
    pins: dict[str, tuple[str, str]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        key = str(data.get("key") or "")
        if event_type == "routing/pin":
            provider_id = str(data.get("provider_id") or "")
            model_id = str(data.get("model_id") or "")
            if key and provider_id and model_id:
                pins[key] = (provider_id, model_id)
        elif event_type == "routing/unpin" and key:
            pins.pop(key, None)
        elif event_type == "routing/clear":
            pins.clear()
    return pins


@dataclass(frozen=True)
class SessionProjection:
    """Current runtime state reconstructed from the durable event stream."""

    messages: list[dict[str, Any]]
    route_pins: dict[str, tuple[str, str]]


def derive_session_projection(events: Iterable[dict[str, Any]]) -> SessionProjection:
    rows = list(events)
    return SessionProjection(messages=derive_messages(rows), route_pins=derive_route_pins(rows))


def _append_message_events(events: list[dict[str, Any]], message: dict[str, Any], *, message_index: int) -> None:
    visible, context = _split_provider_context(message)
    append_event(events, event_type_for_message(visible), {"message": visible})
    if context is not None:
        append_event(
            events,
            "provider/context",
            {"message_index": int(message_index), "context": context},
        )


def bootstrap_events(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create a canonical event stream from a legacy message projection."""
    events: list[dict[str, Any]] = []
    message_index = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        _append_message_events(events, message, message_index=message_index)
        message_index += 1
    return events


def append_message(session: Any, message: dict[str, Any]) -> None:
    """Commit one provider-visible message and its private continuation context."""
    committed = _clone(message)
    message_index = len(session.messages)
    session.messages.append(committed)
    _append_message_events(session.events, committed, message_index=message_index)


def truncate_messages(session: Any, length: int, *, reason: str = "") -> None:
    length = max(0, int(length))
    del session.messages[length:]
    append_event(session.events, "transcript/truncate", {"length": length, "reason": str(reason or "")})


def clear_messages(session: Any, *, reason: str = "") -> None:
    session.messages.clear()
    append_event(session.events, "transcript/clear", {"reason": str(reason or "")})


def reset_messages(session: Any, messages: Iterable[dict[str, Any]], *, reason: str = "") -> None:
    rows = [_clone(row) for row in messages if isinstance(row, dict)]
    session.messages[:] = rows
    visible_rows: list[dict[str, Any]] = []
    contexts: list[tuple[int, dict[str, Any]]] = []
    for index, row in enumerate(rows):
        visible, context = _split_provider_context(row)
        visible_rows.append(visible)
        if context is not None:
            contexts.append((index, context))
    append_event(session.events, "transcript/reset", {"messages": visible_rows, "reason": str(reason or "")})
    for index, context in contexts:
        append_event(session.events, "provider/context", {"message_index": index, "context": context})


def compact_to_summary(session: Any, message: dict[str, Any]) -> None:
    committed = _clone(message)
    session.messages[:] = [committed]
    visible, context = _split_provider_context(committed)
    append_event(session.events, "compaction/summary", {"message": visible})
    if context is not None:
        append_event(session.events, "provider/context", {"message_index": 0, "context": context})


def revise_last_assistant(session: Any, message: dict[str, Any], *, reason: str = "review") -> None:
    committed = _clone(message)
    target_index = -1
    for idx in range(len(session.messages) - 1, -1, -1):
        if str(session.messages[idx].get("role") or "") == "assistant":
            session.messages[idx] = committed
            target_index = idx
            break
    else:
        session.messages.append(committed)
        target_index = len(session.messages) - 1
    visible, context = _split_provider_context(committed)
    append_event(session.events, "assistant/revision", {"message": visible, "reason": reason})
    if context is not None:
        append_event(session.events, "provider/context", {"message_index": target_index, "context": context})


def set_route_pin(session: Any, key: str, provider_id: str, model_id: str, *, reason: str = "") -> None:
    key = str(key or "")
    provider_id = str(provider_id or "")
    model_id = str(model_id or "")
    if not key or not provider_id or not model_id:
        return
    value = (provider_id, model_id)
    if getattr(session, "route_pins", {}).get(key) == value:
        return
    session.route_pins[key] = value
    append_event(
        session.events,
        "routing/pin",
        {"key": key, "provider_id": provider_id, "model_id": model_id, "reason": str(reason or "")},
    )


def clear_route_pin(session: Any, key: str, *, reason: str = "") -> None:
    key = str(key or "")
    if not key or key not in getattr(session, "route_pins", {}):
        return
    session.route_pins.pop(key, None)
    append_event(session.events, "routing/unpin", {"key": key, "reason": str(reason or "")})


def ensure_event_projection(session: Any, *, reason: str = "compat_sync") -> None:
    """Synchronize legacy/direct ``messages`` mutations into the canonical log."""
    current = [_clone(row) for row in getattr(session, "messages", []) if isinstance(row, dict)]
    events = getattr(session, "events", None)
    if not isinstance(events, list):
        session.events = bootstrap_events(current)
        return
    if derive_messages(events) != current:
        reset_messages(session, current, reason=reason)


def ensure_route_pin_projection(session: Any, *, reason: str = "compat_sync") -> None:
    """Synchronize legacy/direct sticky-pin mutation into explicit routing events."""
    events = getattr(session, "events", None)
    if not isinstance(events, list):
        session.events = []
        events = session.events
    projected = derive_route_pins(events)
    raw = getattr(session, "route_pins", {})
    current: dict[str, tuple[str, str]] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if (
                isinstance(key, str)
                and isinstance(value, (tuple, list))
                and len(value) == 2
                and all(isinstance(item, str) and item for item in value)
            ):
                current[key] = (str(value[0]), str(value[1]))
    for key in sorted(set(projected) - set(current)):
        append_event(events, "routing/unpin", {"key": key, "reason": str(reason or "")})
    for key in sorted(current):
        if projected.get(key) != current[key]:
            provider_id, model_id = current[key]
            append_event(
                events,
                "routing/pin",
                {"key": key, "provider_id": provider_id, "model_id": model_id, "reason": str(reason or "")},
            )


def append_lifecycle(session: Any, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return append_event(session.events, event_type, data)
