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
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp

from kiro_crew.metrics.provider import get_recorder

logger = logging.getLogger(__name__)

# Telegram message text limit.
TELEGRAM_MAX_TEXT = 4096
# Safe chunk boundary (leave room for markdown overhead).
TELEGRAM_CHUNK_LIMIT = 4000

# Bot API base URL.
_API_BASE = "https://api.telegram.org/bot{token}/{method}"

#: Consecutive polling failures before the status callback reports unhealthy.
_STATUS_FAILURE_THRESHOLD = 3

#: Telegram's supported HTML tag set. Anything we may have to re-close when a
#: rendered message has to be truncated mid-document.
_TG_HTML_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")

#: Longest HTML entity we expect ("&blockquote;"-class names are not used by the
#: renderer, but stay generous so a cut never lands inside "&amp;"/"&#1234;").
_MAX_ENTITY_LEN = 12


def _cut_points(text: str) -> list[tuple[int, int, int]]:
    """``[(lo, hi, closers_len)]``, one entry per run of text BETWEEN tags.

    Cutting at any index in ``[lo, hi]`` therefore lands outside every tag by
    construction, and ``closers_len`` is how many chars of closing tags that
    prefix needs to balance.

    Single forward pass, with the open-tag stack length maintained incrementally.
    Re-deriving the stack for each candidate cut is O(n) per probe and made the
    search quadratic -- measured 122 ms on one 4 KB document whose limit landed
    inside a nested-close cluster, which would block the event loop.
    """
    points: list[tuple[int, int, int]] = []
    stack: list[str] = []
    closers_len = 0
    prev_end = 0
    for m in _TG_HTML_TAG_RE.finditer(text):
        points.append((prev_end, m.start(), closers_len))
        closing, name = m.group(1), m.group(2).lower()
        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == name:
                    del stack[i]
                    closers_len -= len(name) + 3  # len("</name>")
                    break
        else:
            stack.append(name)
            closers_len += len(name) + 3
        prev_end = m.end()
    points.append((prev_end, len(text), closers_len))
    return points


def _entity_safe_cut(text: str, cut: int, lo: int) -> int:
    """Back ``cut`` out of an HTML entity, without leaving the run at ``lo``.

    Only entities need handling here: a cut inside ``[lo, hi]`` is already
    outside every tag, so the tag/entity guard-ordering hazard cannot arise.
    """
    amp = text.rfind("&", lo, cut)
    semi = text.rfind(";", lo, cut)
    if amp > semi and (cut - amp) <= _MAX_ENTITY_LEN:
        return amp
    return cut


def truncate_html_safe(text: str, limit: int = TELEGRAM_MAX_TEXT) -> str:
    """Truncate Telegram HTML to ``limit`` chars without breaking the parse.

    Guarantees the result (a) never splits a tag or entity, (b) closes every tag
    it leaves open, and (c) is a prefix of the input plus closing tags.

    Scans the between-tag runs right to left and takes the first one where the
    prefix AND its closers fit, which is the longest such prefix. Returning a
    bare prefix when nothing fits would emit UNCLOSED tags -- precisely the
    "Can't find end tag" 400 this exists to prevent -- so the degenerate answer
    is the empty string, which is always valid.

    Note: ``limit`` counts Python code points while Telegram counts UTF-16 code
    units, so an astral char (emoji, CJK ext) costs 1 here and 2 there. An
    emoji-dense message near the cap can still be rejected. That mismatch
    predates this helper (the plain slice shared it) and is tracked separately.
    """
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    for lo, hi, closers_len in reversed(_cut_points(text)):
        room = limit - closers_len
        if room < lo:
            continue  # even an empty prefix in this run cannot fit its closers
        cut = _entity_safe_cut(text, min(hi, room), lo)
        if cut < lo:
            continue
        closers = _open_tag_closers(text[:cut])
        if cut + len(closers) <= limit:
            return text[:cut] + closers
    return ""


