"""Notification delivery, persistence, and client-state coordination."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Iterable
from concurrent.futures import Executor
from typing import Any

from kiro_crew.notifications.bus import PRIORITIES
from kiro_crew.notifications.settings import PROTECTED_CHANNELS

# Byte cap on a native-notification tag, counted in serialized bytes (the UTF-8
# length of its JSON-escaped form, as the Web Push payload encodes it). The push
# payload holds its tag to the same budget, so a tag minted here always fits the
# push path unchanged (a cut tag would differ from the one the open tab shows).
NATIVE_TAG_MAX_BYTES = 128
# A note key comes first: a keyed note's replacement must take over its banner,
# so its tag has to name the key even when the note also carries an approval,
# job or task id. A note with no key falls through to those ids unchanged.
NATIVE_TAG_ID_KEYS = ("key", "approval_id", "job_id", "task_id", "ts")


def _tag_id(note: dict[str, Any], field: str) -> Any:
    """The id *field* gives *note*'s banner; a note key is scoped to its source."""
    value = note.get(field)
    if field == "key" and isinstance(value, str) and value:
        # A key names a note within its source (``_notes_held_under``), so two
        # sources' equal keys never share a banner.
        return f"{note.get('source') or ''}:{value}"
    return value


def _serialized_bytes(text: str) -> int:
    """Bytes *text* occupies inside a JSON payload, quotes excluded.

    A lone surrogate (valid in a JSON ``\\ud800`` escape, not in UTF-8) is
    counted as three bytes instead of raising, so measuring never aborts the
    delivery that asked; the push encoder replaces it with one byte.
    """
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8", "surrogatepass")) - 2


def _utf8_encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def native_notification_tag(note: dict[str, Any]) -> str:
    """The one OS-notification tag for *note*, shared by every banner path.

    A delivered note can raise two OS banners: the open-but-hidden dashboard tab
    (``useNativeNotification``) and the service worker's ``push`` handler (or
    Safari's declarative Web Push, which renders the payload with no worker at
    all). The OS collapses banners only when their tags are equal, so both paths
    read this value -- stamped on the note as ``tag`` -- instead of each deriving
    its own. Derived from the note's event id (a keyed note's source and key,
    else the approval, job or task id, then the note's own timestamp), so
    distinct events never collapse onto one banner, and a keyed replacement
    takes over the banner of the note it replaces.
    An id that is over budget, or not valid UTF-8 (a lone surrogate an API
    caller escaped into its metadata), is hashed, so the tag always fits and
    always encodes: optional metadata can never abort local delivery.
    """
    for key in NATIVE_TAG_ID_KEYS:
        value = _tag_id(note, key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value):
            tag = f"{key}:{value}"
            if _utf8_encodable(tag) and _serialized_bytes(tag) <= NATIVE_TAG_MAX_BYTES:
                return tag
            digest = hashlib.sha256(tag.encode("utf-8", "surrogatepass")).hexdigest()
            return f"{key}#" + digest[:32]
    return "kirocrew-notif"


def session_is_muted(state: Any, session_key: str) -> bool:
    """Whether *session_key*'s attention is muted, as the dashboard reads it.

    Mirrors ``isSlotMuted`` (``website/src/hooks/sessionMute.ts``): the session's
    own ``muted`` flag, ``apps.<name>.mute_sessions`` on the app that owns it or a
    session up its ``_created_by`` chain, or ``mutes_opened`` on a session up that
    chain, walking from its creator. A missing creator or a cycle ends it unmuted.
    """
    from kiro_crew.config import live
    from kiro_crew.dashboard.session_control import caller_slot_key

    slot = state.get_slot(caller_slot_key(state, session_key))
    if slot is None:
        return False
    cfg = live.snapshot()
    muted_apps = {name for name, app in cfg.apps.items() if app.mute_sessions} if cfg else set()
    if slot.muted or slot._app in muted_apps:
        return True
    seen = {slot.key}
    key = slot._created_by
    while key and key not in seen:
        creator = state.get_slot(key)
        if creator is None:
            return False
        if creator.mutes_opened or creator._app in muted_apps:
            return True
        seen.add(key)
        key = creator._created_by
    return False


def unread_count(notes: Iterable[dict[str, Any]]) -> int:
    """How many of *notes* still need the user: not read, and not passive.

    Counted from the notes themselves on every read, so acknowledging a note,
    deleting one, an expiry sweep and a gateway restart all agree with it. The
    rule is the bell badge's (``shell/topbar/shellHeader.tsx``): a passive note
    and a silenced one (a muted channel or session, which delivery also forces
    to passive) are history, not attention.
    """
    return sum(
        1
        for note in notes
        if not note.get("acked") and note.get("priority") != "passive" and not note.get("silenced")
    )


