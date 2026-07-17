"""WeCom AI Bot WebSocket client transport layer.

Inbound: an outbound WebSocket long-connection to WeCom receives user messages
(cmd ``aibot_msg_callback``), each carrying a server-assigned ``headers.req_id``
and a single-use ``response_url``.

Outbound: the bot streams its reply over the SAME WebSocket via
``aibot_respond_msg`` frames that replay the inbound ``req_id`` (the sole routing
key; ``body.stream.content`` is the full accumulated text, replacing the bubble
each frame). ``response_url`` remains a one-shot HTTP fallback for short acks and
for when the stream channel is unavailable/expired.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp

logger = logging.getLogger(__name__)

# WeCom markdown reply content cap (bytes-ish; keep well under platform limits).
_REPLY_MAX_CHARS = 20000

# A WS connection must live at least this long to count as "healthy" and reset the
# reconnect backoff. A connect->immediate-close (bad creds, or a WeCom anti-kick
# that closes the socket without raising) stays on the backoff curve so it cannot
# hot-loop with zero delay.
_MIN_HEALTHY_CONN_SECS = 5.0

# Inbound frames carry a single-use response_url whose response_code is a 1h
# credential. Mask it before any (debug) logging so logs pasted into bug reports
# cannot leak it.
_RESP_URL_RE = re.compile(r'("response_url"\s*:\s*")[^"]*(")')


def _redact_frame(raw: str) -> str:
    """Mask the response_url value in a raw WS frame string for safe logging."""
    return _RESP_URL_RE.sub(r"\1<redacted>\2", raw)


@dataclass
class WeComInbound:
    """Inbound user message from a WeCom AI Bot callback."""

    userid: str
    text: str
    response_url: str = ""
    chatid: str = ""
    msgtype: str = "text"
    req_id: str = ""


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
        # Live turn tasks — kept referenced so they aren't GC'd mid-flight.
        self._handler_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """Launch the background connect/serve loop as a task."""
        self._closed = False
        self._kicked = False
        self._task = asyncio.create_task(self._run_loop())

    async def close(self) -> None:
        """Gracefully shut down the client."""
        self._closed = True
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _ws_send(self, ws: aiohttp.ClientWebSocketResponse[Any], frame: dict) -> None:
        """Serialize a single WS send under ``_send_lock`` (concurrent-safe)."""
        async with self._send_lock:
            await ws.send_json(frame)

    async def send_stream(self, req_id: str, stream_id: str, content: str, *, finish: bool) -> bool:
        """Stream one reply frame over the WS (cmd ``aibot_respond_msg``).

        ``content`` is the FULL accumulated text — each frame replaces the
        bubble's content (not a delta). Routing to the user's chat is via
        ``req_id`` (the inbound frame's server-assigned id). ``finish=True``
        locks the bubble. Returns True if the frame was sent on a live WS.
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
                    "content": (content or "…")[:_REPLY_MAX_CHARS],
                },
            },
        }
        try:
            await self._ws_send(ws, frame)
            return True
        except (ConnectionError, RuntimeError, aiohttp.ClientError) as exc:
            logger.warning("WeCom stream send failed: %s", exc)
            return False

    async def send_reply(self, response_url: str, content: str) -> None:
        """Post a one-shot reply to an inbound message's response_url.

        Used for short command acks and as the fallback when the WS stream
        channel is unavailable/expired. The response_code embedded in the URL
        is the only credential required (single-use, 1h TTL).
        """
        if not response_url:
            logger.warning("WeCom: no response_url on inbound, cannot reply")
            return
        text = (content or "…")[:_REPLY_MAX_CHARS]
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
                    logger.warning(
                        "WeCom reply failed: http=%s errcode=%s errmsg=%s",
                        resp.status,
                        data.get("errcode"),
                        data.get("errmsg"),
                    )
                else:
                    logger.info("WeCom reply delivered (%d chars)", len(text))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("WeCom reply POST error: %s", exc)
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
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
            ) as exc:
                reason = exc
            except asyncio.CancelledError:
                break

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
                reason = "server closed connection immediately"

            attempt += 1
            delay = min(1.0 * (2 ** (attempt - 1)), 30.0)
            logger.warning(
                "WeCom WS disconnected (%s), reconnect in %.1fs",
                reason,
                delay,
            )
            await asyncio.sleep(delay)

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

            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_message(msg.data)
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
        logger.debug("WeCom WS inbound frame: %s", _redact_frame(raw)[:500])
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("WeCom WS: unparseable frame: %s", _redact_frame(raw)[:200])
            return

        cmd = data.get("cmd", "")

        # Cmd-less frame carrying errcode: either a pong ACK or a stream-reply ACK.
        if "errcode" in data and not cmd:
            rid = data.get("headers", {}).get("req_id", "")
            if rid in self._ping_reqs:
                self._ping_reqs.discard(rid)
                self._pending_pongs = max(0, self._pending_pongs - 1)
            else:
                ec = data.get("errcode")
                if ec not in (0, None):
                    # 846605 invalid req_id / 846608 stream expired, etc.
                    logger.warning(
                        "WeCom stream ACK error: errcode=%s errmsg=%s",
                        ec,
                        data.get("errmsg"),
                    )
            return

        if cmd in ("aibot_msg_callback", "aibot_callback"):
            self._dispatch_callback(data)
        elif cmd == "disconnected_event":
            logger.error("WeCom WS: disconnected_event (anti-kick), stopping reconnect")
            self._kicked = True
            if self._ws and not self._ws.closed:
                await self._ws.close()
        else:
            logger.debug("WeCom WS: unhandled cmd=%s", cmd)

    def _dispatch_callback(self, data: dict) -> None:
        """Parse an aibot_msg_callback and run on_message as a background task.

        The turn runs in its own task so the receive loop keeps reading ACK /
        pong / new-message frames while a (streaming, possibly 10-30s) turn is
        in flight — otherwise long turns would starve the ping loop and trip a
        false disconnect.
        """
        headers = data.get("headers", {})
        req_id = headers.get("req_id", "")
        body = data.get("body", {})
        from_obj = body.get("from", {})
        userid = from_obj.get("userid", "")
        text_obj = body.get("text", {})
        text_content = text_obj.get("content", "") if text_obj else ""
        response_url = body.get("response_url", "")
        chatid = body.get("chatid", "")
        msgtype = body.get("msgtype", "text")

        inbound = WeComInbound(
            userid=userid,
            text=text_content,
            response_url=response_url,
            chatid=chatid,
            msgtype=msgtype,
            req_id=req_id,
        )

        task = asyncio.create_task(self._invoke_handler(inbound))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    def set_message_handler(
        self, on_message: Callable[[WeComInbound], Awaitable[None]]
    ) -> None:
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
    return {
        "cmd": "aibot_subscribe",
        "headers": {"req_id": _req_id()},
        "body": {"bot_id": bot_id, "secret": secret},
    }


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