def _open_tag_closers(html_text: str) -> str:
    """Closing tags needed to balance ``html_text``, innermost first.

    Telegram rejects the WHOLE message when a start tag has no matching end tag
    ("Can't find end tag corresponding to start tag \"code\""), so a truncated
    document must carry its own closers.
    """
    stack: list[str] = []
    for m in _TG_HTML_TAG_RE.finditer(html_text):
        closing, name = m.group(1), m.group(2).lower()
        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == name:
                    del stack[i]
                    break
        else:
            stack.append(name)
    return "".join(f"</{name}>" for name in reversed(stack))


def _cap_text(text: str, parse_mode: str | None) -> str:
    """Length-cap outbound text: tag-safe when it is HTML, plain slice otherwise.

    Plaintext can be sliced anywhere. HTML cannot -- a blind slice is what turns
    an oversize rendered message into a hard 400. Reaching the HTML branch means
    the renderer's own budget under-estimated the rendered length, so warn: the
    split, not this backstop, is where the fix belongs.
    """
    if len(text) <= TELEGRAM_MAX_TEXT:
        return text
    if parse_mode and parse_mode.upper() == "HTML":
        logger.warning(
            "Telegram HTML text is %d chars (> %d); truncating tag-safely. "
            "The renderer should have split this before sending.",
            len(text),
            TELEGRAM_MAX_TEXT,
        )
        return truncate_html_safe(text, TELEGRAM_MAX_TEXT)
    return text[:TELEGRAM_MAX_TEXT]


#: Histogram for Bot API call latency. Outbound sends/edits are awaited inline in
#: the token-streaming path, so a call's duration *is* the time the render loop
#: was blocked -- which is what tells us whether the perceived slowness is
#: dominated by the fixed edit throttle or by network round-trips.
_API_DURATION_METRIC = "kirocrew.telegram.api.duration"


def _record_api_duration(
    method: str,
    elapsed_ms: float,
    *,
    ok: bool,
    err_code: int | None,
    timed_out: bool = False,
) -> None:
    """Emit one Bot API call duration to the metrics recorder.

    Best-effort: a metrics failure must never break a Telegram send.
    """
    logger.debug("Telegram API %s took %.0fms (ok=%s)", method, elapsed_ms, ok)
    if timed_out:
        outcome = "timeout"
    elif err_code == 429:
        outcome = "rate_limited"
    elif ok:
        outcome = "ok"
    else:
        outcome = "error"
    try:
        get_recorder().histogram(
            _API_DURATION_METRIC,
            elapsed_ms,
            unit="ms",
            attrs={"method": method, "outcome": outcome},
        )
    except Exception:
        logger.debug("telegram api duration metric emit failed", exc_info=True)


class TelegramAuthError(RuntimeError):
    """Telegram rejected an authenticated call (e.g. getMe with a bad token).

    Carries a short, token-free message safe to surface in the settings UI.
    """


