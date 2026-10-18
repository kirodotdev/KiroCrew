"""Layer 2b -- Telegram ``Renderer`` + interactive approval decider.

``TelegramRenderer`` maps the channel-neutral ``OutputEvent`` stream (routed by
the base :class:`Renderer`'s ``dispatch``) onto Telegram's Bot API:

* ``on_turn_start`` -- typing indicator, refreshed for the turn's duration; the
  same tick publishes a stall mark when nothing has moved for a while.
* ``on_text_chunk`` -- throttled ``editMessageText`` streaming (typewriter),
  with any trailing ``[OPTIONS:]`` markup and any local image markup held back
  from the visible stream.
* ``on_thinking`` -- accumulated and posted once as an expandable blockquote,
  only when ``telegram.show_thinking`` is on.
* ``on_tool_call`` -- a transient ``🔧 {tool}…`` footer.
* ``on_prompt_choice`` -- inline Approve/Deny buttons as a SEPARATE message
  (so streaming edits don't clobber them); byte-safe ``callback_data``.
* ``on_compaction`` -- a lightweight "compacting…" note.
* ``on_done`` -- the final edit, splitting long output at the capability's
  char cap, attaching the ``[OPTIONS:]`` inline keyboard to the last chunk, and
  uploading any local image the answer referenced as its own follow-up message.

``TelegramApprovalDecider`` is the interactive ladder's awaiter: ``__call__``
registers a Future keyed by ``session:request_id`` and awaits a button press,
denying by default on timeout; the callback handler resolves it via
``resolve_global``.

Dependency direction is ``telegram -> messaging`` (allowed).

``TelegramRenderer`` is composed from owners: display-form redaction, UTF-16 measures
and the option and steer strips (``renderer_display``), table structure
(``renderer_tables``), the approval prompt and ``TelegramApprovalDecider``
(``renderer_approval``), the typing indicator, stall marks and footers
(``renderer_status``), chunk intake and the live frame (``renderer_live``) and outbound
images (``renderer_uploads``). An owner function whose first parameter is ``self`` is
bound as the class attribute of the same name. This module keeps the renderer's state,
rotation, the credential-aware splitters, every sealing path, the markdown translations,
the inline keyboard and finalization, and stays the only import path and patch surface:
:mod:`kiro_crew._facade_mirror` carries a patch of one of its names to every owner that
reads it. Which owner takes new work is recorded in
``docs/system-specs/modules/messaging.md`` (Telegram channel).
"""

from __future__ import annotations

import asyncio
import html
import importlib as _importlib
import logging
import os
import re
import sys as _sys
import time
from typing import TYPE_CHECKING, Any

from kiro_crew import _facade_mirror
from kiro_crew.constants import (
    DENY_CAUSE_APPROVAL_TIMEOUT,
    split_trailing_protocol_suffix,
    strip_control_comments,
)
from kiro_crew.messaging.approval import APPROVAL_TIMEOUT_S, adoptable_reservation
from kiro_crew.messaging.display_safety import (
    TELEGRAM_FALLBACK_BOLD_STAR,
    TELEGRAM_FALLBACK_BOLD_USCORE,
    TELEGRAM_FALLBACK_FENCE,
    TELEGRAM_FALLBACK_HEADING,
    TELEGRAM_FALLBACK_INLINE_CODE,
    TELEGRAM_FALLBACK_LINK,
    TELEGRAM_FALLBACK_LINK_TEXT,
    redact_for_display,
    safe_split_offset,
    severs_a_credential,
    telegram_fallback_heading_text,
)
from kiro_crew.messaging.outbound_files import (
    ExtractLimits,
    OutboundFile,
    Rejection,
    extract_local_refs_off_loop,
    hide_local_refs,
    protected_ref_spans,
)
from kiro_crew.messaging.renderer import (
    Renderer,
    _default_redactor,
    apply_options_cap,
    count_redaction_tags,
    new_approval_nonce,
    nonce_eq,
    redaction_notice,
    repaired_after_a_sent_tail,
    session_provenance_tag,
    split_options_trailer,
)
from kiro_crew.messaging.split import (
    bounded_for_delivery,
    repaired_for_delivery,
    split_markdown_safe,
)
from kiro_crew.messaging.tables import display_width
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.sel import sel
from kiro_crew.telegram import renderer_approval as _renderer_approval
from kiro_crew.telegram import renderer_live as _renderer_live
from kiro_crew.telegram import renderer_status as _renderer_status
from kiro_crew.telegram import renderer_uploads as _renderer_uploads
from kiro_crew.telegram.client import (
    TELEGRAM_MAX_MEDIA_GROUP,
    TELEGRAM_MAX_PHOTO_BYTES,
    TELEGRAM_MAX_TOTAL_UPLOAD_BYTES,
    TELEGRAM_RICH_MAX_CHARS,
)
from kiro_crew.telegram.renderer_display import (
    _MIN_SPLIT_LIMIT,
    _delivered_form,
    _display_safe,
    _extract_options,
    _may_exceed_rendered,
    _neutralize_md,
    _shrunk_limit,
)
from kiro_crew.telegram.renderer_tables import (
    _FENCE_LINE_RE,
    _has_table,
    _split_table_rows,
    _table_blocks,
)

if TYPE_CHECKING:
    from kiro_crew.telegram.client import TelegramClient

logger = logging.getLogger("kiro_crew.telegram.renderer")

#: How far past one message a segment may grow while it holds an image reference
#: back from length rotation. The hold exists so a cut cannot bisect
#: ``![alt](path)``; without a ceiling it also means a reference arriving early in
#: a long answer disables rotation for the REST of the turn, so the buffer grows
#: unbounded and every later chunk re-scans all of it. Past this the hold is
#: abandoned and the segment rotates normally — the reference then keeps its
#: markup and prints its path, which is the documented honest degradation.
_UPLOAD_HOLD_LIMIT_FACTOR = 4

# Fallback placeholder for a turn that failed without a user-safe reason. The
# retry wording is only correct for transient failures; a permanent failure
# (e.g. the account lacks the selected model) passes its own bounded reason via
# ``close(failure_reason=...)`` so the user is never told to retry an error
# that says retrying will not help.
_GENERIC_ERROR_TEXT = "⚠️ Error — please try again"
#: Sent in place of a part of a reply whose every send failed, so the later parts
#: do not read as the whole reply.
_LOST_PART_NOTICE = "Part of this reply may not have been delivered."


def md_to_telegram_html_safe(text: str) -> str:
    """Redact against the rendered form, THEN translate markdown to HTML.

    The pairing is the point. ``_md_to_telegram_html`` is the vector
    :func:`_display_safe` exists to close: it strips the ``**`` out from inside a
    split credential and emits the two halves as one rendered key. Every sink that
    introduces tags therefore owes the redaction first, and a sink that calls the
    translator directly is one that silently does not.

    So the translator stays private to this module and this is what leaves it: the
    two steps cannot be reordered, and adding a third markdown sink cannot mean
    adding a third place to remember the order. Callers on the event loop pass this
    whole function to ``asyncio.to_thread`` -- one hop covering both steps, since
    the redaction is the part that scans.

    ``_html_len`` and the split budget keep using the raw translator: they MEASURE
    the render, and redacting there would size a segment against text that is not
    the text being sent.
    """
    return _md_to_telegram_html(_display_safe(text))


# Widest label that still shows whole on a half-width inline button on a
# narrow phone, in display cells: a CJK ideograph or fullwidth form counts as
# two, a combining mark as none. Still approximate, since proportional fonts
# vary by phone.
_HALF_WIDTH_LABEL_CELLS = 16


