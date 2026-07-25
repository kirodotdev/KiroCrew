"""Notification bus core: payload schema v2, channel registry, delivery pipeline.

Design (see docs/request-for-change/rfc-local-notification-bus.md):

- Every notification flows through :meth:`NotificationBus.push` as a
  :class:`NotificationPayload`.
- Channels are producer-declared. Phase 1 registers only the system channels;
  app channels arrive in Phase 2 via the app manifest.
- Priorities: ``critical`` > ``default`` > ``passive``. The channel supplies a
  default; an explicit payload priority wins.
- The persisted note dict keeps the legacy ``kind`` field alongside the v2
  ``source``/``channel`` fields so existing frontend selectors keep working.
- Legacy JSONL rows (``kind`` only) are normalized at load time via
  :func:`normalize_note`; the file is never rewritten in place for migration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── Constants ──

PRIORITIES = ("critical", "default", "passive")
_DEFAULT_PRIORITY = "default"
_SYSTEM_SOURCE = "system"

# System channels mirror the legacy notification kinds one-to-one.
# Values are the channel's default priority (RFC "Priority semantics"):
# approval is critical (blocks an agent turn); subagent is passive (the
# completion event already injects into the originating chat session).
SYSTEM_CHANNELS: dict[str, str] = {
    "system.cron": _DEFAULT_PRIORITY,
    "system.heartbeat": _DEFAULT_PRIORITY,
    "system.hook": _DEFAULT_PRIORITY,
    "system.agent": _DEFAULT_PRIORITY,
    "system.approval": "critical",
    "system.subagent": "passive",
    "system.taskrunner": _DEFAULT_PRIORITY,
}

# Fallback channel for legacy kinds that have no dedicated system channel
# (defensive: nothing emits unknown kinds today, but old JSONL rows might).
_FALLBACK_CHANNEL = "system.agent"

_MAX_TITLE_LEN = 500
_MAX_BODY_LEN = 20000

# Schema-owned note keys. Meta merging skips these entirely: setdefault alone
# would only protect keys already present, letting meta smuggle unvalidated
# values into schema fields that were unset (e.g. meta={"url": "https://..."}
# bypassing the deep-link validation when payload.url is None).
_RESERVED_NOTE_KEYS = frozenset(
    {
        "kind",
        "source",
        "channel",
        "priority",
        "title",
        "body",
        "ts",
        "group_key",
        "actions",
        "url",
        "icon",
        "ttl",
        # Sink/frontend-owned status fields: a caller must not be able to
        # pre-suppress or pre-acknowledge a note it is pushing.
        "silenced",
        "acked",
    }
)


class NotificationValidationError(ValueError):
    """Raised when a payload fails schema validation or channel resolution."""


@dataclass
class NotificationPayload:
    """Schema v2 notification (RFC "Notification payload").

    Phase 1 carries the full field set so persistence and the wire format are
    stable from the start; ``actions``/``url``/``icon``/``ttl`` are consumed by
    the frontend in later phases.
    """

    source: str
    channel: str
    title: str
    body: str
    priority: str | None = None  # None = use the channel default
    kind: str | None = None  # legacy kind override; None = derive from channel
    group_key: str | None = None
    actions: list[dict[str, Any]] | None = None
    url: str | None = None
    icon: str | None = None
    ttl: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Raise :class:`NotificationValidationError` on any schema violation."""
        if not self.source or not isinstance(self.source, str):
            raise NotificationValidationError("source is required")
        if not self.channel or not isinstance(self.channel, str):
            raise NotificationValidationError("channel is required")
        if not self.title or not isinstance(self.title, str):
            raise NotificationValidationError("title is required")
        if not isinstance(self.body, str):
            raise NotificationValidationError("body must be a string")
        if len(self.title) > _MAX_TITLE_LEN:
            raise NotificationValidationError(f"title exceeds {_MAX_TITLE_LEN} chars")
        if len(self.body) > _MAX_BODY_LEN:
            raise NotificationValidationError(f"body exceeds {_MAX_BODY_LEN} chars")
        if self.priority is not None and self.priority not in PRIORITIES:
            raise NotificationValidationError(
                f"priority must be one of {PRIORITIES}, got {self.priority!r}"
            )
        if self.ttl is not None and (not isinstance(self.ttl, int) or self.ttl <= 0):
            raise NotificationValidationError("ttl must be a positive integer")
        if self.actions is not None:
            for action in self.actions:
                if not isinstance(action, dict) or not action.get("id") or not action.get("label"):
                    raise NotificationValidationError(
                        "each action requires 'id' and 'label' fields"
                    )
        if self.url is not None:
            # Deep-links are dashboard-internal routes only (RFC "Security
            # considerations") — no external schemes, no protocol-relative
            # URLs ("//evil.example.com" resolves to an external origin),
            # and no backslashes: browsers normalize "\" to "/" per the
            # WHATWG URL spec, so "/\evil.example.com" is equivalent to
            # "//evil.example.com".
            if not self.url.startswith("/"):
                raise NotificationValidationError("url must be a dashboard-internal path")
            if self.url.startswith("//") or "\\" in self.url:
                raise NotificationValidationError("url must be a dashboard-internal path")
            if any(c in self.url for c in "\t\n\r"):
                # WHATWG URL parsing strips ASCII tab/newline/CR before
                # interpreting the URL, so "/\t/evil.example.com" would become
                # the protocol-relative "//evil.example.com".
                raise NotificationValidationError("url must be a dashboard-internal path")


