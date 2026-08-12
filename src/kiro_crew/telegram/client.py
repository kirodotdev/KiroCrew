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
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import aiohttp

from kiro_crew.metrics.provider import get_recorder

logger = logging.getLogger(__name__)

# Telegram message text limit.
TELEGRAM_MAX_TEXT = 4096

# ── Album (media group) coalescing ──
# Telegram delivers an album as N separate `message` updates sharing one
# `media_group_id`, with the caption on only one member. We buffer members and
# emit ONE merged message so a four-screenshot album is one turn, not four.
#: Idle gap after the last member before flushing. Album members arrive
#: back-to-back (typically in a single getUpdates batch), so this only has to
#: outlast intra-batch jitter -- not a user's typing.
_ALBUM_WINDOW_S = 1.0
#: Hard ceiling from the FIRST member, so a stream that keeps appending to one
#: group can never defer the flush indefinitely.
_ALBUM_MAX_WAIT_S = 5.0
#: Per-group member cap. Telegram's own album limit is 10, so this is only
#: reachable via a malformed/spoofed stream; it keeps the buffer bounded.
_ALBUM_MAX_MEMBERS = 10
#: Concurrent buffered groups. Defence-in-depth: each group self-flushes within
#: _ALBUM_MAX_WAIT_S, so this only matters under a burst of incomplete groups.
_ALBUM_MAX_GROUPS = 64
# Safe chunk boundary (leave room for markdown overhead).
TELEGRAM_CHUNK_LIMIT = 4000

# Bot API base URL.
_API_BASE = "https://api.telegram.org/bot{token}/{method}"

#: Consecutive polling failures before the status callback reports unhealthy.
_STATUS_FAILURE_THRESHOLD = 3

