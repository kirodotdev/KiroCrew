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

``TelegramClient`` is composed from owners: one Bot API round trip (``client_http``), the
``getUpdates`` loop and its durable cursor (``client_polling``) and album coalescing
(``client_albums``). An owner function whose first parameter is ``self`` is bound as the
class attribute of the same name. This module keeps the client's state, its Bot API
methods, the update parse and handler dispatch, the Bot API limits, the reaction set and
the tag-safe HTML cap, and stays the only import path and patch surface:
:mod:`kiro_crew._facade_mirror` carries a patch of one of its names to every owner that
reads it. Which owner takes new work is recorded in
``docs/system-specs/modules/messaging.md`` (Telegram channel).
"""

from __future__ import annotations

import asyncio
import importlib as _importlib
import json
import logging
import os
import re
import sys as _sys
import time
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Sequence

import aiohttp

from kiro_crew import _facade_mirror
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.messaging.outbound_files import OutboundFile, upload_filename
from kiro_crew.metrics.provider import get_recorder
from kiro_crew.telegram import client_albums as _client_albums
from kiro_crew.telegram import client_http as _client_http
from kiro_crew.telegram import client_polling as _client_polling
from kiro_crew.telegram.client_http import _file_base, _resolve_proxy
from kiro_crew.telegram.client_polling import _CURSOR_DIRNAME, _CURSOR_FILENAME

logger = logging.getLogger("kiro_crew.telegram.client")

# Telegram message text limit.
TELEGRAM_MAX_TEXT = 4096

#: Bot API 7.0+ link_preview_options payload disabling server-side link
#: previews. ONE shared source injected into EVERY text-bearing call
#: (sendMessage, sendMessageDraft, editMessageText, sendRichMessage): a URL
#: appearing mid-stream must not gain a preview on the edit path that the
#: send path already denies — the fetch happens with no recipient click.
_LINK_PREVIEW_DISABLED: dict[str, Any] = {"is_disabled": True}

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
#: OGG/Opus, and handing it a WAV produces a 400 rather than a fallback. The
#: system engine emits WAV and Polly emits MP3, so today's synthesizers take the sendAudio
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
# Safe chunk boundary (leave room for markdown overhead).
TELEGRAM_CHUNK_LIMIT = 4000

#: sendRichMessage markdown payload limit (Bot API 10.1 Rich Messages). Far
#: larger than sendMessage's 4096. The rich path carries the segment's raw
#: markdown with no HTML render step, so source length IS payload length and
#: table-bearing segments are budgeted against this cap, not the render cap.
TELEGRAM_RICH_MAX_CHARS = 32768

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
        #: Optional teardown callback: called once from :meth:`close`, best-effort.
        #: The gateway sets it to unregister the dispatcher's spawn-approval
        #: delivery hook so the host gate stops routing to a channel that is going
        #: away (the registration is process-global; see
        #: ``messaging/spawn_approval_delivery.py``).
        self.on_close: Callable[[], None] | None = None
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

    # Bound from client_http: one Bot API round trip.
    _proxy_for = _client_http._proxy_for
    _call_raw = _client_http._call_raw
    _ensure_session = _client_http._ensure_session
    _api = _client_http._api
    _api_multipart = _client_http._api_multipart
    _api_request = _client_http._api_request
    # Bound from client_polling: getUpdates and the cursor.
    _load_offset = _client_polling._load_offset
    _discard_legacy_offset = _client_polling._discard_legacy_offset
    _save_offset = _client_polling._save_offset
    _notify_status = _client_polling._notify_status
    _poll_backoff = _client_polling._poll_backoff
    _polling_loop = _client_polling._polling_loop
    _get_updates = _client_polling._get_updates
    _resolve_updates = _client_polling._resolve_updates
    _persistable_offset = _client_polling._persistable_offset
    _maybe_persist_offset = _client_polling._maybe_persist_offset
    _persist_offset = _client_polling._persist_offset
    # Bound from client_albums: album coalescing.
    _buffer_album_member = _client_albums._buffer_album_member
    _arm_album_timer = _client_albums._arm_album_timer
    _album_flush_after = _client_albums._album_flush_after
    _flush_album = _client_albums._flush_album
    _flush_all_albums = _client_albums._flush_all_albums

    async def close(self) -> None:
        """Gracefully shut down."""
        self._closed = True
        # Retire any process-global registration this channel holds (the
        # spawn-approval delivery hook) BEFORE tearing the transport down, so the
        # host spawn gate stops routing to a dispatcher that is going away.
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:
                logger.debug("Telegram on_close callback failed", exc_info=True)
        # Best-effort flush of buffered albums BEFORE cancelling the polling
        # task. This is NOT a delivery guarantee -- see _flush_all_albums: the
        # handler it spawns races SessionManager._closing and may be refused,
        # exactly as a plain message arriving at shutdown already is today. It
        # costs nothing, sometimes wins, and drains the buffer either way.
        # Session close in a `finally` -- see DiscordClient.close() for why the
        # steps above it can raise and what leaking the session costs.
        try:
            self._flush_all_albums()
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
            "link_preview_options": _LINK_PREVIEW_DISABLED,
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
        err: dict[str, Any] = {}
        result = await self._api("sendMessage", params, err_out=err)
        if result:
            return result.get("message_id")
        # Only retry when a parse_mode was actually requested AND the caller
        # allows it. Renderers that send HTML pass retry_plain=False so a parse
        # failure never re-sends the literal tags.
        # A 400 means Telegram rejected the request, so nothing was posted and
        # the retry drops parse_mode. After any other failure (a timeout, a
        # reset, a 5xx) the message may already be in the chat. Telegram has no
        # idempotency key, so the retry keeps parse_mode: a copy of a message
        # that landed renders like the first, and one that never landed arrives.
        if parse_mode and retry_plain:
            if err.get("error_code") == 400:
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
            "link_preview_options": _LINK_PREVIEW_DISABLED,
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
        # Only the Bot API's own answer can say the method is missing. An error
        # page from an edge or a proxy (a body that is not JSON) is a failed call
        # like a 5xx: it neither counts toward the 400 streak nor latches.
        if (
            err.get("body_is_json", True)
            and isinstance(code, int)
            and 400 <= code < 500
            and code != 429
        ):
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
            "link_preview_options": _LINK_PREVIEW_DISABLED,
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
            "link_preview_options": _LINK_PREVIEW_DISABLED,
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

        Retires an ``[OPTIONS:]`` keyboard after a choice is tapped
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

    async def send_document(
        self,
        chat_id: int,
        document: "OutboundFile",
        *,
        caption: str | None = None,
        message_thread_id: int | None = None,
        disable_notification: bool = True,
    ) -> int | None:
        """Upload ONE file via ``sendDocument``. Returns the message_id or None.

        The generic-file counterpart of :meth:`send_photo`, for attachments that
        are not raster images (PDF, CSV, archives — whatever the caller's own
        gates admitted). Sent silently by default: every current caller has
        already landed a text bubble for the same turn, and a second ping for
        the same action is the pattern the other media sends avoid.

        ``filenames`` pins the document's real name: the multipart sanitizer is
        aimed at LLM-authored reference paths, and this caller's name has
        already passed the file_send gates — letting the sanitizer rewrite the
        extension would break the receiver's file-type association.
        """
        params: dict[str, Any] = {"chat_id": chat_id}
        if message_thread_id is not None:
            params["message_thread_id"] = message_thread_id
        if caption:
            params["caption"] = caption[:TELEGRAM_MAX_CAPTION]
        if disable_notification:
            params["disable_notification"] = True
        result = await self._api_multipart(
            "sendDocument",
            params,
            [document],
            field_names=["document"],
            filenames=[Path(document.path).name],
        )
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
        than degrading, and the shipped synthesizers emit WAV (system) and MP3
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

    async def download_file(self, file_id: str, dest: str) -> None:
        """Download a Telegram file by ``file_id`` to *dest*.

        Two-step process per Bot API docs:
        1. ``getFile(file_id)`` → returns a ``File`` object with ``file_path``
        2. Construct ``<file_base>/file/bot<token>/<file_path>`` and download
           the bytes, where ``<file_base>`` is the public host by default or a
           configured proxy origin (with any path prefix) via :func:`_file_base`.
           A template with no derivable download route is refused before any
           request.

        Redirects are refused so a compromised file_path cannot exfiltrate data
        via an open redirect. Errors raise token-free messages (the download URL
        contains the bot token, so aiohttp's default exception str() must never
        propagate).
        """
        base = _file_base()
        if base is None:
            raise ValueError(
                "TELEGRAM_API_BASE_URL must end in bot{token}/{method}, with no query, "
                "for file downloads"
            )
        result = await self._api("getFile", {"file_id": file_id})
        if not result or not isinstance(result, dict):
            raise ValueError(f"getFile returned no result for file_id={file_id!r}")
        file_path = result.get("file_path", "")
        if not file_path:
            raise ValueError(f"getFile returned empty file_path for file_id={file_id!r}")

        url = f"{base}/file/bot{self._token}/{file_path}"

        session = await self._ensure_session()
        try:
            async with session.get(
                url,
                proxy=self._proxy_for(url),
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
        bot does not serve. An empty list is refused rather than sent, because
        Telegram would read it as "this bot has no commands" and wipe the menu.
        """
        if not commands:
            return False
        return bool(await self._api("setMyCommands", {"commands": commands}))

    # ── Polling loop ──

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