def payload_from_legacy(
    kind: str, title: str, body: str, meta: dict[str, Any] | None = None
) -> NotificationPayload:
    """Build a v2 payload from the legacy ``notify(kind, ...)`` call shape.

    Legacy callers never validated inputs and ``notify()`` never dropped a
    notification, so this adapter repairs rather than rejects: oversized
    titles/bodies are truncated, an empty title gets a placeholder, and the
    original ``kind`` is preserved verbatim even when it has no dedicated
    system channel (the frontend filters on ``note.kind``).
    """
    channel = f"system.{kind}"
    if channel not in SYSTEM_CHANNELS:
        channel = _FALLBACK_CHANNEL
    # Coerce first: notify() never raised, and a non-string here would
    # TypeError out of the adapter before validation could catch it.
    if not isinstance(title, str):
        title = str(title) if title is not None else ""
    if not isinstance(body, str):
        body = str(body) if body is not None else ""
    if not title:
        title = "Notification"
    elif len(title) > _MAX_TITLE_LEN:
        title = title[: _MAX_TITLE_LEN - 1] + "…"
    if len(body) > _MAX_BODY_LEN:
        body = body[: _MAX_BODY_LEN - 1] + "…"
    return NotificationPayload(
        source=_SYSTEM_SOURCE,
        channel=channel,
        title=title,
        body=body,
        kind=kind,
        meta=dict(meta) if meta else {},
    )


def normalize_note(note: dict[str, Any]) -> dict[str, Any]:
    """Normalize a persisted JSONL row to schema v2 in memory.

    Legacy rows carry only ``kind``; v2 rows carry ``source``/``channel``/
    ``priority``. Missing v2 fields are derived from ``kind`` so every
    in-memory note has both shapes. The input dict is returned mutated —
    callers own the row and the file itself is never rewritten for migration.
    """
    if "channel" not in note:
        kind = note.get("kind", "agent")
        channel = f"system.{kind}"
        if channel not in SYSTEM_CHANNELS:
            channel = _FALLBACK_CHANNEL
        note["channel"] = channel
        note["source"] = _SYSTEM_SOURCE
    if "priority" not in note:
        note["priority"] = SYSTEM_CHANNELS.get(note["channel"], _DEFAULT_PRIORITY)
    return note