#: Consecutive sendRichMessage 400s before we treat the method as unavailable.
#: 400 is ambiguous -- a wrong payload shape fails every call, one oversized or
#: 20+-column table fails only itself -- so latch on a streak, not one answer.
_RICH_400_LATCH = 3

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
    #: Raw file attachment dicts extracted from the Telegram update (photo,
    #: document, audio, voice, video_note, video, animation). Each dict carries
    #: at minimum ``file_id`` and ``file_unique_id``; optional fields include
    #: ``file_size``, ``mime_type``, ``file_name``, and ``width``/``height``.
    attachments: list[dict[str, Any]] = field(default_factory=list)


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
        #: Latched True once sendRichMessage is known unavailable on this
        #: server -- see send_rich_message for the error taxonomy.
        self._rich_unsupported = False
        #: Consecutive sendRichMessage 400s. A wrong payload shape fails every
        #: call and latches; one bad table is cleared by the next good send.
        self._rich_400_streak = 0
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
        # Album (media group) coalescing buffers, keyed by media_group_id.
        self._albums: dict[str, list[TelegramInbound]] = {}
        self._album_timers: dict[str, asyncio.Task[None]] = {}
        self._album_first_seen: dict[str, float] = {}
        self._album_dropped: dict[str, int] = {}

    # ── Lifecycle ──

    async def start(self) -> None:
        """Launch the background polling loop."""
        self._closed = False
        self._task = asyncio.create_task(self._polling_loop())

    async def close(self) -> None:
        """Gracefully shut down."""
        self._closed = True
        # Best-effort flush of buffered albums BEFORE cancelling the polling
        # task. This is NOT a delivery guarantee -- see _flush_all_albums: the
        # handler it spawns races SessionManager._closing and may be refused,
        # exactly as a plain message arriving at shutdown already is today. It
        # costs nothing, sometimes wins, and drains the buffer either way.
        self._flush_all_albums()
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

    async def send_rich_message(
        self,
        chat_id: int,
        markdown: str,
        *,
        reply_markup: dict | None = None,
        message_thread_id: int | None = None,
        disable_notification: bool = False,
    ) -> int | None:
        """Send a Rich Message (Bot API 10.1+). Returns message_id on success.

        Rich Messages natively render tables, headings, code blocks, lists, and
        other structured markdown that the legacy sendMessage + parse_mode=HTML
        cannot represent. The *markdown* field accepts standard GitHub-Flavored
        Markdown including pipe-table syntax.

        Pass ``disable_notification`` when this send REPLACES a message the user
        was already notified about, so replacing a bubble does not ping twice.

        Returns None on failure so the caller can fall back to sendMessage.

        Availability is *learned*. A server that does not implement the method
        rejects every call identically, so re-probing it per table would burn a
        wasted round-trip forever; ``_rich_unsupported`` latches instead:

        * **401/403/404 and any other 4xx except 400/429** -- server- or
          auth-level, identical for every message: latch immediately.
        * **400** -- ambiguous. It is what a wrong payload shape returns (every
          call fails, so it must latch) but ALSO what one oversized or
          20+-column table returns (content-specific, so it must NOT latch or a
          single bad message disables rich rendering for the whole process).
          Resolved by counting CONSECUTIVE 400s and latching at
          ``_RICH_400_LATCH``: a wrong payload shape reaches that immediately,
          while one bad table is cleared by the next table that sends.
        * **429, 5xx, transport errors** -- transient: never latch, and clear
          the 400 streak so unrelated failures cannot accumulate into a latch.
        """
        if self._rich_unsupported:
            return None
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": {"markdown": markdown},
        }
        if message_thread_id is not None:
            params["message_thread_id"] = message_thread_id
        if reply_markup:
            params["reply_markup"] = reply_markup
        if disable_notification:
            params["disable_notification"] = True
        err: dict[str, Any] = {}
        result = await self._api("sendRichMessage", params, err_out=err)
        if result:
            self._rich_400_streak = 0
            return result.get("message_id")
        code = err.get("error_code")
        if isinstance(code, int) and 400 <= code < 500 and code != 429:
            if code == 400:
                self._rich_400_streak += 1
                if self._rich_400_streak < _RICH_400_LATCH:
                    return None
            logger.info(
                "sendRichMessage unavailable on this Bot API server (code=%s); "
                "falling back to HTML for the rest of the process.",
                code,
            )
            self._rich_unsupported = True
        else:
            # Transient (429 / 5xx / transport): keep rich enabled.
            self._rich_400_streak = 0
        return None

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

    # ── File download (attachment ingestion) ──

    #: The only host Telegram file downloads may resolve to. A redirect or
    #: different host means the URL is not from Telegram and must be refused.
    _FILE_HOST = "api.telegram.org"

    async def download_file(self, file_id: str, dest: str) -> None:
        """Download a Telegram file by ``file_id`` to *dest*.

        Two-step process per Bot API docs:
        1. ``getFile(file_id)`` → returns a ``File`` object with ``file_path``
        2. Construct ``https://api.telegram.org/file/bot<token>/<file_path>``
           and download the bytes.

        Host-allowlisted: only ``api.telegram.org`` is accepted. Redirects are
        refused so a compromised file_path cannot exfiltrate data via an open
        redirect. Errors raise token-free messages (the download URL contains
        the bot token, so aiohttp's default exception str() must never propagate).
        """
        result = await self._api("getFile", {"file_id": file_id})
        if not result or not isinstance(result, dict):
            raise ValueError(f"getFile returned no result for file_id={file_id!r}")
        file_path = result.get("file_path", "")
        if not file_path:
            raise ValueError(f"getFile returned empty file_path for file_id={file_id!r}")

        url = f"https://api.telegram.org/file/bot{self._token}/{file_path}"

        session = await self._ensure_session()
        try:
            async with session.get(
                url,
                proxy=self._proxy,
                timeout=aiohttp.ClientTimeout(total=60),
                allow_redirects=False,
            ) as resp:
                if 300 <= resp.status < 400:
                    raise ValueError("refusing redirected Telegram file URL")
                if resp.status >= 400:
                    # Token-free error: aiohttp's ClientResponseError embeds the
                    # full URL (which contains the bot token) in its str().
                    raise ValueError(
                        f"Telegram file download failed (status {resp.status})"
                    )
                # Offload file I/O to a worker thread — a large attachment on
                # slow/FUSE storage must not block the gateway event loop.
                # Mirrors discord/client.py's download_attachment pattern.
                fh = await asyncio.to_thread(open, dest, "wb")
                try:
                    async for chunk in resp.content.iter_chunked(65536):
                        await asyncio.to_thread(fh.write, chunk)
                finally:
                    await asyncio.to_thread(fh.close)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # Strip the token-bearing URL from transport exceptions.
            raise ValueError(
                f"Telegram file download transport error ({type(exc).__name__})"
            ) from None

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

    # ── Album (media group) coalescing ──

    def _buffer_album_member(self, group_id: str, inbound: TelegramInbound) -> None:
        """Hold one album member and (re)arm its flush timer.

        The timer is rearmed on every arrival, so the album flushes
        ``_ALBUM_WINDOW_S`` after the LAST member rather than the first — album
        members arrive back-to-back (usually in one getUpdates batch), so this
        settles almost immediately. ``_ALBUM_MAX_WAIT_S`` is the hard ceiling
        that stops a pathological stream which keeps appending to one group from
        deferring the flush forever.
        """
        members = self._albums.get(group_id)
        if members is None:
            # Cap concurrent groups. Every group self-flushes within
            # _ALBUM_MAX_WAIT_S, so this is defence-in-depth against a burst of
            # never-completed groups rather than an expected path. Flush the
            # oldest rather than dropping it, so no message is silently lost.
            if len(self._albums) >= _ALBUM_MAX_GROUPS:
                oldest = min(self._albums, key=lambda g: self._album_first_seen.get(g, 0.0))
                logger.warning(
                    "Telegram: album buffer at %d groups, force-flushing oldest",
                    _ALBUM_MAX_GROUPS,
                )
                self._flush_album(oldest)
            members = self._albums[group_id] = []
            self._album_first_seen[group_id] = time.monotonic()

        if len(members) < _ALBUM_MAX_MEMBERS:
            members.append(inbound)
        else:
            # Telegram's own album limit is 10, so this is unreachable for a
            # well-formed album. Count rather than grow, and surface it at flush
            # so an over-cap group is visible instead of silently truncated.
            self._album_dropped[group_id] = self._album_dropped.get(group_id, 0) + 1

        self._arm_album_timer(group_id)

    def _arm_album_timer(self, group_id: str) -> None:
        """(Re)schedule the flush for *group_id*, respecting the hard ceiling."""
        existing = self._album_timers.pop(group_id, None)
        if existing is not None and not existing.done():
            existing.cancel()
        elapsed = time.monotonic() - self._album_first_seen.get(group_id, 0.0)
        delay = min(_ALBUM_WINDOW_S, max(0.0, _ALBUM_MAX_WAIT_S - elapsed))
        task = asyncio.create_task(self._album_flush_after(group_id, delay))
        self._album_timers[group_id] = task
        # Tracked alongside handler tasks so a pending flush is not garbage
        # collected mid-flight.
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def _album_flush_after(self, group_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return  # a newer member rearmed the timer
        self._album_timers.pop(group_id, None)
        self._flush_album(group_id)

    def _flush_album(self, group_id: str) -> None:
        """Merge one buffered album into a single message and dispatch it."""
        members = self._albums.pop(group_id, None)
        self._album_first_seen.pop(group_id, None)
        dropped = self._album_dropped.pop(group_id, 0)
        timer = self._album_timers.pop(group_id, None)
        if timer is not None and not timer.done():
            timer.cancel()
        if not members:
            return

        # Usually the caption rides on exactly one member, but Telegram Desktop
        # and Android let the user caption individual items of a media group --
        # so join every non-empty caption in album order rather than taking the
        # first. For the single-caption case this is identical; for the
        # per-item case it is the difference between the model seeing all of
        # the user's words and silently seeing only the first.
        # Everything else comes from the first member: its message_id is what a
        # reply or a steer-ack reaction should target.
        head = members[0]
        text = "\n\n".join(m.text for m in members if m.text)
        attachments: list[dict[str, Any]] = []
        for member in members:
            attachments.extend(member.attachments)
        if dropped:
            logger.warning(
                "Telegram: album %s exceeded %d members; %d ignored",
                group_id,
                _ALBUM_MAX_MEMBERS,
                dropped,
            )
        merged = TelegramInbound(
            chat_id=head.chat_id,
            user_id=head.user_id,
            username=head.username,
            text=text,
            message_id=head.message_id,
            chat_type=head.chat_type,
            message_thread_id=head.message_thread_id,
            attachments=attachments,
        )
        self._spawn_handler(merged)

    def _flush_all_albums(self) -> None:
        """Best-effort flush of every buffered album, used on shutdown.

        **Not a delivery guarantee.** Shutdown runs the channel teardown and
        ``SessionManager.close_all()`` concurrently in one ``cleanup_tasks``
        gather, and ``close_all`` sets ``_closing``, after which ``begin_turn``
        raises ``SessionClosingError``. So a handler spawned here may lose the
        race and be refused.

        Kept anyway because it is free and sometimes wins, and because the
        residual is not a new failure mode: a plain single message that arrives
        just before shutdown is refused by that same ``_closing`` gate today.
        Buffering an album widens that pre-existing window by at most
        ``_ALBUM_WINDOW_S``; it does not introduce a class of loss that was not
        already there. Draining the buffer here also keeps a closed client from
        holding album state.
        """
        for group_id in list(self._albums):
            self._flush_album(group_id)

    @staticmethod
    def _build_inbound(msg: dict) -> TelegramInbound:
        """Map ONE Telegram ``message`` envelope onto ``TelegramInbound``.

        Pure and side-effect free so both the single-message path and the album
        merge path share exactly one envelope interpretation.
        """
        text = msg.get("text", "") or msg.get("caption", "")
        chat = msg.get("chat", {})
        user = msg.get("from", {})
        # Extract file attachments. Telegram delivers each media type in its
        # own top-level key. ``photo`` is an array of sizes — pick the last
        # (largest). Each attachment dict carries at minimum ``file_id``.
        attachments: list[dict[str, Any]] = []
        if "photo" in msg and msg["photo"]:
            # Largest photo is last in the array (Bot API guarantee).
            largest = msg["photo"][-1]
            # Synthesize a filename — photos have no file_name field.
            largest.setdefault("file_name", "photo.jpg")
            largest.setdefault("mime_type", "image/jpeg")
            attachments.append(largest)
        for key in ("document", "audio", "voice", "video_note", "video", "animation"):
            if key in msg and isinstance(msg[key], dict):
                attachments.append(msg[key])
        # Stickers are intentionally excluded — they are decorative, not
        # content the model should ingest.
        return TelegramInbound(
            chat_id=chat.get("id", 0),
            user_id=user.get("id", 0),
            username=user.get("username", ""),
            text=text,
            message_id=msg.get("message_id", 0),
            chat_type=chat.get("type", ""),
            message_thread_id=msg.get("message_thread_id"),
            attachments=attachments,
        )

    def _spawn_handler(self, inbound: TelegramInbound) -> None:
        """Run the message handler as a tracked background task."""
        task = asyncio.create_task(self._invoke_message(inbound))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    def _dispatch(self, update: dict) -> None:
        """Route a single Update to the appropriate handler as a background task."""
        if "message" in update:
            msg = update["message"]
            inbound = self._build_inbound(msg)
            # An album (media group) is delivered as N SEPARATE updates sharing
            # one media_group_id, with the caption on only one member. Buffer
            # them and emit a single merged message instead of N turns.
            # Keyed by (chat_id, media_group_id), NOT media_group_id alone:
            # nothing guarantees the id is unique across the chats one bot
            # serves, and a collision would merge two chats' members into one
            # message addressed to head.chat_id -- silently swallowing the other
            # chat's copy and delivering its content into the wrong
            # conversation. The composite key removes that class outright.
            group_id = msg.get("media_group_id")
            if isinstance(group_id, str) and group_id:
                self._buffer_album_member(f"{inbound.chat_id}:{group_id}", inbound)
                return
            self._spawn_handler(inbound)

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
        self,
        method: str,
        params: dict,
        timeout: int = 30,
        *,
        record: bool = True,
        err_out: dict | None = None,
    ) -> Any:
        """Call a Bot API method. Returns the 'result' field or None on error.

        Honors a single 429 ``retry_after`` back-off: a rate-limited edit that
        we simply dropped would freeze the streaming bubble until the next
        chunk, which reads as a stutter -- so we wait out the (usually short)
        cool-down once and retry instead.

        ``err_out``, when supplied, is populated with ``error_code`` and
        ``description`` on a Telegram-level failure. Callers use it to tell a
        PERMANENT failure (the method does not exist on this server) apart from
        a transient one (rate limit, network), so they can stop re-probing an
        unsupported method without disabling it on a blip.
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
                    if err_out is not None:
                        err_out["error_code"] = err_code
                        err_out["description"] = err_desc
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
