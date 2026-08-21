"""The ``imsg`` bridge client -- iMessage semantics over the JSON-RPC peer.

Owns everything that is true of iMessage rather than of JSON-RPC: the readiness
probe, the resumable inbound watch, outbound sends, and the two progress
affordances (typing indicator, read receipt).

Three behaviours here exist because the bridge's watch contract demands them,
not as defensive extras:

* **The row cursor is persisted.** Without it a gateway restart silently drops
  every message sent while it was down; with it the watch resubscribes at the
  last row it actually observed.
* **A bounded dedupe window keyed on message GUID.** The overflow cursor is at
  or before the first dropped message, so the bridge documents duplicate replay
  as possible by design. Dedupe is what makes the resume safe, not optional.
* **``watch.overflow`` is terminal and must be answered.** The subscription
  ENDS when its buffer fills; a client that ignores the notification goes
  permanently silent under a burst rather than losing one message.

Handles are normalized before comparison so an allowlist entry written as
``+61 400 000 000`` matches the ``+61400000000`` the bridge reports.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.imessage.bridge_path import (
    BRIDGE_BINARY,
    TRUSTED_BRIDGE_PATHS,
    resolve_bridge_path,
)
from kiro_crew.imessage.rpc import JsonRpcPeer, RpcError, RpcTransportError

logger = logging.getLogger(__name__)

#: Protocol version this client speaks. The bridge rejects anything else.
PROTOCOL_VERSION = 1

#: How many delivered GUIDs to remember for duplicate suppression. Sized well
#: above the watch buffer so a full-buffer overflow replay is covered end to
#: end: a window smaller than the buffer would let part of the replay through.
DEDUPE_WINDOW = 1024

#: Watch buffer size requested from the bridge (its own default is 256).
WATCH_BUFFER_LIMIT = 256

#: Backoff bounds for re-establishing a dropped watch.
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 30.0

#: Bridge error code for "configured database currently unavailable" --
#: retryable, and the usual symptom of missing Full Disk Access.
ERR_DATABASE_UNAVAILABLE = -32002

#: Non-digit characters to drop when comparing phone-shaped handles.
_PHONE_NOISE = re.compile(r"[\s()\-.]")


def normalize_handle(handle: str) -> str:
    """Canonicalize a handle for allowlist comparison.

    An email handle folds to lowercase; a phone handle loses formatting so the
    same number written three ways compares equal. Anything else is lowercased
    and stripped, which is still stable.
    """
    value = (handle or "").strip()
    if not value:
        return ""
    if "@" in value:
        return value.lower()
    return _PHONE_NOISE.sub("", value)


@dataclass
class IMessageInbound:
    """One inbound message, as the transport and dispatcher need it."""

    handle: str
    text: str
    guid: str = ""
    rowid: int = 0
    chat_guid: str = ""
    chat_identifier: str = ""
    chat_id: int = 0
    is_group: bool = False
    is_from_me: bool = False

    @property
    def chat_selector(self) -> dict[str, Any]:
        """The bridge selector for this chat, preferring the portable GUID.

        ``chat_id`` is a row id scoped to one database instance, so it is the
        last resort: it stops resolving after a Messages restore.
        """
        if self.chat_guid:
            return {"chat_guid": self.chat_guid}
        if self.chat_identifier:
            return {"chat_identifier": self.chat_identifier}
        if self.chat_id:
            return {"chat_id": self.chat_id}
        return {}


InboundHandler = Callable[[IMessageInbound], Awaitable[None]]


def parse_inbound(message: dict[str, Any]) -> IMessageInbound | None:
    """Map a bridge Message object onto :class:`IMessageInbound`.

    The bridge OMITS inapplicable string fields rather than sending null, so
    every read is a ``get`` with a typed fallback; a field carrying the wrong
    type is treated as absent rather than crashing the reader.
    """
    if not isinstance(message, dict):
        return None
    return IMessageInbound(
        handle=_str(message.get("sender")),
        text=_str(message.get("text")),
        guid=_str(message.get("guid")),
        rowid=_int(message.get("id")),
        chat_guid=_str(message.get("chat_guid")),
        chat_identifier=_str(message.get("chat_identifier")),
        chat_id=_int(message.get("chat_id")),
        is_group=bool(message.get("is_group")),
        is_from_me=bool(message.get("is_from_me")),
    )


class IMessageClient:
    """Long-lived ``imsg rpc`` child plus a resumable all-chat watch."""

    def __init__(
        self,
        *,
        db_path: str = "",
        service: str = "imessage",
        on_message: InboundHandler | None = None,
        cursor_path: Path | None = None,
        buffer_limit: int = WATCH_BUFFER_LIMIT,
    ) -> None:
        self._db_path = db_path
        self._service = service or "imessage"
        self._on_message = on_message
        self._buffer_limit = buffer_limit
        self._cursor_path = cursor_path or (data_home() / "imessage_cursor.json")

        self._peer: Optional[JsonRpcPeer] = None
        self._subscription: int | None = None
        self._since_rowid: int = 0
        self._seen_guids: dict[str, None] = {}
        self._resubscribe_task: Optional[asyncio.Task[None]] = None
        self._closing = False

        #: Set once the watch is established, so the gateway can report a
        #: truthful badge instead of green over a missing permission.
        self.ready = asyncio.Event()
        self.last_error = ""
        self.on_state_change: Callable[[bool, str], None] | None = None

        #: Probed from the bridge's readiness snapshot. Both degrade silently:
        #: iMessage cannot edit a sent message, so a typing indicator is the
        #: only progress signal available -- but it is not worth failing over.
        self.typing_supported = False
        self.read_supported = False

    # -- lifecycle ----------------------------------------------------------

    def set_message_handler(self, on_message: InboundHandler) -> None:
        """Wire the inbound handler after construction.

        Breaks the client<->transport construction cycle: the transport needs
        the client, and the client needs the transport's ``receive``.
        """
        self._on_message = on_message

    async def start(self) -> None:
        """Spawn the bridge, probe it, and open the watch."""
        self._closing = False
        self._since_rowid = self._load_cursor()
        await self._spawn_peer()
        await self._probe()
        await self._subscribe()

    async def _spawn_peer(self) -> None:
        """Resolve the binary, spawn it, and hold the peer.

        Split out of ``start`` so a reconnect can rebuild the peer: a bridge that
        exited leaves a dead process behind, and re-subscribing on it can never
        succeed no matter how many times it is retried.
        """
        # Resolved here, never taken from configuration or PATH -- see bridge_path.
        cli_path = resolve_bridge_path()
        if not cli_path:
            raise RpcTransportError(
                f"{BRIDGE_BINARY} is not installed (looked in "
                f"{', '.join(TRUSTED_BRIDGE_PATHS)}); "
                "install it with 'brew install steipete/tap/imsg'"
            )
        argv = [cli_path, "rpc"]
        if self._db_path:
            argv += ["--db-path", self._db_path]
        peer = JsonRpcPeer(
            argv,
            on_notification=self._on_notification,
            on_disconnect=self._on_peer_lost,
        )
        await peer.start()
        self._peer = peer

    def _on_peer_lost(self, reason: str) -> None:
        """The bridge went away on its own: say so, and start trying to get back.

        Marking state is the half that makes the failure VISIBLE -- the badge
        would otherwise stay connected while inbound is dead -- and dropping the
        peer is the half that makes the retry meaningful, since the reconnect
        respawns rather than re-subscribing on a corpse.

        A replacement peer cannot re-deliver a row the dead one was mid-way
        through: the cursor advanced when that row arrived, so ``watch.subscribe``
        resumes after it. The old peer is still closed rather than abandoned, so
        its in-flight handlers stop instead of trying to send on a dead bridge.
        """
        old = self._peer
        self._peer = None
        self._subscription = None
        self.ready.clear()
        self.last_error = reason[:120]
        logger.warning("imessage: %s; will try to reconnect", reason)
        self._set_state(False, reason[:120])
        if old is not None:
            # Sync callback, async teardown: schedule it rather than block the
            # reader's own exit path.
            asyncio.create_task(self._close_peer(old))
        self._schedule_resubscribe()

    async def _close_peer(self, peer: JsonRpcPeer) -> None:
        try:
            await peer.close()
        except Exception:
            logger.debug("imessage: closing the lost peer failed", exc_info=True)

    async def close(self) -> None:
        self._closing = True
        self.ready.clear()
        task = self._resubscribe_task
        self._resubscribe_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        peer = self._peer
        self._peer = None
        self._subscription = None
        if peer is not None:
            await peer.close()

    async def wait_ready(self, timeout: float = 15.0) -> bool:
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # -- outbound -----------------------------------------------------------

    async def send(self, to: str, text: str) -> str:
        """Send ``text`` to a handle; return the message GUID when known.

        ``id``/``guid`` are best-effort in the bridge's own contract, so their
        absence is success with no id -- never a failure.

        A transport or bridge-level failure RAISES. It deliberately does not
        collapse into the same empty string: a caller cannot tell those apart,
        so swallowing the error here would let a turn be recorded as answered
        when nothing was delivered. Callers that genuinely want best-effort
        delivery (an advisory notice) catch it at their own call site.
        """
        if not text:
            return ""
        params: dict[str, Any] = {"to": to, "text": text}
        # The bridge defaults to iMessage; only name a service when the
        # operator asked for something else, so a default install never
        # exercises the SMS-fallback path by accident.
        if self._service != "imessage":
            params["service"] = self._service
        result = await self._call("send", params)
        return _str(result.get("guid"))

    async def send_typing(self, selector: dict[str, Any]) -> None:
        """Show a typing indicator, if the bridge offers one.

        The only progress affordance iMessage has: a sent message cannot be
        edited, so there is no placeholder to update. Any failure disables the
        feature for the process rather than retrying it every turn -- the
        method's parameters are not part of the bridge's documented surface,
        so a rejection is treated as "not available here".
        """
        if not self.typing_supported or not selector:
            return
        try:
            await self._call("typing", dict(selector), timeout=10.0)
        except (RpcError, RpcTransportError) as exc:
            logger.debug("imessage: typing unavailable, disabling (%s)", exc)
            self.typing_supported = False

    async def mark_read(self, selector: dict[str, Any]) -> None:
        """Mark the chat read, if the bridge offers it. Same degrade policy."""
        if not self.read_supported or not selector:
            return
        try:
            await self._call("read", dict(selector), timeout=10.0)
        except (RpcError, RpcTransportError) as exc:
            logger.debug("imessage: read unavailable, disabling (%s)", exc)
            self.read_supported = False

    # -- readiness probe ----------------------------------------------------

    async def _probe(self) -> None:
        """Handshake + capability probe.

        ``initialize`` is optional and idempotent in the bridge's contract, and
        returns the same readiness snapshot as ``status``. ``methods`` on that
        snapshot is the structurally usable surface AT THAT INSTANT, which is
        what decides which optional methods this process will attempt.
        """
        snapshot = await self._call("initialize", {"protocol_version": PROTOCOL_VERSION})
        raw_methods = snapshot.get("methods")
        available = (
            {m for m in raw_methods if isinstance(m, str)}
            if isinstance(raw_methods, list)
            else set()
        )
        # typing and read are documented exceptions to the injected-helper
        # requirement (typing keeps a direct fallback, read activates the
        # bridge itself), so they can be present with bridge.ready false --
        # which is the state of a default install that has not disabled SIP.
        self.typing_supported = "typing" in available
        self.read_supported = "read" in available
        database = snapshot.get("database")
        if isinstance(database, dict) and not database.get("ready", True):
            # Almost always missing Full Disk Access for THIS process context.
            # The grant is recorded per process context, so a headless launch
            # agent needs its own one-time interactive grant.
            self.last_error = _str(database.get("error")) or "Messages database unavailable"
            logger.warning("imessage: %s", self.last_error)

    # -- inbound watch ------------------------------------------------------

    async def _subscribe(self) -> None:
        params: dict[str, Any] = {"buffer_limit": self._buffer_limit}
        if self._since_rowid > 0:
            params["since_rowid"] = self._since_rowid
        result = await self._call("watch.subscribe", params)
        self._subscription = _int(result.get("subscription")) or None
        self._set_state(True, "")
        logger.info(
            "imessage: watching all chats (subscription=%s, since_rowid=%d)",
            self._subscription,
            self._since_rowid,
        )

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "message":
            message = params.get("message")
            await self._handle_message(message if isinstance(message, dict) else {})
        elif method == "watch.overflow":
            # Terminal: the subscription is already dead. Resume at the cursor
            # the bridge hands back, or the channel stays silent forever.
            resume = _int(params.get("resume_after_rowid"))
            reason = _str(params.get("reason")) or "buffer_limit_exceeded"
            logger.warning("imessage: watch overflow (%s); resuming after rowid %d", reason, resume)
            self._subscription = None
            if resume > 0:
                self._since_rowid = resume
                self._save_cursor(resume)
            self._schedule_resubscribe()

    async def _handle_message(self, message: dict[str, Any]) -> None:
        """Account for a row, then hand it to the handler. Delivery is at-most-once.

        The cursor advances HERE, before dispatch -- deliberately, and matching
        every other channel in this repo:

        * Telegram advances its ``getUpdates`` offset at fetch time, before
          dispatch, and does not persist it at all.
        * Discord's sequence number is for websocket resume, not delivery.
        * Webex and Teams keep no inbound cursor whatsoever.
        * ``issue_radar``, the one persisted watermark in the tree, advances
          BEFORE notifying and says so: at-most-once, chosen so a delivery
          hiccup cannot re-announce.

        Advancing only after the handler returned -- at-least-once -- is the one
        shape nothing here implements, and it does not come for free: it forces
        global serialization (so a failed row is not passed by a later one), a
        buffer to serialize behind, and a rule for retiring the old consumer on
        reconnect. That machinery was built here once and every piece of it was a
        defect. The cost of this direction is the honest one: a message arriving
        while the gateway is dying is lost, and the user re-sends.

        Ordering is likewise not this layer's job. Two messages from the same
        handle cannot run two turns concurrently because
        ``sessions.get_or_create`` holds a per-session semaphore for the turn's
        duration; messages from different handles have no ordering relationship.
        """
        inbound = parse_inbound(message)
        if inbound is None:
            return
        self._advance_cursor(inbound.rowid)
        # The bridge documents duplicate replay after an overflow resume (the
        # resume point is at or before the first dropped row), so the GUID window
        # stays -- but only to suppress that replay, never as a correctness
        # mechanism for delivery.
        if inbound.guid and self._already_seen(inbound.guid):
            return
        if self._on_message is None:
            return
        await self._on_message(inbound)

    def _advance_cursor(self, rowid: int) -> None:
        """Persist ``rowid`` as the resume point when it moves the cursor forward."""
        if rowid > self._since_rowid:
            self._since_rowid = rowid
            self._save_cursor(rowid)

    def _already_seen(self, guid: str) -> bool:
        """Whether ``guid`` was seen before, recording it as a side effect.

        Check and record are one step again: with the cursor advancing on receipt
        there is no failure path that needs the record held back.
        """
        if guid in self._seen_guids:
            return True
        self._seen_guids[guid] = None
        while len(self._seen_guids) > DEDUPE_WINDOW:
            # dicts preserve insertion order, so this evicts the oldest GUID.
            self._seen_guids.pop(next(iter(self._seen_guids)))
        return False

    def _schedule_resubscribe(self) -> None:
        if self._closing or self._resubscribe_task is not None:
            return
        self._resubscribe_task = asyncio.create_task(self._resubscribe_loop())

    async def _resubscribe_loop(self) -> None:
        delay = RECONNECT_MIN_S
        try:
            while not self._closing and self._subscription is None:
                try:
                    # A lost bridge needs the process back before the watch can
                    # reopen; an overflow only needs the watch. Both land here,
                    # so respawn is conditional on the peer actually being gone.
                    if self._peer is None:
                        await self._spawn_peer()
                        await self._probe()
                    await self._subscribe()
                    return
                except RpcError as exc:
                    if exc.code == ERR_DATABASE_UNAVAILABLE:
                        self._set_state(False, "Messages database unavailable")
                    else:
                        self._set_state(False, exc.message[:120])
                except RpcTransportError as exc:
                    self._set_state(False, str(exc)[:120])
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_S)
        except asyncio.CancelledError:
            raise
        finally:
            self._resubscribe_task = None

    # -- helpers ------------------------------------------------------------

    async def _call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        peer = self._peer
        if peer is None:
            raise RpcTransportError("bridge is not running")
        return await peer.call(method, params, timeout=timeout)

    def _set_state(self, connected: bool, error: str) -> None:
        self.last_error = error
        if connected:
            self.ready.set()
        else:
            self.ready.clear()
        if self.on_state_change is not None:
            try:
                self.on_state_change(connected, error)
            except Exception:
                logger.debug("imessage: state callback failed", exc_info=True)

    def _load_cursor(self) -> int:
        try:
            raw = self._cursor_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return 0
        try:
            data = json.loads(raw)
        except ValueError:
            return 0
        return _int(data.get("since_rowid")) if isinstance(data, dict) else 0

    def _save_cursor(self, rowid: int) -> None:
        try:
            self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self._cursor_path, json.dumps({"since_rowid": rowid}))
        except OSError:
            # A read-only home must not stop message delivery; the cost is a
            # replay window on the next restart, which dedupe absorbs.
            logger.debug("imessage: cursor persist failed", exc_info=True)


def redact_handle(handle: str) -> str:
    """A handle is a phone number or an email -- never log it whole."""
    value = handle or ""
    return f"{value[:3]}***" if value else "?"


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    return value if isinstance(value, int) else 0