#: BASE imports this module binds for its importers and for the patches the mirror
#: routes through it, though its own code does not read them.
_BOUND_FOR_IMPORTERS = (
    Iterable,
    atomic_write,
    get_recorder,
    json,
    os,
    replace,
    time,
    upload_filename,
    urllib,
)
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.telegram.client_http",
    "kiro_crew.telegram.client_polling",
    "kiro_crew.telegram.client_albums",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
#: The unsplit module's names: everything the family binds except the methods the class
#: binds from its owners, which were never module names.
_SURFACE = frozenset().union(globals(), *(vars(_sys.modules[o]) for o in _OWNERS)) - {
    n for n, m in vars(TelegramClient).items() if getattr(m, "__module__", "") in _OWNERS
}
_EXPORTS.update(
    _facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=(), surface=_SURFACE).served
)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.telegram.client_albums import (  # noqa: F401
        _ALBUM_MAX_GROUPS,
        _ALBUM_MAX_MEMBERS,
        _ALBUM_MAX_WAIT_S,
        _ALBUM_WINDOW_S,
    )
    from kiro_crew.telegram.client_http import (  # noqa: F401
        _API_DURATION_METRIC,
        _DEFAULT_API_BASE,
        _LOOPBACK_HOSTS,
        _METHOD_TAIL,
        _api_base,
        _is_loopback_host,
        _record_api_duration,
        _url_is_loopback,
    )
    from kiro_crew.telegram.client_polling import (  # noqa: F401
        _LEGACY_CURSOR_FILENAME,
        _POLL_BACKOFF_MAX_S,
        _STATUS_FAILURE_THRESHOLD,
    )
