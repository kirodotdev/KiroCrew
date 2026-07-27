"""Webex Messaging client transport layer.

Inbound: Webex has no long-polling API, and webhooks require a public URL.
Instead we register a *device* with the Webex Device Management service
(WDM) to obtain a per-device WebSocket URL, connect, authorize with the
bot token, and receive ``conversation.activity`` events in real time --
the same mechanism the official ``webex-bot`` SDK uses. Activity events
carry raw UUIDs; the public-API message id is the base64 "Hydra" encoding
of ``ciscospark://us/MESSAGE/{uuid}``. The event payload is only a signal:
the actual message (decrypted text, sender, room type) is fetched via the
documented ``GET /v1/messages/{id}`` REST call.

Outbound: plain REST -- ``POST /v1/messages`` to send (roomId or
toPersonEmail), ``PUT /v1/messages/{id}`` to edit (Webex caps a message at
10 edits -- callers must budget), ``DELETE /v1/messages/{id}`` to remove.

No Webex SDK dependency -- pure aiohttp (REST + WebSocket). This keeps the
module lightweight, OSS-clean, and easy to audit.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp

logger = logging.getLogger(__name__)

# Webex REST API base.
_API_BASE = "https://webexapis.com/v1"
# Webex Device Management (device registration -> WebSocket URL).
_DEVICE_BASE = "https://wdm-a.wbx2.com/wdm/api/v1"

# Webex caps a message's text/markdown at 7439 BYTES; stay comfortably under.
# The cap is enforced in UTF-8 bytes (not characters) — see ``truncate_utf8``.
WEBEX_MAX_TEXT = 7000


def truncate_utf8(text: str, max_bytes: int = WEBEX_MAX_TEXT) -> str:
    """Truncate ``text`` to at most *max_bytes* UTF-8 bytes without splitting
    a code point.

    Webex's message limit is 7439 bytes, so a multibyte-heavy reply can be
    under the character cap but over the byte limit — Webex would reject the
    send and the user would get nothing. ``errors="ignore"`` on the decode
    drops a trailing partial sequence cleanly. Last-resort safety net for
    single sends; multi-message content must be split losslessly with
    :func:`chunk_utf8` first.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def chunk_utf8(text: str, max_bytes: int = WEBEX_MAX_TEXT) -> list[str]:
    """Split ``text`` into chunks of at most *max_bytes* UTF-8 bytes each,
    never splitting a code point and never dropping content.

    The neutral ``chunk_text`` helper splits by CHARACTERS, but Webex limits
    BYTES — a multibyte-heavy chunk under the character cap could exceed the
    byte limit and be silently tail-truncated by the send path, losing the
    remainder. Splitting on the encoded bytes and re-decoding with
    ``errors="ignore"`` finds the largest whole-code-point prefix per chunk;
    the loop then resumes from exactly the characters consumed, so the
    concatenation of all chunks always equals the input.
    """
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while remaining:
        encoded = remaining.encode("utf-8")
        if len(encoded) <= max_bytes:
            chunks.append(remaining)
            break
        piece = encoded[:max_bytes].decode("utf-8", errors="ignore")
        chunks.append(piece)
        remaining = remaining[len(piece) :]
    return chunks


# A WS connection must live at least this long to count as "healthy" and reset
# the reconnect backoff. A connect->immediate-close (bad token) stays on the
# backoff curve so it cannot hot-loop with zero delay. Mirrors WeComClient.
_MIN_HEALTHY_CONN_SECS = 5.0

_DEVICE_PAYLOAD = {
    "name": "kirocrew",
    "deviceName": "kirocrew-gateway",
    "deviceType": "DESKTOP",
    "model": "kirocrew",
    "localizedModel": "kirocrew",
    "systemName": "kirocrew",
    "systemVersion": "1.0.0",
}


@dataclass
class WebexInbound:
    """Normalized inbound message from a Webex conversation.activity event."""

    person_email: str
    room_id: str
    text: str
    person_id: str = ""
    room_type: str = ""  # "direct" | "group"
    message_id: str = ""