@dataclass
class TelegramInbound:
    """Normalised inbound message from a Telegram update."""

    chat_id: int
    user_id: int
    username: str = ""
    text: str = ""
    message_id: int = 0
    chat_type: str = ""  # "private" | "group" | "supergroup" | "channel"
    # Forum-topic id in a supergroup (Bot API ``message_thread_id``); None in a
    # 1:1 DM or the supergroup's General topic.
    message_thread_id: int | None = None


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
    # Forum-topic id of the message the button lives on (None outside a topic).
    message_thread_id: int | None = None


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
        # Optional health callback: called with (healthy, reason) when polling
        # transitions to persistently-failing or recovers. Set by the gateway
        # to keep the settings status badge truthful after startup.
        self.on_status: Callable[[bool, str], None] | None = None
        #: Last health state reported through on_status (None = never
        #: reported). The gateway seeds this with the startup getMe outcome so
        #: transitions are relative to the boot state.
        self._last_status: bool | None = None
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

    def set_message_handler(self, on_message: Callable[[TelegramInbound], Awaitable[None]]) -> None:
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
        message_thread_id: int | None = None,
    ) -> int | None:
        """Send a new message. Returns the message_id on success.

        Default is plaintext: the agent emits markdown/plaintext, not HTML, so
        sending with parse_mode=HTML would make any bare ``<``/``>``/``&`` trip a
        Telegram 400 and force a second round-trip. Callers that generate real
        markup (e.g. a static help card) may pass parse_mode explicitly.

        ``message_thread_id`` targets a supergroup forum Topic; it is included
        only when set, so DM sends are byte-for-byte unchanged.
        """
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": _cap_text(text, parse_mode),
        }
        if message_thread_id is not None:
            params["message_thread_id"] = message_thread_id
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
        self,
        chat_id: int,
        draft_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        message_thread_id: int | None = None,
    ) -> bool:
        """Stream an ephemeral partial-message draft (Bot API 9.3+ sendMessageDraft).

        Reusing the same non-zero ``draft_id`` animates the update in place, which
        is native, smooth streaming with no editMessageText reflow. The draft is a
        ~30s preview -- the finished message must still be sent via send_message.
        Requires the bot to have Forum Topic Mode enabled in BotFather; returns
        False (so the caller can fall back) if the API rejects it. Sent as
        plaintext (no parse_mode by default) so partial markdown never 400s.

        ``message_thread_id`` targets a supergroup forum Topic; included only
        when set.
        """
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "draft_id": draft_id,
            "text": _cap_text(text, parse_mode),
        }
        if message_thread_id is not None:
            params["message_thread_id"] = message_thread_id
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
            "text": _cap_text(text, parse_mode),
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

    async def send_typing(self, chat_id: int, *, message_thread_id: int | None = None) -> None:
        """Send 'typing...' chat action. ``message_thread_id`` targets a forum
        Topic (included only when set)."""
        params: dict[str, Any] = {"chat_id": chat_id, "action": "typing"}
        if message_thread_id is not None:
            params["message_thread_id"] = message_thread_id
        await self._api("sendChatAction", params)

    async def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        """Acknowledge a callback_query to stop the spinner on the button."""
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text[:200]
        await self._api("answerCallbackQuery", params)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        """Delete a message (e.g. remove stale inline keyboards)."""
        await self._api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def set_message_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
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

    async def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
        """Publish the bot's ``/`` autocomplete menu (``setMyCommands``).

        Telegram REPLACES the whole default-scope menu on each call, so the full
        list must be sent every time — that is also what retires a command the
        bot no longer serves. An empty list is refused rather than sent, because
        Telegram would read it as "this bot has no commands" and wipe the menu.
        """
        if not commands:
            return False
        return bool(await self._api("setMyCommands", {"commands": commands}))

    # ── Polling loop ──

    async def _call_raw(self, method: str, params: dict, timeout: int = 15) -> Any:
        """POST a Bot API method and return the parsed JSON body.

        Unlike :meth:`_api`, transport errors PROPAGATE (aiohttp / timeout /
        OSError) instead of collapsing to ``None``, so callers can distinguish
        "Telegram said no" from "network down".
        """
        session = await self._ensure_session()
        url = _API_BASE.format(token=self._token, method=method)
        async with session.post(
            url,
            json=params,
            proxy=self._proxy,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            return await resp.json(content_type=None)

    async def get_me(self) -> dict:
        """Fetch the bot's own identity (``getMe``).

        The cheapest authenticated Bot API call — used by the gateway to prove
        the token is valid *before* reporting the channel as connected. Raises
        :class:`TelegramAuthError` when Telegram rejects the call (e.g. 401
        bad token); transport errors (network down) propagate as
        aiohttp/OSError so callers can distinguish "bad token" from "offline".
        """
        data = await self._call_raw("getMe", {})
        if isinstance(data, dict) and data.get("ok") and data.get("result"):
            return data["result"]
        desc = ""
        if isinstance(data, dict):
            # Telegram error descriptions are short fixed strings
            # ("Unauthorized") — token-free and safe to surface in settings.
            desc = str(data.get("description") or "")
        raise TelegramAuthError(f"Telegram rejected getMe ({desc or 'invalid bot token'})")

    def _notify_status(self, healthy: bool, reason: str) -> None:
        """Invoke the health callback on state CHANGE, swallowing its errors.

        Deduplicated on the last reported state so the polling loop can call
        it unconditionally on every successful poll — only actual transitions
        (healthy↔unhealthy) reach the callback.
        """
        if self.on_status is None or self._last_status == healthy:
            return
        self._last_status = healthy
        try:
            self.on_status(healthy, reason)
        except Exception:
            logger.debug("Telegram on_status callback failed", exc_info=True)

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
                    if attempt == _STATUS_FAILURE_THRESHOLD:
                        self._notify_status(
                            False, "getUpdates rejected by Telegram (check the bot token)"
                        )
                    delay = min(1.0 * (2 ** (attempt - 1)), 30.0)
                    await asyncio.sleep(delay)
                    continue
                # Deduped in _notify_status: only an actual unhealthy→healthy
                # transition (incl. recovery from an offline boot) fires.
                self._notify_status(True, "")
                attempt = 0  # reset on success
                for update in updates:
                    self._dispatch(update)
            except asyncio.CancelledError:
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                if self._closed:
                    break
                attempt += 1
                if attempt == _STATUS_FAILURE_THRESHOLD:
                    self._notify_status(False, f"getUpdates transport error ({type(exc).__name__})")
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
        # record=False: the long-poll deliberately blocks for ~polling_timeout
        # (30s default) and runs back-to-back forever, so recording it would bury
        # the outbound send/edit distribution the metric exists to measure under
        # a permanent ~30000ms mode. The Telemetry surface does not split on the
        # `method` attribute (see _OTHER_SPLIT_ATTRS), so filtering here is the
        # only way to keep the percentiles meaningful.
        result = await self._api(
            "getUpdates", params, timeout=self._polling_timeout + 10, record=False
        )
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
                message_thread_id=msg.get("message_thread_id"),
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
                message_thread_id=(msg or {}).get("message_thread_id"),
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

    async def _api(
        self, method: str, params: dict, timeout: int = 30, *, record: bool = True
    ) -> Any:
        """Call a Bot API method. Returns the 'result' field or None on error.

        Honors a single 429 ``retry_after`` back-off: a rate-limited edit that
        we simply dropped would freeze the streaming bubble until the next
        chunk, which reads as a stutter -- so we wait out the (usually short)
        cool-down once and retry instead.
        """
        session = await self._ensure_session()

        url = _API_BASE.format(token=self._token, method=method)
        # ONE timer for the whole call, not per attempt: the caller is blocked
        # for the entire span including a 429 ``retry_after`` sleep, and that
        # multi-second stall is exactly the user-visible latency the metric
        # exists to expose. Per-attempt timing dropped it (it fell between two
        # timers) and split one logical call into two misleadingly short samples.
        call_started = time.monotonic()

        def _elapsed_ms() -> float:
            return (time.monotonic() - call_started) * 1000.0

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
                        if record:
                            _record_api_duration(method, _elapsed_ms(), ok=True, err_code=None)
                        return data.get("result")
                    # Log Telegram API errors.
                    err_code = data.get("error_code") if data else None
                    err_desc = data.get("description") if data else None
                    # 400 "message is not modified" is benign during streaming.
                    if err_code == 400 and "not modified" in (err_desc or "").lower():
                        if record:
                            _record_api_duration(method, _elapsed_ms(), ok=True, err_code=None)
                        return {}  # treat as success (no change needed)
                    # 429: respect the server's retry_after once, then give up.
                    # Deliberately NOT recorded here -- the retry continues the
                    # same logical call, so the sample is emitted once at the
                    # terminal outcome with the sleep included.
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
                    if record:
                        _record_api_duration(method, _elapsed_ms(), ok=False, err_code=err_code)
                    logger.warning(
                        "Telegram API %s failed: code=%s desc=%s",
                        method,
                        err_code,
                        err_desc,
                    )
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # Record BEFORE returning: a timeout or connection failure is the
                # LONGEST a caller ever blocks, so dropping it here biased the
                # histogram towards calls that got a response and hid the worst
                # stalls (survivorship bias).
                if record:
                    _record_api_duration(
                        method,
                        _elapsed_ms(),
                        ok=False,
                        err_code=None,
                        timed_out=isinstance(exc, asyncio.TimeoutError),
                    )
                # Log only the exception type — its str() can embed the request
                # URL, which contains the bot token (a registered credential).
                logger.warning("Telegram API %s transport error: %s", method, type(exc).__name__)
                return None
        return None


def _resolve_proxy() -> str | None:
    """Resolve outbound proxy from environment."""
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        val = os.environ.get(var)
        if val:
            return val
    return None
