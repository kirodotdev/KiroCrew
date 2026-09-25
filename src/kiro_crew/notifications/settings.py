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
    rule_from_settings,
)
from kiro_crew.notifications.bus import PRIORITIES

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

#: The display fields of a stored entry that an APP token wrote while no route was
#: armed, as a sorted list of field names. Such a write is allowed -- the pair is
#: display state until a route exists -- but it was made by a caller with no routing
#: authority, so arming a route discards those fields rather than letting them decide
#: what leaves the host. A field the owner wrote carries no mark and survives arming,
#: which is the whole distinction: without it, arming would silently clear a mute the
#: owner set deliberately.
#:
#: PER FIELD rather than one flag for the pair. With a single flag, an owner request
#: that sets only ``priority`` clears it while an app's ``muted`` is still stored, and
#: the later arm then sees no mark and keeps that mute -- armed and muted, which
#: ``apply()`` forces to ``passive``, suppressing the DM the owner just armed.
#:
#: Internal bookkeeping rather than a setting, so every reader of the stored row is
#: given it withheld.
APP_DISPLAY_OVERRIDE_KEY = "display_override_by_app"

#: Stamped on any stored row that keeps a display field, recording that the row's
#: field provenance is known. Its ABSENCE is the load-bearing half: a row holding
#: ``muted`` or ``priority`` without this stamp cannot have those fields attributed to
#: the owner, because an owner write clears :data:`APP_DISPLAY_OVERRIDE_KEY` too, so a
#: missing mark alone proves nothing. Such fields are treated as written without
#: routing authority until the owner names one in a PUT. Internal bookkeeping, withheld
#: from every reader.
DISPLAY_PROVENANCE_KEY = "display_provenance_recorded"

#: What a caller reading a stored entry is never shown: the routing keys (owner-only,
#: withheld from an app) plus the internal provenance markers (withheld from everyone).
INTERNAL_SETTING_KEYS = frozenset({APP_DISPLAY_OVERRIDE_KEY, DISPLAY_PROVENANCE_KEY})


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    """A stored entry as any reader outside this module sees it.

    The provenance marker is bookkeeping this module keeps for itself, so it is
    withheld here rather than at each of the four carriers of the stored row. The
    writer reads ``self._settings`` directly and therefore still sees it.
    """
    return {k: v for k, v in entry.items() if k not in INTERNAL_SETTING_KEYS}


_SETTINGS_FILENAME = "notification_settings.json"
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
        return {ch: _public(entry) for ch, entry in settings.items()}

    def get(self, channel: str) -> dict[str, Any]:
        """One channel's stored settings (lock-free; see all_settings)."""
        return _public(self._settings.get(channel, {}))

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
        stored route is armed, such a caller may not set ``muted`` or
        ``priority``, and :class:`ChannelSettingsAuthError` is raised instead.
        The check belongs in here rather than in the caller because it has to be
        atomic with the write it guards -- a lock-free check outside is a TOCTOU,
        since an owner PUT arming the route can commit between that check and
        this method's read-modify-write, leaving the channel muted AND armed.
        Under the lock the decision reads the same entry the write merges onto.

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
            # Decided against the entry this write is about to merge onto, so the
            # route cannot be armed in between. `muted`/`priority` are display
            # state until a route is armed; once one is, `apply()` forces a muted
            # channel to `passive` and only an `all` floor routes it, so an app's
            # mute or down-rank silences a DM the owner armed.
            was_armed = rule_from_settings(entry).armed
            if app_caller and (muted is not None or priority is not None or clear_priority):
                if was_armed:
                    raise ChannelSettingsAuthError(
                        "mute and priority are owner-only on a channel that routes to chat"
                    )
            # Which display fields THIS request speaks for, and with what authority.
            # An app may set the pair while no route is armed (the fence above allows
            # it), so the stored entry has to remember WHICH fields came from a caller
            # with no routing authority -- that is the only thing separating them from
            # the owner's own settled preference, which arming must NOT disturb.
            #
            # Per FIELD, not per entry: with one flag for the pair, an owner editing
            # only `priority` clears the flag while an app's `muted` is still stored,
            # and the later arm then sees no mark and keeps that mute. Each field
            # carries its own provenance, and an owner write takes over only the field
            # it actually names.
            request_sets_muted = muted is not None
            request_sets_priority = priority is not None or clear_priority
            stored_marks = entry.get(APP_DISPLAY_OVERRIDE_KEY)
            marked: set[str] = (
                {f for f in stored_marks if isinstance(f, str)}
                if isinstance(stored_marks, list)
                else set()
            )
            # An unstamped row carries no marks, so the loop below would leave
            # `marked` empty and arming would drop nothing -- letting a stored mute an
            # app wrote survive into a routed channel and silence the DM the owner
            # just armed. A missing mark cannot stand in for owner authorship here,
            # because an owner write clears the mark key as well; only the stamp tells
            # the two apart. So an unstamped display field is held to carry no routing
            # authority, and the owner reaffirms one by naming it -- the discard below
            # runs after this and takes precedence.
            if not entry.get(DISPLAY_PROVENANCE_KEY):
                marked.update(f for f in ("muted", "priority") if f in entry)
            for field, written in (
                ("muted", request_sets_muted),
                ("priority", request_sets_priority),
            ):
                if not written:
                    continue
                if app_caller:
                    marked.add(field)
                else:
                    marked.discard(field)
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
            # Arming is when display state becomes delivery authority, so a field
            # written WITHOUT that authority does not cross the line. Only marked
            # fields are dropped: the owner's own mute carries no mark and survives,
            # which is the whole point -- the stored value is identical in both cases.
            # A field this request set as the owner was already unmarked above, so an
            # owner arming and muting in one PUT keeps that mute without a separate
            # check here.
            if entry.get("deliver_to"):
                for field in sorted(marked):
                    entry.pop(field, None)
                marked.clear()
            # Retire a mark whose field is absent from `entry`. An APP that CLEARS one of
            # these fields (`muted: false`, or a priority clear) takes the marking branch
            # above and then has the field popped below it, so the mark outlives the value
            # it describes. That matters beyond tidiness: a mark alone keeps `entry`
            # non-empty, which persists a channel row holding no settings at all and
            # surfaces that phantom channel in listings.
            #
            # The owner path does not need this, because naming a field unmarks it -- so a
            # reading that covers only the owner makes this step look unnecessary while the
            # app branch still reaches it.
            for field in ("muted", "priority"):
                if field not in entry:
                    marked.discard(field)
            if marked:
                entry[APP_DISPLAY_OVERRIDE_KEY] = sorted(marked)
            else:
                entry.pop(APP_DISPLAY_OVERRIDE_KEY, None)
            # Stamped only while a display field remains, for the same reason a stale
            # mark is retired above: a bookkeeping key on an otherwise-empty entry
            # keeps it truthy below and persists a channel row holding no settings.
            if any(field in entry for field in ("muted", "priority")):
                entry[DISPLAY_PROVENANCE_KEY] = True
            else:
                entry.pop(DISPLAY_PROVENANCE_KEY, None)
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
            _write_settings_staged(_settings_path(), payload)
            self._settings = candidate
            return _public(entry)

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
