"""Notification delivery, persistence, and client-state coordination."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import Executor
from typing import Any

from kiro_crew.notifications.bus import PRIORITIES
from kiro_crew.notifications.settings import PROTECTED_CHANNELS


def session_is_muted(state: Any, session_key: str) -> bool:
    """Whether *session_key*'s attention is muted, as the dashboard reads it.

    Mirrors ``isSlotMuted`` (``website/src/hooks/sessionMute.ts``): the session's
    own ``muted`` flag, or ``mutes_opened`` on a session up its ``_created_by``
    chain, walking from its creator. A missing creator or a cycle ends it unmuted.
    """
    from kiro_crew.dashboard.session_control import caller_slot_key

    slot = state.get_slot(caller_slot_key(state, session_key))
    if slot is None:
        return False
    if slot.muted:
        return True
    seen = {slot.key}
    key = slot._created_by
    while key and key not in seen:
        creator = state.get_slot(key)
        if creator is None:
            return False
        if creator.mutes_opened:
            return True
        seen.add(key)
        key = creator._created_by
    return False


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


def _remove_keyed_notes(state: Any, source: Any, key: Any) -> list[dict[str, Any]]:
    """Remove and return the stored notes *source* published under *key*.

    A key names a note within its source, so two sources never collide. A note
    without a key is never matched.
    """
    if not key:
        return []
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for held in state._notification_log:
        same_key = held.get("key") == key and held.get("source") == source
        (removed if same_key else kept).append(held)
    if removed:
        state._notification_log[:] = kept
    return removed


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
        # Delivery-only metadata: read here, so it is never redacted, stored or sent.
        publisher = note.pop("publisher_session", None)
        # Urgency is what the producer asked for, read before the user's channel
        # settings reshape it: a blanket channel override must not exempt a mute.
        urgent = note.get("priority") == "critical" or note.get("channel") in PROTECTED_CHANNELS
        for key, value in note.items():
            if key != "ts":
                note[key] = self._redact_value(value)
        state.notification_channel_settings.apply(note)
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
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._persist_one(note)
                state.last_notification_persist = None
            else:
                # An append mutates memory and enters the FIFO in one synchronous
                # step; locked snapshots do the same, so later appends remain later.
                state.last_notification_persist = loop.run_in_executor(
                    self._executor_provider(), self._persist_one, dict(note)
                )
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            rows, held_snapshot = self._replacement_rows(state, note)
            if self._rewrite_all(rows):
                self._commit_replacement(state, note, held_snapshot)
                state._broadcast(note)
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

    @staticmethod
    def _set_quiet(note: dict[str, Any], replaced: list[dict[str, Any]]) -> None:
        note.pop("quiet", None)
        if replaced and _priority_rank(note) >= min(_priority_rank(old) for old in replaced):
            note["quiet"] = True

    def _replacement_rows(
        self, state: Any, note: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        source = note.get("source")
        key = note.get("key")
        replaced = _notes_held_under(state, source, key)
        self._set_quiet(note, replaced)
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
        removed = _remove_keyed_notes(state, note.get("source"), note.get("key"))
        for old in removed:
            if old.get("priority") != "passive":
                state._unread_count = max(0, state._unread_count - 1)

        insert_at = 0
        for index, held in enumerate(state._notification_log):
            if any(held is snapshot_row for snapshot_row in held_snapshot):
                insert_at = index + 1
        state._notification_log.insert(insert_at, note)
        if note.get("priority") != "passive":
            state._unread_count += 1

        self._trim_held_notes(state)

    def _trim_held_notes(self, state: Any) -> None:
        overflow = len(state._notification_log) - self._max_persisted
        if overflow <= 0:
            return
        evicted = state._notification_log[:overflow]
        del state._notification_log[:overflow]
        for old in evicted:
            if old.get("priority") != "passive":
                state._unread_count = max(0, state._unread_count - 1)

    def _hold(self, state: Any, note: dict[str, Any]) -> None:
        state._notification_log.append(note)
        if not self._keyed_write_in_flight and len(state._notification_log) > self._max_persisted:
            del state._notification_log[: len(state._notification_log) - self._max_persisted]
        if note.get("priority") != "passive":
            state._unread_count += 1

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
                    removed = _remove_keyed_notes(state, source, key)
                    for note in removed:
                        if note.get("priority") != "passive":
                            state._unread_count = max(0, state._unread_count - 1)
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
        state._unread_count = 0
        state.broadcast_ws("notifications_clear", {})
        await state._rewrite_notifications_async()
