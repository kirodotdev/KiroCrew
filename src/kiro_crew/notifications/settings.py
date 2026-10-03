"""Per-channel notification settings.

User preferences for each notification channel: mute, priority override, and
bridge routing. Stored in ``~/.kiro/crew/notification_settings.json`` as::

    {"channel_settings": {"system.heartbeat": {"muted": true},
                          "system.monitor": {},
                          "oncall-radar.ticket-update": {"priority": "critical"},
                          "system.approval": {"deliver_to": ["slack"],
                                              "deliver_min_priority": "critical"}}}

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

import contextlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import refuse_linked_parent, replace_with_retry
from kiro_crew.config.loader import config_dir
from kiro_crew.notifications.bridge import (
    DEFAULT_DELIVER_MIN_PRIORITY,
    normalize_deliver_to,
    normalize_min_priority,
)
from kiro_crew.notifications.bus import MONITOR_CHANNEL, PRIORITIES

logger = logging.getLogger(__name__)

# Channels whose attention semantics the user may not weaken: approvals gate
# agent actions, so silencing them would stall work invisibly.
PROTECTED_CHANNELS = frozenset({"system.approval"})

# The stored keys that decide EGRESS rather than dashboard presentation. Named
# once because every carrier of the stored row must agree on them -- the settings
# PUT refuses an app token that sets them, the channels GET withholds them from
# one, the PUT's own reply withholds them (a mute-only PUT never reaches the
# refusal), and the WS frame is stripped per client in
# ``dashboard/ws_event_scope.channel_settings_for_app`` -- and a set spelled
# twice is how a third routing key added later reaches an app token through
# whichever carrier was not updated.
DELIVERY_SETTING_KEYS = frozenset({"deliver_to", "deliver_min_priority"})

_SETTINGS_FILENAME = "notification_settings.json"
# One-time seed: when a stored settings mapping has no ``system.monitor`` key,
# its ``system.agent`` entry is copied to ``system.monitor`` in memory. Every
# subsequent write retains a ``system.monitor`` key, including an empty entry
# for an unmuted channel, so its presence records that the seed is complete.
_SEED_SOURCE_CHANNEL = "system.agent"
#: The staging directory every write publishes through. Spelled to match
#: ``sandbox._NOTIFICATION_SETTINGS_STAGING_LEAF``, which masks it from every sandboxed
#: process; ``test_sandbox_notification_settings_mask.py`` pins the two equal.
_STAGING_LEAF = "notification-settings-staging"
_lock = threading.Lock()


def _settings_path():
    return config_dir() / _SETTINGS_FILENAME


def _staging_dir():
    """The masked directory this module's writes stage in.

    Not the target's parent, which is what ``atomic_write`` would use: the parent is the
    crew data-home root, writable and visible in every sandbox, so the temp carrying the
    real ``deliver_to`` bytes sat at a name the leaf mask does not cover. A same-UID
    sandbox could hold that temp's descriptor across the rename, and a crash between the
    write and the rename left the bytes readable indefinitely.

    A TOP-LEVEL sibling of the settings file rather than a child of anything the agent can
    rename, for the reason ``aws-control-staging`` records: a mask covers the leaf, not its
    ancestors. It stays on the same filesystem as the target, which is the only property
    the publish rename depends on.
    """
    return config_dir() / _STAGING_LEAF


def _write_settings_staged(target, payload: str) -> None:
    """Stage *payload* in the masked staging dir, then rename it onto *target*.

    ``mkstemp`` opens the temp 0600 on POSIX before any payload byte. The planted-link
    refusal that ``atomic_write`` performs is kept rather than shed by moving off it: both
    chains this write walks are judged BEFORE the mkdirs, because ``mkdir(parents=True)``
    walks THROUGH a planted link and would build the tree under its target while the
    caller saw success. On failure the temp is removed, and a removal that itself fails
    leaves the orphan inside the mask rather than beside the target.
    """
    staging = _staging_dir()
    refuse_linked_parent(target)
    refuse_linked_parent(staging / ".chain-probe")
    staging.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(staging), suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(payload)
        replace_with_retry(tmp, target)
    except BaseException:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


class ChannelSettingsError(ValueError):
    """Invalid channel settings input (unknown priority, protected channel)."""


class ChannelSettingsAuthError(ChannelSettingsError):
    """An app caller may not make this change to a channel that routes to chat.

    A SUBCLASS so the refusal fails closed: a caller that handles only
    ``ChannelSettingsError`` still refuses the write rather than letting it
    through, and only the status code it reports is less precise. The write
    itself never happens either way -- this is raised inside the writer lock,
    before the entry is persisted or committed to memory.
    """


class ChannelSettings:
    """Load/store/apply per-channel user settings.

    In-memory dict guarded by a lock; writes are atomic-rename, staged in a
    masked directory rather than beside the target (see :func:`_staging_dir`).
    Owned by
    ``DashboardState`` (one instance per gateway) like the bus and the rate
    limiter.
    """

    def __init__(self) -> None:
        self._settings: dict[str, dict[str, Any]] = {}
        self._load()
        self._seed_monitor_from_agent()

    def _load(self) -> None:
        path = _settings_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                raw = data.get("channel_settings", {})
                if isinstance(raw, dict):
                    self._settings = {
                        ch: dict(entry) for ch, entry in raw.items() if isinstance(entry, dict)
                    }
        except Exception:
            # Corrupt settings must not take down the gateway; fall back to
            # defaults (everything unmuted, producer priorities honored).
            logger.warning("Failed to load %s; using defaults", path, exc_info=True)
            self._settings = {}

    def _seed_monitor_from_agent(self) -> None:
        """Copy a stored ``system.agent`` entry to ``system.monitor`` once.

        A stored ``system.monitor`` key, including an empty entry, records that
        the seed is complete. Otherwise the copy happens only in memory, so a
        boot never writes the file. Until an update persists the entry, every
        load derives the same seed from the same file.
        """
        if MONITOR_CHANNEL in self._settings:
            return
        source = self._settings.get(_SEED_SOURCE_CHANNEL)
        if source is None:
            return
        self._settings = {**self._settings, MONITOR_CHANNEL: dict(source)}

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
        deliver_to: list[str] | None = None,
        deliver_min_priority: str | None = None,
        clear_delivery: bool = False,
        app_caller: str = "",
    ) -> dict[str, Any]:
        """Update one channel's settings and persist. Returns the new entry.

        Raises :class:`ChannelSettingsError` for an unknown priority value,
        an attempt to mute / lower a protected channel, or an unusable
        bridge routing rule (unknown transport id, bad floor).

        ``app_caller`` names an app token and arms the display fence: while the
        stored route is ARMED, such a caller may not set ``muted`` or
        ``priority``, and :class:`ChannelSettingsAuthError` is raised instead. On
        an UNARMED channel the write is accepted -- ``muted``/``priority`` are
        display state with no delivery authority until a route exists, so an app
        setting them there changes nothing that leaves the host. Arming later is
        the fence: the arm transition drops any ``muted``/``priority`` this
        arming request does not itself name, so a display value stored earlier
        (by an app or a prior owner PUT) never silently becomes delivery
        authority. The refusal check belongs in here rather than in the caller
        because it has to be atomic with the write it guards -- a lock-free check
        outside is a TOCTOU, since an owner PUT arming the route can commit
        between that check and this method's read-modify-write. Under the lock
        the decision reads the same entry the write merges onto.

        ``deliver_to=[]`` and ``clear_delivery=True`` both disarm bridging;
        the empty list is what the Settings UI sends when a user clears the
        multi-select, and clearing drops both keys so a disarmed entry keeps
        no floor for an absent route.
        """
        if priority is not None and priority not in PRIORITIES:
            raise ChannelSettingsError(f"priority must be one of {PRIORITIES}, got {priority!r}")
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
            was_armed = bool(entry.get("deliver_to"))
            # `muted`/`priority` are display state until a route is armed; once one
            # is, `apply()` forces a muted channel to `passive` and only an `all` floor
            # routes it, so an app's mute or down-rank silences a DM the owner armed.
            #
            # An app caller may set these fields on an UNARMED channel (display-only
            # state, no delivery authority) but not on an ARMED one, where they would
            # decide what leaves the host. The arm transition below is what keeps the
            # unarmed acceptance safe: it drops any display field this request does not
            # name, so a value an app stored earlier never rides the arm into authority.
            if (
                was_armed
                and app_caller
                and (muted is not None or priority is not None or clear_priority)
            ):
                raise ChannelSettingsAuthError(
                    "mute and priority are owner-only on an armed notification channel"
                )
            # Which display fields THIS request names. The arm transition keeps only
            # these; any other stored display field is dropped when the route arms,
            # regardless of who wrote it, so no provenance bookkeeping is stored.
            request_names_muted = muted is not None
            request_names_priority = priority is not None or clear_priority
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
            # THE ARM TRANSITION. A channel that becomes armed on this write (it was
            # not armed before and is now) turns display state into delivery authority,
            # so a `muted`/`priority` value this request does not itself name must not
            # cross that line: drop it. The owner arming and muting in one PUT keeps the
            # mute because this request names it. A value stored earlier -- by an app on
            # the then-unarmed channel, or a stale prior owner write -- is dropped, which
            # is what makes accepting the unarmed app write safe without tracking who
            # wrote it. A route that was already armed does not re-run this (an app write
            # is refused above; an owner edit names its own fields).
            now_armed = bool(entry.get("deliver_to"))
            if now_armed and not was_armed:
                if not request_names_muted:
                    entry.pop("muted", None)
                if not request_names_priority:
                    entry.pop("priority", None)
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
            _write_settings_staged(_settings_path(), payload)
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
