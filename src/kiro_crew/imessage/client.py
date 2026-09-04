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

It also keeps a short-lived **ledger of what it has sent**, because the watch is
an all-chat stream and the bridge's own attribution is not sufficient to
recognise the channel's own traffic in every chat. See
:meth:`IMessageClient.is_own_echo`.

Handles are normalized before comparison so an allowlist entry written as
``+61 400 000 000`` matches the ``+61400000000`` the bridge reports.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
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

#: How long a message this client sent stays recognisable as its own echo.
#: This single number balances the two failures, in opposite directions: too
#: short and an echo arriving late is answered, which is the unbounded loop this
#: guard exists to stop; too long and the window in which a user's genuine
#: repeat of the agent's exact words is read as that echo grows. So it is sized
#: at the smallest value comfortably above a delivery round trip -- the bridge's
#: own watch debounce is 500ms, and the echo is a local database row appearing
#: after a local send -- rather than at anything to do with a conversation's
#: length.
ECHO_TTL_S = 30.0

#: Bound on remembered outbound messages. A long answer is delivered as several
#: sends, so this is entries, not turns; the oldest are evicted first.
ECHO_WINDOW = 256

#: Watch buffer size requested from the bridge (its own default is 256).
WATCH_BUFFER_LIMIT = 256

#: Backoff bounds for re-establishing a dropped watch.
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 30.0

#: Bridge error code for "configured database currently unavailable" --
#: retryable, and the usual symptom of missing Full Disk Access.
ERR_DATABASE_UNAVAILABLE = -32002

#: JSON-RPC invalid params. The bridge rejects at validation, before dispatch.
ERR_INVALID_PARAMS = -32602

#: The only failures that PROVE a send never reached Messages, when the bridge
#: did not spell out a delivery disposition of its own.
_DEFINITIVE_REJECTIONS = frozenset({ERR_DATABASE_UNAVAILABLE, ERR_INVALID_PARAMS})

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


def _echo_key(handle: str, text: str) -> str:
    """The ledger key for one (handle, body) pair.

    The body is NFC-normalized and stripped before comparison because the text
    makes a round trip through Messages and back out of the database, and a
    composed/decomposed mismatch on a single accented character would silently
    stop an echo being recognised as one. Handle and body are joined on a
    newline, which ``normalize_handle`` can never produce, so no pair of
    distinct inputs can collide on one key.
    """
    return f"{normalize_handle(handle)}\n{unicodedata.normalize('NFC', text).strip()}"


def _proves_nothing_was_sent(exc: BaseException) -> bool:
    """Whether ``exc`` PROVES the send never reached Messages.

    Fails closed, because the two mistakes do not cost the same. Dropping the
    echo guard for a message that DID go out lets its echo be answered, which is
    the unbounded loop this whole mechanism exists to stop. Keeping a guard for a
    message that never went out only suppresses one identical inbound for the
    rest of the TTL. So anything not recognisably definitive is treated as
    possibly delivered.

    The bridge's own delivery disposition is the authority when it gives one: it
    documents ``retry_safe`` and ``disposition`` on a delivery failure's ``data``,
    where ``not_started`` is the one value that proves the transport never
    dispatched. Absent that, only a rejection that happens before dispatch
    counts. A transport failure, a timeout and a cancellation prove nothing --
    the request may already have been written to the bridge's stdin.
    """
    if not isinstance(exc, RpcError):
        return False
    if isinstance(exc.data, dict):
        return exc.data.get("retry_safe") is True or exc.data.get("disposition") == "not_started"
    return exc.code in _DEFINITIVE_REJECTIONS


