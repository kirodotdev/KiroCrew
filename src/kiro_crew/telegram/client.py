"""Telegram Bot API transport layer — long-polling + message send/edit.

Inbound: long-polling loop calls getUpdates, dispatches Message and
CallbackQuery objects to the on_message / on_callback handlers.

Outbound:
  - send_message: posts a new message, returns message_id
  - edit_message: edits an existing message in-place (for streaming)
  - send_typing: sends "typing..." chat action
  - answer_callback: acknowledges an inline-keyboard button press

No external Telegram library dependency — pure aiohttp + Bot API REST.
This keeps the module lightweight, OSS-clean, and easy to audit.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp

logger = logging.getLogger(__name__)

# Telegram message text limit.
TELEGRAM_MAX_TEXT = 4096
# Safe chunk boundary (leave room for markdown overhead).
TELEGRAM_CHUNK_LIMIT = 4000

# Bot API base URL.
_API_BASE = "https://api.telegram.org/bot{token}/{method}"


@dataclass
class TelegramInbound:
    """Normalised inbound message from a Telegram update."""

    chat_id: int
    user_id: int
    username: str = ""
    text: str = ""
    message_id: int = 0
    chat_type: str = ""  # "private" | "group" | "supergroup" | "channel"


@dataclass
class TelegramCallback:
    """Normalised callback_query from an inline keyboard button press."""

    callback_query_id: str
    chat_id: int
    user_id: int
    message_id: int
    data: str = ""
    label: str = ""  # button text, recovered from the message's reply_markup
    username: str = ""
    chat_type: str = ""  # "private" | "group" | "supergroup" | "channel"


class TelegramClient:
    """Telegram Bot API client with long-polling and auto-reconnect.

    Connects to Telegram via getUpdates long-polling (no webhook needed —
    works behind NAT/firewall). Dispatches messages to on_message and
    inline-keyboard presses to on_callback.
    """

    def __init__(
        self,
        *,
        token: str,
        on_message: Callable[[TelegramInbound], Awaitable[None]] | None = None,
        on_callback: Callable[[TelegramCallback], Awaitable[None]] | None = None,
        polling_timeout: int = 30,
        proxy: str | None = None,
    ) -> None:
        self._token = token
        self._on_message = on_message
        self._on_callback = on_callback
        self._polling_timeout = polling_timeout
        self._proxy = proxy or _resolve_proxy()
        self._session: aiohttp.ClientSession | None = None
        self._session_lock: asyncio.Lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._offset: int = 0
        # Live turn tasks — prevent GC of in-flight handlers.
        self._handler_tasks: set[asyncio.Task[None]] = set()

    # ── Lifecycle ──

    async def start(self) -> None:
        """Launch the background polling loop."""
        self._closed = False
        self._task = asyncio.create_task(self._polling_loop())

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
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def set_message_handler(
        self, on_message: Callable[[TelegramInbound], Awaitable[None]]
    ) -> None:
        """Set/replace the inbound-message handler after construction.

        Lets the gateway wire ``transport.receive`` in once the transport (which
        needs the client) has been built, avoiding a construction cycle.
        """
        self._on_message = on_message

    # ── Outbound API ──

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
        retry_plain: bool = True,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        """Send a new message. Returns the message_id on success.

        Default is plaintext: the agent emits markdown/plaintext, not HTML, so
        sending with parse_mode=HTML would make any bare ``<``/``>``/``&`` trip a
        Telegram 400 and force a second round-trip. Callers that generate real
        markup (e.g. a static help card) may pass parse_mode explicitly.
        """
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:TELEGRAM_MAX_TEXT],
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup:
            params["reply_markup"] = reply_markup
        if reply_to_message_id:
            # allow_sending_without_reply: still send if the target was deleted.
            params["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        result = await self._api("sendMessage", params)
        if result:
            return result.get("message_id")
        # Only retry (drop parse_mode) when a parse_mode was actually requested
        # AND the caller allows it. Renderers that send HTML pass
        # retry_plain=False so a parse failure never re-sends the literal tags.
        if parse_mode and retry_plain:
            params.pop("parse_mode", None)
            result = await self._api("sendMessage", params)
        return result.get("message_id") if result else None

    async def send_message_draft(
        self, chat_id: int, draft_id: int, text: str, *, parse_mode: str | None = None
    ) -> bool:
        """Stream an ephemeral partial-message draft (Bot API 9.3+ sendMessageDraft).

        Reusing the same non-zero ``draft_id`` animates the update in place, which
        is native, smooth streaming with no editMessageText reflow. The draft is a
        ~30s preview -- the finished message must still be sent via send_message.
        Requires the bot to have Forum Topic Mode enabled in BotFather; returns
        False (so the caller can fall back) if the API rejects it. Sent as
        plaintext (no parse_mode by default) so partial markdown never 400s.
        """
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "draft_id": draft_id,
            "text": text[:TELEGRAM_MAX_TEXT],
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        result = await self._api("sendMessageDraft", params)
        return result is not None

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
        retry_plain: bool = True,
    ) -> bool:
        """Edit an existing message in-place (for streaming). Returns True on success.

        Plaintext by default (see ``send_message``) so streaming edits carrying
        markdown/code never 400 and burn the ~30/min/chat edit budget on retries.
        """
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:TELEGRAM_MAX_TEXT],
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup:
            params["reply_markup"] = reply_markup
        result = await self._api("editMessageText", params)
        if result is not None:
            return True
        if parse_mode and retry_plain:
            params.pop("parse_mode", None)
            result = await self._api("editMessageText", params)
        return result is not None

    async def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: dict | None = None
    ) -> bool:
        """Edit ONLY a message's inline keyboard, leaving its text intact.

        Used to retire an ``[OPTIONS:]`` keyboard after a choice is tapped
        without clobbering the answer text that carried it. Pass
        ``{"inline_keyboard": []}`` to remove the buttons.
        """
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        result = await self._api("editMessageReplyMarkup", params)
        return result is not None

    async def send_typing(self, chat_id: int) -> None:
        """Send 'typing...' chat action."""
        await self._api("sendChatAction", {"chat_id": chat_id, "action": "typing"})

    async def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        """Acknowledge a callback_query to stop the spinner on the button."""
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text[:200]
        await self._api("answerCallbackQuery", params)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        """Delete a message (e.g. remove stale inline keyboards)."""
        await self._api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def set_message_reaction(
        self, chat_id: int, message_id: int, emoji: str
    ) -> None:
        """Set a single emoji reaction on a message (Bot API 7.0+ ``setMessageReaction``).

        Used as an instant, no-extra-bubble acknowledgement that a mid-turn steer
        was received. ``emoji`` must be one of Telegram's allowed reaction emojis
        (e.g. "🫡"). Best-effort: callers should treat failures as non-fatal.
        """
        await self._api(
            "setMessageReaction",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction": [{"type": "emoji", "emoji": emoji}],
            },
        )

    # ── Polling loop ──

    async def _polling_loop(self) -> None:
        """Long-polling loop with exponential backoff on failure."""
        attempt = 0
        while not self._closed:
            try:
                updates = await self._get_updates()
                if updates is None:
                    # API-level failure (ok:false — 401 bad token, 409 conflict,
                    # etc). _api already logged it; back off like a transport
                    # error instead of hot-looping getUpdates with zero delay.
                    attempt += 1
                    delay = min(1.0 * (2 ** (attempt - 1)), 30.0)
                    await asyncio.sleep(delay)
                    continue
                attempt = 0  # reset on success
                for update in updates:
                    self._dispatch(update)
            except asyncio.CancelledError:
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                if self._closed:
                    break
                attempt += 1
                delay = min(1.0 * (2 ** (attempt - 1)), 30.0)
                # Log only the exception type — an aiohttp exc's str() can embed
                # the request URL, which contains the bot token (a registered
                # credential). Mirrors _api's transport-error logging.
                logger.warning(
                    "Telegram polling error (%s), retry in %.1fs",
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)
            except Exception:
                if self._closed:
                    break
                logger.exception("Telegram polling unexpected error")
                await asyncio.sleep(5.0)

    async def _get_updates(self) -> list[dict] | None:
        """Call getUpdates with long-poll timeout."""
        params = {
            "offset": self._offset,
            "timeout": self._polling_timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        result = await self._api("getUpdates", params, timeout=self._polling_timeout + 10)
        if result is None:
            return None  # API-level failure — signal the polling loop to back off
        # result is the array of Update objects ([] when there are none).
        if isinstance(result, list):
            for upd in result:
                uid = upd.get("update_id", 0)
                if uid >= self._offset:
                    self._offset = uid + 1
            return result
        return []

    def _dispatch(self, update: dict) -> None:
        """Route a single Update to the appropriate handler as a background task."""
        if "message" in update:
            msg = update["message"]
            text = msg.get("text", "")
            chat = msg.get("chat", {})
            user = msg.get("from", {})
            inbound = TelegramInbound(
                chat_id=chat.get("id", 0),
                user_id=user.get("id", 0),
                username=user.get("username", ""),
                text=text,
                message_id=msg.get("message_id", 0),
                chat_type=chat.get("type", ""),
            )
            task = asyncio.create_task(self._invoke_message(inbound))
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)

        elif "callback_query" in update:
            cq = update["callback_query"]
            user = cq.get("from", {})
            msg = cq.get("message", {})
            chat = msg.get("chat", {})
            data = cq.get("data", "")
            # Recover the pressed button's display text from the message's
            # inline keyboard (callback_data carries only the index).
            label = ""
            for kb_row in msg.get("reply_markup", {}).get("inline_keyboard", []):
                for btn in kb_row:
                    if btn.get("callback_data") == data:
                        label = btn.get("text", "")
                        break
                if label:
                    break
            callback = TelegramCallback(
                callback_query_id=cq.get("id", ""),
                chat_id=chat.get("id", 0),
                user_id=user.get("id", 0),
                message_id=msg.get("message_id", 0),
                data=data,
                label=label,
                username=user.get("username", ""),
                chat_type=chat.get("type", ""),
            )
            task = asyncio.create_task(self._invoke_callback(callback))
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)

    async def _invoke_message(self, inbound: TelegramInbound) -> None:
        if self._on_message is None:
            return
        try:
            await self._on_message(inbound)
        except Exception:
            logger.exception("Telegram on_message handler raised for user=%s", inbound.user_id)

    async def _invoke_callback(self, callback: TelegramCallback) -> None:
        if self._on_callback:
            try:
                await self._on_callback(callback)
            except Exception:
                logger.exception("Telegram on_callback handler raised")

    # ── HTTP transport ──

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Return the shared ClientSession, creating it once on demand.

        ``_api`` runs concurrently — the polling loop calls it via
        ``_get_updates`` while each spawned ``_invoke_message`` /
        ``_invoke_callback`` handler task also calls it. Guard the lazy init
        with a lock (double-checked) so two coroutines can't each build a
        session and leak one unclosed.
        """
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()
        return self._session

    async def _api(self, method: str, params: dict, timeout: int = 30) -> Any:
        """Call a Bot API method. Returns the 'result' field or None on error.

        Honors a single 429 ``retry_after`` back-off: a rate-limited edit that
        we simply dropped would freeze the streaming bubble until the next
        chunk, which reads as a stutter -- so we wait out the (usually short)
        cool-down once and retry instead.
        """
        session = await self._ensure_session()

        url = _API_BASE.format(token=self._token, method=method)
        for attempt in range(2):
            try:
                async with session.post(
                    url,
                    json=params,
                    proxy=self._proxy,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    data = await resp.json(content_type=None)
                    if data and data.get("ok"):
                        return data.get("result")
                    # Log Telegram API errors.
                    err_code = data.get("error_code") if data else None
                    err_desc = data.get("description") if data else None
                    # 400 "message is not modified" is benign during streaming.
                    if err_code == 400 and "not modified" in (err_desc or "").lower():
                        return {}  # treat as success (no change needed)
                    # 429: respect the server's retry_after once, then give up.
                    if err_code == 429 and attempt == 0:
                        retry_after = 1.0
                        try:
                            retry_after = float(
                                (data.get("parameters") or {}).get("retry_after", 1.0)
                            )
                        except (TypeError, ValueError):
                            pass
                        await asyncio.sleep(min(max(retry_after, 0.5), 5.0))
                        continue
                    logger.warning(
                        "Telegram API %s failed: code=%s desc=%s",
                        method,
                        err_code,
                        err_desc,
                    )
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # Log only the exception type — its str() can embed the request
                # URL, which contains the bot token (a registered credential).
                logger.warning(
                    "Telegram API %s transport error: %s", method, type(exc).__name__
                )
                return None
        return None


def _resolve_proxy() -> str | None:
    """Resolve outbound proxy from environment."""
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        val = os.environ.get(var)
        if val:
            return val
    return None