def hydra_id(raw_id: str, resource_type: str = "MESSAGE") -> str:
    """Base64-encode a raw UUID into the public-API ("Hydra") id format.

    WS activity events carry raw UUIDs; the REST API expects
    ``base64("ciscospark://us/{TYPE}/{uuid}")`` without padding.
    """
    if not raw_id:
        return ""
    prefix = f"ciscospark://us/{resource_type}/{raw_id}"
    return base64.b64encode(prefix.encode()).decode().rstrip("=")


class WebexClient:
    """Webex Messaging client with device-WebSocket inbound and auto-reconnect.

    Registers a device with WDM to obtain a WebSocket URL, connects, and
    dispatches inbound messages to the on_message handler. Outbound sends
    ride the documented REST API.
    """

    def __init__(
        self,
        *,
        token: str,
        on_message: Callable[[WebexInbound], Awaitable[None]] | None = None,
        device_base: str = _DEVICE_BASE,
        api_base: str = _API_BASE,
        proxy: str | None = None,
    ) -> None:
        self._token = token
        self._on_message = on_message
        self._device_base = device_base.rstrip("/")
        self._api_base = api_base.rstrip("/")
        self._proxy = proxy or _resolve_proxy()
        self._session: aiohttp.ClientSession | None = None
        self._session_lock: asyncio.Lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        # Bot identity (fetched once on connect) for self-message filtering.
        self.bot_email: str = ""
        self.bot_person_id: str = ""
        # Set once the WS is connected + authorized (cleared while
        # disconnected/reconnecting). ``wait_ready`` gates "connected" status.
        self.ready: asyncio.Event = asyncio.Event()
        # Short reason from the most recent connection failure; empty when
        # connected. Read by the status callback path.
        self.last_error: str = ""
        # Optional observer called with (connected: bool, error: str) on
        # connect and on disconnect — lets the gateway keep the dashboard
        # status badge truthful after boot. Mirrors DiscordClient.
        self.on_state_change: Callable[[bool, str], None] | None = None
        # Live turn tasks -- prevent GC of in-flight handlers.
        self._handler_tasks: set[asyncio.Task[None]] = set()

    # ── Lifecycle ──

    async def start(self) -> None:
        """Launch the background connect/serve loop."""
        self._closed = False
        self._task = asyncio.create_task(self._run_loop())

    async def close(self) -> None:
        """Gracefully shut down."""
        self._closed = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._handler_tasks:
            for t in list(self._handler_tasks):
                t.cancel()
            await asyncio.gather(*self._handler_tasks, return_exceptions=True)
            self._handler_tasks.clear()
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def set_message_handler(self, on_message: Callable[[WebexInbound], Awaitable[None]]) -> None:
        """Set/replace the inbound-message handler after construction.

        Lets the gateway wire ``transport.receive`` in once the transport
        (which needs the client) has been built, avoiding a construction
        cycle. Mirrors TelegramClient/WeComClient.
        """
        self._on_message = on_message

    async def wait_ready(self, timeout: float = 15.0) -> bool:
        """Wait for the WS to be connected + authorized. Returns False on
        timeout (bad token, unreachable endpoint). Mirrors DiscordClient."""
        try:
            await asyncio.wait_for(self.ready.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _notify_state(self, connected: bool, error: str) -> None:
        if self.on_state_change is not None:
            try:
                self.on_state_change(connected, error)
            except Exception:
                logger.debug("Webex on_state_change observer raised", exc_info=True)

    # ── Outbound REST API ──

    async def send_message(
        self,
        conversation_id: str,
        markdown: str,
        *,
        parent_id: str | None = None,
    ) -> str | None:
        """Send a new message; return its message id on success.

        ``conversation_id`` is a Webex roomId, or an email address (contains
        ``@``) to open/reuse the 1:1 space with that person -- the shape
        ``resolve_conversation`` hands proactive senders.
        """
        payload: dict[str, Any] = {"markdown": truncate_utf8(markdown or "…")}
        if "@" in conversation_id:
            payload["toPersonEmail"] = conversation_id
        else:
            payload["roomId"] = conversation_id
        if parent_id:
            payload["parentId"] = parent_id
        result = await self._api("POST", "/messages", payload)
        return result.get("id") if isinstance(result, dict) else None

    async def edit_message(self, message_id: str, room_id: str, markdown: str) -> bool:
        """Edit an existing message in-place. Returns True on success.

        Webex allows at most 10 edits per message (further edits 400) --
        callers must budget their edits (see WebexRenderer).
        """
        payload = {"roomId": room_id, "markdown": truncate_utf8(markdown or "…")}
        result = await self._api("PUT", f"/messages/{message_id}", payload)
        return result is not None

    async def delete_message(self, message_id: str) -> None:
        """Delete a message (best-effort)."""
        await self._api("DELETE", f"/messages/{message_id}", None)

    async def fetch_message(self, message_id: str) -> dict | None:
        """Fetch a message's full record (decrypted text, sender, room)."""
        result = await self._api("GET", f"/messages/{message_id}", None)
        return result if isinstance(result, dict) else None

    # ── Identity ──

    async def _fetch_me(self) -> None:
        """Resolve the bot's own identity once (self-message filtering)."""
        me = await self._api("GET", "/people/me", None)
        if isinstance(me, dict):
            emails = me.get("emails") or []
            self.bot_email = (emails[0] if emails else "").lower()
            self.bot_person_id = me.get("id", "")

    # ── WebSocket serve loop ──

    async def _run_loop(self) -> None:
        """Reconnect loop with exponential backoff (mirrors WeComClient)."""
        attempt = 0
        while not self._closed:
            started = time.monotonic()
            reason: object | None = None
            try:
                await self._connect_and_serve()
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                reason = type(exc).__name__
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Webex WS unexpected error")
                reason = "unexpected error"

            self.ready.clear()
            if self._closed:
                break
            if reason is None:
                # Clean server close: only reset backoff if the connection
                # actually lived a while, else it hot-loops on a bad token.
                if time.monotonic() - started >= _MIN_HEALTHY_CONN_SECS:
                    attempt = 0
                    continue
                reason = "server closed connection immediately"

            attempt += 1
            delay = min(1.0 * (2 ** (attempt - 1)), 60.0)
            self.last_error = str(reason)[:120]
            self._notify_state(False, self.last_error)
            logger.warning("Webex WS disconnected (%s), reconnect in %.1fs", reason, delay)
            await asyncio.sleep(delay)

    async def _connect_and_serve(self) -> None:
        """Single connection lifecycle: register device, connect, authorize, serve."""
        session = await self._ensure_session()
        if not self.bot_email:
            await self._fetch_me()

        ws_url = await self._get_websocket_url()
        if not ws_url:
            raise aiohttp.ClientError("device registration returned no webSocketUrl")

        # heartbeat= keeps protocol-level ping/pong flowing so a dead
        # connection surfaces as an error instead of hanging silently.
        async with session.ws_connect(ws_url, proxy=self._proxy, heartbeat=20) as ws:
            auth_frame = {
                "id": str(uuid.uuid4()),
                "type": "authorization",
                "data": {"token": f"Bearer {self._token}"},
            }
            await ws.send_json(auth_frame)
            logger.info("Webex WS connected and authorized")
            self.ready.set()
            self.last_error = ""
            self._notify_state(True, "")
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        self._handle_frame(json.loads(msg.data))
                    except json.JSONDecodeError:
                        logger.warning("Webex WS: unparseable frame (%d bytes)", len(msg.data))
                    except Exception:
                        logger.exception("Webex WS: frame handler error; dropping frame")
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break

    async def _get_websocket_url(self) -> str:
        """Register (or reuse) a WDM device and return its WebSocket URL."""
        session = await self._ensure_session()
        try:
            async with session.post(
                f"{self._device_base}/devices",
                json=_DEVICE_PAYLOAD,
                headers=self._headers(),
                proxy=self._proxy,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status in (200, 201):
                    data = await resp.json(content_type=None)
                    return data.get("webSocketUrl", "")
            # Device cap reached / already exists: reuse the first device.
            async with session.get(
                f"{self._device_base}/devices",
                headers=self._headers(),
                proxy=self._proxy,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
                devices = data.get("devices") or []
                return devices[0].get("webSocketUrl", "") if devices else ""
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # Log only the exception type -- never the URL/response, which
            # could carry token-adjacent material.
            logger.warning("Webex device registration error: %s", type(exc).__name__)
            return ""

    def _handle_frame(self, data: Any) -> None:
        """Filter a WS frame down to new-message activities and dispatch.

        The activity event is treated purely as a *signal* (ids only); the
        message content is fetched via the documented REST API in the
        background task, so the receive loop keeps breathing during long
        turns.
        """
        if not isinstance(data, dict):
            return
        payload = data.get("data")
        if not isinstance(payload, dict):
            return
        if payload.get("eventType") != "conversation.activity":
            return
        activity = payload.get("activity")
        if not isinstance(activity, dict) or activity.get("verb") != "post":
            return
        actor = activity.get("actor") or {}
        actor_email = str(actor.get("emailAddress", "")).lower()
        # Ignore the bot's own messages (echo of our sends).
        if actor_email and actor_email == self.bot_email:
            return
        raw_msg_id = (activity.get("object") or {}).get("id") or activity.get("id")
        message_id = hydra_id(str(raw_msg_id or ""), "MESSAGE")
        if not message_id:
            return
        task = asyncio.create_task(self._hydrate_and_dispatch(message_id))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def _hydrate_and_dispatch(self, message_id: str) -> None:
        """Fetch the full message via REST, normalize, and invoke the handler."""
        try:
            msg = await self.fetch_message(message_id)
            if not msg:
                return
            person_id = msg.get("personId", "")
            # Belt-and-braces self filter: the WS actor email can be absent.
            if self.bot_person_id and person_id == self.bot_person_id:
                return
            inbound = WebexInbound(
                person_email=str(msg.get("personEmail", "")).lower(),
                room_id=msg.get("roomId", ""),
                text=msg.get("text", "") or "",
                person_id=person_id,
                room_type=msg.get("roomType", ""),
                message_id=message_id,
            )
            if self._on_message is not None:
                await self._on_message(inbound)
        except Exception:
            logger.exception("Webex on_message handler raised")

    # ── HTTP transport ──

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Return the shared ClientSession, creating it once on demand.

        Guarded by a lock (double-checked) so concurrent callers -- the WS
        serve loop plus per-turn handler tasks -- can't each build a session
        and leak one unclosed. Mirrors TelegramClient.
        """
        if self._closed:
            raise RuntimeError("WebexClient is closed")
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._closed:
                    raise RuntimeError("WebexClient is closed")
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()
        return self._session

    async def _api(self, method: str, path: str, payload: dict | None, timeout: int = 30) -> Any:
        """Call a Webex REST endpoint. Returns the parsed JSON body (or ``{}``
        for empty 2xx responses) on success, None on error.

        Honors a single 429 ``Retry-After`` back-off, mirroring the Telegram
        client: a rate-limited status edit that we simply dropped would freeze
        the placeholder, so wait out the (usually short) cool-down once.
        """
        session = await self._ensure_session()
        url = f"{self._api_base}{path}"
        for attempt in range(2):
            try:
                async with session.request(
                    method,
                    url,
                    json=payload,
                    headers=self._headers(),
                    proxy=self._proxy,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 429 and attempt == 0:
                        retry_after = 1.0
                        try:
                            retry_after = float(resp.headers.get("Retry-After", "1"))
                        except (TypeError, ValueError):
                            pass
                        await asyncio.sleep(min(max(retry_after, 0.5), 10.0))
                        continue
                    if 200 <= resp.status < 300:
                        if resp.status == 204:
                            return {}
                        try:
                            return await resp.json(content_type=None)
                        except (json.JSONDecodeError, ValueError):
                            return {}
                    # Response bodies are externally-derived; log status only.
                    logger.warning("Webex API %s %s failed: http=%s", method, path, resp.status)
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "Webex API %s %s transport error: %s", method, path, type(exc).__name__
                )
                return None
        return None


def _resolve_proxy() -> str | None:
    """Resolve an outbound proxy from the environment, if set."""
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        val = os.environ.get(var)
        if val:
            return val
    return None