@dataclass
class _OwnSend:
    """One message this client sent, remembered long enough to recognise its echo.

    A sent message is recognisable two ways -- by the body it was sent with, and
    by the GUID the bridge reports for it -- and they are held together in ONE
    record on purpose. As two independent entries, matching on either left the
    other alive to swallow the next genuine message carrying it.

    The lifetime rule is the other half, and it is why ``expires_at`` is optional
    rather than a timestamp computed up front: **the TTL clock starts when the
    send's outcome is known, and a record with no outcome yet never expires.** A
    pre-computed expiry cannot express "not counting yet", and overloading one
    absolute timestamp with both meanings is what let a slow send outlive its own
    guard -- ``_call`` waits up to 30s and the TTL is 30s, so a send that reached
    its timeout left a record already eligible for pruning at the moment its echo
    could arrive, and the echo was answered.
    """

    body_key: str
    #: ``None`` while the send is in flight: an unresolved record never expires,
    #: because the echo can arrive before the call returns. Set once, to
    #: ``monotonic() + ECHO_TTL_S``, when the send succeeds or fails ambiguously.
    expires_at: float | None = None
    #: Set once the send returns, since the bridge reports it best-effort.
    guid: str = ""


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
        #: Ledger of what THIS client sent, oldest first. One record per sent
        #: message, carrying both of the keys it can be recognised by.
        self._own_sends: list[_OwnSend] = []
        #: Handles already reported as looping, so the warning is written once
        #: per handle instead of once per suppressed message.
        self._echo_reported: set[str] = set()
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
        # Recorded BEFORE the call, not after: the watch can emit the row for
        # this message while `send` is still awaiting its own result (the bridge
        # verifies the inserted row before answering, and its watch debounce is
        # only 500ms), and an echo that arrives before it is remembered is an
        # echo that gets answered. A send that then fails leaves one stale entry
        # behind, which costs no more than the TTL already does.
        # Recorded BEFORE the call, and with NO expiry: the watch can emit the row
        # for this message while `send` is still awaiting its own result (the
        # bridge verifies the inserted row before answering, and its watch
        # debounce is only 500ms), so an echo that arrives before the record
        # exists -- or after a pre-computed TTL would already have pruned it --
        # is an echo that gets answered. The clock starts below, once the outcome
        # is known.
        record = self._remember_own_send(_echo_key(to, text))
        try:
            result = await self._call("send", params)
        except BaseException as exc:
            if _proves_nothing_was_sent(exc):
                # Definitively rejected, so nothing can echo -- and leaving the
                # record would have the undelivered body suppress a genuine
                # message carrying those words for the rest of the TTL.
                self._forget_own_send(record)
            else:
                # Ambiguous: the bridge documents -32001 as "may have completed
                # or remains in flight", and a timeout, a cancellation or a dead
                # child says nothing about what it did with a request already
                # written to its stdin. The message may still arrive and echo, so
                # the guard stays and its clock starts here.
                record.expires_at = time.monotonic() + ECHO_TTL_S
            raise
        record.expires_at = time.monotonic() + ECHO_TTL_S
        guid = _str(result.get("guid"))
        # `id`/`guid` are best-effort in the bridge's contract, so this is an
        # additional key on the same record rather than the mechanism. It is the
        # stronger of the two -- a GUID this client sent can never legitimately
        # arrive as user input, so unlike the body it has no false positive.
        if guid:
            record.guid = guid
        return guid

    def _remember_own_send(self, body_key: str) -> _OwnSend:
        """Record one outbound message, evicting the oldest RESOLVED record if needed.

        The record starts with no expiry -- see :class:`_OwnSend` -- and eviction
        skips unresolved records for the same reason pruning does: an unresolved
        record is the guard for a send that has not returned, so dropping it is
        exactly the loop this ledger exists to prevent. Reaching that needs many
        sends outstanding at once, which this channel does not do today (``send``
        awaits its own call, so one conversation has one send in flight), but the
        invariant is cheaper to hold than to reason about per caller.

        The list can therefore overshoot ``ECHO_WINDOW`` while sends are in
        flight. That is bounded by concurrent sends rather than by conversation
        length, and it always drains, because every send resolves: success and an
        ambiguous failure both set an expiry, and a definitive rejection removes
        the record outright.
        """
        record = _OwnSend(body_key=body_key)
        self._own_sends.append(record)
        # A long answer is delivered as several sends, so the bound is entries,
        # not turns; the oldest RESOLVED ones go first.
        overflow = len(self._own_sends) - ECHO_WINDOW
        if overflow > 0:
            kept: list[_OwnSend] = []
            for held in self._own_sends:
                if overflow > 0 and held.expires_at is not None:
                    overflow -= 1
                    continue
                kept.append(held)
            self._own_sends = kept
        return record

    def _forget_own_send(self, record: _OwnSend) -> None:
        """Drop one record. Identity, not equality -- two sends can be identical."""
        for index, held in enumerate(self._own_sends):
            if held is record:
                del self._own_sends[index]
                return

    def is_own_echo(self, inbound: IMessageInbound) -> bool:
        """Whether ``inbound`` is this channel's own outbound message coming back.

        The all-chat watch sees every row, including the ones this client caused,
        and ``is_from_me`` alone does not separate them in every chat. In a
        **self-chat** -- the user's own handle, which the docs used to prescribe
        for the first run -- the allow-listed sender IS the identity the agent
        sends as, so an echo of the agent's reply is indistinguishable from user
        input unless that flag is both present and correct on the row. The
        bridge's contract says it need not be: ``watch.subscribe`` defaults to a
        500ms debounce expressly so an ``is_from_me`` *correction* can land
        first, and ``sender`` is documented as empty for some self-sent
        messages. Trusting it as the only guard is what let the channel answer
        itself without limit (issue #5246).

        So the guard is a record of what this client itself sent, which is the
        one signal it fully owns. The matching record is CONSUMED WHOLE, so the
        ledger suppresses each sent message exactly once and a genuine later
        repeat of the same words is delivered normally. Rows the bridge already
        attributes to us (``is_from_me``) are dropped by the transport before
        they reach here, so they never consume the entry the real echo needs.
        """
        now = time.monotonic()
        # Expired records are dropped rather than skipped: an expired entry can
        # never legitimately match again, and pruning here is what keeps a run of
        # failed sends from holding the list at its bound. A record with no expiry
        # is still IN FLIGHT and is never pruned -- that is the whole point of the
        # optional expiry, since the echo of a slow send arrives before its
        # outcome does.
        self._own_sends = [r for r in self._own_sends if r.expires_at is None or r.expires_at > now]
        body_key = _echo_key(inbound.handle, inbound.text)
        for index, record in enumerate(self._own_sends):
            if not ((inbound.guid and record.guid == inbound.guid) or record.body_key == body_key):
                continue
            del self._own_sends[index]
            handle = normalize_handle(inbound.handle)
            # Every suppression is recorded, because a drop with no signal at all
            # is indistinguishable from a message that never arrived -- and this
            # one is silent to the user by construction (replying would be the
            # loop). The WARNING fires once per handle so a self-chat announces
            # itself without writing a line per outbound message; the rest land
            # at debug so a support question has something to read.
            if handle not in self._echo_reported:
                self._echo_reported.add(handle)
                logger.warning(
                    "imessage: dropping this channel's own message echoed back from %s. "
                    "This is what a self-chat looks like -- the handle the agent "
                    "replies to is its own -- and without this the channel would "
                    "answer itself in a loop.",
                    redact_handle(inbound.handle),
                )
            else:
                logger.debug(
                    "imessage: suppressed an echo of this channel's own message from %s",
                    redact_handle(inbound.handle),
                )
            return True
        return False

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