def build_inline_keyboard(options: list[str], session_key: str) -> dict | None:
    """Build an InlineKeyboardMarkup from ``[OPTIONS:]`` labels.

    ``callback_data`` is ``opt:<index>:<session-tag>``. The label stays out of
    the payload because Telegram caps it at 64 bytes and a multi-byte CJK/emoji
    label could overflow. The compact deterministic tag binds a later press to
    the session that posted the keyboard; the label is recovered from the button
    text at callback time.

    Rows hold two buttons while every label fits a half-width button, and one
    button each as soon as any label is wider than ``_HALF_WIDTH_LABEL_CELLS``
    display cells: a phone truncates a half-width button mid-label, so two
    choices that differ late in their text would read the same. Width is
    measured with ``display_width``, so a CJK label counts its rendered width,
    not its code points. The rule covers the whole keyboard, not one row, so
    the buttons stay a uniform width. Keyboard height is bounded by the
    25-button ceiling, not by this rule.

    A label is MODEL-authored text that Telegram renders, so it is a display sink
    like the answer body: the driver's byte-level scan can see a credential as
    broken that the rendered button shows whole. Scanned HERE because this is the
    one place both callers pass through. Each label is redacted WHOLE and bounded
    to 64 chars only after the scan — cutting first can split a credential at the
    boundary into fragments no redaction regex matches. Bounded work by
    construction — at most ``max_buttons`` single-line labels — so it stays on
    the loop.
    """
    if not options:
        return None
    origin_tag = session_provenance_tag(session_key)
    labels: list[str] = []
    for opt in options:
        safe, _ = redact_for_display(opt, _default_redactor)
        labels.append(safe[:64])
    per_row = 1 if any(display_width(label) > _HALF_WIDTH_LABEL_CELLS for label in labels) else 2
    buttons: list[list[dict]] = []
    row: list[dict] = []
    for i, label in enumerate(labels):
        row.append({"text": label, "callback_data": f"opt:{i}:{origin_tag}"})
        if len(row) == per_row:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return {"inline_keyboard": buttons}


def _split_markdown(text: str, limit: int) -> list[str]:
    """Split markdown into <=``limit`` chunks, keeping fenced code blocks balanced.

    A cut inside a ``` fence leaves the chunk unbalanced, the per-chunk HTML pass
    never matches it, and the code body is sent unescaped — which 400s the whole
    request. So every chunk has to be self-contained markdown.

    Delegates to the shared :func:`kiro_crew.messaging.split.split_markdown_safe`,
    which is the reference implementation Discord already uses. The channel-local
    predecessor rebalanced by COUNTING backticks (``ch.count("```") % 2``), and
    that is not the fence grammar: a ``` line *inside* a ````-delimited block flips
    the parity, after which the state is inverted for the rest of the message. The
    observable result was a fabricated 3-backtick closer that closes nothing, a
    reopener that drops both the run length and the info string (so ```` ```diff ````
    continues as bare ``` ``` ``` and loses its highlighting), and — once inverted —
    code delivered as prose with a stray opener glued after it.

    The shared splitter tracks run length and info string, reopens with the
    ORIGINAL opener, and never emits a delimiter the source did not contain. Its
    cut-preference ladder (paragraph break past half the budget, else line break
    past a quarter, else a hard cut) is the same one this channel used, so chunk
    boundaries are unchanged for text with no fence in it.

    The cut is credential-aware, because this channel rotates: each chunk but the
    last is sealed as its OWN message and the redaction runs per segment, so a key
    severed by a boundary is a key neither message holds and neither redacts,
    while the reader scrolling the two reads it whole. Passing the redactor grades
    the boundaries as the reader sees them and moves the cut instead, which keeps
    every character. Nothing here asks for the prefix-stable mode: a rotation
    replaces the buffer with the retained tail, so text already sealed is never
    part of a later cut.
    """
    return split_markdown_safe(text, limit, redactor=_default_redactor)


# Telegram renders a small HTML subset (<b>/<i>/<code>/<pre>/<a>) far more
# reliably than MarkdownV2 (which needs every '.', '-', '!', '(' escaped). The
# agent emits generic Markdown, so we translate it to Telegram HTML for the
# final message. Code spans are stashed first so their contents are never
# treated as markup, then the remaining text is HTML-escaped before any tags
# are introduced -- so raw '<', '>' and '&' in the answer can't break the parse.
# The fence, inline-code, heading, ``**``, ``__`` and link patterns are the
# display-safety screen's (``TELEGRAM_FALLBACK_*``): the screen models
# ``_strip_md`` with the same objects, so what it scans is what the fallback
# shows. The link's label class is the screen's (no ``[``, ``]`` or line break),
# so this never links (or, in ``_strip_md``, flattens) a label the screen left raw,
# and its url is the shared destination unit, so a balanced pair may sit inside
# it; see :func:`kiro_crew.constants.md_link_destination`.
_ITALIC_STAR_RE = re.compile(r"(?<!\w)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\w)")
_ITALIC_USCORE_RE = re.compile(r"(?<!\w)_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)


def _seal_table_fallback(text: str) -> str:
    """Render *text* for the no-Rich-Messages path: tables monospace, prose rich.

    Run detection is ``_table_blocks``, shared with table-aware splitting, and is
    deliberately LOOSER than ``_has_table``: a pipe-bearing line above a
    separator row is monospaced whatever the two rows' cell counts are. The two
    predicates answer different questions and must not be unified.
    ``_has_table`` decides a TRANSPORT and so must agree with the server's own
    GFM cell-count rule -- claiming a table the server will not parse is what
    ships a flattened paragraph. This one only decides a RENDERING, and ``<pre>``
    reproduces its input verbatim, so widening it costs nothing and covers the
    malformed markup ``_has_table`` correctly refuses.

    Reached only when ``sendRichMessage`` failed, which -- if this server never
    supports it -- is the PERMANENT path for every table-bearing reply, so it
    has to render at least as well as the plain HTML seal it replaces.

    Wrapping the whole segment in one ``<pre>`` does not: a reply of three
    paragraphs plus one small table would lose every bold, link and inline code
    span and arrive as a monospace block showing literal ``**`` markers -- worse
    than the ragged-pipes-but-formatted output it was meant to improve on. So
    only the table runs are wrapped; surrounding prose keeps the normal HTML
    rendering, and the table at least keeps its columns aligned.

    A segment containing ANY code fence is handed to ``_md_to_telegram_html``
    whole and never split. Splitting means rendering the pieces with separate
    calls, so a cut that lands inside a fence tears the block in half and leaks
    its delimiters -- and deciding reliably where a fence begins and ends means
    reimplementing CommonMark's fence rules (delimiter character, run length,
    indentation, info strings) as a SECOND parser that must agree with the one
    the HTML renderer already uses. Declining to split is the cheap invariant
    that removes the whole failure class: a fenced reply then renders exactly as
    it does on the unmodified path, only without table alignment.
    """
    if _FENCE_LINE_RE.search(text):
        return _md_to_telegram_html(text)
    # No fence can appear below -- a fenced segment returned above.
    parts: list[str] = []
    for is_table, lines in _table_blocks(text):
        if is_table:
            parts.append(f"<pre>{html.escape(chr(10).join(lines))}</pre>")
        else:
            parts.append(_md_to_telegram_html("\n".join(lines)))
    return "\n".join(p for p in parts if p)


def _md_to_telegram_html(text: str) -> str:
    """Translate the agent's Markdown into Telegram's supported HTML subset."""
    stash: list[str] = []

    def _keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = TELEGRAM_FALLBACK_FENCE.sub(
        lambda m: _keep(f"<pre>{html.escape((m.group(1) or '').rstrip(chr(10)))}</pre>"), text
    )
    text = TELEGRAM_FALLBACK_INLINE_CODE.sub(
        lambda m: _keep(f"<code>{html.escape(m.group(1))}</code>"), text
    )
    text = html.escape(text)
    text = TELEGRAM_FALLBACK_HEADING.sub(
        lambda m: f"<b>{telegram_fallback_heading_text(m)}</b>", text
    )
    text = TELEGRAM_FALLBACK_BOLD_STAR.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = TELEGRAM_FALLBACK_BOLD_USCORE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _ITALIC_STAR_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _ITALIC_USCORE_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = TELEGRAM_FALLBACK_LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}\u2022 ", text)
    # Group consecutive "> " lines (escaped to "&gt; ") into a native Telegram
    # <blockquote> — the ▎ quote bar. Runs after inline formatting so bold/italic
    # inside a quote still work; before un-stashing so code/pre placeholders ride
    # through untouched.
    text = re.sub(
        r"(?m)^&gt;[ \t]?.*(?:\n&gt;[ \t]?.*)*",
        lambda m: "<blockquote>"
        + re.sub(r"(?m)^&gt;[ \t]?", "", m.group(0)).rstrip("\n")
        + "</blockquote>",
        text,
    )
    text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
    return text


def _rendered_len(source: str) -> int:
    """Length of ``source`` once converted to Telegram HTML."""
    return len(_md_to_telegram_html(source))


