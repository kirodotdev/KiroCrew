"""Per-channel notification settings.

User preferences for each notification channel: mute, priority override, and
bridge routing. Stored in ``~/.kiro/crew/notification_settings.json`` as::

    {"channel_settings": {"system.heartbeat": {"muted": true},
                          "oncall-radar.ticket-update": {"priority": "critical"},
                          "system.approval": {"deliver_to": ["slack"],
                                              "deliver_min_priority": "critical"}}}

Semantics (applied at the delivery sink, keeping the bus pure):

- **muted**: the note is still delivered to history (history is a cache and
  the user asked to silence, not to destroy) but is stamped ``silenced: true``
  and its priority forced to ``passive``, so every attention surface (badge
  count, sound, native banner, feed styling) skips it.
- **priority**: user override wins over the producer-requested priority and
  the channel default.
- **deliver_to** / **deliver_min_priority**: the notification bridge's routing
  rule for this channel -- which chat transports receive the note as an owner
  DM, and the minimum effective priority that routes. Absent or empty means no
  bridging, which is the behavior an install has before a user arms a route.
  Read by ``notifications.bridge.BridgeDispatcher``, never by ``apply()``:
  ``apply()`` mutates the note every sink sees, and routing is one sink's
  decision about a note it did not change.
- ``system.approval`` is protected: it cannot be muted and its priority
  cannot be lowered (approval still interrupts while heartbeat can be
  silenced everywhere). Protection is about attention, not about egress, so a
  protected channel may be routed or unrouted freely.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.notifications.bridge import (
    DEFAULT_DELIVER_MIN_PRIORITY,
    normalize_deliver_to,
    normalize_min_priority,
)
from kiro_crew.notifications.bus import PRIORITIES

logger = logging.getLogger(__name__)

# Channels whose attention semantics the user may not weaken: approvals gate
# agent actions, so silencing them would stall work invisibly.
PROTECTED_CHANNELS = frozenset({"system.approval"})

# The stored keys that decide EGRESS rather than dashboard presentation. Named
# once because two call sites must agree on them -- the settings PUT refuses an
# app token that sets them, and the channels GET withholds them from one -- and a
# set spelled twice is how a third routing key added later reaches an app token
# through whichever site was not updated.
DELIVERY_SETTING_KEYS = frozenset({"deliver_to", "deliver_min_priority"})

_SETTINGS_FILENAME = "notification_settings.json"
_lock = threading.Lock()


def _settings_path():
    return config_dir() / _SETTINGS_FILENAME


class ChannelSettingsError(ValueError):
    """Invalid channel settings input (unknown priority, protected channel)."""


class ChannelSettings:
    """Load/store/apply per-channel user settings.

    In-memory dict guarded by a lock; writes are atomic-rename. Owned by
    ``DashboardState`` (one instance per gateway) like the bus and the rate
    limiter.
    """

    def __init__(self) -> None:
        self._settings: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        path = _settings_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                raw = data.get("channel_settings", {})
                if isinstance(raw, dict):
                    self._settings = {
                        ch: dict(entry)
                        for ch, entry in raw.items()
                        if isinstance(entry, dict)
                    }
        except Exception:
            # Corrupt settings must not take down the gateway; fall back to
            # defaults (everything unmuted, producer priorities honored).
            logger.warning("Failed to load %s; using defaults", path, exc_info=True)
            self._settings = {}

    def all_settings(self) -> dict[str, dict[str, Any]]:
        """Snapshot of every channel's stored settings.

        Lock-free: ``update()`` rebinds ``self._settings`` to a fresh dict
        (never mutates in place), so readers see either the old or the new
        complete mapping. Loop-side callers (``apply()`` on every delivery)
        therefore never block on a worker-thread writer holding the lock
        across the file write.
        """
        settings = self._settings
        return {ch: dict(entry) for ch, entry in settings.items()}

    def get(self, channel: str) -> dict[str, Any]:
        """One channel's stored settings (lock-free; see all_settings)."""
        return dict(self._settings.get(channel, {}))

    def update(
        self,
        channel: str,
        *,
        muted: bool | None = None,
        priority: str | None = None,
        clear_priority: bool = False,
        deliver_to: list[str] | None = None,
        deliver_min_priority: str | None = None,
        clear_delivery: bool = False,
    ) -> dict[str, Any]:
        """Update one channel's settings and persist. Returns the new entry.

        Raises :class:`ChannelSettingsError` for an unknown priority value,
        an attempt to mute / lower a protected channel, or an unusable
        bridge routing rule (unknown transport id, bad floor).

        ``deliver_to=[]`` and ``clear_delivery=True`` both disarm bridging;
        the empty list is what the Settings UI sends when a user clears the
        multi-select, and clearing drops both keys so a disarmed entry keeps
        no floor for an absent route.
        """
        if priority is not None and priority not in PRIORITIES:
            raise ChannelSettingsError(
                f"priority must be one of {PRIORITIES}, got {priority!r}"
            )
        if channel in PROTECTED_CHANNELS:
            if muted:
                raise ChannelSettingsError(f"{channel} cannot be muted")
            if priority is not None and priority != "critical":
                raise ChannelSettingsError(f"{channel} priority cannot be lowered")
        # Validate the routing rule BEFORE taking the lock: a rejected value
        # must not reach the read-modify-write, let alone the file.
        transports: tuple[str, ...] | None = None
        floor: str | None = None
        if deliver_to is not None:
            try:
                transports = normalize_deliver_to(deliver_to)
            except ValueError as exc:
                raise ChannelSettingsError(str(exc)) from exc
        if deliver_min_priority is not None:
            try:
                floor = normalize_min_priority(deliver_min_priority)
            except ValueError as exc:
                raise ChannelSettingsError(str(exc)) from exc
        # _lock serializes WRITERS only (read-modify-write below); readers
        # are lock-free because this method rebinds self._settings wholesale.
        with _lock:
            entry = dict(self._settings.get(channel, {}))
            if muted is not None:
                if muted:
                    entry["muted"] = True
                else:
                    entry.pop("muted", None)
            if clear_priority:
                entry.pop("priority", None)
            elif priority is not None:
                entry["priority"] = priority
            if clear_delivery:
                entry.pop("deliver_to", None)
                entry.pop("deliver_min_priority", None)
            else:
                if transports is not None:
                    if transports:
                        entry["deliver_to"] = list(transports)
                    else:
                        # Disarmed: the floor describes an absent route, so it
                        # goes with it rather than lingering to be silently
                        # reused if the route is re-armed.
                        entry.pop("deliver_to", None)
                        entry.pop("deliver_min_priority", None)
                if floor is not None and entry.get("deliver_to"):
                    entry["deliver_min_priority"] = floor
            # An armed route with no explicit floor gets the default written
            # down, so what routes is readable from the stored entry instead
            # of depending on a reader applying the same default.
            if entry.get("deliver_to") and not entry.get("deliver_min_priority"):
                entry["deliver_min_priority"] = DEFAULT_DELIVER_MIN_PRIORITY
            # Persist the candidate FIRST, commit memory only on success:
            # otherwise a full/read-only filesystem would leave the rejected
            # setting active in memory (until restart) while disk kept the
            # old value -- runtime disagreeing with both the HTTP response
            # and persisted configuration.
            candidate = dict(self._settings)
            if entry:
                candidate[channel] = entry
            else:
                candidate.pop(channel, None)
            payload = json.dumps({"channel_settings": candidate}, indent=2)
            atomic_write(_settings_path(), payload)
            self._settings = candidate
            return dict(entry)

    def apply(self, note: dict[str, Any]) -> dict[str, Any]:
        """Apply the note's channel settings in place and return it.

        Mute stamps ``silenced: true`` and forces priority ``passive`` (all
        attention surfaces key off these); a priority override replaces the
        effective priority. Notes without a channel pass through untouched.
        """
        channel = note.get("channel")
        if not channel:
            return note
        entry = self.get(channel)
        if not entry:
            return note
        override = entry.get("priority")
        if override in PRIORITIES and not (
            channel in PROTECTED_CHANNELS and override != "critical"
        ):
            # Protected channels keep their attention floor even for rows
            # that never went through update() (hand-edited settings file):
            # a non-critical override on system.approval is ignored.
            note["priority"] = override
        if entry.get("muted") and channel not in PROTECTED_CHANNELS:
            note["silenced"] = True
            note["priority"] = "passive"
        return note
