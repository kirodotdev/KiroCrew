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
import hashlib
import hmac
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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

    After the publish the payload's digest is recorded as the file's write stamp; see
    :func:`_record_write_stamp` for why routes are honoured only against it. A stamp
    that fails to land fails the whole save and leaves the previous file and stamp as a
    matching pair: the previous bytes are staged as a rollback copy BEFORE anything is
    replaced, so putting them back is a rename that needs no new space (the disk-full
    case that broke the stamp would break a second temp file too). A rollback copy that
    cannot be staged refuses the save before the settings file is touched. Either way
    the error is raised, so the caller commits nothing to memory and the owner's save is
    reported as failed instead of its routes vanishing at the next restart.
    """
    refuse_linked_parent(target)
    try:
        previous: bytes | None = target.read_bytes()
    except OSError:
        previous = None
    rollback = _stage_bytes(previous) if previous is not None else None
    try:
        _publish_staged(target, payload.encode("utf-8"))
        try:
            _record_write_stamp(payload.encode("utf-8"))
        except OSError:
            if rollback is None:
                with contextlib.suppress(OSError):
                    target.unlink()
            else:
                try:
                    replace_with_retry(rollback, target)
                    rollback = None
                except OSError:
                    logger.error(
                        "Could not restore the previous %s after a failed save; its routes "
                        "stay inactive until notification settings are saved again",
                        target.name,
                        exc_info=True,
                    )
            raise
    finally:
        if rollback is not None:
            with contextlib.suppress(OSError):
                rollback.unlink()


def _stage_bytes(data: bytes, suffix: str = ".tmp") -> Path:
    """Write *data* to a fresh 0600 temp in the masked staging dir and return its path.

    The chain is judged before the mkdir for the reason :func:`_write_settings_staged`
    gives. On failure the temp is removed and the error raised.
    """
    staging = _staging_dir()
    refuse_linked_parent(staging / ".chain-probe")
    staging.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(staging), suffix=suffix)
    tmp = Path(tmp_name)
    try:
        # Binary mode: the bytes on disk must be exactly the bytes the stamp hashes, so
        # no platform newline translation (CRLF on Windows) may happen in between.
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(data)
    except BaseException:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return tmp


def _publish_staged(target, data: bytes) -> None:
    """Stage *data* in the masked staging dir and rename it onto *target* (no stamp)."""
    refuse_linked_parent(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _stage_bytes(data)
    try:
        replace_with_retry(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


#: The write stamp: the sha256 of the bytes this module last published to the settings
#: file. It lives in the staging directory, which is masked as a whole directory from every
#: sandbox, so nothing an agent runs can write it -- even when the settings file's own name
#: cannot be masked (a dotfile manager's symlink, a snapshot tool's second hard link).
_WRITE_STAMP_NAME = "settings.sha256"


def _write_stamp_path():
    return _staging_dir() / _WRITE_STAMP_NAME


def _record_write_stamp(published: bytes) -> None:
    """Record *published* as the bytes this gateway wrote, so its routes are honoured.

    Why at all: ``deliver_to`` is an owner-only egress authorization, and the settings
    file's NAME sits in the agent-writable data home. When that name is unmaskable, a
    sandboxed process can replace the file with one of its own that arms a route, and a
    shape check cannot tell that file from the owner's. The stamp can: only this module
    writes it, and :func:`_read_stored_strict` drops every routing key from a file whose
    bytes do not match it. Raises ``OSError`` when the stamp cannot be written, so the
    save it belongs to fails rather than reporting success for routes the next load drops.
    """
    staging = _staging_dir()
    refuse_linked_parent(staging / ".chain-probe")
    staging.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(staging), suffix=".stamp.tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fd = -1
            fh.write(hashlib.sha256(published).hexdigest())
        replace_with_retry(tmp, _write_stamp_path())
    except BaseException:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _matches_write_stamp(raw: bytes) -> bool:
    """Whether *raw* is exactly what this module last published (unreadable stamp: no)."""
    try:
        recorded = _write_stamp_path().read_text(encoding="ascii").strip()
    except OSError:
        return False
    return hmac.compare_digest(recorded, hashlib.sha256(raw).hexdigest())


def _without_unstamped_routes(
    settings: dict[str, dict[str, Any]], raw: bytes
) -> dict[str, dict[str, Any]]:
    """*settings* with every routing key dropped unless *raw* carries the write stamp.

    Display state (``muted``/``priority``) is kept: it has no delivery authority until a
    route exists, and the only way to arm one is an owner write through :meth:`update`,
    which re-stamps. A file this gateway did not write therefore cannot send anything.
    """
    if not any(DELIVERY_SETTING_KEYS & entry.keys() for entry in settings.values()):
        return settings
    if _matches_write_stamp(raw):
        return settings
    logger.warning(
        "%s holds notification routes this gateway did not write; ignoring them until "
        "the owner saves notification settings again",
        _settings_path().name,
    )
    return {
        ch: {k: v for k, v in entry.items() if k not in DELIVERY_SETTING_KEYS}
        for ch, entry in settings.items()
    }


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
    raw_bytes = path.read_bytes()
    data = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    raw = data.get("channel_settings", {})
    if not isinstance(raw, dict):
        return {}
    stored = {ch: dict(entry) for ch, entry in raw.items() if isinstance(entry, dict)}
    return _without_unstamped_routes(stored, raw_bytes)


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
        # Collected across the field loop and reconciled after it, because the two
        # delivery fields arm together: a floor is meaningful only with a transport
        # list, exactly as update() stores them. Sentinels distinguish "field
        # absent" from "field present but refused".
        transports: tuple[str, ...] | None = None
        floor: str | None = None
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
            elif field == "deliver_to":
                # An armed route is the egress authorization the whole bridge
                # keys off, so a Replace import must carry it rather than silently
                # disarm every saved route. Validated with the live writer's own
                # normalizer; an unusable value is refused and counted, never kept.
                try:
                    transports = normalize_deliver_to(value)
                except ValueError:
                    refused += 1
            elif field == "deliver_min_priority":
                try:
                    floor = normalize_min_priority(value)
                except ValueError:
                    refused += 1
            else:
                refused += 1
        # Reconcile the delivery pair exactly as update() does: a route arms only
        # when deliver_to names at least one transport; a floor rides only an armed
        # route and defaults to critical when the archive armed delivery without
        # naming one. An empty-or-absent deliver_to keeps neither key, so an
        # unarmed channel stays unarmed.
        if transports:
            clean["deliver_to"] = list(transports)
            clean["deliver_min_priority"] = floor or DEFAULT_DELIVER_MIN_PRIORITY
        dropped += refused
        if refused and not clean:
            # Every field was refused: keep no row at all (see the docstring). An
            # entry that is legitimately empty ({} or only muted:false) still
            # rides, exactly as before.
            continue
        kept[channel] = clean
    return _seeded(kept), dropped


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
    def replacing_file(
        self, installed: dict[str, dict[str, Any]] | None = None
    ) -> Iterator[Callable[[], None]]:
        """Hold the writer lock while something other than :meth:`update` swaps the file.

        The settings import's replace mode swaps ``notification_settings.json``
        under a running gateway. Without the lock, an :meth:`update` landing during
        the swap writes its PRE-import mapping over the restored file; without the
        re-read, the in-memory copy keeps applying the old mutes and the next
        :meth:`update` writes them back. Both happen inside one lock hold, so no
        writer runs between the swap and the re-read. The new mapping is built first
        and bound in one step, so a lock-free reader never sees an empty interim.

        *installed* is the validated mapping the swap writes. The context yields a
        publisher for it, which the swap calls INSIDE its own rollback boundary: it
        writes the validated mapping through the staged writer (stamping exactly the
        bytes it writes, never a re-read of a name a sandbox may write) while this
        hold still owns the lock, so a failed stamp raises where the swap can put the
        previous file/stamp pair back. Memory takes *installed* only once that publish
        succeeded; a swap that never published, or raised, re-reads the file, because
        whatever it holds then is what memory must match.
        """
        published = False

        def _publish() -> None:
            nonlocal published
            if installed is None:
                return
            _write_settings_staged(_settings_path(), self._payload(installed))
            published = True

        with _lock:
            try:
                yield _publish
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
            if installed is not None and published:
                self._settings = _seeded({ch: dict(entry) for ch, entry in installed.items()})
            else:
                self._settings = _seeded(_read_stored())

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
            _write_settings_staged(path, self._payload(candidate))
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


def publish_restored_settings(text: str, *, home: Path) -> bool:
    """Write a snapshot-restored settings document through the stamped writer.

    *text* is the archive's ``notification_settings.json``, validated by the restore's
    pre-flight with :func:`parse_imported_settings`; it is parsed again here so the bytes
    published are the validated mapping, never whatever now sits at the live name. A
    restore is an owner action, so its routes are honoured -- which takes a stamp of the
    bytes written, exactly as :meth:`ChannelSettings.update` does.

    Returns ``False``, writing nothing, when *home* is not this store's data home (its
    stamp would land beside the wrong file). Raises when the write or its stamp fails, so
    the restore rolls back and reports the failure instead of succeeding with routes the
    next load discards.
    """
    if Path(home).resolve() != _settings_path().parent.resolve():
        return False
    incoming, _dropped = parse_imported_settings(text)
    candidate = {channel: dict(entry) for channel, entry in incoming.items()}
    candidate.setdefault(MONITOR_CHANNEL, {})
    with _lock:
        _write_settings_staged(_settings_path(), ChannelSettings._payload(candidate))
    return True
