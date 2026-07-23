"""Per-channel notification settings (RFC local notification bus, Phase 3).

User preferences for each notification channel: mute and priority override.
Stored in ``~/.kirocrew/notification_settings.json`` as::

    {"channel_settings": {"system.heartbeat": {"muted": true},
                          "oncall-radar.ticket-update": {"priority": "critical"}}}

Semantics (applied at the delivery sink, keeping the bus pure):

- **muted**: the note is still delivered to history (history is a cache and
  the user asked to silence, not to destroy) but is stamped ``silenced: true``
  and its priority forced to ``passive``, so every attention surface (badge
  count, sound, native banner, feed styling) skips it.
- **priority**: user override wins over the producer-requested priority and
  the channel default.
- ``system.approval`` is protected: it cannot be muted and its priority
  cannot be lowered (RFC exit criteria: approval still interrupts while
  heartbeat can be silenced everywhere).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.notifications.bus import PRIORITIES

logger = logging.getLogger(__name__)

# Channels whose attention semantics the user may not weaken: approvals gate
# agent actions, so silencing them would stall work invisibly.
PROTECTED_CHANNELS = frozenset({"system.approval"})

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
    ) -> dict[str, Any]:
        """Update one channel's settings and persist. Returns the new entry.

        Raises :class:`ChannelSettingsError` for an unknown priority value,
        or an attempt to mute / lower a protected channel.
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