class NotificationBus:
    """Single entry point for notification delivery.

    The bus validates and enriches payloads, then hands the resulting note
    dict to the delivery sink (``DashboardState`` in Phase 1 — it owns the
    in-memory log, unread counter, SSE/WS broadcast, and JSONL persistence).
    """

    def __init__(self, sink: Callable[[dict[str, Any]], None]) -> None:
        self._sink = sink
        self._channels: dict[str, str] = dict(SYSTEM_CHANNELS)

    def register_channel(self, channel: str, default_priority: str = _DEFAULT_PRIORITY) -> None:
        """Register a channel (Phase 2 uses this for app-declared channels)."""
        if default_priority not in PRIORITIES:
            raise NotificationValidationError(
                f"default_priority must be one of {PRIORITIES}, got {default_priority!r}"
            )
        self._channels[channel] = default_priority

    def unregister_channel(self, channel: str) -> None:
        """Remove a channel; system channels cannot be removed."""
        if channel in SYSTEM_CHANNELS:
            return
        self._channels.pop(channel, None)

    def unregister_app_channels(self, app_name: str) -> int:
        """Remove every channel registered under *app_name* (``<app>.<id>``).

        Called on app uninstall/disable so channels don't linger as ghosts in
        the registry. System channels are never removed (the ``<app>.``
        prefix cannot collide with them: ``system`` is a reserved app name).
        Returns the number of channels removed.
        """
        prefix = f"{app_name}."
        doomed = [
            c for c in self._channels if c.startswith(prefix) and c not in SYSTEM_CHANNELS
        ]
        for channel in doomed:
            self._channels.pop(channel, None)
        return len(doomed)

    def is_registered(self, channel: str) -> bool:
        return channel in self._channels

    def channels(self) -> dict[str, str]:
        """Snapshot of registered channels: {channel: default_priority}."""
        return dict(self._channels)

    def push(self, payload: NotificationPayload) -> dict[str, Any]:
        """Validate, enrich, and deliver a notification.

        Returns the note dict handed to the sink. Raises
        :class:`NotificationValidationError` for schema violations or an
        unregistered channel.
        """
        payload.validate()
        if payload.channel not in self._channels:
            raise NotificationValidationError(f"unregistered channel: {payload.channel!r}")
        priority = payload.priority or self._channels[payload.channel]
        # Legacy ``kind`` kept for frontend selectors during the transition.
        # The legacy adapter passes the caller's kind verbatim (it may have no
        # dedicated channel); otherwise derive it from the channel's last
        # dotted segment (channel "system.cron" -> kind "cron").
        note: dict[str, Any] = {
            "kind": payload.kind or payload.channel.rsplit(".", 1)[-1],
            "source": payload.source,
            "channel": payload.channel,
            "priority": priority,
            "title": payload.title,
            "body": payload.body,
            "ts": datetime.now(tz=timezone.utc).isoformat(),
        }
        for key, value in (
            ("group_key", payload.group_key),
            ("actions", payload.actions),
            ("url", payload.url),
            ("icon", payload.icon),
            ("ttl", payload.ttl),
        ):
            if value is not None:
                note[key] = value
        if payload.meta:
            # Meta keys merge flat into the note (legacy behavior — the
            # frontend reads note.job_id / note.slot / note.session_key
            # directly). The merge is default-safe: a meta key is admitted only
            # if it clears THREE independent guards, so no single denylist has
            # to be kept perfectly in sync as the schema grows:
            #   1. not already in `note` — every field the builder above wrote
            #      (kind/source/.../group_key/actions/url/icon/ttl, present or
            #      added in future) is off-limits by construction, closing the
            #      "unset schema field" smuggle vector without re-listing them.
            #   2. not underscore-prefixed — all internal broadcast-envelope
            #      keys (DashboardState._broadcast branches on note["_type"] and
            #      reads companion keys like slot/role/content once it is set)
            #      are private by convention; blocking the whole `_`-namespace
            #      neutralizes envelope hijacking for current AND future keys.
            #   3. not in _RESERVED_NOTE_KEYS — the residual sink/frontend-owned
            #      fields that are NOT underscore-prefixed and are set LATER than
            #      this merge (silenced/acked), so guards 1-2 can't catch them.
            for key, value in payload.meta.items():
                if key in note or key.startswith("_") or key in _RESERVED_NOTE_KEYS:
                    continue
                note[key] = value
        self._sink(note)
        return note
