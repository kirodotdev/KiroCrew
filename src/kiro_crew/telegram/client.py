"""Telegram Bot API transport layer — long-polling + message send/edit.

Inbound: long-polling loop calls getUpdates, dispatches Message and
CallbackQuery objects to the on_message / on_callback handlers.

Outbound:
  - send_message: posts a new message, returns message_id
  - edit_message: edits an existing message in-place (for streaming)
  - send_photo / send_media_group: uploads images by multipart
  - send_typing: sends "typing..." chat action
  - answer_callback: acknowledges an inline-keyboard button press
  - set_message_reaction: one allow-listed emoji reaction

No external Telegram library dependency — pure aiohttp + Bot API REST.
This keeps the module lightweight, OSS-clean, and easy to audit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

import aiohttp

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.messaging.outbound_files import OutboundFile, upload_filename
from kiro_crew.metrics.provider import get_recorder

logger = logging.getLogger(__name__)

# Telegram message text limit.
TELEGRAM_MAX_TEXT = 4096

#: Caption cap on every media-bearing send (sendPhoto, sendDocument, …),
#: measured after entities parsing — a quarter of the text budget, which is why
#: the renderer never folds an answer into a caption.
TELEGRAM_MAX_CAPTION = 1024

#: Per-file ceiling for a multipart PHOTO upload. Uploading by URL caps photos
#: at 5 MB and by ``file_id`` not at all; multipart is the transport a local
#: file has to take, so its 10 MB is the ceiling that actually binds.
TELEGRAM_MAX_PHOTO_BYTES = 10 * 1024 * 1024

#: Items ``sendMediaGroup`` accepts (the Bot API's own 2-10 range, upper bound).
TELEGRAM_MAX_MEDIA_GROUP = 10

#: Aggregate bytes one seal may hold in memory across its uploads. Ten photos
#: at the per-file ceiling would be 100 MiB resident, so the aggregate — not
#: files × per-file — is what bounds the extraction read.
TELEGRAM_MAX_TOTAL_UPLOAD_BYTES = 25 * 1024 * 1024

#: Per-file ceiling for a multipart NON-photo upload (audio, document). The Bot
#: API's own limit; separate from the photo ceiling because they genuinely differ
#: and one symbol for both would silently retune whichever was not being changed.
TELEGRAM_MAX_AUDIO_BYTES = 50 * 1024 * 1024

#: Mime types ``sendVoice`` accepts. Anything else has to go via ``sendAudio``:
#: sendVoice renders the native push-to-listen bubble but the Bot API requires
#: OGG/Opus, and handing it a WAV produces a 400 rather than a fallback. Piper
#: emits WAV and Polly emits MP3, so today's synthesizers take the sendAudio
#: path; the branch exists so an OGG-capable provider gets the better bubble
#: without another change here.
TELEGRAM_VOICE_MIMES = frozenset({"audio/ogg", "audio/opus", "audio/ogg; codecs=opus"})

#: U+FE0F, the emoji variation selector.
_VS16 = "️"

#: Every emoji ``setMessageReaction`` accepts, and there is no graceful
#: degradation for the rest: an off-list emoji is a hard 400, with no
#: nearest-match and no partial success.
#:
#: Spelled WITHOUT U+FE0F throughout, matching the Bot API reference — seven
#: members (❤, ❤‍🔥, 🕊, ✍, ☃, 🤷‍♂, 🤷‍♀) are documented bare while every
#: keyboard emits the VS16 form, and the two major Python libraries disagree
#: about which three of them carry it. So membership is tested on the
#: VS16-stripped form (:func:`normalize_reaction_emoji`) and the bare spelling
#: is what goes on the wire.
#:
#: Notably ABSENT, and therefore unusable as status marks: ✅, 🚀, ⏳, 🤖, 🌐, 🔧.
REACTION_EMOJI: frozenset[str] = frozenset(
    (
        "❤",
        "👍",
        "👎",
        "🔥",
        "🥰",
        "👏",
        "😁",
        "🤔",
        "🤯",
        "😱",
        "🤬",
        "😢",
        "🎉",
        "🤩",
        "🤮",
        "💩",
        "🙏",
        "👌",
        "🕊",
        "🤡",
        "🥱",
        "🥴",
        "😍",
        "🐳",
        "❤‍🔥",
        "🌚",
        "🌭",
        "💯",
        "🤣",
        "⚡",
        "🍌",
        "🏆",
        "💔",
        "🤨",
        "😐",
        "🍓",
        "🍾",
        "💋",
        "🖕",
        "😈",
        "😴",
        "😭",
        "🤓",
        "👻",
        "👨‍💻",
        "👀",
        "🎃",
        "🙈",
        "😇",
        "😨",
        "🤝",
        "✍",
        "🤗",
        "🫡",
        "🎅",
        "🎄",
        "☃",
        "💅",
        "🤪",
        "🗿",
        "🆒",
        "💘",
        "🙉",
        "🦄",
        "😘",
        "💊",
        "🙊",
        "😎",
        "👾",
        "🤷‍♂",
        "🤷",
        "🤷‍♀",
        "😡",
    )
)


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

#: sendRichMessage markdown payload limit (Bot API 10.1 Rich Messages). Far
#: larger than sendMessage's 4096. The rich path carries the segment's raw
#: markdown with no HTML render step, so source length IS payload length and
#: table-bearing segments are budgeted against this cap, not the render cap.
TELEGRAM_RICH_MAX_CHARS = 32768

# Bot API base URL.
_API_BASE = "https://api.telegram.org/bot{token}/{method}"

#: Consecutive polling failures before the status callback reports unhealthy.
_STATUS_FAILURE_THRESHOLD = 3

#: Ceiling on the polling loop's exponential retry delay, in seconds.
_POLL_BACKOFF_MAX_S = 30.0

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

#: Where the getUpdates cursor lives. ``routing`` is the keystone-registered
#: directory the Teams routing store already uses; see ``TelegramClient.__init__``
#: for why an intake cursor belongs behind the same fence as delivery addressing.
_CURSOR_DIRNAME = "routing"
_CURSOR_FILENAME = "telegram_offset.json"

#: The pre-keystone location, in the data-home root where the agent could write it.
#: Removed rather than migrated: a cursor at a writable path is exactly the
#: "cursor we cannot trust" that ``_load_offset`` already answers 0 for in five
#: other cases, and carrying its value forward would carry a poisoned one forward
#: too. The cost is the one bounded replay that module documents as the accepted
#: degradation -- Telegram redelivers what it still holds.
_LEGACY_CURSOR_FILENAME = "telegram_offset.json"


def normalize_reaction_emoji(emoji: str) -> str:
    """Strip U+FE0F so a keyboard's ❤️ and the docs' ❤ are one value.

    Applied to both sides of the membership test in
    :meth:`TelegramClient.set_message_reaction`, and to what is sent, so a
    pasted or config-supplied emoji is not rejected purely for carrying the
    variation selector every real keyboard adds.
    """
    return (emoji or "").replace(_VS16, "")


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


#: Bot API entity types that carry a URL the visible text does not. ``text_link``
#: is the normal encoding for anything pasted from a channel post or formatted by a
#: Telegram client; ``text_mention`` names a user with no @handle.
_URL_ENTITY_TYPES = frozenset({"text_link"})


def _flatten_text_links(text: str, entities: "list[dict[str, Any]]") -> str:
    """Append each hidden URL as ``anchor (url)``, the way Slack flattens a link.

    Telegram sends a formatted link as anchor TEXT plus a ``text_link`` entity
    holding the target, so reading ``message.text`` alone hands the model the words
    and silently drops the address. The failure is quiet in the worst way: a bare
    URL still works, so the bot looks like it is refusing the request rather than
    like it never received the link.

    Offsets are in UTF-16 code units, which is why the anchor is not sliced out of
    *text* here — a message with an emoji ahead of the link would slice the wrong
    span. The URL is appended instead, which needs no offset arithmetic and reads
    the same to a model as Slack's ``text (url)``.

    Order is the entity order Telegram sent, so several links stay distinguishable.
    Malformed entities are skipped rather than raising: this runs on every inbound
    message, and a strange entity must not cost the message.
    """
    if not text or not entities:
        return text
    urls: list[str] = []
    for ent in entities:
        if not isinstance(ent, dict) or ent.get("type") not in _URL_ENTITY_TYPES:
            continue
        url = str(ent.get("url") or "").strip()
        # A URL already visible in the text needs no second copy.
        if url and url not in text and url not in urls:
            urls.append(url)
    if not urls:
        return text
    return text + "".join(f"\n[link] {u}" for u in urls)


def _mention_handles(text: str, entities: "list[dict[str, Any]]") -> tuple[str, ...]:
    """The ``@handle``s Telegram ITSELF marked as mentions, lowercased, no ``@``.

    Telegram classifies every span it recognises, and a handle sitting inside a URL
    is a ``url`` (or ``text_link``) entity, never a ``mention``. So this is the
    authoritative answer to "was the bot addressed", and a text scan is not: the
    activation gate's own matcher happily fires on ``https://host/@thebot/x``,
    and ``_flatten_text_links`` appends a formatted link's target INTO the text,
    so anyone able to post a link could hand the scan a handle to find.

    Offsets are UTF-16 code units, which is the Bot API's unit and not Python's --
    one emoji ahead of the mention shifts every later offset by one if sliced as
    Python characters. Encoding to UTF-16-LE and slicing by byte pairs is the exact
    conversion; a surrogate-splitting offset would raise, so it is guarded and the
    entity skipped rather than costing the message its turn.

    Returns a tuple, not a set, so an empty result stays distinguishable from
    "entities were never parsed" at the call site.
    """
    if not text or not entities:
        return ()
    units = text.encode("utf-16-le")
    found: list[str] = []
    for ent in entities:
        if not isinstance(ent, dict) or ent.get("type") != "mention":
            continue
        offset, length = ent.get("offset"), ent.get("length")
        if not isinstance(offset, int) or not isinstance(length, int):
            continue
        try:
            span = units[offset * 2 : (offset + length) * 2].decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        handle = span.strip().lstrip("@").lower()
        if handle and handle not in found:
            found.append(handle)
    return tuple(found)


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
    #: Numeric id of the sender of the message this one REPLIES to, or 0. In
    #: Telegram, replying to the bot is how a participant addresses it without
    #: typing its @handle, so the activation gate needs it; the comparison against
    #: the bot's own id happens in the dispatcher, which is where ``getMe`` lands.
    reply_to_user_id: int = 0
    #: Raw file attachment dicts extracted from the Telegram update (photo,
    #: document, audio, voice, video_note, video, animation). Each dict carries
    #: at minimum ``file_id`` and ``file_unique_id``; optional fields include
    #: ``file_size``, ``mime_type``, ``file_name``, and ``width``/``height``.
    attachments: list[dict[str, Any]] = field(default_factory=list)
    #: Handles Telegram marked as ``mention`` entities, lowercased and without the ``@``.
    #: The activation gate reads this rather than scanning the text, because a
    #: handle inside a URL is not a mention and a text scan cannot tell.
    #: ``has_entities`` distinguishes "no mentions" from "never parsed".
    mentions: tuple[str, ...] = ()
    #: Whether the update carried an entity list at all. A synthesized message
    #: (an album merge, a button press) has none, and the gate must not read an
    #: absent list as "nobody was mentioned".
    has_entities: bool = False


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


def _apply_reply_target(params: dict[str, Any], reply_to_message_id: int | None) -> None:
    """Attach *reply_to_message_id* to *params* as Bot API ``reply_parameters``.

    One copy for every send that can open a turn (``sendMessage`` and
    ``sendRichMessage``), because the ``allow_sending_without_reply`` decision is
    the interesting half and must not diverge between them: a user who deletes the
    message they asked in would otherwise get the answer on one path and silence on
    the other.
    """
    if not reply_to_message_id:
        return
    params["reply_parameters"] = {
        "message_id": reply_to_message_id,
        "allow_sending_without_reply": True,
    }


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
        offset_path: "Path | None" = None,
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
        #: getUpdates cursor: the lowest update_id we still want. Calling
        #: getUpdates with it is ALSO the ack for everything below it, so an
        #: in-memory-only cursor means a restart re-requests from 0 and Telegram
        #: redelivers every update the previous process never got to confirm — the
        #: user's last messages arrive a second time as fresh turns. Persisted for
        #: that reason, on the same reasoning as the iMessage watch cursor.
        self._offset: int = 0
        # Under ``routing/``, which ``security`` registers as a keystone leaf, so the
        # agent can neither read nor write it. Not because it is a secret -- it is a
        # bot id and an integer -- but because writing it STEERS THE OPERATOR'S
        # INTAKE: calling getUpdates with an offset is also the ack for everything
        # below it, so a plausible-looking large value makes the gateway skip every
        # queued and future message, and it survives the restart that would
        # otherwise clear it. Same class of control as the Teams routing store beside
        # it: not a credential, but delivery plumbing an agent must not aim.
        #
        # The DIRECTORY is what is registered, and that matters here too:
        # ``_save_offset`` publishes through an ``atomic_write`` temp sibling in the
        # same parent, so a file-name leaf would leave the pre-rename window open.
        self._offset_path = offset_path or (data_home() / _CURSOR_DIRNAME / _CURSOR_FILENAME)
        # The bot this cursor belongs to. ``update_id`` sequences are PER BOT, so a
        # cursor recorded under one token and applied to another either skips
        # everything below a higher foreign offset or replays from a lower one.
        # The bot id is the token's public prefix (``<bot_id>:<secret>``) and is
        # not itself a credential, so it is safe to persist beside the offset.
        self._bot_id = token.partition(":")[0]
        #: Last value written, so an idle poll (offset unchanged) writes nothing.
        self._offset_saved: int = 0
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
        #: update_ids dispatched to a handler that has not finished. The persisted
        #: cursor holds at the oldest of these, so a crash mid-turn replays that
        #: turn rather than losing the message. See _persistable_offset.
        self._in_flight: set[int] = set()
        #: Serializes the cursor write. Created lazily per running loop, since the
        #: client is constructed before the loop exists in several call paths.
        self._offset_lock_obj: "asyncio.Lock | None" = None
        # Album (media group) coalescing buffers, keyed by media_group_id.
        self._albums: dict[str, list[TelegramInbound]] = {}
        self._album_timers: dict[str, asyncio.Task[None]] = {}
        self._album_first_seen: dict[str, float] = {}
        self._album_dropped: dict[str, int] = {}
        #: group_id -> the update_ids buffered for it. The merged flush resolves
        #: them together, so a crash while an album is settling replays the whole
        #: album rather than losing whichever members had already arrived.
        self._album_updates: dict[str, list[int]] = {}

    # ── Lifecycle ──

    async def start(self) -> None:
        """Launch the background polling loop, resuming the persisted cursor."""
        self._closed = False
        # Off-loop, and BEFORE the load, so a boot that reads the fenced cursor is
        # also the boot that clears the unfenced one. Both are filesystem calls that
        # can block on slow storage.
        await asyncio.to_thread(self._discard_legacy_offset)
        self._offset = self._offset_saved = await asyncio.to_thread(self._load_offset)
        if self._offset:
            logger.info("Telegram: resuming getUpdates at offset %d", self._offset)
        self._task = asyncio.create_task(self._polling_loop())

    def _load_offset(self) -> int:
        """The persisted getUpdates cursor, or 0 when there is none to trust.

        Every failure mode — absent file, unreadable, non-UTF-8, non-JSON, wrong
        shape, negative — answers 0, which is exactly the pre-persistence
        behaviour: Telegram redelivers what it still holds and the operator sees
        a bounded replay. Blocking; callers run it off the loop.
        """
        try:
            data = json.loads(self._offset_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return 0
        if not isinstance(data, dict):
            return 0
        # A cursor from a DIFFERENT bot is not ours to resume; start clean rather
        # than apply another bot's id space. An older file with no recorded bot is
        # also refused: we cannot tell whose it is.
        if data.get("bot_id") != self._bot_id:
            return 0
        value = data.get("offset")
        # bool is an int subclass, and True would read as offset 1.
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return value

    def _discard_legacy_offset(self) -> None:
        """Remove a pre-keystone cursor from the data-home root. Never raises.

        Not migrated. A cursor at a path the agent can write is the same
        "cursor we cannot trust" that :meth:`_load_offset` answers 0 for in five
        other cases, and reading it forward would read a poisoned value forward
        with it. Deleting it costs one bounded replay on the first boot after the
        upgrade, which is the degradation that method already documents.

        Only ever removes the exact legacy filename in the data-home ROOT, so it
        cannot touch the live cursor now under ``routing/``.
        """
        legacy = data_home() / _LEGACY_CURSOR_FILENAME
        if legacy == self._offset_path:
            return
        try:
            legacy.unlink(missing_ok=True)
        except OSError:
            logger.debug("Telegram: could not remove the legacy offset file", exc_info=True)

    def _save_offset(self, offset: int) -> None:
        """Persist the cursor atomically. Blocking; callers run it off the loop.

        A write failure is logged and swallowed: a read-only or full data home
        must not stop message delivery, and the cost is one bounded replay window
        on the next restart rather than a dead channel.
        """
        try:
            self._offset_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(
                self._offset_path,
                json.dumps({"bot_id": self._bot_id, "offset": offset}),
            )
        except OSError:
            logger.debug("Telegram: offset persist failed", exc_info=True)

    async def close(self) -> None:
        """Gracefully shut down."""
        self._closed = True
        # Best-effort flush of buffered albums BEFORE cancelling the polling
        # task. This is NOT a delivery guarantee -- see _flush_all_albums: the
        # handler it spawns races SessionManager._closing and may be refused,
        # exactly as a plain message arriving at shutdown already is today. It
        # costs nothing, sometimes wins, and drains the buffer either way.
        self._flush_all_albums()
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
        disable_notification: bool = False,
    ) -> int | None:
        """Send a new message. Returns the message_id on success.

        Default is plaintext: the agent emits markdown/plaintext, not HTML, so
        sending with parse_mode=HTML would make any bare ``<``/``>``/``&`` trip a
        Telegram 400 and force a second round-trip. Callers that generate real
        markup (e.g. a static help card) may pass parse_mode explicitly.

        ``message_thread_id`` targets a supergroup forum Topic; it is included
        only when set, so DM sends are byte-for-byte unchanged.

        ``disable_notification`` suppresses the push for a message the user was
        already notified about by an earlier one in the same turn (a reasoning
        blockquote posted after its answer, an image posted after its bubble), so
        one reply does not buzz twice.
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
        if disable_notification:
            params["disable_notification"] = True
        _apply_reply_target(params, reply_to_message_id)
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
        reply_to_message_id: int | None = None,
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
        _apply_reply_target(params, reply_to_message_id)
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

    async def set_message_reaction(self, chat_id: int, message_id: int, emoji: str) -> bool:
        """Set a single emoji reaction on a message (Bot API 7.0+ ``setMessageReaction``).

        Used as an instant, no-extra-bubble acknowledgement that a mid-turn steer
        was received. Returns whether Telegram accepted the reaction.

        The emoji is validated against :data:`REACTION_EMOJI` and normalized
        first (see :func:`normalize_reaction_emoji`), so an off-list emoji is
        refused HERE with a log line rather than becoming a hard 400 on every
        turn that reacts. Best-effort either way: callers treat a False as
        non-fatal, because passing the global allow-list is necessary but not
        sufficient — a chat's own ``available_reactions`` can narrow it further,
        and a bot may hold only one reaction per message.
        """
        normalized = normalize_reaction_emoji(emoji)
        if normalized not in REACTION_EMOJI:
            logger.warning(
                "Telegram: refusing reaction %r — not one of the %d emoji "
                "setMessageReaction accepts.",
                emoji,
                len(REACTION_EMOJI),
            )
            return False
        result = await self._api(
            "setMessageReaction",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction": [{"type": "emoji", "emoji": normalized}],
            },
        )
        return result is not None

    # ── File upload (outbound images) ──

    async def send_photo(
        self,
        chat_id: int,
        photo: "OutboundFile",
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
        message_thread_id: int | None = None,
        disable_notification: bool = False,
    ) -> int | None:
        """Upload ONE image via ``sendPhoto``. Returns the message_id or None."""
        params: dict[str, Any] = {"chat_id": chat_id}
        if message_thread_id is not None:
            params["message_thread_id"] = message_thread_id
        if caption:
            params["caption"] = caption[:TELEGRAM_MAX_CAPTION]
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup:
            params["reply_markup"] = reply_markup
        if disable_notification:
            params["disable_notification"] = True
        result = await self._api_multipart("sendPhoto", params, [photo], field_names=["photo"])
        return result.get("message_id") if isinstance(result, dict) else None

    async def send_voice(
        self,
        chat_id: int,
        audio: bytes,
        *,
        filename: str,
        mime: str,
        caption: str | None = None,
        message_thread_id: int | None = None,
        disable_notification: bool = True,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        """Upload a synthesized spoken reply. Returns the message_id or None.

        Routes to ``sendVoice`` for OGG/Opus — the native push-to-listen bubble —
        and to ``sendAudio`` otherwise. The split is a Bot API constraint, not a
        preference: ``sendVoice`` rejects anything but OGG/Opus with a 400 rather
        than degrading, and the shipped synthesizers emit WAV (Piper) and MP3
        (Polly). ``sendAudio`` accepts both and renders a player, which is a worse
        bubble than a voice note and a much better one than an error.

        Sent silently by default. The text answer landed first and already pinged
        the user; a second notification for the same turn is the thing that makes a
        chat with voice replies on feel broken, and Telegram's rate limit is per
        chat and already spent on the streaming edits.

        Refuses over ``TELEGRAM_MAX_AUDIO_BYTES`` rather than uploading into a
        413 — the caller gets None and keeps the text reply it already sent.
        """
        if not audio:
            return None
        if len(audio) > TELEGRAM_MAX_AUDIO_BYTES:
            logger.warning(
                "telegram: refusing a %d-byte voice reply (ceiling %d)",
                len(audio),
                TELEGRAM_MAX_AUDIO_BYTES,
            )
            return None
        as_voice = mime.lower() in TELEGRAM_VOICE_MIMES
        method = "sendVoice" if as_voice else "sendAudio"
        field = "voice" if as_voice else "audio"
        params: dict[str, Any] = {"chat_id": chat_id}
        if message_thread_id is not None:
            params["message_thread_id"] = message_thread_id
        if caption:
            params["caption"] = caption[:TELEGRAM_MAX_CAPTION]
        if disable_notification:
            params["disable_notification"] = True
        _apply_reply_target(params, reply_to_message_id)
        # A local OutboundFile purely as the multipart carrier: `filenames`
        # overrides the untrusted-name sanitizer, which is aimed at LLM-authored
        # paths and would rewrite this generated temp name's extension.
        carrier = OutboundFile(path=filename, data=audio, alt="", mime=mime)
        result = await self._api_multipart(
            method, params, [carrier], field_names=[field], filenames=[filename]
        )
        return result.get("message_id") if isinstance(result, dict) else None

    async def send_media_group(
        self,
        chat_id: int,
        photos: "Sequence[OutboundFile]",
        *,
        message_thread_id: int | None = None,
        disable_notification: bool = False,
    ) -> list[int]:
        """Upload 2-10 images as one album via ``sendMediaGroup``.

        Returns the message_ids Telegram created (empty on failure). An album
        carries no ``reply_markup`` — the Bot API has no such field on this
        method — so a caller with a keyboard puts it on its own message.

        Each part is referenced by an ``attach://<name>`` descriptor whose name
        is built where the part is added, so a descriptor can never name a part
        that is not in the body.
        """
        items = list(photos)[:TELEGRAM_MAX_MEDIA_GROUP]
        if not items:
            return []
        if len(items) == 1:
            mid = await self.send_photo(
                chat_id,
                items[0],
                message_thread_id=message_thread_id,
                disable_notification=disable_notification,
            )
            return [mid] if mid is not None else []
        field_names = [f"file{index}" for index in range(len(items))]
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "media": [{"type": "photo", "media": f"attach://{name}"} for name in field_names],
        }
        if message_thread_id is not None:
            params["message_thread_id"] = message_thread_id
        if disable_notification:
            params["disable_notification"] = True
        result = await self._api_multipart("sendMediaGroup", params, items, field_names=field_names)
        if not isinstance(result, list):
            return []
        return [
            entry["message_id"]
            for entry in result
            if isinstance(entry, dict) and isinstance(entry.get("message_id"), int)
        ]

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
                    raise ValueError(f"Telegram file download failed (status {resp.status})")
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

    def _poll_backoff(self, attempt: int, reason: str) -> float:
        """The retry delay for consecutive-failure *attempt*, reporting unhealthy
        once the streak reaches ``_STATUS_FAILURE_THRESHOLD``.

        Called on EVERY failure with the ALREADY-incremented count, so the first
        one waits a second. *reason* reaches the status callback only on the
        attempt that crosses the threshold, which is what keeps a single blip
        from flipping the settings badge.
        """
        if attempt == _STATUS_FAILURE_THRESHOLD:
            self._notify_status(False, reason)
        return min(1.0 * (2 ** (attempt - 1)), _POLL_BACKOFF_MAX_S)

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
                    await asyncio.sleep(
                        self._poll_backoff(
                            attempt, "getUpdates rejected by Telegram (check the bot token)"
                        )
                    )
                    continue
                # Deduped in _notify_status: only an actual unhealthy→healthy
                # transition (incl. recovery from an offline boot) fires.
                self._notify_status(True, "")
                attempt = 0  # reset on success
                for update in updates:
                    self._dispatch(update)
                # AFTER the whole batch is dispatched, so every update that became
                # a live handler is already registered in flight and the low-water
                # mark holds behind the oldest of them. An empty batch, or one whose
                # updates were all of a kind nothing handles, advances the cursor
                # here — which is what stops an undeliverable update replaying for
                # the life of the install.
                self._maybe_persist_offset()
            except asyncio.CancelledError:
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                if self._closed:
                    break
                attempt += 1
                delay = self._poll_backoff(
                    attempt, f"getUpdates transport error ({type(exc).__name__})"
                )
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
            # The in-memory cursor advances on OBSERVATION, because that is what the
            # next getUpdates call has to send to avoid refetching the batch. It is
            # deliberately NOT persisted here: at this point the batch has not been
            # dispatched, so nothing is registered in flight and the low-water mark
            # would equal the observed cursor — writing it would be the same
            # lose-the-message bug in a different place. The polling loop persists
            # once the whole batch is registered.
            return result
        return []

    # ── Album (media group) coalescing ──

    def _buffer_album_member(
        self, group_id: str, inbound: TelegramInbound, *, update_id: int = 0
    ) -> None:
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

        if update_id:
            self._album_updates.setdefault(group_id, []).append(update_id)
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
        self._track(task)

    async def _album_flush_after(self, group_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return  # a newer member rearmed the timer
        self._album_timers.pop(group_id, None)
        self._flush_album(group_id)

    def _flush_album(self, group_id: str) -> None:
        """Merge one buffered album into a single message and dispatch it."""
        pending = self._album_updates.pop(group_id, [])
        members = self._albums.pop(group_id, None)
        self._album_first_seen.pop(group_id, None)
        dropped = self._album_dropped.pop(group_id, 0)
        timer = self._album_timers.pop(group_id, None)
        if timer is not None and not timer.done():
            timer.cancel()
        if not members:
            # Nothing to deliver, so every id this group was holding is resolved —
            # otherwise an emptied group would pin the cursor for the process's life.
            self._resolve_updates(pending)
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
        # Derived from the HEAD rather than enumerated field by field. An album is
        # the head message with more photos and a joined caption, so the only things
        # the merge decides are those two — everything else is the head's identity
        # and has to survive verbatim. Enumerating meant a field added to
        # TelegramInbound was silently dropped here: `reply_to_user_id` went missing
        # that way, and a reply-to-the-bot album in a mention-mode forum Topic was
        # then discarded by the activation gate with no trace. `replace` carries a
        # new field by construction, so the class cannot recur.
        # `mentions` and `has_entities` join that short list because both describe
        # the TEXT, which the merge does decide: a mention in a non-head member's
        # caption is in the joined text, so keeping only the head's would let
        # mention-only activation drop an album that addressed the bot.
        merged = replace(
            head,
            text=text,
            attachments=attachments,
            mentions=tuple(dict.fromkeys(h for m in members for h in m.mentions)),
            has_entities=any(m.has_entities for m in members),
        )
        # The ids go WITH the merged message rather than being resolved here:
        # _spawn_handler only creates a task, so acking at this point would advance
        # the cursor past an album whose turn has not run. The handler resolves them
        # as a unit in its finally, because replaying half an album would deliver the
        # same photos again under a caption that no longer matches.
        self._spawn_handler(merged, tuple(pending))

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
        raw_text = msg.get("text", "") or msg.get("caption", "")
        entities = msg.get("entities") or msg.get("caption_entities") or []
        # Mentions come from the RAW text: `_flatten_text_links` appends a
        # formatted link's target, which would shift every later offset.
        mentions = _mention_handles(raw_text, entities)
        text = _flatten_text_links(raw_text, entities)
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
            reply_to_user_id=int(
                ((msg.get("reply_to_message") or {}).get("from") or {}).get("id", 0) or 0
            ),
            attachments=attachments,
            mentions=mentions,
            has_entities=bool(entities),
        )

    @property
    def _offset_lock(self) -> asyncio.Lock:
        """The cursor-write lock, bound to the loop that first asks for it.

        Lazy because the client is constructed outside a running loop in several
        paths, and an ``asyncio.Lock`` created there binds to the wrong loop.
        """
        if self._offset_lock_obj is None:
            self._offset_lock_obj = asyncio.Lock()
        return self._offset_lock_obj

    def _resolve_updates(self, update_ids: "Iterable[int]") -> None:
        """Mark *update_ids* handled, and persist the cursor if that advanced it.

        Called from a handler task's ``finally``, so it runs whether the turn
        answered, was refused by authorization, or raised. All three are TERMINAL:
        the update will never be worth redelivering, which is exactly what the
        cursor is allowed to skip past.
        """
        self._in_flight.difference_update(update_ids)
        self._maybe_persist_offset()

    def _persistable_offset(self) -> int:
        """The cursor it is SAFE to resume from: the oldest unresolved update.

        WHAT THIS DOES NOT DO, stated first because the name invites the wrong
        reading: it is **not** a delivery guarantee across a crash. ``getUpdates``
        is Telegram's own acknowledgement — calling it with ``offset=N`` confirms
        everything below N SERVER-SIDE, and there is no API call to un-confirm. So
        once the loop polls again, an in-flight update is gone from Telegram
        whatever this file says, and a crash mid-turn loses that message. Closing
        that would mean either serializing inbound behind turn completion (a long
        turn would then block ``/stop``) or durably storing the update PAYLOAD
        before confirming it, which is a different feature: a persistent inbound
        queue. This cursor bounds DUPLICATE REPLAY, not loss.

        Within that scope, two failure modes have to be avoided at once:

        * Persisting what was OBSERVED replays nothing but re-answers nothing
          either: on restart the process resumes past updates it never handled, so
          any that Telegram had NOT yet confirmed are skipped too.
        * Persisting only what COMPLETED replays forever. An update the gateway
          deliberately never turns into a turn (a sticker, an unauthorized sender)
          has no completion to wait for, so a strict high-water mark on success
          would redeliver it on every restart for the life of the install.

        A low-water mark resolves both, because "resolved" covers refusal as well as
        delivery: advance to the observed cursor when nothing is in flight, and hold
        at the oldest in-flight id otherwise. The replay window is then exactly the
        set of turns that were still running — the smallest honest answer, and a
        strict improvement on tracking nothing, which is what this channel did
        before.
        """
        if not self._in_flight:
            return self._offset
        return min(self._in_flight)

    def _maybe_persist_offset(self) -> None:
        """Persist the safe cursor when it advanced. Fire-and-forget, off the loop."""
        if self._persistable_offset() <= self._offset_saved:
            return
        self._track(asyncio.create_task(self._persist_offset()))

    async def _persist_offset(self) -> None:
        """Write the safe cursor, serialized and monotonic.

        Two concurrent turns finishing hand two writes to the thread pool, and
        nothing orders those threads: the lower value can land last and the file
        REGRESSES, which on the next restart replays turns that had already been
        answered. Both halves are needed to stop that, and neither alone is enough:

        * the lock serializes the writes, so two are never in the pool at once;
        * the value is recomputed INSIDE the lock, and refused if it is not an
          advance. Recomputing is what makes the write current rather than whatever
          was true when the task was created, and the monotonic guard is a property
          of the file rather than an assumption about scheduling — so it holds even
          if a future caller reaches this from somewhere the lock does not cover.
        """
        async with self._offset_lock:
            safe = self._persistable_offset()
            if safe <= self._offset_saved:
                return
            self._offset_saved = safe
            # A small write on a slow disk still stalls every other task on this
            # single loop, so it goes to a thread.
            await asyncio.to_thread(self._save_offset, safe)

    def _track(self, task: asyncio.Task[None]) -> None:
        """Hold a strong reference to *task* until it finishes.

        asyncio keeps only a weak reference to a running task, so an untracked
        one can be garbage collected mid-flight and silently drop what it was
        carrying: an inbound turn, a pending album flush, or an inline-button
        callback. The last is the costliest to lose -- a button press is an
        approval or an option choice, so dropping it leaves the turn waiting on
        a decision that never arrives.
        """
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    def _spawn_handler(self, inbound: TelegramInbound, update_ids: "tuple[int, ...]" = ()) -> None:
        """Run the message handler as a tracked background task.

        *update_ids* are registered in flight BEFORE the task is created, so the
        cursor cannot advance past them in the window before the task starts
        running, and are resolved in the task's own ``finally``.

        A tuple rather than one id because an album is N updates delivered as ONE
        message: the merged handler is responsible for all of them, and resolving
        them at the flush site instead would ack them before the turn it merged them
        into has run. One shape for both paths, so neither can drift.
        """
        self._in_flight.update(update_ids)
        self._track(asyncio.create_task(self._invoke_message(inbound, update_ids)))

    def _dispatch(self, update: dict) -> None:
        """Route a single Update to the appropriate handler as a background task.

        Every branch that spawns work registers the update as in flight so the
        persisted cursor holds behind it; a branch that deliberately handles nothing
        (an unrecognised update kind) leaves it unregistered, which is what stops a
        permanently-undeliverable update from pinning the cursor forever.
        """
        update_id = int(update.get("update_id", 0) or 0)
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
                # Buffered, not spawned: the member is held until the album settles.
                # Registered anyway, so a crash during the buffer window replays the
                # album rather than losing the photos; the merged flush resolves
                # every member's id together.
                if update_id:
                    self._in_flight.add(update_id)
                self._buffer_album_member(
                    f"{inbound.chat_id}:{group_id}", inbound, update_id=update_id
                )
                return
            self._spawn_handler(inbound, (update_id,) if update_id else ())

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
                message_thread_id=msg.get("message_thread_id"),
            )
            press_ids = (update_id,) if update_id else ()
            self._in_flight.update(press_ids)
            self._track(asyncio.create_task(self._invoke_callback(callback, press_ids)))

    async def _invoke_message(
        self, inbound: TelegramInbound, update_ids: "tuple[int, ...]" = ()
    ) -> None:
        try:
            if self._on_message is None:
                # Nothing this update could ever become, so holding the cursor on it
                # would replay it on every restart.
                return
            await self._on_message(inbound)
        except Exception:
            logger.exception("Telegram on_message handler raised for user=%s", inbound.user_id)
        finally:
            # In `finally`, so a raising handler resolves too: a turn that crashed
            # will crash again on replay, and holding the cursor would wedge every
            # later message behind it.
            self._resolve_updates(update_ids)

    async def _invoke_callback(
        self, callback: TelegramCallback, update_ids: "tuple[int, ...]" = ()
    ) -> None:
        try:
            if self._on_callback:
                await self._on_callback(callback)
        except Exception:
            logger.exception("Telegram on_callback handler raised")
        finally:
            # A button press is an approval or an option choice, so losing one leaves
            # a turn waiting on a decision that never arrives — which is why the
            # cursor holds behind it until it is resolved either way.
            self._resolve_updates(update_ids)

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
        """Call a Bot API method with a JSON body. Returns 'result' or None.

        Thin wrapper over :meth:`_api_request` with a JSON body; see there for
        the 429 back-off and ``err_out`` semantics.
        """
        return await self._api_request(
            method,
            lambda: {"json": params},
            timeout=timeout,
            record=record,
            err_out=err_out,
        )

    async def _api_multipart(
        self,
        method: str,
        params: dict,
        files: "Sequence[OutboundFile]",
        *,
        field_names: "Sequence[str]",
        filenames: "Sequence[str] | None" = None,
        timeout: int = 120,
        err_out: dict | None = None,
    ) -> Any:
        """Call a Bot API method with a ``multipart/form-data`` body.

        Uploading by multipart is the only transport with the useful ceiling —
        by-URL caps photos at 5 MB, multipart at 10 MB — and it is the only one
        that works at all for a local file the Bot API has never seen.

        Scalar params are sent as form fields and non-scalars are JSON-encoded,
        which is what the Bot API asks for ("all queries must be made using
        UTF-8"; nested objects such as ``reply_markup`` and ``media`` travel as
        JSON strings). *field_names* is positional against *files*, so the
        caller decides whether a part is ``photo`` or ``document`` or an
        ``attach://`` name a ``media`` descriptor refers to.

        *filenames*, when supplied, is used verbatim in place of
        :func:`upload_filename`. That helper exists to sanitize and re-scan a name
        that came out of LLM-authored reply text; a file this process generated
        itself — a synthesized voice reply in a temp dir — has no such provenance,
        and putting it through a raster-oriented extension mapping would rename it
        to something the Bot API then rejects. Omit it for anything whose name the
        model influenced.

        The body is rebuilt per attempt because an aiohttp ``FormData`` is
        consumed as it is written — replaying one sends an empty body, which
        Telegram answers with a 400 that reads like a payload-shape bug.
        """

        def _body() -> dict:
            form = aiohttp.FormData()
            for key, value in params.items():
                if isinstance(value, bool):
                    form.add_field(key, "true" if value else "false")
                elif isinstance(value, (int, float, str)):
                    form.add_field(key, str(value))
                else:
                    form.add_field(key, json.dumps(value))
            for index, (name, item) in enumerate(zip(field_names, files)):
                form.add_field(
                    name,
                    item.data,
                    filename=(
                        filenames[index]
                        if filenames is not None and index < len(filenames)
                        else upload_filename(item, index)
                    ),
                    content_type=item.mime or "application/octet-stream",
                )
            return {"data": form}

        return await self._api_request(method, _body, timeout=timeout, err_out=err_out)

    async def _api_request(
        self,
        method: str,
        body: "Callable[[], dict]",
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

        *body* is a FACTORY, not a body: it is called once per attempt so a
        streamed request body (a multipart form) is rebuilt for the retry
        instead of replayed empty.
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
                    proxy=self._proxy,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    **body(),
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
