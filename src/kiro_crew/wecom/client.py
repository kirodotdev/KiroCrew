"""WeCom AI Bot WebSocket client transport layer.

Inbound: an outbound WebSocket long-connection to WeCom receives user messages
(cmd ``aibot_msg_callback``), each carrying a server-assigned ``headers.req_id``
and a single-use ``response_url``.

Outbound: the bot streams its reply over the SAME WebSocket via
``aibot_respond_msg`` frames that replay the inbound ``req_id`` (the sole routing
key; ``body.stream.content`` is the full accumulated text, replacing the bubble
each frame).

``response_url`` is NOT part of the long-connection contract. The documented
``aibot_msg_callback`` body carries no such field — it belongs to callback-URL
mode, and to card-event payloads — so an ack that can only go out over HTTP is an
ack the user may never see. It is still parsed and kept as a last-resort fallback
for a frame that arrives with no ``req_id``, so a deployment that does supply one
is served, but nothing may DEPEND on it: out-of-band messages go through
:meth:`WeComClient.say`, which opens a fresh ``stream_id`` on the inbound
``req_id`` — a path the WS always supports.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import aiohttp

from kiro_crew.messaging.split import truncate_utf8
from kiro_crew.wecom.attachments import to_attachments
from kiro_crew.wecom.media import mixed_text

logger = logging.getLogger(__name__)

# WeCom caps a reply in UTF-8 **BYTES**, not characters: `stream.content` and
# `markdown.content` are both documented at 20480 bytes. The unit is the whole
# point — one CJK character is three bytes and an emoji four, so 20000
# CHARACTERS of Chinese is ~60000 bytes, three times over the limit, and WeCom
# rejects the frame outright. This channel's users write Chinese, so that was the
# common case, not the edge one.
WECOM_MAX_REPLY_BYTES = 20480

# The CHARACTER budget a caller may plan against, derived so the worst case (four
# bytes per character) still fits the byte cap. This is what the transport
# declares as ``max_message_chars``, because the shared splitter counts
# characters and cannot see bytes; ``truncate_utf8`` is then the exact guard at
# the wire. Same derivation, and the same reason, as Webex's
# ``WEBEX_MAX_TEXT // 4``.
WECOM_SAFE_REPLY_CHARS = WECOM_MAX_REPLY_BYTES // 4

# A WS connection must live at least this long to count as "healthy" and reset the
# reconnect backoff. A connect->immediate-close (bad creds, or a WeCom anti-kick
# that closes the socket without raising) stays on the backoff curve so it cannot
# hot-loop with zero delay.
_MIN_HEALTHY_CONN_SECS = 5.0

# ``chattype`` on an inbound callback: a 1:1 bot chat, versus a group room.
# A group is a shared disclosure boundary, so the transport gates on this.
CHAT_TYPE_SINGLE = "single"

# What an unrecognizable ``chattype`` becomes. Deliberately not "single" and not
# "group": it is neither, and the point is that no gate keyed on a known value can
# mistake it for one it allows.
CHAT_TYPE_UNKNOWN = "unknown"

# The subscribe frame's ``req_id`` carries this prefix so its ACK — a cmd-less
# frame indistinguishable from a pong or a reply receipt except by id — can be
# recognized and its ``errcode`` believed. Without it a REJECTED credential is
# invisible on the wire and the channel reports the failure only as "the server
# closed the connection immediately", which is also what an anti-kick looks
# like. Mirrors the ``generate_req_id(cmd)`` convention in WeCom's own SDKs.
_SUBSCRIBE_REQ_PREFIX = "aibot_subscribe-"

# A proactive push carries this prefix for the same reason, and additionally has
# its ACK AWAITED: unlike a stream frame -- which can be recovered by rolling to a
# new bubble -- there is no later frame to notice a refusal on, and the caller
# (the dashboard mirror) records delivery from the return value. So "sent" has to
# mean "the platform accepted it".
_PUSH_REQ_PREFIX = "aibot_send_msg-"

# How long to wait for that ACK. Generous enough to survive a slow round trip,
# short enough that a mirror send cannot hang a dashboard turn; a timeout is
# reported as failure, because an unacknowledged push is exactly the case the
# caller must not record as delivered.
_PUSH_ACK_TIMEOUT_SECS = 10.0

# Reply-ACK errcodes that mean THIS stream bubble can never be written again:
# 846605 the req_id is not (or no longer) routable, 846608 the bubble passed the
# platform's 10-minute lifetime and is sealed. Both are terminal for the
# stream_id, not for the connection, so the renderer's answer is recoverable —
# but only if it learns the frame was refused. ``send_stream`` returning True
# means "put on the wire", never "accepted".
_STREAM_DEAD_ERRCODES = frozenset({846605, 846608})

# Bounded inbound-dedupe window. WeCom documents ``msgid`` as the dedupe key and
# warns a callback "可能因为网络等原因重复回调" — a redelivery runs the whole turn a
# second time, which bills twice and can repeat a side effect. Sized well above
# any plausible in-flight burst; the TTL bounds a quiet connection's memory.
_MSGID_WINDOW_MAX = 512
_MSGID_WINDOW_TTL_SECS = 600.0

# Bound the per-req_id stream bookkeeping the same way, so a long-lived
# connection cannot accumulate one entry per turn forever.
_STREAM_TRACK_MAX = 256

# Event types the router knows about and deliberately drops. Each owes a reply
# inside a 5-second, single-delivery window, so answering one is a feature with
# its own design rather than something to bolt onto the router.
_RECOGNIZED_UNHANDLED_EVENTS = frozenset({"enter_chat", "template_card_event", "feedback_event"})


@dataclass
class WeComInbound:
    """Inbound user message from a WeCom AI Bot callback."""

    userid: str
    text: str
    response_url: str = ""
    chatid: str = ""
    msgtype: str = "text"
    req_id: str = ""
    msgid: str = ""
    chattype: str = CHAT_TYPE_SINGLE
    #: Downloadable media records paired with their per-object aeskey. Consumed
    #: (and cleared) by the dispatcher before the turn runs.
    attachments: list = field(default_factory=list)


class WeComClient:
    """WeCom AI Bot WebSocket client with auto-reconnect.

    Connects to the WeCom AI Bot WebSocket endpoint, subscribes with bot
    credentials, and dispatches inbound callbacks to the on_message handler.
    Replies stream over the WS via send_stream(); send_reply() is the one-shot
    response_url fallback.
    """

    def __init__(
        self,
        *,
        bot_id: str,
        secret: str,
        ws_url: str,
        on_message: Callable[[WeComInbound], Awaitable[None]] | None = None,
        proxy: str | None = None,
    ) -> None:
        self._bot_id = bot_id
        self._secret = secret
        self._ws_url = ws_url
        self._on_message: Callable[[WeComInbound], Awaitable[None]] | None = on_message
        self._proxy = proxy or _resolve_proxy()
        self._ws: aiohttp.ClientWebSocketResponse[Any] | None = None
        # Serializes all WS sends: aiohttp's WebSocketResponse is not safe for
        # concurrent send_json from multiple tasks (per-turn dispatch tasks + the
        # ping loop), which can interleave frames or raise on newer aiohttp.
        self._send_lock: asyncio.Lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._kicked = False
        self._pending_pongs: int = 0
        # req_ids of pings we sent, so their ACKs can be told apart from
        # stream-reply ACKs (both are cmd-less frames carrying errcode).
        self._ping_reqs: set[str] = set()
        # Live turn tasks — kept referenced so they aren't GC'd mid-flight, and
        # awaited by ``close()`` so shutdown is quiescent.
        self._handler_tasks: set[asyncio.Task[None]] = set()
        # msgid -> first-seen monotonic time, insertion-ordered so the oldest
        # entry is the one evicted. Recorded only for frames that passed
        # authorization (see ``already_delivered``).
        self._seen_msgids: OrderedDict[str, float] = OrderedDict()
        # req_id -> the most recent stream_id sent on it. A cmd-less reply ACK
        # names only the req_id, and one req_id legitimately carries several
        # bubbles (the answer plus any out-of-band notice), so the newest send is
        # the only attribution available — and it is the right one, because sends
        # are serialized under ``_send_lock`` and the ACK follows the send.
        self._stream_of_req: OrderedDict[str, str] = OrderedDict()
        # stream_ids the server has refused terminally (see _STREAM_DEAD_ERRCODES).
        self._dead_streams: OrderedDict[str, bool] = OrderedDict()
        # stream_ids whose NEWEST frame drew a non-zero ACK that was not terminal.
        # The bubble is still writable, so the next frame supersedes the refused
        # one -- but if no next frame comes (the bubble rotates for age instead),
        # its last frame was never delivered and the continuation must not resume
        # past it. Recorded so the renderer can tell "sent" from "accepted".
        self._rejected_streams: OrderedDict[str, bool] = OrderedDict()
        # req_id -> future awaiting that command's ACK. Only proactive pushes wait
        # on one (see _PUSH_REQ_PREFIX).
        self._pending_acks: dict[str, asyncio.Future[int]] = {}
        # Optional live connection-status callback (healthy: bool, reason: str),
        # set by the gateway to keep the settings badge truthful. Fired from the
        # reconnect loop on state TRANSITIONS only (deduped via _last_status):
        # True once a connection is up + subscribed, False with a reason when a
        # connect attempt fails, the server closes immediately (bad creds /
        # anti-kick), or the server kicks the bot.
        self.on_status: Callable[[bool, str], None] | None = None
        self._last_status: bool | None = None

    @property
    def proxy(self) -> str | None:
        """The resolved outbound proxy, if any.

        Exposed because a caller that opens its OWN session (the attachment
        ingest) has to be told: aiohttp does not read ``HTTPS_PROXY`` on its own,
        so an unproxied session silently fails on a proxy-only host while this
        client's own WS and HTTP legs work.
        """
        return self._proxy

    def _notify_status(self, healthy: bool, reason: str) -> None:
        """Report a connection-state transition to on_status (deduped, safe)."""
        if healthy == self._last_status:
            return
        self._last_status = healthy
        cb = self.on_status
        if cb is None:
            return
        try:
            cb(healthy, reason)
        except Exception:  # never let a status observer break the WS loop
            logger.exception("WeCom on_status callback failed")

    async def start(self) -> None:
        """Launch the background connect/serve loop as a task."""
        self._closed = False
        self._kicked = False
        self._task = asyncio.create_task(self._run_loop())

    async def close(self) -> None:
        """Gracefully shut down the client, quiescently.

        Inbound frames are fast-acked into background turn tasks
        (``_dispatch_callback``), and those tasks use ``_session`` for the
        ``response_url`` fallback. So they are cancelled and AWAITED before the
        session they borrow is closed — otherwise teardown returns while a turn is
        still unwinding against an already-closed session, which surfaces as
        spurious ``ClientError`` noise at every shutdown and, on a slow unwind, as
        a reply written after the gateway believed the channel was gone. This
        ordering is the ``messaging`` module's "transport shutdown is quiescent"
        invariant; ``TeamsClient.close`` owns the same one.
        """
        self._closed = True
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        try:
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                finally:
                    self._task = None
        finally:
            # Fail any push still waiting on an ACK: the socket is going away, so the
            # answer is "not delivered", and leaving the future pending would hang the
            # caller until its timeout for no reason.
            for waiter in list(self._pending_acks.values()):
                if not waiter.done():
                    waiter.set_result(-1)
            self._pending_acks.clear()
            await self._drain_handler_tasks()
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None

    async def _drain_handler_tasks(self) -> None:
        """Cancel and await the in-flight turn tasks.

        Kept as its own step rather than inlined in ``close()`` so it composes
        with the separate work hardening that method against a leaked session:
        the ordering requirement is only "before the session those tasks borrow
        is closed".
        """
        # Snapshot first: a task's done-callback mutates the set as it retires.
        handler_tasks = list(self._handler_tasks)
        for task in handler_tasks:
            task.cancel()
        if handler_tasks:
            await asyncio.gather(*handler_tasks, return_exceptions=True)

    async def _ws_send(self, ws: aiohttp.ClientWebSocketResponse[Any], frame: dict) -> None:
        """Serialize a single WS send under ``_send_lock`` (concurrent-safe)."""
        async with self._send_lock:
            await ws.send_json(frame)

    # -- inbound dedupe ----------------------------------------------------

    def already_delivered(self, msgid: str) -> bool:
        """Record *msgid* and report whether it had already been delivered.

        WeCom names ``msgid`` as the dedupe key and states a callback may be
        redelivered (network retry, or a reconnect replaying an unacked frame).
        Each redelivery would otherwise run the whole turn again — a second
        provider round-trip, a second set of tool side effects, and two answers
        in the bubble.

        MUST be called only AFTER authorization. The window is a fixed-size
        cache, so recording unauthorized traffic would let an unauthorized sender
        evict genuine entries and re-open the gap this closes. An empty ``msgid``
        is never recorded and never suppressed: no id means no evidence of a
        duplicate, and dropping such a frame would silently lose a real message.
        """
        if not msgid:
            return False
        now = time.monotonic()
        first_seen = self._seen_msgids.get(msgid)
        if first_seen is not None:
            if now - first_seen < _MSGID_WINDOW_TTL_SECS:
                return True
            # Expired: treat as new, and let the refresh below re-date it.
            del self._seen_msgids[msgid]
        self._seen_msgids[msgid] = now
        while len(self._seen_msgids) > _MSGID_WINDOW_MAX:
            self._seen_msgids.popitem(last=False)
        return False

    # -- stream liveness ---------------------------------------------------

    def forget_msgid(self, msgid: str) -> None:
        """Drop *msgid* from the dedupe window so a redelivery is handled.

        The window means "already delivered", which a turn that raised was not.
        Called by the transport when dispatch fails, so WeCom's retry is not
        suppressed as a duplicate of a message nobody ever answered.
        """
        if msgid:
            self._seen_msgids.pop(msgid, None)

    def stream_is_dead(self, stream_id: str) -> bool:
        """True once the server has terminally refused writes to *stream_id*.

        The renderer consults this to open a FRESH bubble instead of continuing
        to push frames the platform is discarding.
        """
        return bool(stream_id) and stream_id in self._dead_streams

    def stream_had_rejection(self, stream_id: str) -> bool:
        """True if any frame on *stream_id* drew a non-zero ACK, terminally or not.

        Answers a narrower question than :meth:`stream_is_dead`: not "can this
        bubble still be written" but "was everything written to it accepted". A
        non-terminal refusal is normally self-healing, because every frame carries
        the bubble's full accumulated text and the next one supersedes the refused
        one — the exception is a refusal of the LAST frame before the bubble
        rotates for age, where there is no next frame and resuming after it would
        skip text the reader never received.
        """
        return bool(stream_id) and stream_id in self._rejected_streams

    def _mark_stream_rejected(self, stream_id: str) -> None:
        if not stream_id:
            return
        self._rejected_streams[stream_id] = True
        while len(self._rejected_streams) > _STREAM_TRACK_MAX:
            self._rejected_streams.popitem(last=False)

    def _mark_stream_dead(self, stream_id: str) -> None:
        if not stream_id:
            return
        # A terminal refusal is also a refusal: the renderer's aged path asks the
        # narrower question and must get the same answer for both.
        self._mark_stream_rejected(stream_id)
        self._dead_streams[stream_id] = True
        while len(self._dead_streams) > _STREAM_TRACK_MAX:
            self._dead_streams.popitem(last=False)

    def _track_stream(self, req_id: str, stream_id: str) -> None:
        """Remember the newest stream_id sent on *req_id* for ACK attribution.

        Also RETIRES a rejection recorded against this stream. Every frame carries
        the bubble's full accumulated text, so the frame being sent now supersedes
        whatever an earlier one was refused for — leaving the marker set would make
        ``stream_had_rejection`` mean "was ever refused" when what the callers need
        is "the latest verdict is a refusal". For ``_recover_unconfirmed_seal`` that
        difference is a duplicated answer: a transient refusal early in a bubble
        would re-push a sealing frame the platform actually accepted.

        A terminal refusal is NOT retired: ``_dead_streams`` is separate and
        permanent, because that bubble can never be written again no matter what is
        sent to it.
        """
        if stream_id in self._rejected_streams and stream_id not in self._dead_streams:
            self._rejected_streams.pop(stream_id, None)
        self._stream_of_req[req_id] = stream_id
        self._stream_of_req.move_to_end(req_id)
        while len(self._stream_of_req) > _STREAM_TRACK_MAX:
            self._stream_of_req.popitem(last=False)

    async def send_stream(self, req_id: str, stream_id: str, content: str, *, finish: bool) -> bool:
        """Stream one reply frame over the WS (cmd ``aibot_respond_msg``).

        ``content`` is the FULL accumulated text — each frame replaces the
        bubble's content (not a delta). Routing to the user's chat is via
        ``req_id`` (the inbound frame's server-assigned id). ``finish=True``
        locks the bubble.

        Returns True when the frame reached the socket — NOT that WeCom accepted
        it. Acceptance arrives later on a cmd-less ACK frame; a terminal refusal
        there marks the stream dead (see :meth:`stream_is_dead`), which is the
        only way a caller can learn that a bubble stopped taking writes.

        There is deliberately no per-frame ACK wait here. Every frame of a turn
        replays the same inbound ``req_id``, and that is the only correlation key an
        ACK carries, so a waiter for one frame can be resolved by another frame's
        ACK — reporting success for a refusal, which is worse than not asking. Only
        ``send_proactive`` waits, because it mints its OWN unique req_id and the
        correlation is therefore exact. A stream caller instead rotates its bubble
        before the platform expires it.
        """
        ws = self._ws
        if ws is None or ws.closed or not req_id:
            return False
        frame = {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": req_id},
            "body": {
                "msgtype": "stream",
                "stream": {
                    "id": stream_id,
                    "finish": finish,
                    # Byte-exact guard at the wire. The renderer splits a long
                    # answer against WECOM_SAFE_REPLY_CHARS first, so nothing
                    # reaches here with a tail worth losing.
                    "content": truncate_utf8(content or "…", WECOM_MAX_REPLY_BYTES),
                },
            },
        }
        try:
            # Track before awaiting the send: the ACK can only arrive after the
            # frame is written, and sends are serialized, so recording first
            # guarantees the attribution table is populated when it lands.
            self._track_stream(req_id, stream_id)
            await self._ws_send(ws, frame)
            return True
        except (ConnectionError, RuntimeError, aiohttp.ClientError) as exc:
            logger.warning("WeCom stream send failed: %s", exc)
            return False

    async def send_proactive(self, chat_id: str, content: str) -> bool:
        """Push a message into a conversation with no inbound request to answer.

        ``aibot_send_msg`` is the long connection's unsolicited-push command. It
        needs no token and has no expiry, but WeCom will only deliver into a
        conversation the user has already messaged the bot in at least once — so a
        caller must treat an unwarmed peer as unavailable rather than as an error
        (see ``WeComTransport.configured_targets``).

        ``chat_id`` is the ``userid``, and ``chat_type`` is sent explicitly as 1
        (single) because the documented default when it is ABSENT prefers group,
        which would mis-route a DM. There is no group parameter: group traffic is
        refused inbound, so a group push has no conversation it could legitimately
        answer. It arrives with group support, together with the sessions and
        allow-list that make a group safe to write to.

        This is what makes the dashboard mirror, cron delivery and threshold
        notices reachable on WeCom at all; before it, every reply had to be an
        answer to a live ``req_id``.
        """
        ws = self._ws
        if ws is None or ws.closed or not chat_id:
            return False
        req_id = _PUSH_REQ_PREFIX + _req_id()
        frame = {
            "cmd": "aibot_send_msg",
            "headers": {"req_id": req_id},
            "body": {
                "chatid": chat_id,
                # Never omitted: absent is documented to prefer GROUP, which would
                # deliver a DM to the wrong place.
                "chat_type": 1,
                "msgtype": "markdown",
                "markdown": {"content": truncate_utf8(content or "…", WECOM_MAX_REPLY_BYTES)},
            },
        }
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[int] = loop.create_future()
        self._pending_acks[req_id] = waiter
        try:
            await self._ws_send(ws, frame)
        except (ConnectionError, RuntimeError, aiohttp.ClientError) as exc:
            logger.warning("WeCom proactive send failed: %s", exc)
            self._pending_acks.pop(req_id, None)
            return False
        try:
            errcode = await asyncio.wait_for(waiter, timeout=_PUSH_ACK_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            # Unacknowledged is NOT delivered. Reporting success here is what
            # would let the mirror record a message the platform dropped.
            logger.warning("WeCom proactive send was not acknowledged in time")
            return False
        finally:
            self._pending_acks.pop(req_id, None)
        if errcode:
            # The code is externally-derived and is not logged, matching the reply
            # ACK branch; a rate-limit rejection lands here too.
            logger.warning("WeCom proactive send was refused by the platform")
            return False
        return True

    async def say(self, inbound: "WeComInbound", content: str) -> bool:
        """Deliver a one-shot out-of-band message for *inbound*'s conversation.

        Every command ack, notice and refusal goes through here. It opens a FRESH
        ``stream_id`` on the inbound ``req_id`` and seals it immediately, which is
        the only delivery path the long connection is documented to support —
        ``response_url`` is absent from an ``aibot_msg_callback`` body, so an ack
        sent only over HTTP can silently reach nobody.

        Kept out of the answer's own bubble on purpose: a notice folded into the
        reply would be persisted as part of the assistant's turn and replayed as
        though the model had said it.

        Delivered as a PROACTIVE PUSH first, because that is the only path whose
        acceptance can be confirmed: a push mints its own ``req_id``, so its ACK is
        exactly correlatable, where a stream frame replays the inbound one and its
        refusal arrives with nothing to attribute it to and no later frame to notice
        it on. An ack that silently vanishes is worse here than anywhere else — the
        user pressed a command and got nothing back, which reads as the command not
        existing. The conversation is warm by construction: this only runs on a turn
        the user just sent.

        Falls back to a sealed stream frame, then to ``response_url``, so a
        deployment where the push is unavailable still gets its ack.
        """
        if inbound.userid and await self.send_proactive(inbound.userid, content):
            return True
        if inbound.req_id:
            if await self.send_stream(inbound.req_id, new_stream_id(), content, finish=True):
                return True
        if inbound.response_url:
            return await self.send_reply(inbound.response_url, content)
        logger.warning("WeCom: no delivery path available, cannot deliver an ack")
        return False

    async def send_reply(self, response_url: str, content: str) -> bool:
        """Post a one-shot reply to an inbound message's response_url.

        Used for short command acks and as the fallback when the WS stream
        channel is unavailable/expired. The response_code embedded in the URL
        is the only credential required (single-use, 1h TTL).

        Returns whether the platform ACCEPTED it. The status and errcode were
        always inspected here and then thrown away, which left every caller
        guessing: the renderer had to treat "a response_url exists" as proof of
        delivery, so a failed POST looked identical to a delivered one and a
        headless answer tail went out behind it. Reporting the verdict makes that
        a decision instead of an assumption.
        """
        if not response_url:
            logger.warning("WeCom: no response_url on inbound, cannot reply")
            return False
        text = truncate_utf8(content or "…", WECOM_MAX_REPLY_BYTES)
        payload = {"msgtype": "markdown", "markdown": {"content": text}}
        # Reuse the live WS ClientSession when available; only open (and close)
        # a throwaway session when called with no live connection (e.g. a
        # response_url fallback before/after the WS is up).
        session = self._session
        owns_session = session is None or session.closed
        if owns_session:
            session = aiohttp.ClientSession()
        assert session is not None
        try:
            async with session.post(
                response_url,
                json=payload,
                proxy=self._proxy,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200 or data.get("errcode") not in (0, None):
                    # The errcode/errmsg are externally-derived frame content and
                    # must not be logged: errmsg can echo the rejected payload, which
                    # is the answer text itself. Log the http status and the
                    # CLASSIFICATION, which is what an operator can act on.
                    logger.warning("WeCom reply failed: http=%s, platform refused", resp.status)
                    return False
                logger.info("WeCom reply delivered (%d chars)", len(text))
                return True
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("WeCom reply POST error: %s", type(exc).__name__)
            return False
        finally:
            if owns_session and session is not None:
                await session.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Reconnect loop with exponential backoff."""
        attempt = 0
        while not self._closed and not self._kicked:
            started = time.monotonic()
            reason: object | None = None
            try:
                await self._connect_and_serve()
            except asyncio.CancelledError:
                break
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
            ) as exc:
                reason = exc
            except Exception as exc:  # noqa: BLE001 - see below
                # Anything unexpected must still be a RECONNECT, not the end of
                # the channel. Letting it escape kills this task while
                # ``_closed`` stays False, so the badge keeps whatever state it
                # last had — typically green — and WeCom is silently dead until
                # the gateway restarts. Cancellation is re-raised above so a real
                # shutdown is never mistaken for a fault.
                logger.exception("WeCom WS: unexpected error in connect/serve")
                reason = f"{type(exc).__name__}"

            if self._closed or self._kicked:
                break

            if reason is None:
                # Clean return: the server closed the WS without raising. Only reset
                # the backoff if the connection actually lived a while -- a
                # connect->immediate-close (bad creds / anti-kick) must still back
                # off, otherwise it hot-loops with zero delay.
                if time.monotonic() - started >= _MIN_HEALTHY_CONN_SECS:
                    attempt = 0
                    continue
                reason = "server closed connection immediately (check bot ID / secret)"

            attempt += 1
            delay = min(1.0 * (2 ** (attempt - 1)), 30.0)
            # Keep the settings badge truthful: surface the failure reason
            # (deduped — only the transition and the first reason report).
            self._notify_status(False, f"{reason}"[:120])
            logger.warning(
                "WeCom WS disconnected (%s), reconnect in %.1fs",
                reason,
                delay,
            )
            await asyncio.sleep(delay)
        if self._kicked:
            self._notify_status(
                False, "server kicked the bot (disconnected_event); reconnect stopped"
            )

    async def _connect_and_serve(self) -> None:
        """Single connection lifecycle: connect, subscribe, serve."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        async with self._session.ws_connect(self._ws_url, proxy=self._proxy) as ws:
            self._ws = ws
            self._pending_pongs = 0
            self._ping_reqs.clear()

            subscribe_frame = _build_subscribe_frame(self._bot_id, self._secret)
            await self._ws_send(ws, subscribe_frame)
            logger.info("WeCom WS connected and subscribed")
            # Healthy transition: the WS is up and the subscribe frame was
            # accepted by the socket. If the server rejects the credentials it
            # closes this connection immediately and the run loop reports
            # not-healthy with a reason (deduped transition back).
            self._notify_status(True, "")

            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            await self._handle_message(msg.data)
                        except Exception:
                            logger.exception("WeCom WS: frame handler error; dropping frame")
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
                self._ws = None

    async def _ping_loop(self, ws: aiohttp.ClientWebSocketResponse[Any]) -> None:
        """Send ping every 30s, detect dead connection after 3 missed pongs."""
        while not ws.closed:
            await asyncio.sleep(30)
            if self._pending_pongs >= 3:
                logger.error("WeCom WS: 3 missed pongs, closing connection")
                await ws.close()
                return
            try:
                pid = _req_id()
                # Bound the tracking set defensively (pongs normally clear it).
                if len(self._ping_reqs) > 100:
                    self._ping_reqs.clear()
                self._ping_reqs.add(pid)
                await self._ws_send(ws, {"cmd": "ping", "headers": {"req_id": pid}, "body": {}})
                self._pending_pongs += 1
            except (ConnectionError, asyncio.CancelledError):
                return

    async def _handle_message(self, raw: str) -> None:
        """Parse and dispatch an inbound WS text frame."""
        logger.debug("WeCom WS inbound frame (%d bytes)", len(raw))
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("WeCom WS: unparseable frame (%d bytes)", len(raw))
            return

        if not isinstance(data, dict):
            # Log only safe metadata (the Python type), never the parsed payload:
            # a non-dict frame may be a bare scalar carrying secret-like content.
            logger.warning("WeCom WS: dropping non-object frame (type=%s)", type(data).__name__)
            return

        cmd = data.get("cmd", "")

        # Cmd-less frame carrying errcode: either a pong ACK or a stream-reply ACK.
        if "errcode" in data and not cmd:
            headers = data.get("headers", {})
            if not isinstance(headers, dict):
                logger.warning("WeCom WS: malformed ACK headers")
                return
            rid = headers.get("req_id", "")
            if rid in self._ping_reqs:
                self._ping_reqs.discard(rid)
                self._pending_pongs = max(0, self._pending_pongs - 1)
                return
            ec = data.get("errcode")
            waiter = self._pending_acks.get(rid) if isinstance(rid, str) else None
            if waiter is not None:
                # Still mark the bubble: a caller recovering by rolling needs
                # ``stream_is_dead`` to be true, and resolving the waiter alone
                # would leave the sealed bubble looking writable.
                if isinstance(ec, int) and ec in _STREAM_DEAD_ERRCODES:
                    self._mark_stream_dead(self._stream_of_req.get(rid, ""))
                if not waiter.done():
                    # A non-integer errcode is UNKNOWN, not zero. Mapping it to 0
                    # would report a refused mirror send as delivered -- the
                    # fail-open direction, on the one call whose return value the
                    # caller treats as proof of delivery.
                    waiter.set_result(ec if isinstance(ec, int) else -1)
                return
            if isinstance(rid, str) and rid.startswith(_SUBSCRIBE_REQ_PREFIX):
                self._handle_subscribe_ack(ec)
                return
            if ec == 0:
                # An explicit acceptance supersedes an earlier refusal on the same
                # stream, for the reason spelled out in ``_track_stream``: the
                # callers need the LATEST verdict, not "was ever refused". Kept
                # separate from the send-side retirement because either signal may
                # be the one that arrives — WeCom is not documented to acknowledge
                # an accepted frame, so this cannot be relied on alone.
                acked = self._stream_of_req.get(rid, "")
                if acked and acked not in self._dead_streams:
                    self._rejected_streams.pop(acked, None)
            if ec not in (0, None):
                # errcode/errmsg are externally-derived frame content and must
                # not be logged (clear-text-logging) — errmsg can echo the
                # rejected payload. Log the CLASSIFICATION instead, which is what
                # an operator can act on: whether the bubble is recoverable.
                # Codes seen in practice: 846605 invalid req_id, 846608 stream
                # expired.
                if isinstance(ec, int) and ec in _STREAM_DEAD_ERRCODES:
                    logger.warning(
                        "WeCom stream ACK error: bubble sealed or unroutable, "
                        "the answer continues in a new one"
                    )
                    self._mark_stream_dead(self._stream_of_req.get(rid, ""))
                else:
                    # Non-terminal: the bubble is still writable, so there is
                    # nothing to recover -- the next frame carries the full
                    # accumulated text anyway and supersedes this one. Recorded so
                    # a burst of these is visible (a rate-limit rejection lands
                    # here). The bubble stays writable, so nothing is torn down --
                    # but the frame was NOT accepted, and the renderer needs that
                    # distinction: if this turns out to be the bubble's last frame
                    # (it rotates for age instead of getting a successor), the
                    # continuation has to replay it rather than resume after it.
                    logger.warning("WeCom stream ACK error (non-zero errcode)")
                    self._mark_stream_rejected(self._stream_of_req.get(rid, ""))
            return

        if cmd in ("aibot_msg_callback", "aibot_callback"):
            self._dispatch_callback(data)
        elif cmd == "aibot_event_callback":
            await self._handle_event(data)
        elif cmd == "disconnected_event":
            # Retained: some deployments deliver the kick as a bare cmd rather
            # than wrapped in an event envelope. Both spellings must stop the
            # reconnect loop, because reconnecting after a kick fights the
            # connection that replaced us (WeCom allows one per bot).
            await self._handle_kick()
        else:
            # cmd is externally-derived; log a generic event, never its value.
            logger.debug("WeCom WS: unhandled cmd")

    def _handle_subscribe_ack(self, errcode: Any) -> None:
        """Believe the subscribe ACK instead of inferring auth from a close.

        A rejected ``bot_id``/``secret`` is reported here, in band, with its
        code. Previously the only signal was the connection closing straight
        away, which the run loop reports as the generic "server closed
        connection immediately" — indistinguishable from an anti-kick, so an
        operator with a bad secret was told to check something else. The badge is
        the documented compensating control for not verifying credentials at save
        time, so it has to carry the real reason.

        A non-zero code does NOT stop reconnecting: a secret can be corrected in
        the dashboard while the gateway runs, and the backoff already prevents a
        hot loop.
        """
        if errcode in (0, None):
            return
        # The code and message are externally-derived frame content, so neither
        # is logged and neither is put on the badge (the same posture the reply
        # ACK keeps). The operator's action does not depend on the number: a
        # rejected subscribe means the bot id or secret is wrong.
        logger.error("WeCom WS: subscribe rejected by the server")
        self._notify_status(False, "bot credentials rejected by WeCom (check bot ID / secret)")

    async def _handle_kick(self) -> None:
        """Stop reconnecting: another connection has replaced this one."""
        logger.error("WeCom WS: disconnected_event (anti-kick), stopping reconnect")
        self._kicked = True
        if self._ws and not self._ws.closed:
            await self._ws.close()

    async def _handle_event(self, data: dict) -> None:
        """Route ``aibot_event_callback`` by ``body.event.eventtype``.

        The kick is delivered here, as an event, in the documented long-connection
        protocol — matching it only as a top-level ``cmd`` meant the anti-kick
        branch could never fire, so a replaced connection kept reconnecting and
        the two instances took turns evicting each other.

        The other event types (``enter_chat``, ``template_card_event``,
        ``feedback_event``) are recognized and dropped deliberately: each needs a
        reply within a 5-second, single-delivery window, so answering one is a
        feature with its own design, not something to bolt onto this router.
        """
        body = data.get("body", {})
        event = body.get("event", {}) if isinstance(body, dict) else {}
        eventtype = event.get("eventtype", "") if isinstance(event, dict) else ""
        if eventtype == "disconnected_event":
            await self._handle_kick()
            return
        # eventtype comes off the wire, so its VALUE is never interpolated into a
        # log line -- the same rule the cmd and errcode branches keep. Which of
        # the two messages is emitted still says whether the type was recognized,
        # which is the part worth knowing.
        if eventtype in _RECOGNIZED_UNHANDLED_EVENTS:
            logger.debug("WeCom WS: recognized event type, not handled yet")
        else:
            logger.debug("WeCom WS: unhandled event")

    def _dispatch_callback(self, data: dict) -> None:
        """Parse an aibot_msg_callback and run on_message as a background task.

        The turn runs in its own task so the receive loop keeps reading ACK /
        pong / new-message frames while a (streaming, possibly 10-30s) turn is
        in flight — otherwise long turns would starve the ping loop and trip a
        false disconnect.
        """
        headers = data.get("headers", {})
        body = data.get("body", {})
        if not isinstance(headers, dict) or not isinstance(body, dict):
            logger.warning("WeCom WS: malformed callback object fields")
            return

        from_obj = body.get("from", {})
        text_obj = body.get("text", {})
        if not isinstance(from_obj, dict) or not isinstance(text_obj, dict):
            logger.warning("WeCom WS: malformed callback object fields")
            return

        req_id = headers.get("req_id", "")
        userid = from_obj.get("userid", "")
        text_content = text_obj.get("content", "")
        # A captioned screenshot puts its caption in the mixed item list, not in
        # ``text``; reading only ``text`` loses it. A voice note carries WeCom's
        # OWN transcript, which is the useful payload -- there is no audio asset
        # to fetch, and no shipped backend decodes WeCom's codec anyway.
        if not text_content:
            text_content = mixed_text(body) or _voice_transcript(body)
        response_url = body.get("response_url", "")
        chatid = body.get("chatid", "")
        msgtype = body.get("msgtype", "text")
        msgid = body.get("msgid", "")
        # ABSENT ``chattype`` means a 1:1 chat -- WeCom sends it (with ``chatid``)
        # for group traffic, and defaulting the other way would fail closed on
        # every ordinary DM.
        #
        # A chattype that is PRESENT but not a string is a different case, and it
        # must NOT collapse to "single": that is the fail-open shape, where a
        # malformed group callback reads as direct, passes the direct-only gate,
        # and publishes a private session's output into the room. Anything
        # unrecognizable becomes a sentinel the gate rejects.
        chattype = _normalize_chattype(body)

        inbound = WeComInbound(
            userid=userid,
            text=text_content,
            response_url=response_url,
            chatid=chatid,
            msgtype=msgtype,
            req_id=req_id,
            msgid=msgid if isinstance(msgid, str) else "",
            attachments=to_attachments(body),
            chattype=chattype,
        )

        task = asyncio.create_task(self._invoke_handler(inbound))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    def set_message_handler(self, on_message: Callable[[WeComInbound], Awaitable[None]]) -> None:
        """Set the inbound handler post-construction.

        Avoids the client<->transport construction cycle: the transport needs
        the client to send, and the client needs ``transport.receive`` to
        dispatch. Build the client first (handler unset), then wire it here.
        """
        self._on_message = on_message

    async def _invoke_handler(self, inbound: WeComInbound) -> None:
        if self._on_message is None:
            return
        try:
            await self._on_message(inbound)
        except Exception:
            logger.exception("WeCom on_message handler raised for userid=%s", inbound.userid)


# ------------------------------------------------------------------
# Frame builders (pure functions, no side effects)
# ------------------------------------------------------------------


def _build_subscribe_frame(bot_id: str, secret: str) -> dict:
    # The prefix is load-bearing, not cosmetic: the subscribe ACK carries no cmd,
    # so its req_id is the only thing that distinguishes it from a pong or a
    # reply receipt. See _SUBSCRIBE_REQ_PREFIX.
    return {
        "cmd": "aibot_subscribe",
        "headers": {"req_id": _SUBSCRIBE_REQ_PREFIX + _req_id()},
        "body": {"bot_id": bot_id, "secret": secret},
    }


def _normalize_chattype(body: dict) -> str:
    """Resolve ``chattype`` without ever failing open to a direct chat.

    Absent -> ``single`` (WeCom omits it for 1:1). Present and a non-empty string
    -> that string, whatever it is, so an unknown-but-real value still reads as
    "not a DM". Present and anything else -> ``CHAT_TYPE_UNKNOWN``, which every
    gate rejects.
    """
    if "chattype" not in body:
        return CHAT_TYPE_SINGLE
    raw = body.get("chattype")
    if isinstance(raw, str) and raw:
        return raw
    return CHAT_TYPE_UNKNOWN


def _voice_transcript(body: dict) -> str:
    """WeCom's server-side transcript of a voice note, if this is one.

    The platform transcribes voice itself and hands the text back, so the local
    path is strictly worse than the transcript it already gave us: nothing shipped
    here decodes WeCom's voice codec, and downloading it would produce an asset no
    backend can read.
    """
    if body.get("msgtype") != "voice":
        return ""
    voice = body.get("voice")
    if not isinstance(voice, dict):
        return ""
    content = voice.get("content", "")
    return content if isinstance(content, str) else ""


def _req_id() -> str:
    return uuid.uuid4().hex[:16]


def new_stream_id() -> str:
    """A self-generated stream bubble id (correlates chunks to one bubble)."""
    return "stream_" + uuid.uuid4().hex


def _resolve_proxy() -> str | None:
    """Resolve an outbound proxy from the environment, if set."""
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        val = os.environ.get(var)
        if val:
            return val
    return None
