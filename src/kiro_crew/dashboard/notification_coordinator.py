"""Notification delivery, persistence, and client-state coordination."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from concurrent.futures import Executor
from typing import Any

from kiro_crew.notifications.settings import PROTECTED_CHANNELS

# Byte cap on a native-notification tag, counted in serialized bytes (the UTF-8
# length of its JSON-escaped form, as the Web Push payload encodes it). The push
# payload holds its tag to the same budget, so a tag minted here always fits the
# push path unchanged (a cut tag would differ from the one the open tab shows).
NATIVE_TAG_MAX_BYTES = 128
NATIVE_TAG_ID_KEYS = ("approval_id", "job_id", "task_id", "ts")


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
    its own. Derived from the note's event id (approval, job, task, then the
    note's own timestamp), so distinct events never collapse onto one banner.
    An id that is over budget, or not valid UTF-8 (a lone surrogate an API
    caller escaped into its metadata), is hashed, so the tag always fits and
    always encodes: optional metadata can never abort local delivery.
    """
    for key in NATIVE_TAG_ID_KEYS:
        value = note.get(key)
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
        rewrite_all: Callable[[list[dict[str, Any]]], None],
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
        # Stamped after redaction so every surface (the WebSocket note the tab
        # toasts from, the stored row, the Web Push payload) carries one tag.
        note["tag"] = native_notification_tag(note)
        # Applied last, so a channel priority override cannot undo the mute.
        if publisher and not urgent and session_is_muted(state, publisher):
            note["silenced"] = True
            note["priority"] = "passive"
        self._sweep_expired(state._notification_log)
        state._notification_log.append(note)
        if len(state._notification_log) > self._max_persisted:
            del state._notification_log[: len(state._notification_log) - self._max_persisted]
        if note.get("priority") != "passive":
            state._unread_count += 1
        state._broadcast(note)

        # Secondary sink: fan out to browser Web Push. A silenced (muted) note
        # or one forced to passive priority is history-only — the same gate the
        # unread badge uses above — so it must not raise an OS banner on a
        # closed device either. Best-effort: the fan-out schedules its own
        # async delivery and never blocks or fails local delivery.
        if self._web_push_fanout is not None and not note.get("silenced"):
            if note.get("priority") != "passive":
                try:
                    self._web_push_fanout(dict(note))
                except Exception:
                    self._logger_provider().warning(
                        "Web push fan-out failed to schedule", exc_info=True
                    )

        # One FIFO executor owns append and rewrite jobs.  Snapshot the row
        # because acknowledgement may mutate the in-memory object afterward.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._persist_one(note)
            state.last_notification_persist = None
        else:
            state.last_notification_persist = loop.run_in_executor(
                self._executor_provider(), self._persist_one, dict(note)
            )

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

    async def rewrite(self, state: Any) -> None:
        """Durably rewrite a snapshot after all earlier FIFO append jobs."""
        snapshot = [dict(note) for note in state._notification_log]
        await asyncio.get_running_loop().run_in_executor(
            self._executor_provider(), self._rewrite_all, snapshot
        )

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