def _split_markdown_bounded(text: str, rendered_limit: int) -> list[str]:
    """Split markdown so every chunk's RENDERED HTML fits ``rendered_limit``.

    ``_split_markdown`` budgets the *source*, but ``_md_to_telegram_html``
    inflates it: ``html.escape`` turns ``&`` into ``&amp;`` (+4) and ``<`` into
    ``&lt;`` (+3), plus the tags we add. A code-heavy chunk (a diff, JSON,
    ``Map<String,Object>``, shell redirects) therefore renders well past a source
    budget that looked safe -- and Telegram rejects the whole message. Measure
    the rendered form and shrink the source budget until it actually fits.

    Shrinks all the way down to ``_MIN_SPLIT_LIMIT`` instead of stopping after a
    fixed pass count: giving up early returns chunks that are still oversize, and
    the client backstop then truncates them, silently dropping content. Only at
    the floor -- where the content is genuinely indivisible -- may oversize chunks
    be returned.

    Each candidate answer also goes through
    :func:`~kiro_crew.messaging.split.bounded_for_delivery`, because a
    credential-aware cut may DECLINE to cut: when no budget has clean boundaries
    the splitter answers with the text whole, which is fail-closed but is one
    chunk over the budget, and this channel's client truncates a larger payload
    after every scan has run. The bound cuts that answer back and grades the
    sequence it actually produced.
    """
    src_limit = max(_MIN_SPLIT_LIMIT, rendered_limit)
    while True:
        chunks = bounded_for_delivery(
            _split_markdown(text, src_limit), src_limit, _default_redactor
        )
        worst = max((_rendered_len(c) for c in chunks), default=0)
        if worst <= rendered_limit or src_limit <= _MIN_SPLIT_LIMIT:
            return chunks
        src_limit = _shrunk_limit(src_limit, rendered_limit, worst)


def _split_markdown_table_aware(text: str, rendered_limit: int, rich_limit: int) -> list[str]:
    """Split markdown that holds at least one table, keeping table runs whole.

    Table runs are atomic the way fenced code blocks are in ``_split_markdown``:
    a cut inside one strands header-less body rows that fail table detection
    and seal as literal pipes. Prose between tables budgets against
    ``rendered_limit`` (it seals through the HTML path); a table run budgets
    against ``rich_limit`` in SOURCE chars (it seals through sendRichMessage,
    which takes the markdown unrendered) and is split at row boundaries with
    the header repeated only when it alone exceeds that.

    Fence-bearing text falls back to the fence-aware bounded splitter: deciding
    where a fence begins and ends means reimplementing CommonMark's fence rules
    as a second parser (the same invariant ``_seal_table_fallback`` documents),
    and a pipe pattern inside a fence is not a table anyway.

    The assembled sequence is graded once at the end. Each block is cut on its own
    here, so a boundary BETWEEN two blocks -- a table run and the prose after it,
    or two row-split pieces -- belongs to no single cut and is graded by none of
    them, while the reader still reads those messages in order. The repair's
    subject is ``text``, the body this function holds, never the concatenation of
    the blocks, which is not the reply.

    Every block is redacted BEFORE it enters the list, which the grade requires:
    :func:`~kiro_crew.messaging.split._rejoins_a_key` reads the sequence as one
    text and is sound only on chunks that are already a fixed point of that scan,
    so an unredacted table cell holding a whole credential would fire the seam
    repair for something no seam severed. The prose branch gets that redaction from
    the splitter; a table run bypasses the splitter and needs it here.
    """
    if _FENCE_LINE_RE.search(text):
        return _split_markdown_bounded(text, rendered_limit)
    out: list[str] = []
    for is_table, lines in _table_blocks(text):
        block = "\n".join(lines)
        if is_table:
            safe_block = _display_safe(block)
            if len(safe_block) <= rich_limit:
                out.append(safe_block)
            else:
                out.extend(_split_table_rows(safe_block.split("\n"), rich_limit))
        elif block.strip():
            out.extend(_split_markdown_bounded(block, rendered_limit))
    kept = [c for c in out if c.strip()]

    def bounded(repaired: str) -> list[str]:
        return _split_markdown_bounded(repaired, rendered_limit)

    repaired = repaired_for_delivery(text, kept, _default_redactor, bounded)
    if repaired is None:
        return kept
    return bounded(repaired)


def _strip_md(text: str) -> str:
    """Flatten Markdown to clean plaintext for the streaming typewriter frames
    (and as the safe fallback if an HTML final edit is ever rejected) -- avoids
    showing raw ``**``/``##``/``[x](url)`` noise while the answer is forming.
    The fence, inline-code, heading, ``**``, ``__`` and link passes are the
    display-safety screen's own objects and replacements, in the order its
    ``_plain_reading`` applies them. The bullet pass keeps a visible bullet
    between surrounding text and remains this renderer's own."""
    text = TELEGRAM_FALLBACK_FENCE.sub(lambda m: m.group(1) or "", text)
    text = TELEGRAM_FALLBACK_INLINE_CODE.sub(lambda m: m.group(1), text)
    text = TELEGRAM_FALLBACK_HEADING.sub(telegram_fallback_heading_text, text)
    text = TELEGRAM_FALLBACK_BOLD_STAR.sub(lambda m: m.group(1), text)
    text = TELEGRAM_FALLBACK_BOLD_USCORE.sub(lambda m: m.group(1), text)
    text = TELEGRAM_FALLBACK_LINK.sub(TELEGRAM_FALLBACK_LINK_TEXT, text)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}\u2022 ", text)
    return text