def _priority_rank(note: dict[str, Any]) -> int:
    """0 for the most urgent priority; an unknown priority ranks as the default."""
    priority = note.get("priority")
    return PRIORITIES.index(priority if priority in PRIORITIES else "default")


def _notes_held_under(state: Any, source: Any, key: Any) -> list[dict[str, Any]]:
    """The stored notes *source* published under *key*; none for a note without a key."""
    if not key:
        return []
    return [
        held
        for held in state._notification_log
        if held.get("key") == key and held.get("source") == source
    ]


def _remove_keyed_notes(state: Any, source: Any, key: Any) -> None:
    """Remove the stored notes *source* published under *key*.

    A key names a note within its source, so two sources never collide. A note
    without a key is never matched.
    """
    if not key:
        return
    state._notification_log[:] = [
        held
        for held in state._notification_log
        if not (held.get("key") == key and held.get("source") == source)
    ]


class NotificationCoordinator:
    """Coordinate notification state while the DashboardState facade owns it."""

    def __init__(
        self,
        *,
        logger_provider: Callable[[], logging.Logger],
        payload_from_legacy: Callable[..., Any],
        validation_error: type[Exception],
        redact_value: Callable[[Any], Any],
        sweep_expired: Callable[[list[dict[str, Any]]], int],
        persist_one: Callable[[dict[str, Any]], bool],
        rewrite_all: Callable[[list[dict[str, Any]]], bool],
        executor_provider: Callable[[], Executor],
        max_persisted: int,
        web_push_fanout: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._logger_provider = logger_provider
        self._payload_from_legacy = payload_from_legacy
        self._validation_error = validation_error
        self._redact_value = redact_value
        self._sweep_expired = sweep_expired
        self._persist_one = persist_one
        self._rewrite_all = rewrite_all
        self._executor_provider = executor_provider
        self._max_persisted = max_persisted
        # Optional secondary sink: fan the delivered note out to browser Web
        # Push subscriptions. Placed after local delivery so it inherits the
        # redaction and channel-settings (mute/priority) already applied to the
        # note. None keeps the coordinator usable without a push subsystem
        # (e.g. bare test states), so delivery never depends on it.
        self._web_push_fanout = web_push_fanout
        self._full_rewrite_lock: asyncio.Lock | None = None
        self._full_rewrite_loop: asyncio.AbstractEventLoop | None = None
        self._keyed_write_in_flight = False

    def _rewrite_lock(self) -> asyncio.Lock:
        """Return this state's lock, bound lazily to the running loop."""
        loop = asyncio.get_running_loop()
        if self._full_rewrite_lock is None or self._full_rewrite_loop is not loop:
            self._full_rewrite_lock = asyncio.Lock()
            self._full_rewrite_loop = loop
        return self._full_rewrite_lock

    def notify(
        self,
        state: Any,
        kind: str,
        title: str,
        body: str,
        *,
        meta: dict | None,
        url: str | None,
        actions: list[dict[str, Any]] | None,
        channel: str | None = None,
        member: dict[str, str] | None = None,
        publisher_session: str | None = None,
        i18n: dict[str, Any] | None = None,
    ) -> None:
        """Validate a legacy notification and deliver it through the bus."""
        try:
            payload = self._payload_from_legacy(
                kind,
                title,
                body,
                meta,
                url=url,
                actions=actions,
                channel=channel,
                member=member,
                publisher_session=publisher_session,
                i18n=i18n,
            )
            state.notification_bus.push(payload)
        except self._validation_error:
            # Existing producers rely on invalid legacy payloads being dropped,
            # not raised back through unrelated work.
            self._logger_provider().warning(
                "Dropped invalid notification (kind=%s)", kind, exc_info=True
            )

    def deliver(self, state: Any, note: dict[str, Any]) -> None:
        """Apply settings, fan out, and queue durable notification storage."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None and self._handed_to_serving_loop(state, note):
            return
        # Delivery-only metadata: read here, so it is never redacted, stored or sent.
        publisher = note.pop("publisher_session", None)
        # Urgency is what the producer asked for, read before the user's channel
        # settings reshape it: a blanket channel override must not exempt a mute.
        urgent = note.get("priority") == "critical" or note.get("channel") in PROTECTED_CHANNELS
        for key, value in note.items():
            if key != "ts":
                note[key] = self._redact_value(value)
        state.notification_channel_settings.apply(note)
        # Stamped after redaction so every surface (the WebSocket note the tab
        # toasts from, the stored row, the Web Push payload) carries one tag.
        note["tag"] = native_notification_tag(note)
        # Applied last, so a channel priority override cannot undo the mute.
        if publisher and not urgent and session_is_muted(state, publisher):
            note["silenced"] = True
            note["priority"] = "passive"
        self._sweep_expired(state._notification_log)
        # A keyed note always takes the locked write-then-commit path below, even
        # when nothing holds its key yet: its publisher may replace or dismiss it
        # next, and that change must queue behind this one rather than race it.
        if not note.get("key"):
            self._hold(state, note)
            state._broadcast(note)
            self._send_to_web_push(note)
            if loop is None:
                self._persist_one(note)
                state.last_notification_persist = None
            else:
                # An append mutates memory and enters the FIFO in one synchronous
                # step; locked snapshots do the same, so later appends remain later.
                state.last_notification_persist = loop.run_in_executor(
                    self._executor_provider(), self._persist_one, dict(note)
                )
            return

        if loop is None:
            rows, held_snapshot = self._replacement_rows(state, note)
            if self._rewrite_all(rows):
                self._commit_replacement(state, note, held_snapshot)
                state._broadcast(note)
                self._send_to_web_push(note)
            state.last_notification_persist = None
            return

        async def _replace_once_written() -> bool:
            async with self._rewrite_lock():
                self._keyed_write_in_flight = True
                try:
                    rows, held_snapshot = self._replacement_rows(state, note)
                    if not await loop.run_in_executor(
                        self._executor_provider(), self._rewrite_all, rows
                    ):
                        return False
                    self._commit_replacement(state, note, held_snapshot)
                    state._broadcast(note)
                    self._send_to_web_push(note)
                    return True
                finally:
                    try:
                        self._trim_held_notes(state)
                    finally:
                        self._keyed_write_in_flight = False

        operation = asyncio.create_task(_replace_once_written())
        # The loop holds tasks weakly; once a caller cancels the shield, this set
        # is what keeps the write-then-commit alive until it finishes.
        state._background_tasks.add(operation)
        operation.add_done_callback(state._background_tasks.discard)
        state.last_notification_persist = asyncio.shield(operation)

    def _handed_to_serving_loop(self, state: Any, note: dict[str, Any]) -> bool:
        """Queue a worker thread's delivery on the gateway's serving loop.

        A keyed rewrite snapshots the log and replaces the file under the serving
        loop's lock, so a worker thread appending to that file directly could
        land between the two and be lost on disk. Delivering on the loop puts the
        append in the same FIFO as the rewrite. With no serving loop, or one that
        has stopped, nothing can be rewriting, so the caller delivers inline.
        """
        serving_loop = getattr(state, "serving_loop", None)
        if serving_loop is None or serving_loop.is_closed() or not serving_loop.is_running():
            return False
        try:
            serving_loop.call_soon_threadsafe(self.deliver, state, note)
        except RuntimeError:
            return False
        return True

    def _send_to_web_push(self, note: dict[str, Any]) -> None:
        """Hand a delivered note to the Web Push sink when it would raise a banner.

        Called only once the note is in memory and broadcast, so a keyed note whose
        write failed never reaches a closed device. A silenced (muted) note and a
        passive one are history only, the same gate the unread badge uses, and a
        quiet replacement lands in the bell unannounced, as the open tab's banner
        leaves it out (``useNativeNotification``). Best-effort: the sink schedules
        its own async delivery, so a failure to schedule is logged and never undoes
        the local delivery that already landed.
        """
        if self._web_push_fanout is None:
            return
        if note.get("silenced") or note.get("priority") == "passive" or note.get("quiet"):
            return
        try:
            self._web_push_fanout(dict(note))
        except Exception:
            self._logger_provider().warning("Web push fan-out failed to schedule", exc_info=True)

    @staticmethod
    def _stamp_replacement(note: dict[str, Any], replaced: list[dict[str, Any]]) -> None:
        note.pop("quiet", None)
        note.pop("replaced", None)
        if not replaced:
            return
        note["replaced"] = True
        if _priority_rank(note) >= min(_priority_rank(old) for old in replaced):
            note["quiet"] = True

    def _replacement_rows(
        self, state: Any, note: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        source = note.get("source")
        key = note.get("key")
        replaced = _notes_held_under(state, source, key)
        self._stamp_replacement(note, replaced)
        held_snapshot = [
            held
            for held in state._notification_log
            if not (held.get("key") == key and held.get("source") == source)
        ]
        rows = [dict(held) for held in held_snapshot]
        rows.append(dict(note))
        if len(rows) > self._max_persisted:
            del rows[: len(rows) - self._max_persisted]
        return rows, held_snapshot

    def _commit_replacement(
        self, state: Any, note: dict[str, Any], held_snapshot: list[dict[str, Any]]
    ) -> None:
        _remove_keyed_notes(state, note.get("source"), note.get("key"))

        insert_at = 0
        for index, held in enumerate(state._notification_log):
            if any(held is snapshot_row for snapshot_row in held_snapshot):
                insert_at = index + 1
        state._notification_log.insert(insert_at, note)

        self._trim_held_notes(state)

    def _trim_held_notes(self, state: Any) -> None:
        overflow = len(state._notification_log) - self._max_persisted
        if overflow <= 0:
            return
        del state._notification_log[:overflow]

    def _hold(self, state: Any, note: dict[str, Any]) -> None:
        state._notification_log.append(note)
        if not self._keyed_write_in_flight and len(state._notification_log) > self._max_persisted:
            del state._notification_log[: len(state._notification_log) - self._max_persisted]

    @staticmethod
    def register_sse(state: Any) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        state._sse_queues.append(queue)
        return queue

    @staticmethod
    def unregister_sse(state: Any, queue: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            state._sse_queues.remove(queue)
        except ValueError:
            pass

    async def rewrite(self, state: Any) -> bool:
        """Durably rewrite a snapshot after all earlier FIFO append jobs."""
        async with self._rewrite_lock():
            snapshot = [dict(note) for note in state._notification_log]
            return await asyncio.get_running_loop().run_in_executor(
                self._executor_provider(), self._rewrite_all, snapshot
            )

    async def dismiss(self, state: Any, *, source: str, key: str) -> tuple[bool, bool]:
        """Persist, then remove the note *source* published under *key*.

        Returns ``(removed, persisted)``. A failed rewrite leaves committed
        memory and open views unchanged, so the dismissal remains retryable.
        """

        async def _dismiss_once_written() -> tuple[bool, bool]:
            async with self._rewrite_lock():
                matched = _notes_held_under(state, source, key)
                if not matched:
                    return False, True
                matched_timestamps = [note.get("ts") for note in matched]
                self._keyed_write_in_flight = True
                try:
                    rows = [
                        dict(note)
                        for note in state._notification_log
                        if not (note.get("key") == key and note.get("source") == source)
                    ]
                    if not await asyncio.get_running_loop().run_in_executor(
                        self._executor_provider(), self._rewrite_all, rows
                    ):
                        return True, False
                    _remove_keyed_notes(state, source, key)
                    self._trim_held_notes(state)
                    for timestamp in matched_timestamps:
                        state.broadcast_ws("notification_dismiss", {"ts": timestamp})
                    return True, True
                finally:
                    try:
                        self._trim_held_notes(state)
                    finally:
                        self._keyed_write_in_flight = False

        operation = asyncio.create_task(_dismiss_once_written())
        state._background_tasks.add(operation)
        operation.add_done_callback(state._background_tasks.discard)
        return await asyncio.shield(operation)

    @staticmethod
    async def delete(state: Any, ts: str) -> bool:
        before = len(state._notification_log)
        state._notification_log = [note for note in state._notification_log if note.get("ts") != ts]
        removed = len(state._notification_log) < before
        if removed:
            await state._rewrite_notifications_async()
        return removed

    @staticmethod
    async def set_acknowledged(state: Any, ts: str, *, acknowledged: bool) -> bool:
        for note in state._notification_log:
            if note.get("ts") != ts:
                continue
            note["acked"] = acknowledged
            await state._rewrite_notifications_async()
            event = "notification_ack" if acknowledged else "notification_unack"
            state.broadcast_ws(event, {"ts": ts})
            return True
        return False

    @staticmethod
    async def resolve_skill_reviews(state: Any, slug: str, consumed_at: str) -> int:
        """Acknowledge only the consumed generation of one skill review."""
        if not slug or not consumed_at:
            return 0
        acknowledged: list[str] = []
        for note in state._notification_log:
            ts = note.get("ts")
            if (
                note.get("channel") == "system.skills"
                and note.get("slug") == slug
                and not note.get("acked")
                and isinstance(ts, str)
                and ts
                and ts <= consumed_at
            ):
                note["acked"] = True
                acknowledged.append(ts)
        if not acknowledged:
            return 0
        await state._rewrite_notifications_async()
        for ts in acknowledged:
            state.broadcast_ws("notification_ack", {"ts": ts})
        return len(acknowledged)

    @staticmethod
    async def clear(state: Any) -> None:
        """Clear clients before awaiting disk so a concurrent delivery survives."""
        state._notification_log.clear()
        state.broadcast_ws("notifications_clear", {})
        await state._rewrite_notifications_async()
