"""Per-channel notification settings.

User preferences for each notification channel: mute and priority override.
Stored in ``~/.kiro/crew/notification_settings.json`` as::

    {"channel_settings": {"system.heartbeat": {"muted": true},
                          "system.monitor": {},
                          "oncall-radar.ticket-update": {"priority": "critical"}}}

Every file this build writes contains a ``system.monitor`` entry, possibly an
empty one, which records that the one-time seed from ``system.agent`` is done.
Empty entries stay out of :meth:`ChannelSettings.all_settings`.

Semantics (applied at the delivery sink, keeping the bus pure):

- **muted**: the note is still delivered to history (history is a cache and
  the user asked to silence, not to destroy) but is stamped ``silenced: true``
  and its priority forced to ``passive``, so every attention surface (badge
  count, sound, native banner, feed styling) skips it.
- **priority**: user override wins over the producer-requested priority and
  the channel default.
- ``system.approval`` is protected: it cannot be muted and its priority
  cannot be lowered (approval still interrupts while heartbeat can be
  silenced everywhere).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.notifications.bus import MONITOR_CHANNEL, PRIORITIES

logger = logging.getLogger(__name__)

# Channels whose attention semantics the user may not weaken: approvals gate
# agent actions, so silencing them would stall work invisibly.
PROTECTED_CHANNELS = frozenset({"system.approval"})

_SETTINGS_FILENAME = "notification_settings.json"
# One-time seed: when a stored settings mapping has no ``system.monitor`` key,
# its ``system.agent`` entry is copied to ``system.monitor`` in memory. Every
# subsequent write retains a ``system.monitor`` key, including an empty entry
# for an unmuted channel, so its presence records that the seed is complete.
_SEED_SOURCE_CHANNEL = "system.agent"
_lock = threading.Lock()


def _settings_path():
    return config_dir() / _SETTINGS_FILENAME


class ChannelSettingsError(ValueError):
    """Invalid channel settings input (unknown priority, protected channel)."""


#: Longest channel name an import keeps, matching the settings endpoint's own bound.
_MAX_CHANNEL_LEN = 256

#: Most channels an import keeps. Channels are declared by the product (``system.*``
#: plus one per connector or crew surface), so a real file holds a few dozen entries;
#: 512 keeps every legitimate mapping with an order of magnitude to spare while
#: refusing a crafted archive the room to grow the parsed mapping without bound.
_MAX_IMPORT_CHANNELS = 512


def _read_stored_strict() -> dict[str, dict[str, Any]]:
    """The stored mapping; ``{}`` only when the file is absent or holds none.

    Raises when the file exists but cannot be read or parsed, so a caller that
    already holds a good mapping can tell "read, and empty" from "not read".
    """
    path = _settings_path()
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    raw = data.get("channel_settings", {})
    if not isinstance(raw, dict):
        return {}
    return {ch: dict(entry) for ch, entry in raw.items() if isinstance(entry, dict)}


def _read_stored() -> dict[str, dict[str, Any]]:
    """The stored mapping, or ``{}`` when the file is absent or unusable."""
    path = _settings_path()
    try:
        return _read_stored_strict()
    except Exception:
        # Corrupt settings must not take down the gateway; fall back to
        # defaults (everything unmuted, producer priorities honored).
        logger.warning("Failed to load %s; using defaults", path, exc_info=True)
    return {}


def _seeded(settings: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """*settings* with the one-time ``system.agent`` -> ``system.monitor`` seed applied."""
    if MONITOR_CHANNEL in settings:
        return settings
    source = settings.get(_SEED_SOURCE_CHANNEL)
    if source is None:
        return settings
    return {**settings, MONITOR_CHANNEL: dict(source)}


def parse_imported_settings(text: str) -> tuple[dict[str, dict[str, Any]], int]:
    """Validate a ``notification_settings.json`` taken from an import archive.

    Returns ``(channel_settings, dropped)``. Raises :class:`ChannelSettingsError`
    when the document is not this file's shape at all -- not JSON, not an object,
    or a ``channel_settings`` that is not an object -- so the import reports it and
    installs nothing. Below that level a value the live writer would refuse is
    DROPPED and counted rather than failing the whole file: an unknown priority, a
    non-boolean ``muted``, a field this build does not know, an over-long channel
    name, a channel past :data:`_MAX_IMPORT_CHANNELS`, and anything that would mute
    ``system.approval`` or lower its priority. A channel whose every field was
    refused is dropped whole rather than kept as ``{}``: an empty row would still
    land on the install (replace writes it out, and a planted ``system.monitor``
    key would mark the one-time seed complete). The archive is untrusted input, so
    it gets :meth:`ChannelSettings.update`'s rules, never a looser set.
    """
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ChannelSettingsError(f"not valid JSON ({exc})") from None
    if not isinstance(doc, dict):
        raise ChannelSettingsError("not a JSON object")
    raw = doc.get("channel_settings", {})
    if not isinstance(raw, dict):
        raise ChannelSettingsError("'channel_settings' is not an object")
    kept: dict[str, dict[str, Any]] = {}
    dropped = 0
    for channel, entry in raw.items():
        if not isinstance(entry, dict) or not channel or len(channel) > _MAX_CHANNEL_LEN:
            dropped += 1
            continue
        if len(kept) >= _MAX_IMPORT_CHANNELS:
            # Refused BEFORE any field is retained, so a channel past the cap
            # gets no row anywhere; the overflow is counted with the other drops.
            dropped += 1
            continue
        clean: dict[str, Any] = {}
        refused = 0
        for field, value in entry.items():
            if field == "muted" and value is True and channel not in PROTECTED_CHANNELS:
                clean["muted"] = True
            elif field == "muted" and value is False:
                pass  # unmuted is the absence of the key, as update() stores it
            elif (
                field == "priority"
                and value in PRIORITIES
                and (channel not in PROTECTED_CHANNELS or value == "critical")
            ):
                clean["priority"] = value
            else:
                refused += 1
        dropped += refused
        if refused and not clean:
            # Every field was refused: keep no row at all (see the docstring). An
            # entry that is legitimately empty ({} or only muted:false) still
            # rides, exactly as before.
            continue
        kept[channel] = clean
    return _seeded(kept), dropped


class ChannelSettings:
    """Load/store/apply per-channel user settings.

    In-memory dict guarded by a lock; writes are atomic-rename. Owned by
    ``DashboardState`` (one instance per gateway) like the bus and the rate
    limiter.
    """

    def __init__(self) -> None:
        self._settings: dict[str, dict[str, Any]] = {}
        self._load()
        self._seed_monitor_from_agent()

    def _load(self) -> None:
        self._settings = _read_stored()

    def _seed_monitor_from_agent(self) -> None:
        """Copy a stored ``system.agent`` entry to ``system.monitor`` once.

        A stored ``system.monitor`` key, including an empty entry, records that
        the seed is complete. Otherwise the copy happens only in memory, so a
        boot never writes the file. Until an update persists the entry, every
        load derives the same seed from the same file.
        """
        self._settings = _seeded(self._settings)

    @contextmanager
    def replacing_file(self, installed: dict[str, dict[str, Any]] | None = None) -> Iterator[None]:
        """Hold the writer lock while something other than :meth:`update` swaps the file.

        The settings import's replace mode swaps ``notification_settings.json``
        under a running gateway. Without the lock, an :meth:`update` landing during
        the swap writes its PRE-import mapping over the restored file; without the
        re-read, the in-memory copy keeps applying the old mutes and the next
        :meth:`update` writes them back. Both happen inside one lock hold, so no
        writer runs between the swap and the re-read. The new mapping is built first
        and bound in one step, so a lock-free reader never sees an empty interim.

        *installed* is the validated mapping the swap writes. When the swap
        succeeds, memory takes THAT mapping instead of re-reading the file: a
        transient read error would turn the re-read into an empty mapping, and the
        next :meth:`update` would write it over the restored mutes. When the swap
        raised, the file is re-read, because whatever it holds then (the restored
        one, or the rolled-back one) is what memory must match.
        """
        with _lock:
            try:
                yield
            except BaseException:
                # A refused or failed swap: match whatever the file holds now. If it
                # cannot be read, keep the mapping already in memory -- falling back
                # to {} here would drop every mute, and the next update() would write
                # that loss to disk.
                try:
                    self._settings = _seeded(_read_stored_strict())
                except Exception:
                    logger.warning(
                        "Failed to re-read %s after a failed settings swap; "
                        "keeping the settings already loaded",
                        _settings_path(),
                        exc_info=True,
                    )
                raise
            if installed is None:
                self._settings = _seeded(_read_stored())
            else:
                self._settings = _seeded({ch: dict(entry) for ch, entry in installed.items()})

    @staticmethod
    def _payload(channel_settings: dict[str, dict[str, Any]]) -> str:
        return json.dumps({"channel_settings": channel_settings}, indent=2)

    def all_settings(self) -> dict[str, dict[str, Any]]:
        """Snapshot of every channel's non-empty stored settings.

        Lock-free: ``update()`` rebinds ``self._settings`` to a fresh dict
        (never mutates in place), so readers see either the old or the new
        complete mapping. Loop-side callers (``apply()`` on every delivery)
        therefore never block on a worker-thread writer holding the lock
        across the file write.
        """
        settings = self._settings
        return {ch: dict(entry) for ch, entry in settings.items() if entry}

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
            raise ChannelSettingsError(f"priority must be one of {PRIORITIES}, got {priority!r}")
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
            # Every write keeps a system.monitor key ({} when unset) as the
            # record that _seed_monitor_from_agent ran; see that docstring.
            candidate.setdefault(MONITOR_CHANNEL, {})
            payload = self._payload(candidate)
            atomic_write(_settings_path(), payload)
            self._settings = candidate
            return dict(entry)

    def install_imported(self, incoming: dict[str, dict[str, Any]]) -> bool:
        """Install an archive's settings, only where this install has no settings file.

        *incoming* must come from :func:`parse_imported_settings`, which has
        already applied the same field and protected-channel rules
        :meth:`update` enforces. The dashboard Merge's path in, so it follows
        that merge's never-overwrite contract: an install that already keeps a
        ``notification_settings.json`` keeps it whole. Decided under the same
        lock :meth:`update` writes under, so an update that creates the file
        first wins; persisted before memory is committed, exactly as
        :meth:`update` does. Returns whether the file was written.
        """
        with _lock:
            path = _settings_path()
            if os.path.lexists(path):
                return False
            candidate = {channel: dict(entry) for channel, entry in incoming.items()}
            # Every write keeps a system.monitor key ({} when unset), as update()
            # does: the record that _seed_monitor_from_agent ran.
            candidate.setdefault(MONITOR_CHANNEL, {})
            atomic_write(path, self._payload(candidate))
            self._settings = candidate
            return True

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