class TelegramRenderer(Renderer):
    """Streams a turn to Telegram via ``editMessageText`` + inline keyboards."""

    channel_type = "telegram"

    def __init__(
        self,
        client: "TelegramClient",
        chat_id: int,
        capabilities: TransportCapabilities,
        *,
        session_key: str = "",
        message_thread_id: int | None = None,
        show_thinking: bool = False,
        uploads_allowed: bool = True,
        reply_to_message_id: int | None = None,
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._chat_id = chat_id
        # The user message this turn answers, attached to the turn's FIRST
        # outbound only and then spent (see _consume_reply_to). None in a live DM,
        # where every bubble is already unambiguously the answer to the message
        # above it and a reply quote is just noise.
        self._reply_to = reply_to_message_id
        # Forum-topic id: when set, every outbound send/typing is threaded into
        # that Topic. None for a 1:1 DM or the supergroup General topic. Edits
        # never carry it (message_id already locates the message in its topic).
        self._thread_id = message_thread_id
        self._session_key = session_key
        self._buf: list[str] = []
        self._last_tool = ""
        # Transient tool-activity footer ("🔧 {tool}…") shown ONLY on live
        # streaming frames — never stored in _buf, so seals/finals stay clean.
        # Set by on_tool_call, cleared when text resumes (on_text_chunk).
        self._tool = ""
        self._finalized = False
        self._closed = False
        # Pre-sanitized, user-safe reason for a failed turn, set by
        # ``close(failure_reason=...)``. When present it replaces the generic
        # error placeholder at finalization so a permanent failure (wrong
        # model entitlement, misconfiguration) surfaces its actionable message
        # instead of misleading retry advice. The caller owns sanitization
        # (bounded, single-line, redacted); this field is display-only.
        self._failure_reason: str | None = None
        self._typing_task: "asyncio.Task[None] | None" = None
        # Live edit-streaming (send one real message, edit it in place as text
        # arrives — no draft, which fails for bots / ghosts). On a steer boundary
        # we STOP editing the current message and open a fresh one for the
        # steered continuation ("rotate"). _stream_mid = the message being edited
        # (None -> next render sends a new one). _shown = last text pushed (skip
        # no-op edits). _last_edit throttles edits. _buf = the CURRENT segment's
        # text; a rotation seals _buf into its message and starts _buf fresh.
        self._stream_mid: int | None = None
        self._shown = ""
        self._last_edit = 0.0
        self._seal_count = 0  # rotations so far == index into _steer_texts for chips
        # Redaction placeholders in text that actually LANDED, tallied per
        # delivered frame's final form (live edits supersede each other, so
        # only sealed segments and the posted reasoning count). Feeds the
        # post-answer notice at on_done.
        self._redacted_creds = 0
        self._redacted_urls = 0
        # Chip pending from the last rotation, NOT yet in _buf. It materializes
        # (prepends to the segment) only when real post-steer text arrives — so
        # an end-of-stream marker (no continuation text) never posts a chip-only
        # ack bubble: the answer already covered the steer and the user's
        # message carries the reaction receipt.
        self._pending_chip = ""
        # User's own mid-turn steer texts (in order), recorded by the dispatcher
        # via note_steer. Each rotation seeds the new segment with that steer's
        # chip; a no-rotation turn shows them as one summary chip at on_done.
        self._steer_texts: list[str] = []
        # Serializes frame writes: the typing loop publishes stall marks from its
        # own task, so the live-frame read-modify-send must not interleave with
        # the token stream's.
        self._frame_lock = asyncio.Lock()
        # Stall bookkeeping. ``_last_progress`` is reset by every provider event;
        # ``_shown_stall`` is the mark currently on screen, so the typing tick
        # edits only when it would actually change.
        self._last_progress = time.monotonic()
        self._shown_stall = ""
        # One-slot memo for the live frame's safe body, keyed on its exact source.
        self._safe_src = "\x00"  # a value no segment can equal
        self._safe_out = ""
        self._safe_sent = "\x00"  # the memo also depends on the sealed predecessor
        #: The last segment SEALED as its own message, whole. A rotation replaces
        #: the buffer with the retained tail, so text already sent leaves the
        #: buffer and no later cut can see it -- and a boundary graded at seal time
        #: says nothing about text that arrives afterwards. Keeping the sealed
        #: predecessor is what lets the next thing shown be graded against what the
        #: reader is already looking at. Bounded by one message, not by the turn:
        #: only the message a new one sits under can rejoin anything with it.
        self._sent_tail = ""
        # True between posting an approval prompt and the turn resuming. A turn
        # waiting on a button is blocked on the user, not stalled.
        self._awaiting_approval = False
        # Turn timing + the context gauge for the footer. ``_ctx_client`` is the
        # provider's ACP client, supplied by the dispatcher once the session
        # exists; absent, the footer reports duration only.
        self._turn_started = time.monotonic()
        self._ctx_client: Any = None
        # Reasoning: shown as an expandable blockquote when the operator opted in
        # (``telegram.show_thinking``). Accumulated and posted ONCE per turn
        # rather than streamed — reasoning arrives in many small chunks and each
        # would cost an edit of the answer bubble it must not disturb.
        self._show_thinking = bool(show_thinking)
        self._thinking: list[str] = []
        self._thinking_chars = 0
        self._thinking_posted = False
        # Outbound image upload. ``_upload_root`` is the provider's resolved cwd,
        # supplied by the dispatcher once the session exists; an empty root means
        # extraction has no approved root and uploads stay off.
        self._uploads_allowed = bool(uploads_allowed)
        self._upload_root = ""
        # The session's own scratch directory, set only beside a valid root.
        self._scratch_root = ""

    async def on_steer_consumed(self, summary: str = "") -> None:
        """Seal the pre-steer segment at the driver's structured boundary."""
        self._note_progress()
        self._materialize_chip()
        await self._rotate_on_length()
        # A trailing [OPTIONS:] block belongs to the visible PRE-STEER answer,
        # but the steering marker sits after it in the raw buffer, so the
        # end-of-buffer anchor cannot see it. Extract it here -- BEFORE the
        # seal -- so the choices ship as a keyboard on the sealed message instead of
        # being frozen as literal protocol text the user cannot act on.
        body_raw, opts = _extract_options(strip_control_comments("".join(self._buf)))
        # Trailing control-tag lines are protocol on either side of the trailer;
        # complete tags only -- a partial tail at the seal is prose.
        body_raw = strip_control_comments(body_raw)
        body_raw, opts = apply_options_cap(body_raw, opts, self.capabilities)
        self._buf = [body_raw]
        # apply_options_cap may EXPAND the body (numbered overflow lines), and
        # the rotation above ran before that expansion -- re-check, or a
        # near-limit answer with over-cap options seals past the transport cap.
        await self._rotate_on_length()
        keyboard = build_inline_keyboard(opts, self._session_key) if opts else None
        sealed = bool(self._segment_text().strip()) or keyboard is not None
        await self._seal_current(keyboard=keyboard)
        clean_summary = _neutralize_md(summary)
        if clean_summary:
            chip: str | None = f"> ↪️ {clean_summary}"
        else:
            chip = self._chip_for_seal(self._seal_count)
        self._seal_count += 1
        self._pending_chip = chip or ""
        self._buf = []
        if sealed:
            self._open_new_message()

    async def _rotate_on_length(self) -> None:
        """Rotate when the segment exceeds one Telegram message. Uses
        ``_split_markdown`` so a fenced code block spanning a cut is rebalanced
        (fence closed at the seal, reopened in the next segment) instead of
        leaving literal backticks in both messages. A trailing protocol
        directive — a COMPLETE ``[OPTIONS: …]`` block or a still-streaming
        ``[STEERING …`` / ``[OPTIONS …`` fragment — is detached first and
        reattached to the surviving tail, so length splitting can never cut a
        directive in half (which would leak protocol fragments and lose the
        steer rotation / options keyboard).

        Rotation triggers on the SOURCE budget or on the RENDERED HTML cap,
        whichever binds first: a segment can sit under the source budget and
        still render past Telegram's hard limit once ``html.escape`` inflates
        it.

        Table-bearing segments budget against the RICH cap instead: they seal
        through sendRichMessage, so holding the whole table in one segment is
        what keeps it one rich message rather than a rich head followed by
        header-less pipe-text continuations."""
        limit = self._limit()
        rendered_cap = self._rendered_limit()
        raw = "".join(self._buf)
        if len(raw) <= limit:
            # Provably-small buffers skip the real render (this runs per chunk).
            if not _may_exceed_rendered(raw, rendered_cap):
                return
            if _rendered_len(raw) <= rendered_cap:
                return
        # An image reference — complete, or an opener still arriving — and
        # everything after it stay in the live tail, so the semantic seal sees it
        # whole and can upload it. A length cut here would strand half of
        # ``![alt](path)`` in a sealed message, unrecognisable to any later pass
        # and visible as broken markdown; and only the semantic seal extracts, so
        # a reference that rode out on a length-sealed chunk would print its path.
        # Off-loop: the scan runs over adversarial markup on every chunk.
        # ``"!["`` is a necessary condition for a protected span (both the complete
        # and the still-opening grammar require it), and testing it costs ~1 µs/KB
        # against the scan's 7-15 µs — so the common answer is reached without the
        # scan at all. The scan itself runs INLINE: at those numbers a thread hop
        # (145 µs idle, 650 µs under load) costs 20-90x the work it would carry.
        if (
            self._uploads_enabled()
            and "![" in raw
            and len(raw) <= limit * _UPLOAD_HOLD_LIMIT_FACTOR
        ):
            spans = protected_ref_spans(raw)
            if spans:
                if spans[0][0] == 0:
                    return  # the whole buffer is protected — do not rotate at all
                held = raw[spans[0][0] :]
                # The boundary between the last sealed chunk and ``held`` needs no
                # grade, deliberately. ``held`` begins AT the ref span, so its first
                # two characters are always ``![`` -- and ``!`` survives
                # canonicalising, standing between the sealed chunk's last character
                # and anything the tail could contribute. Measured over alt-text,
                # link-target, trailing-text and bare-adjacency shapes: no join
                # reaches a credential pattern, so a gate there would be a gate on an
                # unreachable boundary.
                #
                # The boundaries AMONG the prefix chunks are a different matter. The
                # splitter redacts before choosing one, but it reads the RAW pieces,
                # where a horizontal rule still stands between two fragments; the
                # seal strips rules, so the reader sees them flush. And the seam
                # repair that does read the delivered form reads one predecessor
                # only. A key whose fragments sit across three chunks separated by
                # ``---`` is therefore clean in every reading that runs. Grade the
                # prefix as a SEQUENCE in its delivered form, the same gate the
                # length path below carries, and carve on it when it severs.
                prefix = raw[: spans[0][0]]
                chunks = await asyncio.to_thread(_split_markdown_bounded, prefix, rendered_cap)
                if await asyncio.to_thread(
                    severs_a_credential, chunks, _default_redactor, _delivered_form
                ):
                    carved = await self._carve_graded(prefix, limit, rendered_cap)
                    if carved is None:
                        # Deliver NOTHING: the whole buffer, reference included, rides
                        # the next rotation and the semantic seal redacts it intact.
                        return
                    chunks = carved
                for chunk in chunks:
                    self._buf = [chunk]
                    await self._seal_current(extract_uploads=False)
                    self._open_new_message()
                self._buf = [held]
                return
        raw, protocol_suffix = split_trailing_protocol_suffix(raw)
        if _has_table(raw):
            # Table segments seal through sendRichMessage, whose payload budget
            # is far larger than the HTML render cap, so they are budgeted
            # against the rich cap: a table sized to the HTML budget cuts
            # row-wise, and its header-less continuations fail table detection
            # and arrive as literal pipes. Under the rich cap, most tables that
            # overflow the HTML budget fit ONE rich message and never split.
            rich_cap = self._rich_limit()
            if len(raw) <= rich_cap:
                return
            # The buffer's final line may still be STREAMING: its next token
            # can arrive after this rotation. A partial row that has not yet
            # received its first pipe reads as prose to the block parser (GFM
            # rows need no outer pipe), which would strand it -- and the rest
            # of the table -- in a header-less segment. Detach the unterminated
            # line, split only complete lines, and keep it with the tail.
            # _has_table guarantees at least two lines, so a newline exists.
            head, nl, partial = raw.rpartition("\n")
            head += nl
            chunks = await asyncio.to_thread(
                _split_markdown_table_aware, head, rendered_cap, rich_cap
            )
            if chunks:
                # Reattach what the line-joining splitter drops: the complete
                # prefix's trailing newlines, then the unterminated line. The
                # tail keeps streaming, so both must survive or the next
                # streamed token glues onto the previous row.
                chunks[-1] += head[len(head.rstrip("\n")) :] + partial
            else:
                chunks = [partial]
        else:
            chunks = await asyncio.to_thread(_split_markdown_bounded, raw, rendered_cap)
        # Mid-stream the source fence is often still OPEN (the model has not
        # emitted its closing ``` yet). _split_markdown balances each chunk by
        # appending a synthetic closer, which is right for the chunks we seal but
        # wrong for the tail we keep streaming into: every later token would land
        # after that closer, rendering outside <pre>, and the model's real closing
        # fence would then show up literally. Drop it from the retained tail.
        if chunks and raw.count("```") % 2 == 1:
            tail = chunks[-1].rstrip()
            if tail.endswith("```"):
                chunks[-1] = tail[:-3].rstrip("\n")
        # The chunks are sealed as separate messages and each is redacted on its own,
        # so a key the cut severed matches nothing in any one of them while the
        # reader's client renders the markup away and reads them as one key down the
        # screen. Grade the DELIVERED form, which the seal strips.
        if await asyncio.to_thread(severs_a_credential, chunks, _default_redactor, _delivered_form):
            # Cut where the reader cannot rejoin rather than where the budget lands.
            # Both sides are SOURCE slices: splitter output does not concatenate back
            # to its input (fences are closed and reopened), so rejoining chunks
            # would hand the user text the model never wrote.
            #
            # The offset is bounded by the SOURCE budget, and escaping inflates, so a
            # safe head can still render past the HTML cap. Shrink the budget by the
            # inflation actually observed and look again, which is the same loop the
            # splitter itself runs -- a safe cut that fits is worth more than giving
            # up on the rotation, since a deferral holds the whole buffer.
            # The search grades the DELIVERED form of both sides, so the offset it
            # returns is one this caller can take. Grading raw here and re-checking
            # afterwards would deadlock the segment: the search is deterministic, so a
            # rejected answer is the same answer every rotation and nothing ever goes
            # out. Off the loop for the cost reason the redaction above carries -- each
            # sampled offset is two full-buffer redaction passes.
            budget, head, offset = limit, "", 0
            while True:
                offset = await asyncio.to_thread(
                    safe_split_offset, raw, budget, _default_redactor, _delivered_form
                )
                head = raw[:offset]
                worst = await asyncio.to_thread(_rendered_len, head)
                if not offset or worst <= rendered_cap:
                    break
                if budget <= _MIN_SPLIT_LIMIT:
                    offset = 0
                    break
                budget = _shrunk_limit(budget, rendered_cap, worst)
            if not offset:
                # Deliver NOTHING: the withheld text rides the next rotation, and the
                # final seal re-splits and seals an over-cap segment chunk by chunk.
                self._buf = [raw + protocol_suffix]
                return
            chunks = [head, raw[offset:]]
        for ch in chunks[:-1]:
            self._buf = [ch]
            # A length rotation never extracts: only a SEMANTIC seal (steer
            # boundary / end of turn) sees the reference in its whole-text fence
            # context, and the guard above has already kept any reference out of
            # these chunks.
            await self._seal_current(extract_uploads=False)
            self._open_new_message()
        self._buf = [(chunks[-1] if chunks else "") + protocol_suffix]

    async def _carve_graded(self, source: str, limit: int, rendered_cap: int) -> list[str] | None:
        """``source`` cut into pieces no reader can rejoin into a credential, or
        ``None`` when no safe cut exists and the text must be withheld whole.

        Every piece this returns is SEALED by the caller, with no rotation left
        ahead of any of them, so the carve continues until the remainder fits the
        budget rather than stopping at one offset. A piece left over the cap is
        re-split by ``_seal_current`` on length alone, with no credential grade,
        which is the cut this gate exists to close -- so taking a single offset
        would buy ONE safe boundary by handing every later boundary in the same
        text to an ungraded one.

        Each step grades its head against the whole remainder, which is the
        per-boundary reading ``severs_a_credential`` applies to the finished list.
        That reading is still asked for once at the end: canonicalising DROPS a
        link target, so markup spanning a middle piece can collapse two distant
        pieces together in a way no single boundary check sees. The cost is
        confined to text that already severs -- a prefix the splitter cut safely
        never reaches here.
        """
        graded: list[str] = []
        rest = source
        while len(rest) > limit or await asyncio.to_thread(_rendered_len, rest) > rendered_cap:
            budget = limit
            while True:
                offset = await asyncio.to_thread(
                    safe_split_offset, rest, budget, _default_redactor, _delivered_form
                )
                if not offset:
                    return None
                head = rest[:offset]
                worst = await asyncio.to_thread(_rendered_len, head)
                if worst <= rendered_cap:
                    break
                if budget <= _MIN_SPLIT_LIMIT:
                    return None
                budget = _shrunk_limit(budget, rendered_cap, worst)
            graded.append(head)
            rest = rest[offset:]
        chunks = [*graded, rest]
        if await asyncio.to_thread(severs_a_credential, chunks, _default_redactor, _delivered_form):
            return None
        return chunks

    async def _safe_body(self, seg: str) -> str:
        """The live frame's plaintext body: markup hidden, markdown flattened, and
        redacted against the rendered form.

        Two costs are balanced here, both measured rather than assumed. The
        redaction is the expensive half — single-digit milliseconds on a
        few-kilobyte segment, tens on a full one — so it runs OFF the loop; the
        markup scan is microseconds, cheaper than the thread hop that would carry
        it, so it rides the same hop instead of getting one of its own.

        Memoized on the exact source string, because most frames re-derive an
        IDENTICAL body: a tool-call frame and a stall-mark frame are both forced
        without ``_buf`` changing, and only the ~20-character footer differs. Exact
        equality, not a heuristic — a segment that has not changed cannot have a
        different safe form.
        """
        if seg == self._safe_src and self._sent_tail == self._safe_sent:
            return self._safe_out
        hide = hide_local_refs if self._uploads_enabled() else None
        sent = self._sent_tail

        def _render() -> str:
            safe = _display_safe(_strip_md(hide(seg) if hide else seg))
            # The frame sits directly under the message sealed before it, so the
            # same seam applies: a key begun at the end of that message and
            # completed here reads whole down the screen. The sealed half cannot be
            # changed, so this half gives up the span that completes it.
            repaired = repaired_after_a_sent_tail(sent, safe, _default_redactor)
            return repaired if repaired is not None else safe

        out = await asyncio.to_thread(_render)
        self._safe_src, self._safe_out, self._safe_sent = seg, out, sent
        return out

    async def _seal_without_rich(self, text: str) -> tuple[str, str, int | None]:
        """HTML for a seal that cannot use Rich Messages, plus the tail segment.

        Pipe blocks are wrapped in ``<pre>`` so a block Rich Markdown would
        reflow keeps its columns and loses no cell; prose around them keeps its
        normal formatting.

        ``<pre>`` only ADDS characters, and the segment was sized against the
        RICH budget, so the result can overflow ``_rendered_limit()`` twice over
        -- first the wrapped form, then the plain render. When even the plain
        render spills, the segment is re-split against the HTML budget and every
        chunk but the last is shipped here: header repetition keeps each chunk
        detected as a table, so the whole thing degrades uniformly to ``<pre>``
        rather than half aligned and half ragged. Letting the client's truncation
        backstop cap it instead would drop content silently.

        Returns the HTML to seal with and the tail segment it renders, which the
        caller seals through its normal path so the keyboard lands on the final
        message. The third value is the streamed bubble the chunks were sent
        below because it could not be edited, for the caller to delete once the
        tail has landed too. It is None when there is no such bubble, and when a
        chunk did not land: the bubble may then show rows that no sent message
        carries.
        """
        html_text = _seal_table_fallback(text)
        replaced: int | None = None
        if len(html_text) > self._rendered_limit():
            html_text = _md_to_telegram_html(text)
            if len(html_text) > self._rendered_limit():
                chunks = await asyncio.to_thread(self._degraded_table_chunks, text)
                all_landed = True
                for ch in chunks[:-1]:
                    landed, frame = await self._seal_chunk_html(ch)
                    all_landed = all_landed and landed
                    if frame is not None:
                        replaced = frame
                if not all_landed:
                    replaced = None
                if chunks:
                    text = chunks[-1]
                # The tail sits under the last chunk this method just sealed, and
                # that chunk did not exist when the segment's own seam was graded.
                # So it is graded again here, against what the reader can now see.
                text = await asyncio.to_thread(self._seam_safe, text)
                html_text = _seal_table_fallback(text)
                if len(html_text) > self._rendered_limit():
                    html_text = _md_to_telegram_html(text)
        return html_text, text, replaced

    def _seam_safe(self, text: str) -> str:
        """*text* display-safe, with its seam to the message above repaired.

        The one place a sealed segment's text is decided, so it is the one place
        that has to know about the message above it. Callers only seal; none of them
        carries a seam rule of its own, which is what keeps a new sealing path from
        shipping an open seam.

        It does NOT record the predecessor. What the next segment must be graded
        against is the message the reader can see, and text this returns has not
        been sent yet: every send and edit path below can fail, and recording here
        would make unsent text the predecessor, after which the next delivered
        message gives up a leading span for a key nobody ever read. ``_record_sent``
        is called once a delivery path confirms.

        Runs on a worker thread: both the redaction and the grade scan.
        """
        safe = _display_safe(text)
        repaired = repaired_after_a_sent_tail(self._sent_tail, safe, _default_redactor)
        return repaired if repaired is not None else safe

    def _record_sent(self, text: str) -> None:
        """Remember *text* as the message the next seam is graded against.

        Called only after a send or edit reports success, and with the text that
        actually went out -- which is not always the text ``_seam_safe`` returned: a
        degraded segment is re-split after that point, so the last chunk shown is
        what the reader is looking at and what the next segment sits under.
        """
        self._sent_tail = text

    async def _seal_current(
        self,
        *,
        keyboard: dict | None = None,
        extract_uploads: bool = True,
        footer: str = "",
    ) -> None:
        """Finalize the current segment, then ship any image it referenced.

        ``extract_uploads`` is False for a length rotation: only a SEMANTIC seal
        sees a local image reference in its whole-text fence context, so it is
        the only place extraction may run and the only place it runs once.

        The upload is a SEPARATE send after the text lands, and it runs on every
        path out of ``_seal_text`` — including the ones that return early (a rich
        replacement, a successful edit) — which is why the two are split rather
        than folded into one method with an upload call per exit.
        """
        source = self._segment_text().strip()
        text = source
        files: list[OutboundFile] = []
        if extract_uploads and source and self._uploads_enabled():
            text, files = await self._extract_uploads(source)
        if text or keyboard is not None:
            await self._seal_text(text or "…", keyboard, footer=footer)
        elif files:
            # Extraction consumed the whole body — an image-only reply. There is
            # no text to seal, but a live bubble may still exist carrying a
            # TRANSIENT frame (a "🔧 {tool}…" footer, a stall mark) that only ever
            # belonged to a turn in progress. Leaving it makes that footer the
            # turn's final message, sitting above the picture forever.
            #
            # Gated on ``files`` deliberately: an empty segment with NOTHING to
            # ship is the separate case where a tool-footer bubble must be KEPT,
            # so a steered continuation replaces it in place rather than orphaning
            # it (pinned by test_tool_only_message_not_orphaned_at_steer_boundary).
            await self._retire_live_frame()
        await self._send_uploads(files)

    async def _seal_text(self, text: str, keyboard: dict | None, *, footer: str = "") -> None:
        """Land one segment's text: replace its live plaintext with the formatted
        HTML (and optional keyboard). Edits the streamed message in place, or
        sends one if the segment never streamed (e.g. throttled out).

        Redaction happens HERE, once, ahead of every rendering decision below —
        the rich send takes raw markdown, the HTML seals introduce tags, and the
        plaintext fallback strips them — so a single call covers all three sinks
        (see :func:`_display_safe`).

        Serialized by ``_frame_lock``, and retiring the live message id on EVERY
        exit. Both halves are load-bearing against the typing loop, which
        publishes stall frames from its OWN task: without the lock a frame could
        interleave with the seal, and without the retire a frame computed BEFORE
        the seal could then edit the message the seal had just finalized,
        replacing the formatted answer with a stale plaintext draft. Once the id
        is cleared, the worst a late frame can do is post a new bubble — visible
        rather than destructive."""
        # Redacted BEFORE the lock and OFF the loop: a table-bearing segment is
        # budgeted against the rich cap, where this measures in the tens of
        # milliseconds — holding the frame lock across it would block the typing
        # task too, and holding the loop would block every other conversation.
        #
        # The seam repair and the predecessor record live HERE, in the sink, for the
        # same reason the redaction does: five paths seal a segment as its own
        # message, and a guard at one of them is not a guard on the channel. A
        # segment whose tail is a credential PREFIX matches nothing, so it seals, and
        # the characters completing the key arrive in the segment below it -- which
        # this is. The message above cannot be recalled, so this one gives up the
        # span that completes the key, and then becomes the predecessor the next seal
        # is graded against.
        text = await asyncio.to_thread(self._seam_safe, text)
        if footer:
            # A quoted line under the answer rather than a separate message: the
            # footer is metadata about the turn, and a second bubble for it would
            # cost a notification and a rate-limit slot the answer needs.
            text = f"{text}\n\n> {footer}"
        async with self._frame_lock:
            try:
                # --- Rich Message path: tables detected → sendRichMessage (Bot API 10.1+) ---
                # Rich Markdown renders pipe tables natively; the legacy HTML subset
                # cannot express a table at all, so a table sealed through HTML always
                # reaches the user as literal `|` characters.
                #
                # There is no editRichMessage, so a segment that already streamed a
                # plaintext bubble cannot be *edited* into a rich one -- it has to be
                # replaced. Order matters: SEND the rich message first and only delete
                # the streamed bubble once it succeeded. Deleting first would lose the
                # answer outright if the rich send then failed.
                #
                # Replacing means Telegram notifies twice: once for the streamed bubble,
                # once for its replacement. The bubble already pinged the user, so the
                # replacement is sent silently -- otherwise every table reply buzzes
                # twice where main buzzed once. When nothing streamed there was no
                # earlier ping, so the rich send is the only notification and must fire.
                if _has_table(text):
                    mid = await self._client.send_rich_message(
                        self._chat_id,
                        text,
                        reply_markup=keyboard,
                        message_thread_id=self._thread_id,
                        disable_notification=self._stream_mid is not None,
                        reply_to_message_id=self._consume_reply_to(),
                    )
                    if mid is not None:
                        self._tally_redactions(text)
                        self._record_sent(text)
                        if self._stream_mid is not None:
                            # The rich message now carries this segment; drop the
                            # superseded plaintext bubble so the user sees one message.
                            await self._client.delete_message(self._chat_id, self._stream_mid)
                            self._stream_mid = None
                        return
                    # Rich send failed -- the streamed bubble (if any) is untouched, so
                    # fall through and seal it the legacy way. Only the table runs are
                    # wrapped in <pre>; prose around them keeps its normal formatting,
                    # so this path never renders worse than the plain HTML seal.
                    logger.debug(
                        "sendRichMessage failed for chat %s, falling back to HTML", self._chat_id
                    )
                    html_text, text, stale_mid = await self._seal_without_rich(text)
                else:
                    # No conforming table, which includes pipe markup GFM rejects -- a
                    # header row whose cell count disagrees with its delimiter. Rich
                    # Markdown renders that as one paragraph with the newlines collapsed,
                    # so it must not take the rich path; the monospace seal shows every
                    # row verbatim on its own line instead.
                    html_text, text, stale_mid = await self._seal_without_rich(text)
                if self._stream_mid is not None:
                    ok = await self._client.edit_message(
                        self._chat_id,
                        self._stream_mid,
                        html_text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                        retry_plain=False,
                    )
                    if not ok:  # malformed HTML -> clean plaintext, never raw tags
                        ok = await self._client.edit_message(
                            self._chat_id,
                            self._stream_mid,
                            _strip_md(text),
                            reply_markup=keyboard,
                        )
                    if ok:
                        self._tally_redactions(text)
                        self._record_sent(text)
                        return
                    # Both edits failed — the live message is gone (e.g. the user
                    # deleted it mid-turn), or it is still there and could not be
                    # edited (the per-chat edit budget, or two timeouts). Fall
                    # through and SEND the final content so the completed answer
                    # (and its keyboard) is never silently lost; once that send
                    # lands, the frame it replaces is deleted.
                    stale_mid, self._stream_mid = self._stream_mid, None
                mid = await self._client.send_message(
                    self._chat_id,
                    html_text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    retry_plain=False,
                    message_thread_id=self._thread_id,
                    reply_to_message_id=self._consume_reply_to(),
                )
                if mid is None:
                    mid = await self._client.send_message(
                        self._chat_id,
                        _strip_md(text),
                        reply_markup=keyboard,
                        message_thread_id=self._thread_id,
                    )
                if mid is not None:
                    self._tally_redactions(text)
                    self._record_sent(text)
                    # Every part has landed: ``stale_mid`` is None when a chunk did not.
                    await self._delete_replaced_frame(stale_mid)
                else:
                    await self._send_lost_part_notice()

            finally:
                # Retire the live message: this segment is final, so nothing
                # may edit it again.
                self._stream_mid = None
                self._shown = ""

    async def _post_thinking(self) -> None:
        """Post the accumulated reasoning as ONE expandable blockquote.

        ``<blockquote expandable>`` is Telegram's native collapsed-by-default
        quote, which is the closest thing the channel has to Slack's 💭 thread
        reply: the reasoning is there for whoever wants it and costs one line for
        everyone else. Posted after the answer so the answer is what the
        notification previews.
        """
        if self._thinking_posted:
            return
        self._thinking_posted = True
        body = "".join(self._thinking).strip()
        if not body:
            return
        # The reasoning is model output reaching an external surface, so it goes
        # through the same display sink as the answer: redact against the RENDERED
        # form, since Telegram's own markup can reassemble a credential the literal
        # bytes split. Off-loop because a reasoning body is unbounded and the scan
        # is a full credential/exfil pass.
        safe = await asyncio.to_thread(_display_safe, body)
        # One message. Reasoning is unbounded and is not the answer, so a
        # tag-safe truncation beats a burst of continuation bubbles.
        inner = html.escape(safe)[: self._thinking_budget()]
        try:
            await self._client.send_message(
                self._chat_id,
                f"<blockquote expandable>💭 {inner}</blockquote>",
                parse_mode="HTML",
                retry_plain=False,
                message_thread_id=self._thread_id,
                disable_notification=True,
            )
            self._tally_redactions(inner)
        except Exception:
            logger.debug("Telegram: thinking post failed", exc_info=True)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        self._stop_typing()
        ok = stop_reason != "error"
        # Flush any trailing rotation, then finalize the current segment: replace
        # its live plaintext with formatted HTML + the [OPTIONS:] keyboard. Each
        # mid-turn steer already rotated its own message; this seals the last one.
        await self._rotate_at_markers()
        self._materialize_chip()  # chip lands only if real post-steer text exists
        # Extract the trailing [OPTIONS:] BEFORE length rotation: if the body
        # overflows, rotation would otherwise seal the options text into an
        # earlier message and the keyboard would never attach.
        body_raw, opts = _extract_options(strip_control_comments("".join(self._buf)))
        # Trailing control-tag lines are protocol on either side of the trailer;
        # complete tags only -- a partial tail at the seal is prose.
        body_raw = strip_control_comments(body_raw)
        body_raw, opts = apply_options_cap(body_raw, opts, self.capabilities)
        self._buf = [body_raw]
        keyboard = build_inline_keyboard(opts, self._session_key) if opts else None
        # No-rotation fallback: steers were injected but kiro-cli emitted no
        # marker to rotate at — prepend one summary chip so they're still shown.
        # This happens BEFORE length rotation so the summary counts against the
        # transport limit and can never push the final segment past it.
        if self._seal_count == 0 and self._steer_texts:
            quoted = [q for q in (_neutralize_md(t) for t in self._steer_texts) if q]
            if quoted:
                body = self._segment_text().strip()
                summary = "> " + " · ".join(quoted)
                self._buf = [summary + ("\n\n" + body if body else "")]
        await self._rotate_on_length()
        if not self._segment_text().strip():
            # Nothing to post. Earlier rotated segments carried the turn -> stay
            # silent; otherwise show a placeholder. An extracted keyboard (an
            # options-only body) is user-facing content and must ALWAYS reach
            # the user — attach it to the placeholder instead of dropping it.
            if self._seal_count > 0 and keyboard is None:
                await self._post_thinking()
                await self._maybe_send_redaction_notice()
                return
            placeholder = "…" if ok else (self._failure_reason or _GENERIC_ERROR_TEXT)
            if self._stream_mid is not None:
                await self._client.edit_message(
                    self._chat_id,
                    self._stream_mid,
                    placeholder,
                    reply_markup=keyboard,
                )
            else:
                await self._client.send_message(
                    self._chat_id,
                    placeholder,
                    reply_markup=keyboard,
                    message_thread_id=self._thread_id,
                )
            await self._post_thinking()
            await self._maybe_send_redaction_notice()
            return
        await self._seal_current(keyboard=keyboard, footer=self._turn_footer())
        # After the answer, so the answer is what the push notification previews.
        await self._post_thinking()
        await self._maybe_send_redaction_notice()

    def _limit(self) -> int:
        """Budget for PLAINTEXT frames (live typewriter edits), in source chars.

        This is NOT a safe budget for HTML: ``html.escape`` inflation is
        unbounded relative to a fixed subtraction, so the rendered form is
        capped separately by ``_rendered_limit`` and enforced by measuring the
        real render (see ``_split_markdown_bounded``).
        """
        cap = self.capabilities.max_message_chars or 4000
        return max(500, cap - 256)

    def _rendered_limit(self) -> int:
        """Hard cap for the RENDERED Telegram HTML of one message.

        Telegram's limit is 4096; ``max_message_chars`` (4000) already leaves
        headroom under it, so use it directly as the rendered ceiling rather
        than subtracting a second, guessed tag allowance.
        """
        return max(500, self.capabilities.max_message_chars or 4000)

    def _rich_limit(self) -> int:
        """Budget for one sendRichMessage payload, in SOURCE chars.

        The rich path passes the segment's markdown through unrendered, so
        unlike the HTML seal there is no escape inflation to measure -- source
        length is payload length. The headroom mirrors ``_limit``'s allowance
        for the steer chip and a reattached protocol suffix.
        """
        return TELEGRAM_RICH_MAX_CHARS - 256

    def _degraded_table_chunks(self, text: str) -> list[str]:
        """Split an oversize degraded segment so every chunk's RENDERED form fits.

        ``_split_table_rows`` budgets source chars, but the degraded seal
        renders through ``_seal_table_fallback`` and ``html.escape`` inflation
        is multiplicative (see ``_may_exceed_rendered``), so a source budget
        with fixed headroom still ships oversize chunks that the client's
        backstop truncates -- silent row loss. Mirror
        ``_split_markdown_bounded``: measure the worst rendered chunk and
        shrink the source budget proportionally until everything fits or the
        floor is reached (where content is genuinely indivisible and the
        backstop is the last resort).
        """
        rendered_cap = self._rendered_limit()
        src_cap = max(_MIN_SPLIT_LIMIT, rendered_cap - 128)
        while True:
            chunks = _split_markdown_table_aware(text, src_cap, src_cap)
            worst = max((len(_seal_table_fallback(c)) for c in chunks), default=0)
            if worst <= rendered_cap:
                return chunks
            if src_cap <= _MIN_SPLIT_LIMIT:
                break
            src_cap = _shrunk_limit(src_cap, rendered_cap, worst)
        # Floor reached with an oversize chunk: a single row exceeds the cap,
        # and no row-boundary cut can help. Hand each offender to the bounded
        # splitter, which cuts inside the line. Those pieces lose their table
        # framing (they arrive as escaped text), but losing alignment on one
        # monster row beats the client backstop truncating its tail away. The
        # headroom covers the <pre> wrapper on any piece that stays detected.
        out: list[str] = []
        for c in chunks:
            if len(_seal_table_fallback(c)) > rendered_cap:
                out.extend(_split_markdown_bounded(c, max(_MIN_SPLIT_LIMIT, rendered_cap - 64)))
            else:
                out.append(c)
        return out

    async def _seal_chunk_html(self, chunk: str) -> tuple[bool, int | None]:
        """Seal one leading chunk of an overflowing degraded segment as HTML.

        The first chunk re-uses the streamed bubble (it is the OLDEST message,
        so it must carry the earliest content or the reply reads out of order);
        later chunks are fresh sends. Mirrors the tail seal's degradation
        ladder: HTML edit -> plaintext edit, or HTML send -> plaintext send.

        Its text goes through ``_seam_safe`` like every other sealed segment: this
        is a separate sink from ``_seal_text``, so it carries the seam repair itself
        rather than relying on the other one, and records the predecessor only where
        a delivery reports success.

        Returns whether the chunk landed and, when both edits of the streamed
        bubble failed and the chunk went out below it, that bubble's id. This
        method never deletes it: the chunks and the tail still to come are not
        sent yet, and the bubble may show rows of theirs that no sent message
        carries.
        """
        chunk = await asyncio.to_thread(self._seam_safe, chunk)
        html_text = _seal_table_fallback(chunk)
        if len(html_text) > self._rendered_limit():
            html_text = _md_to_telegram_html(chunk)
        stale_mid: int | None = None
        if self._stream_mid is not None:
            mid = self._stream_mid
            self._stream_mid = None
            ok = await self._client.edit_message(
                self._chat_id, mid, html_text, parse_mode="HTML", retry_plain=False
            )
            if ok:
                self._record_sent(chunk)
                return True, None
            if await self._client.edit_message(self._chat_id, mid, _strip_md(chunk)):
                self._record_sent(chunk)
                return True, None
            # The frame may still exist; the chunk goes out below it.
            stale_mid = mid
        mid2 = await self._client.send_message(
            self._chat_id,
            html_text,
            parse_mode="HTML",
            retry_plain=False,
            message_thread_id=self._thread_id,
        )
        if mid2 is None:
            if await self._client.send_message(
                self._chat_id, _strip_md(chunk), message_thread_id=self._thread_id
            ):
                self._record_sent(chunk)
                return True, stale_mid
            await self._send_lost_part_notice()
            return False, None
        self._record_sent(chunk)
        return True, stale_mid

    async def _send_lost_part_notice(self) -> None:
        """Mark the place of a part whose every send failed.

        The later parts of the reply still go out, so without a mark the reply
        reads as complete. Best-effort: a notice that cannot be sent either is
        logged, never raised.
        """
        logger.warning("Telegram: a part of the reply to chat %s was not delivered", self._chat_id)
        try:
            await self._client.send_message(
                self._chat_id, _LOST_PART_NOTICE, message_thread_id=self._thread_id
            )
        except Exception:
            logger.debug("Telegram: the lost-part notice failed", exc_info=True)

    def _tally_redactions(self, text: str) -> None:
        """Record the redaction placeholders in one LANDED frame's final text."""
        cred_count, url_count = count_redaction_tags(text)
        self._redacted_creds += cred_count
        self._redacted_urls += url_count

    async def _maybe_send_redaction_notice(self) -> None:
        """One best-effort notice for the whole turn, after its answer landed.

        Best-effort by the shared contract: the answer is already out, so a
        failed notice send is logged, never raised — losing the notice is a
        degraded warning, failing the turn would discard a delivered reply.
        Threaded like the answer so the notice lands under the reply it
        describes rather than in the chat root.
        """
        if not (self._redacted_creds or self._redacted_urls):
            return
        try:
            await self._client.send_message(
                self._chat_id,
                redaction_notice(self._redacted_creds, self._redacted_urls),
                message_thread_id=self._thread_id,
            )
        except Exception:
            logger.warning(
                "telegram: could not deliver the redaction notice (answer already sent)",
                exc_info=True,
            )

    async def close(self, failure_reason: str | None = None) -> None:
        """Idempotent teardown: stop the typing indicator and finalize the turn
        if it never reached on_done.

        ``failure_reason`` is an optional, already-sanitized user-safe message
        (see ``transport_dispatch._user_safe_failure_reason``) shown instead of
        the generic error placeholder. It is display-only and ignored once the
        turn is finalized, so every existing no-argument caller is unaffected.
        """
        self._stop_typing()
        if not self._finalized:
            if failure_reason:
                self._failure_reason = failure_reason
            await self.on_done(stop_reason="error")

    # Bound from renderer_approval: the approval prompt.
    on_prompt_choice = _renderer_approval.on_prompt_choice

    # Bound from renderer_status: typing, stall marks, footers.
    on_turn_start = _renderer_status.on_turn_start
    _typing_loop = _renderer_status._typing_loop
    _stop_typing = _renderer_status._stop_typing
    _note_progress = _renderer_status._note_progress
    _stall_mark = _renderer_status._stall_mark
    attach_context_client = _renderer_status.attach_context_client
    on_thinking = _renderer_status.on_thinking
    _turn_footer = _renderer_status._turn_footer
    _thinking_budget = _renderer_status._thinking_budget
    on_tool_call = _renderer_status.on_tool_call
    on_compaction = _renderer_status.on_compaction

    # Bound from renderer_live: chunk intake and the live frame.
    on_text_chunk = _renderer_live.on_text_chunk
    _materialize_chip = _renderer_live._materialize_chip
    _rotate_at_markers = _renderer_live._rotate_at_markers
    _open_new_message = _renderer_live._open_new_message
    _segment_text = _renderer_live._segment_text
    _consume_reply_to = _renderer_live._consume_reply_to
    _stream_live = _renderer_live._stream_live
    _stream_live_locked = _renderer_live._stream_live_locked
    _retire_live_frame = _renderer_live._retire_live_frame
    _delete_replaced_frame = _renderer_live._delete_replaced_frame
    note_steer = _renderer_live.note_steer
    _chip_for_seal = _renderer_live._chip_for_seal

    # Bound from renderer_uploads: outbound images.
    authorize_upload_root = _renderer_uploads.authorize_upload_root
    _uploads_enabled = _renderer_uploads._uploads_enabled
    _extract_uploads = _renderer_uploads._extract_uploads
    _append_rejections = _renderer_uploads._append_rejections
    _send_uploads = _renderer_uploads._send_uploads


#: BASE imports this module binds for its importers and for the patches the mirror
#: routes through it, though its own code does not read them.
_BOUND_FOR_IMPORTERS = (
    APPROVAL_TIMEOUT_S,
    DENY_CAUSE_APPROVAL_TIMEOUT,
    ExtractLimits,
    Rejection,
    TELEGRAM_MAX_MEDIA_GROUP,
    TELEGRAM_MAX_PHOTO_BYTES,
    TELEGRAM_MAX_TOTAL_UPLOAD_BYTES,
    adoptable_reservation,
    extract_local_refs_off_loop,
    new_approval_nonce,
    nonce_eq,
    os,
    sel,
    split_options_trailer,
)
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.telegram.renderer_display",
    "kiro_crew.telegram.renderer_tables",
    "kiro_crew.telegram.renderer_approval",
    "kiro_crew.telegram.renderer_status",
    "kiro_crew.telegram.renderer_live",
    "kiro_crew.telegram.renderer_uploads",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
#: The unsplit module's names: everything the family binds except the methods the class
#: binds from its owners, which were never module names.
_SURFACE = frozenset().union(globals(), *(vars(_sys.modules[o]) for o in _OWNERS)) - {
    n for n, m in vars(TelegramRenderer).items() if getattr(m, "__module__", "") in _OWNERS
}
_EXPORTS.update(
    _facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=(), surface=_SURFACE).served
)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.telegram.renderer_approval import (  # noqa: F401
        _APPROVAL_INPUT_CHARS,
        _APPROVAL_TIMEOUT_S,
        TelegramApprovalDecider,
    )
    from kiro_crew.telegram.renderer_display import (  # noqa: F401
        _MARKUP_HINT_RE,
        _SHRINK_STEP,
        _STEER_MARKER_RE,
        _STEER_SUMMARY_RE,
        _strip_hr,
        _strip_steering,
        _utf16_chunks,
        _utf16_cut,
        _utf16_len,
    )
    from kiro_crew.telegram.renderer_live import (  # noqa: F401
        _EDIT_THROTTLE_S,
    )
    from kiro_crew.telegram.renderer_status import (  # noqa: F401
        _CTX_GAUGE,
        _FOOTER_MIN_CTX_PCT,
        _FOOTER_MIN_SECS,
        _HARD_STALL_MARK,
        _HARD_STALL_S,
        _SOFT_STALL_MARK,
        _SOFT_STALL_S,
        _THINKING_SCAFFOLD,
        _TYPING_REFRESH_S,
    )
    from kiro_crew.telegram.renderer_tables import (  # noqa: F401
        _TABLE_SEP_CHARS,
        _is_table_separator,
        _row_cell_count,
        _row_cells,
    )
    from kiro_crew.telegram.renderer_uploads import (  # noqa: F401
        _MAX_REJECTION_LINES,
        _UPLOAD_LIMITS,
    )
