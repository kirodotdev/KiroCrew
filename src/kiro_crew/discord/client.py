"""Discord transport layer — Gateway WebSocket (inbound) + REST (outbound).

Inbound: a persistent Gateway WebSocket connection (identify -> heartbeat ->
dispatch) receives MESSAGE_CREATE and INTERACTION_CREATE events and hands
normalized objects to the on_message / on_interaction handlers. Resume is
supported so a transient drop replays missed events instead of re-identifying.

Outbound (REST v10):
  - send_message: posts a new message (optionally with button components)
  - edit_message: edits an existing message in-place (for streaming)
  - send_typing: triggers the "typing..." indicator (~10s)
  - add_reaction: emoji reaction (steer-ack receipts)
  - ack_component_interaction: DEFERRED_UPDATE_MESSAGE ack for a button press

No external Discord library dependency — pure aiohttp against the public
Gateway + REST API. This keeps the module lightweight, OSS-clean, and easy to
audit (mirrors the Telegram client's design).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import urllib.parse
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp

logger = logging.getLogger(__name__)

# Discord message content limit (chars).
DISCORD_MAX_TEXT = 2000
# Safe chunk boundary (leave room for chip/footer overhead).
DISCORD_CHUNK_LIMIT = 1900

_API_BASE = "https://discord.com/api/v10"
_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# Gateway intents: DIRECT_MESSAGES (1<<12) is all a DM-only bot needs.
# Message content is always delivered for DMs — the privileged MESSAGE_CONTENT
# intent (1<<15) only gates *guild* message content, which we deny anyway.
_INTENT_DIRECT_MESSAGES = 1 << 12

# Gateway opcodes.
_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RESUME = 6
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10
_OP_HEARTBEAT_ACK = 11

# Interaction types / callback types.
_INTERACTION_MESSAGE_COMPONENT = 3
_CALLBACK_DEFERRED_UPDATE_MESSAGE = 6


@dataclass
class DiscordInbound:
    """Normalized inbound message from a MESSAGE_CREATE dispatch."""

    channel_id: str
    user_id: str
    username: str = ""
    text: str = ""
    message_id: str = ""
    guild_id: str = ""  # empty string == DM channel
    is_bot: bool = False


@dataclass
class DiscordInteraction:
    """Normalized button press from an INTERACTION_CREATE dispatch."""

    interaction_id: str
    interaction_token: str
    channel_id: str
    user_id: str
    message_id: str
    custom_id: str = ""
    label: str = ""  # button text, recovered from the message's components
    guild_id: str = ""  # empty string == DM channel
    username: str = ""


class DiscordClient:
    """Discord Gateway + REST client with auto-reconnect and resume.

    Connects via the Gateway WebSocket (works behind NAT/firewall — no public
    webhook endpoint needed). Dispatches messages to on_message and button
    presses to on_interaction.
    """

    def __init__(
        self,
        *,
        token: str,
        on_message: Callable[[DiscordInbound], Awaitable[None]] | None = None,
        on_interaction: Callable[[DiscordInteraction], Awaitable[None]] | None = None,
        proxy: str | None = None,
    ) -> None:
        self._token = token
        self._on_message = on_message
        self._on_interaction = on_interaction
        self._proxy = proxy or _resolve_proxy()
        self._session: aiohttp.ClientSession | None = None
        self._session_lock: asyncio.Lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._hb_task: asyncio.Task[None] | None = None
        self._closed = False
        # Resume state (from READY / dispatch sequence numbers).
        self._seq: int | None = None
        self._session_id: str = ""
        self._resume_url: str = ""
        self._hb_acked = True
        self._ws: Any = None  # WS handle; aiohttp generics vary across versions
        # Live turn tasks — prevent GC of in-flight handlers.
        self._handler_tasks: set[asyncio.Task[None]] = set()
        # Bot's own user id (from READY) so we can drop our own messages.
        self.bot_user_id: str = ""
        # Set when the Gateway handshake reaches READY (cleared while
        # disconnected/reconnecting). ``wait_ready`` gates "connected" status.
        self.ready: asyncio.Event = asyncio.Event()
        # Short reason when the connection died non-recoverably (bad token /
        # bad intents); empty otherwise. Read by the status callback path.
        self.fatal_error: str = ""
        # Optional observer called with (connected: bool, error: str) on READY
        # and on non-recoverable close — lets the gateway keep the dashboard
        # status badge truthful after boot.
        self.on_state_change: Callable[[bool, str], None] | None = None

    async def wait_ready(self, timeout: float = 15.0) -> bool:
        """Wait for the Gateway handshake to reach READY. Returns False on
        timeout or when the connection already failed non-recoverably."""
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
                logger.debug("Discord on_state_change observer raised", exc_info=True)

    # ── Lifecycle ──

    async def start(self) -> None:
        """Launch the background Gateway connection loop."""
        self._closed = False
        self._task = asyncio.create_task(self._gateway_loop())

    async def close(self) -> None:
        """Gracefully shut down."""
        self._closed = True
        self._stop_heartbeat()
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
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

    def set_message_handler(self, on_message: Callable[[DiscordInbound], Awaitable[None]]) -> None:
        """Set/replace the inbound-message handler after construction.

        Lets the gateway wire ``transport.receive`` in once the transport
        (which needs the client) has been built, avoiding a construction cycle.
        """
        self._on_message = on_message

    # ── Outbound REST ──

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        components: list[dict] | None = None,
        reply_to_message_id: str | None = None,
    ) -> str | None:
        """Send a new message. Returns the message id (snowflake string).

        Discord renders standard Markdown natively, so ``text`` is sent as-is.
        """
        payload: dict[str, Any] = {"content": text[:DISCORD_MAX_TEXT]}
        if components:
            payload["components"] = components
        if reply_to_message_id:
            payload["message_reference"] = {
                "message_id": reply_to_message_id,
                "fail_if_not_exists": False,
            }
        result = await self._api("POST", f"/channels/{channel_id}/messages", payload)
        return str(result.get("id")) if result else None

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        text: str,
        *,
        components: list[dict] | None = None,
    ) -> bool:
        """Edit an existing message in-place (for streaming)."""
        payload: dict[str, Any] = {"content": text[:DISCORD_MAX_TEXT]}
        if components is not None:
            payload["components"] = components
        result = await self._api("PATCH", f"/channels/{channel_id}/messages/{message_id}", payload)
        return result is not None

    async def send_typing(self, channel_id: str) -> None:
        """Trigger the 'typing...' indicator (Discord shows it ~10s)."""
        await self._api("POST", f"/channels/{channel_id}/typing", {})

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        """Add a unicode emoji reaction to a message (steer-ack receipt).

        Best-effort: callers should treat failures as non-fatal.
        """
        enc = urllib.parse.quote(emoji, safe="")
        await self._api(
            "PUT",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{enc}/@me",
            None,
        )

    async def ack_component_interaction(self, interaction_id: str, interaction_token: str) -> None:
        """Acknowledge a button press without changing the message.

        DEFERRED_UPDATE_MESSAGE stops Discord's "interaction failed" spinner;
        the actual message update happens via a normal ``edit_message``.
        """
        await self._api(
            "POST",
            f"/interactions/{interaction_id}/{interaction_token}/callback",
            {"type": _CALLBACK_DEFERRED_UPDATE_MESSAGE},
        )

    async def create_dm_channel(self, user_id: str) -> str:
        """Create (or fetch) the DM channel for a user id. Returns the channel
        id, or empty string on failure. Needed for proactive sends — outbound
        messages address a channel, not a user."""
        result = await self._api("POST", "/users/@me/channels", {"recipient_id": user_id})
        return str(result.get("id")) if result else ""

    async def edit_message_components(
        self, channel_id: str, message_id: str, components: list[dict]
    ) -> bool:
        """Edit ONLY a message's components, leaving its content intact.

        Used to retire an ``[OPTIONS:]`` button row after a choice is tapped
        without clobbering the answer text that carried it. Pass ``[]`` to
        remove the buttons.
        """
        result = await self._api(
            "PATCH",
            f"/channels/{channel_id}/messages/{message_id}",
            {"components": components},
        )
        return result is not None

    # ── Gateway connection loop ──

    async def _gateway_loop(self) -> None:
        """Persistent Gateway connection with resume + exponential backoff."""
        attempt = 0
        while not self._closed:
            try:
                await self._run_connection()
                attempt = 0  # clean disconnect (e.g. op 7) — reconnect promptly
            except asyncio.CancelledError:
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                if self._closed:
                    break
                attempt += 1
                delay = min(1.0 * (2 ** (attempt - 1)), 60.0) + random.random()
                # Log only the exception type — never the token.
                logger.warning(
                    "Discord gateway error (%s), reconnect in %.1fs",
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)
            except Exception:
                if self._closed:
                    break
                logger.exception("Discord gateway unexpected error")
                await asyncio.sleep(5.0)
            finally:
                self._stop_heartbeat()

    async def _run_connection(self) -> None:
        """One Gateway connection: hello -> identify/resume -> dispatch loop."""
        session = await self._ensure_session()
        url = self._resume_url if (self._session_id and self._resume_url) else _GATEWAY_URL
        async with session.ws_connect(url, proxy=self._proxy, heartbeat=None, max_msg_size=0) as ws:
            self._ws = ws
            try:
                async for raw in ws:
                    if raw.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_frame(ws, json.loads(raw.data))
                    elif raw.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
            finally:
                self._ws = None
        # Close code 4004 (auth failed) etc. surfaces via ws.close_code.
        self.ready.clear()
        code = ws.close_code or 0
        if code in (4004, 4010, 4011, 4012, 4013, 4014):
            # Non-recoverable (bad token / bad intents). Stop rather than
            # hammering the gateway forever; the failure is already logged.
            logger.error(
                "Discord gateway closed with non-recoverable code %s — "
                "check the bot token and intents. Channel stopped.",
                code,
            )
            self._closed = True
            self.fatal_error = f"gateway close {code} (check bot token/intents)"
            self._notify_state(False, self.fatal_error)
        elif code in (4007, 4009):
            # Invalid seq / session timed out — must re-identify from scratch.
            self._session_id = ""
            self._seq = None

    async def _handle_frame(self, ws: Any, frame: dict) -> None:
        op = frame.get("op")
        if frame.get("s") is not None:
            self._seq = frame["s"]
        if op == _OP_HELLO:
            interval = float(frame["d"]["heartbeat_interval"]) / 1000.0
            self._start_heartbeat(ws, interval)
            if self._session_id and self._resume_url:
                await ws.send_json(
                    {
                        "op": _OP_RESUME,
                        "d": {
                            "token": self._token,
                            "session_id": self._session_id,
                            "seq": self._seq or 0,
                        },
                    }
                )
            else:
                await self._identify(ws)
        elif op == _OP_HEARTBEAT:
            await ws.send_json({"op": _OP_HEARTBEAT, "d": self._seq})
        elif op == _OP_HEARTBEAT_ACK:
            self._hb_acked = True
        elif op == _OP_RECONNECT:
            await ws.close()
        elif op == _OP_INVALID_SESSION:
            if not frame.get("d"):
                self._session_id = ""
                self._seq = None
            await asyncio.sleep(1.0 + random.random() * 4.0)  # per docs
            await ws.close()
        elif op == _OP_DISPATCH:
            self._on_dispatch(frame.get("t") or "", frame.get("d") or {})

    async def _identify(self, ws: Any) -> None:
        await ws.send_json(
            {
                "op": _OP_IDENTIFY,
                "d": {
                    "token": self._token,
                    "intents": _INTENT_DIRECT_MESSAGES,
                    "properties": {
                        "os": "linux",
                        "browser": "kirocrew",
                        "device": "kirocrew",
                    },
                },
            }
        )

    def _start_heartbeat(self, ws: Any, interval: float) -> None:
        self._stop_heartbeat()
        self._hb_acked = True
        self._hb_task = asyncio.create_task(self._heartbeat_loop(ws, interval))

    def _stop_heartbeat(self) -> None:
        task, self._hb_task = self._hb_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _heartbeat_loop(self, ws: Any, interval: float) -> None:
        """Heartbeat at the server's interval; a missed ack forces a reconnect
        (close -> the gateway loop resumes the session)."""
        try:
            await asyncio.sleep(interval * random.random())  # jitter per docs
            while not self._closed and not ws.closed:
                if not self._hb_acked:
                    logger.warning("Discord gateway heartbeat not acked — reconnecting")
                    await ws.close()
                    return
                self._hb_acked = False
                await ws.send_json({"op": _OP_HEARTBEAT, "d": self._seq})
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Discord heartbeat loop error", exc_info=True)

    # ── Dispatch normalization ──

    def _on_dispatch(self, event: str, d: dict) -> None:
        if event == "READY":
            self._session_id = d.get("session_id", "")
            self._resume_url = d.get("resume_gateway_url", "")
            if self._resume_url and "?" not in self._resume_url:
                self._resume_url += "/?v=10&encoding=json"
            self.bot_user_id = str((d.get("user") or {}).get("id", ""))
            logger.info("Discord gateway READY (bot user %s)", self.bot_user_id)
            self.ready.set()
            self._notify_state(True, "")
            return
        if event == "RESUMED":
            logger.info("Discord gateway session resumed")
            self.ready.set()
            self._notify_state(True, "")
            return
        if event == "MESSAGE_CREATE":
            author = d.get("author") or {}
            inbound = DiscordInbound(
                channel_id=str(d.get("channel_id", "")),
                user_id=str(author.get("id", "")),
                username=author.get("username", ""),
                text=d.get("content", ""),
                message_id=str(d.get("id", "")),
                guild_id=str(d.get("guild_id", "") or ""),
                is_bot=bool(author.get("bot", False)),
            )
            if inbound.is_bot or inbound.user_id == self.bot_user_id:
                return  # never respond to bots (incl. ourselves) — loop guard
            task = asyncio.create_task(self._invoke_message(inbound))
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)
            return
        if event == "INTERACTION_CREATE":
            if d.get("type") != _INTERACTION_MESSAGE_COMPONENT:
                return
            data = d.get("data") or {}
            msg = d.get("message") or {}
            user = d.get("user") or (d.get("member") or {}).get("user") or {}
            custom_id = data.get("custom_id", "")
            interaction = DiscordInteraction(
                interaction_id=str(d.get("id", "")),
                interaction_token=d.get("token", ""),
                channel_id=str(d.get("channel_id", "")),
                user_id=str(user.get("id", "")),
                message_id=str(msg.get("id", "")),
                custom_id=custom_id,
                label=_find_button_label(msg.get("components") or [], custom_id),
                guild_id=str(d.get("guild_id", "") or ""),
                username=user.get("username", ""),
            )
            task = asyncio.create_task(self._invoke_interaction(interaction))
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)

    async def _invoke_message(self, inbound: DiscordInbound) -> None:
        if self._on_message is None:
            return
        try:
            await self._on_message(inbound)
        except Exception:
            logger.exception("Discord on_message handler raised for user=%s", inbound.user_id)

    async def _invoke_interaction(self, interaction: DiscordInteraction) -> None:
        if self._on_interaction:
            try:
                await self._on_interaction(interaction)
            except Exception:
                logger.exception("Discord on_interaction handler raised")

    # ── HTTP transport ──

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Return the shared ClientSession, creating it once on demand
        (double-checked under a lock — REST calls run concurrently with the
        gateway loop)."""
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()
        return self._session

    async def _api(self, method: str, path: str, payload: dict | None, timeout: int = 30) -> Any:
        """Call a REST endpoint. Returns the parsed JSON body ({} for 204) or
        None on error. Honors a single 429 ``retry_after`` back-off.
        """
        session = await self._ensure_session()
        url = _API_BASE + path
        headers = {"Authorization": f"Bot {self._token}"}
        for attempt in range(2):
            try:
                async with session.request(
                    method,
                    url,
                    json=payload,
                    headers=headers,
                    proxy=self._proxy,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 204:
                        return {}
                    # A proxy or Discord error page can return non-JSON; a
                    # decode failure must degrade to an error result, never
                    # propagate into the renderer/dispatcher.
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = None
                    if 200 <= resp.status < 300:
                        # Malformed 2xx body: succeed with an empty result so
                        # callers treat the operation as done (it was).
                        return data if data is not None else {}
                    if resp.status == 429 and attempt == 0:
                        retry_after = 1.0
                        try:
                            retry_after = float((data or {}).get("retry_after", 1.0))
                        except (TypeError, ValueError):
                            pass
                        await asyncio.sleep(min(max(retry_after, 0.5), 5.0))
                        continue
                    logger.warning(
                        "Discord API %s %s failed: status=%s code=%s message=%s",
                        method,
                        path,
                        resp.status,
                        (data or {}).get("code"),
                        (data or {}).get("message"),
                    )
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # Log only the exception type — never anything token-adjacent.
                logger.warning(
                    "Discord API %s %s transport error: %s",
                    method,
                    path,
                    type(exc).__name__,
                )
                return None
        return None


def _find_button_label(components: list[dict], custom_id: str) -> str:
    """Recover the pressed button's display text from the message's components
    (custom_id carries only our compact routing data)."""
    for row in components:
        for btn in row.get("components", []):
            if btn.get("custom_id") == custom_id:
                return btn.get("label", "")
    return ""


def _resolve_proxy() -> str | None:
    """Resolve outbound proxy from environment."""
    for var in (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
    ):
        val = os.environ.get(var)
        if val:
            return val
    return None
