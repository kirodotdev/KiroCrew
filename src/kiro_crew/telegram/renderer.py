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
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.constants import split_trailing_protocol_suffix
from kiro_crew.messaging.approval import APPROVAL_TIMEOUT_S
from kiro_crew.messaging.display_safety import redact_for_display
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
    new_approval_nonce,
    session_provenance_tag,
    split_options_trailer,
)
from kiro_crew.messaging.split import split_markdown_safe
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.sel import sel
from kiro_crew.telegram.client import (
    TELEGRAM_MAX_MEDIA_GROUP,
    TELEGRAM_MAX_PHOTO_BYTES,
    TELEGRAM_MAX_TOTAL_UPLOAD_BYTES,
    TELEGRAM_RICH_MAX_CHARS,
)

if TYPE_CHECKING:
    from kiro_crew.telegram.client import TelegramClient

logger = logging.getLogger(__name__)

#: Budgets handed to the shared extractor, so an oversize image is refused BY
#: THE READ and keeps its markup rather than being uploaded and 413'd, or
#: stripped out of the text and then dropped.
_UPLOAD_LIMITS = ExtractLimits(
    max_files=TELEGRAM_MAX_MEDIA_GROUP,
    max_total_bytes=TELEGRAM_MAX_TOTAL_UPLOAD_BYTES,
    max_file_bytes=TELEGRAM_MAX_PHOTO_BYTES,
)

#: Refusal lines appended to an answer before the count collapses into a tally.
_MAX_REJECTION_LINES = 3

#: How far past one message a segment may grow while it holds an image reference
#: back from length rotation. The hold exists so a cut cannot bisect
#: ``![alt](path)``; without a ceiling it also means a reference arriving early in
#: a long answer disables rotation for the REST of the turn, so the buffer grows
#: unbounded and every later chunk re-scans all of it. Past this the hold is
#: abandoned and the segment rotates normally — the reference then keeps its
#: markup and prints its path, which is the documented honest degradation.
_UPLOAD_HOLD_LIMIT_FACTOR = 4

# Telegram has no native token streaming: "streaming" meant editing one message
# on every chunk, and each edit is a full HTTP round-trip + a whole-bubble
# re-render, which reads as a stutter (WeCom streams frames over a persistent
# WebSocket, so it stays smooth). Instead we do "block streaming": hold a live
# "typing…" indicator while the answer forms, then post the finished answer as
# one clean block. This kills the edit-jank entirely and only touches this
# renderer -- the shared messaging event stream (and Slack/WeCom) is untouched.
#
# Telegram's "typing" chat action lasts ~5s, so refresh it just under that
# for the duration of a turn (shown while we accumulate before posting).
_TYPING_REFRESH_S = 4.0

# Min seconds between live streaming edits to one message. Telegram rate-limits
# edits (~this cadence), so we coalesce chunks and edit the message in place at
# most this often. The final formatted edit always lands regardless of throttle.
_EDIT_THROTTLE_S = 1.0

# Interactive approval wait; deny-by-default when it elapses with no press.
# Owned by messaging.approval so every channel's window is the same one.
_APPROVAL_TIMEOUT_S = APPROVAL_TIMEOUT_S

# ── Stall marks on the live bubble ──
# Telegram has no message-reaction budget to spend on a phase indicator: a bot
# holds ONE reaction per message (setting is a replace, not an add), its emoji
# allow-list has no globe/wrench/brand mark, a chat's own available_reactions can
# narrow it further at any time, and the rate limit is per CHAT — so a reaction
# per phase competes with the streaming edits this channel already spends. The
# typing indicator plus the transient "🔧 {tool}…" footer already say "working";
# what they cannot say is "working, but nothing has moved in a while". These two
# marks ride the footer the renderer was going to edit anyway, so they cost no
# extra API class. Same thresholds as the Slack controller's soft/hard stalls.
_SOFT_STALL_S = 15.0
_HARD_STALL_S = 45.0
_SOFT_STALL_MARK = "🥱 still working…"
_HARD_STALL_MARK = "😨 this is taking a while…"

#: How much of a tool's arguments the approval prompt shows. Enough to judge a
#: shell command or a file path; past this the operator is reading a payload, not
#: making a decision, and the prompt has to stay inside one message.
_APPROVAL_INPUT_CHARS = 900

#: Context-usage gauge thresholds for the turn footer, and Slack's own: a shared
#: reading of "how close is this conversation to needing /compact".
_CTX_GAUGE = ((70, "🔴"), (50, "🟠"), (30, "🟡"), (0, "🟢"))

#: The footer is shown only when it carries something ACTIONABLE. Slack posts its
#: equivalent on every turn, but Slack has a `context` block — a small grey line
#: — while Telegram's nearest affordance is a quote bar under the answer, and
#: "Finished in 1s · 🟢 ctx 4%" as a permanent fixture under every reply is noise
#: that trains the reader to skip the place the real warning will appear. So:
#: a duration worth noticing, or a context reading worth acting on.
_FOOTER_MIN_SECS = 10.0
_FOOTER_MIN_CTX_PCT = 50

#: The reasoning post's fixed wrapper (``<blockquote expandable>💭 …`` plus its
#: closer). Subtracted from the render budget so the truncation can never cut
#: inside the tags it has to leave balanced.
_THINKING_SCAFFOLD = "<blockquote expandable>💭 </blockquote>"

# Fallback placeholder for a turn that failed without a user-safe reason. The
# retry wording is only correct for transient failures; a permanent failure
# (e.g. the account lacks the selected model) passes its own bounded reason via
# ``close(failure_reason=...)`` so the user is never told to retry an error
# that says retrying will not help.
_GENERIC_ERROR_TEXT = "⚠️ Error — please try again"


def _display_safe(text: str) -> str:
    """Redact against the form Telegram RENDERS, not the bytes we send.

    The byte-level pass in ``TurnDriver`` runs before this renderer introduces any
    markup, and it cannot see a credential that markup will reassemble:
    ``redact_credentials("AKIA**IOSFODNN7EXAMPLE**")`` matches nothing because the
    ``**`` sits inside the key, and then ``_md_to_telegram_html`` emits
    ``AKIA<b>IOSFODNN7EXAMPLE</b>`` — which Telegram displays as an intact access
    key. ``[AKIA](https://x)IOSFODNN7EXAMPLE`` is the same hazard through a link,
    and a zero-width character between two halves is the same hazard with no
    markup at all. ``redact_for_display`` canonicalizes to the rendered form and
    scans BOTH that and the literal, which is why it exists and why Slack and
    Discord both run it at their own render boundaries.

    Applied at the two sinks, BEFORE any tags are introduced: the live plaintext
    frame and the seal. A redaction can make the text LONGER than the segment
    budget that sized it; that is accepted, and the seal re-measures and re-splits
    against the render cap. Losing formatting to keep a rendered secret redacted
    is the documented trade.

    Runs the SHARED ``_default_redactor`` (exfil URLs then credentials), the same
    pair ``TurnDriver`` streams provider text through, so a display sink cannot end
    up scanning for less than the stream did.
    """
    safe, _ = redact_for_display(text or "", _default_redactor)
    return safe


def _utf16_len(text: str) -> int:
    """Length of *text* in UTF-16 code units — the unit of Telegram's caps.

    Python counts code points; the Bot API counts UTF-16 units, so every
    astral character (emoji, most notably) costs 2 against Telegram's 4096
    while costing 1 against ``len``. The entity machinery in
    ``telegram/client.py`` already measures in these units; message budgets
    here historically did not.
    """
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def _utf16_cut(text: str, limit: int) -> int:
    """Largest CODE-POINT index whose prefix fits ``limit`` UTF-16 units.

    Returning a code-point index means ``str`` slicing can never bisect a
    surrogate pair: an astral character either fits whole or is excluded
    whole. The floor of 2 is load-bearing — at ``limit=1`` a leading astral
    character (2 units) would cut at index 0, and a caller consuming the
    text chunk by chunk would stop making progress.
    """
    budget = max(2, limit)
    units = 0
    for index, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > budget:
            return index
    return len(text)


def _utf16_chunks(text: str, limit: int) -> list[str]:
    """Split ``text`` into pieces of at most ``limit`` UTF-16 units each.

    Prefers newline boundaries so a line (one restored image reference, in
    the recovery caller) stays whole within one message; a single line
    larger than the whole budget is hard-cut at a code-point boundary
    rather than dropped. Boundary newlines are consumed by the split.
    """
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if _utf16_len(candidate) <= max(2, limit):
            current = candidate
            continue
        if current:
            chunks.append(current)
        while _utf16_len(line) > max(2, limit):
            cut = _utf16_cut(line, limit)
            chunks.append(line[:cut])
            line = line[cut:]
        current = line
    if current:
        chunks.append(current)
    return chunks


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


def _extract_options(text: str) -> tuple[str, list[str]]:
    """Split text into ``(body, options)``, holding back a streamed partial.

    ``hide_partial=True`` because this renderer STREAMS: a still-arriving
    ``[OPTIONS…`` fragment really may be a marker mid-flight, and the next frame
    re-renders from the full buffer, so hiding it costs nothing permanent.
    """
    return split_options_trailer(text, hide_partial=True)


# kiro-cli emits an inline "[STEERING steer-<id>: …]" ack marker when it folds a
# mid-turn steer at a boundary. The dashboard parses it into a chip; Telegram has
# no parser, so strip it — the user's own steer message already shows the
# instruction (and gets a steer-ack reaction), so the raw inline marker is just
# redundant noise in the bubble.
_STEER_MARKER_RE = re.compile(r"\[STEERING\b[^\]]*\]", re.IGNORECASE)
# Same marker, capturing the ack SUMMARY kiro-cli embeds after "steer-<id>:".
# The dashboard renders this summary as its "Steered — …" chip; we prefer it
# for the Telegram chip too (the user's own words are already on screen as
# their message — the summary is the only NEW information).
_STEER_SUMMARY_RE = re.compile(r"\[STEERING\s+steer-[0-9a-f]+\s*:\s*([^\]]*)\]", re.IGNORECASE)


def _strip_steering(text: str) -> str:
    """Remove kiro-cli's inline ``[STEERING …]`` steer-ack marker from output.

    Also strips an UNCLOSED trailing ``[STEERING …`` (still streaming, no closing
    ``]`` yet): otherwise the marker's long rephrase streams into the live draft
    and then vanishes when ``on_done`` finally strips the completed marker — a
    jarring show-then-vanish. Stripping the partial marker keeps the draft in
    sync with the final message.
    """
    cleaned = _STEER_MARKER_RE.sub("", text)  # complete markers anywhere
    cleaned = re.sub(r"\[STEERING\b[^\]]*$", "", cleaned)  # unclosed, still streaming
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)  # collapse gaps left behind
    return cleaned.strip()


def _neutralize_md(raw: str) -> str:
    """Collapse whitespace, cap length, and strip Markdown control chars from a
    steer's text so the chip renders literally (inside a blockquote) and can't
    perturb surrounding formatting."""
    t = " ".join((raw or "").split())[:120]
    return re.sub(r"[*_`\[\]()]", "", t)


def _strip_hr(text: str) -> str:
    """Drop Markdown horizontal rules (``---`` / ``***`` / ``___`` on their own
    line) — they render as literal dashes on Telegram and just add noise.
    Fenced code blocks are stashed first so a standalone ``---`` INSIDE a code
    block (e.g. a YAML document separator) is never touched. An unclosed fence
    mid-stream isn't stashed, but live frames are throttled previews — the
    final seal sees the closed fence and preserves its content."""
    stash: list[str] = []

    def _keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00H{len(stash) - 1}\x00"

    text = _FENCE_RE.sub(lambda m: _keep(m.group(0)), text)
    # Max 3 leading spaces (markdown HR rule) — a 4-space-indented "---" is
    # indented CODE (e.g. a YAML separator) and must survive.
    out = re.sub(r"(?m)^[ ]{0,3}([-*_])\1{2,}[ \t]*$", "", text)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"\x00H(\d+)\x00", lambda m: stash[int(m.group(1))], out)
    return out.strip()


def build_inline_keyboard(options: list[str], session_key: str) -> dict | None:
    """Build an InlineKeyboardMarkup from ``[OPTIONS:]`` labels.

    ``callback_data`` is ``opt:<index>:<session-tag>``. The label stays out of
    the payload because Telegram caps it at 64 bytes and a multi-byte CJK/emoji
    label could overflow. The compact deterministic tag binds a later press to
    the session that posted the keyboard; the label is recovered from the button
    text at callback time. Two buttons per row keeps the keyboard mobile-friendly.

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
    buttons: list[list[dict]] = []
    row: list[dict] = []
    for i, opt in enumerate(options):
        safe, _ = redact_for_display(opt, _default_redactor)
        row.append({"text": safe[:64], "callback_data": f"opt:{i}:{origin_tag}"})
        if len(row) == 2:
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
    """
    return split_markdown_safe(text, limit)


# Telegram renders a small HTML subset (<b>/<i>/<code>/<pre>/<a>) far more
# reliably than MarkdownV2 (which needs every '.', '-', '!', '(' escaped). The
# agent emits generic Markdown, so we translate it to Telegram HTML for the
# final message. Code spans are stashed first so their contents are never
# treated as markup, then the remaining text is HTML-escaped before any tags
# are introduced -- so raw '<', '>' and '&' in the answer can't break the parse.
_FENCE_RE = re.compile(r"```[^\n]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$", re.MULTILINE)
_BOLD_STAR_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_USCORE_RE = re.compile(r"__(.+?)__", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"(?<!\w)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\w)")
_ITALIC_USCORE_RE = re.compile(r"(?<!\w)_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)

# Characters a GFM separator row may contain (`| --- |`, `|:---|---:|`, `- | -`).
_TABLE_SEP_CHARS = set("-:| \t")


#: A line that opens or closes a fenced code block.
_FENCE_LINE_RE = re.compile(r"^[ \t]*(?:`{3,}|~{3,})", re.MULTILINE)


def _is_table_separator(line: str) -> bool:
    """True if *line* is a GFM separator row (``| --- |``, ``---|---``).

    Every cell must be non-empty and carry a dash. Both halves are what the
    server was observed to do, not what a spec reading predicts: ``| --- | |``
    comes back as a ``paragraph`` (so an empty cell must be rejected, or the
    block ships flattened), while ``| - - | --- |`` comes back as a ``table``
    (so a broken dash run must be ACCEPTED -- demanding a contiguous run here
    would degrade a table the server renders fine into a monospace block).
    """
    stripped = line.strip()
    if not stripped or not set(stripped) <= _TABLE_SEP_CHARS:
        return False
    if "-" not in stripped or "|" not in stripped:
        return False
    cells = _row_cells(stripped)
    if len(cells) > 1 and cells[0].strip() == "":
        cells = cells[1:]
    if len(cells) > 1 and cells[-1].strip() == "":
        cells = cells[:-1]
    return bool(cells) and all("-" in cell for cell in cells)


def _row_cells(row: str) -> list[str]:
    """Split a table row on its UNESCAPED cell boundaries.

    Escaping is decided by walking the row, not by a lookbehind: ``\\|`` is cell
    content, but ``\\\\`` is a literal backslash that leaves a following ``|`` as
    a real boundary. A fixed-width lookbehind cannot express that -- it reads the
    second backslash of an even run as an escape and merges two cells, which
    under-counts the row and can make a malformed header match its delimiter.
    """
    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in row:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            buf.append(ch)
            escaped = True
        elif ch == "|":
            cells.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    cells.append("".join(buf))
    return cells


def _row_cell_count(row: str) -> int:
    """Number of cells in a GFM table row.

    Outer pipes are optional, so a single leading and a single trailing boundary
    -- which show up as empty first/last cells -- are dropped.
    """
    cells = _row_cells(row.strip())
    if len(cells) > 1 and cells[0] == "":
        cells = cells[1:]
    if len(cells) > 1 and cells[-1] == "":
        cells = cells[:-1]
    return len(cells)


def _has_table(text: str) -> bool:
    """True if *text* contains a GFM pipe table.

    A table is a pipe-bearing line immediately followed by a separator row whose
    cell count MATCHES it. The count check is what GFM (and therefore Telegram's
    Rich Markdown) uses to decide a table exists at all, so skipping it sends
    content down the rich path that the server then renders as a plain paragraph
    -- newlines collapsed, pipes literal, worse than the monospace fallback.

    Outer pipes are optional on BOTH rows, because GFM accepts ``a | b`` /
    ``--- | ---`` with no leading or trailing pipe -- anchoring on a leading
    ``|`` silently missed those and rendered them as literal pipes.

    The separator row must contain a dash (so it is a separator, not more data)
    and a pipe (so a bare ``-----`` horizontal rule under a pipe-bearing
    sentence is not mistaken for a table). The header must contain a pipe for
    the same reason: a one-cell separator would otherwise promote any ordinary
    sentence above it to a table.

    Deliberately does NOT exclude fenced code blocks. Table markup inside a
    fence only means one extra rich send, not wrong output: Rich Markdown parses
    fences itself and renders the sample as the code block it is. Screening for
    fences here would mean maintaining CommonMark's fence rules (delimiter
    character, run length, indentation, info strings) as a second parser beside
    the one the HTML renderer already owns, and every gap between the two is a
    bug -- for a check whose only job is deciding which transport to use.
    """
    lines = text.split("\n")
    return any(
        "|" in header
        and _is_table_separator(sep)
        and _row_cell_count(header) == _row_cell_count(sep)
        for header, sep in zip(lines, lines[1:])
    )


def _table_blocks(text: str) -> list[tuple[bool, list[str]]]:
    """Partition *text* into alternating ``(is_table, lines)`` blocks.

    A table run starts on a pipe-bearing line whose successor is a separator row
    and extends while lines carry a pipe. This is the single run detector behind
    both the degraded seal (``_seal_table_fallback``) and table-aware splitting,
    so the two can never disagree about where a table begins or ends.

    It is deliberately looser than ``_has_table``, which additionally requires
    the separator's cell count to match its header. The two are not
    interchangeable and must not be unified: this one decides how a run is
    FRAMED, so over-matching costs nothing (``<pre>`` reproduces its input
    verbatim), while ``_has_table`` decides which TRANSPORT a segment takes and
    so must agree with the server's own GFM rule.

    Callers must screen out fenced text first (see
    ``_split_markdown_table_aware``).
    """
    lines = text.split("\n")
    blocks: list[tuple[bool, list[str]]] = []
    prose: list[str] = []
    i = 0
    while i < len(lines):
        if "|" in lines[i] and i + 1 < len(lines) and _is_table_separator(lines[i + 1]):
            if prose:
                blocks.append((False, prose))
                prose = []
            block = [lines[i], lines[i + 1]]
            i += 2
            # Body rows run until the first line that is not part of the table.
            while i < len(lines) and "|" in lines[i]:
                block.append(lines[i])
                i += 1
            blocks.append((True, block))
            continue
        prose.append(lines[i])
        i += 1
    if prose:
        blocks.append((False, prose))
    return blocks


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

    text = _FENCE_RE.sub(
        lambda m: _keep(f"<pre>{html.escape(m.group(1).rstrip(chr(10)))}</pre>"), text
    )
    text = _INLINE_CODE_RE.sub(lambda m: _keep(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = html.escape(text)
    text = _HEADING_RE.sub(lambda m: f"<b>{m.group(1).strip()}</b>", text)
    text = _BOLD_STAR_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _BOLD_USCORE_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _ITALIC_STAR_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _ITALIC_USCORE_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
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


#: Minimum source-chunk budget when shrinking to fit the rendered cap. Below
#: this, shrinking stops making progress; the caller's tag-safe truncation and
#: plaintext fallback are the backstop for genuinely indivisible content.
_MIN_SPLIT_LIMIT = 400

#: How much one shrink round cuts at minimum, BEFORE the ``_MIN_SPLIT_LIMIT``
#: clamp. The proportional step alone cuts 5% of the current budget, which for a
#: small budget is a few tens of characters, so this is what bounds the number of
#: re-split rounds; above roughly 20x it the 5% cut is the larger of the two and
#: this never binds. Near the floor the clamp wins and the real cut is smaller.
_SHRINK_STEP = 128


def _shrunk_limit(current: int, rendered_cap: int, worst: int) -> int:
    """The next source budget to try after a chunk rendered past ``rendered_cap``.

    Shrinks in proportion to the observed inflation (``rendered_cap / worst``)
    with a 5% margin, taking at least ``_SHRINK_STEP`` unless the floor is
    nearer than that, and drops straight to ``_MIN_SPLIT_LIMIT`` when the
    proportional answer would not shrink at all.

    **Callers must hold ``current > _MIN_SPLIT_LIMIT`` and
    ``worst > rendered_cap > 0``.** Under those the result is strictly below
    *current* and never below the floor, which is what makes a shrink loop
    terminate; at or below the floor there is nowhere left to shrink to and the
    answer is the floor itself, so a caller that skips the guard would spin.
    Both call sites guard it before they get here.
    """
    scaled = int(current * (rendered_cap / worst) * 0.95)
    nxt = max(_MIN_SPLIT_LIMIT, min(scaled, current - _SHRINK_STEP))
    return nxt if nxt < current else _MIN_SPLIT_LIMIT


def _rendered_len(source: str) -> int:
    """Length of ``source`` once converted to Telegram HTML."""
    return len(_md_to_telegram_html(source))


#: Markup that makes ``_md_to_telegram_html`` EMIT TAGS, and therefore grow the
#: text by an amount no per-character arithmetic can bound: a line-leading ``>``
#: (blockquote) or ``#`` (heading), or any ``*``/``_``/backtick/``[`` (bold,
#: italic, code span, link). Cheap single-pass scan; used only as a gate.
_MARKUP_HINT_RE = re.compile(r"(?m)^[ \t]{0,3}[>#]|[*_`\[]")


def _may_exceed_rendered(source: str, cap: int) -> bool:
    """Conservative "could this render past ``cap``?" gate.

    A ``False`` MUST mean "provably fits" -- it is the only case the caller skips
    the authoritative ``_rendered_len`` check for, so under-estimating ships
    oversize HTML. Over-returning ``True`` is always safe: it costs one real
    render on a buffer that is nowhere near the cap.

    Per-construct growth constants were tried and rejected as whack-a-mole --
    blockquote, heading, bold/italic and code spans each need their own term,
    the measured inflation reaches 3.75x (``"`x` " * 700``: 2800 source chars ->
    10500 rendered), and any new converter rule silently reintroduces
    unsoundness. Instead trust the cheap arithmetic ONLY for prose containing no
    tag-producing markup at all, and measure everything else for real.
    """
    if len(source) >= cap:
        return True
    if _MARKUP_HINT_RE.search(source):
        return True  # tags will be emitted -- refuse to guess, measure instead
    # Tag-free prose: the only growth left is ``html.escape``, which adds at most
    # 5 chars per escape-worthy character (``'`` -> ``&#x27;``).
    escapes = sum(source.count(c) for c in ("&", "<", ">", '"', "'"))
    return len(source) + 5 * escapes >= cap


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
    """
    src_limit = max(_MIN_SPLIT_LIMIT, rendered_limit)
    chunks = _split_markdown(text, src_limit)
    while True:
        worst = max((_rendered_len(c) for c in chunks), default=0)
        if worst <= rendered_limit or src_limit <= _MIN_SPLIT_LIMIT:
            return chunks
        src_limit = _shrunk_limit(src_limit, rendered_limit, worst)
        chunks = _split_markdown(text, src_limit)


def _split_table_rows(rows: list[str], limit: int) -> list[str]:
    """Split one table run at ROW boundaries into chunks of <=``limit`` chars.

    Every continuation chunk repeats the header and separator row, so each
    chunk is independently detected by ``_has_table`` and seals through the
    rich path. The alternative (bare body rows) fails detection and arrives as
    literal pipes -- the exact defect table-aware splitting exists to fix.

    A single row longer than ``limit`` cannot be cut (there is no sub-row
    boundary that keeps the table valid); its chunk is returned oversize and
    the client's tag-safe truncation is the backstop.
    """
    header, sep = rows[0], rows[1]
    head_len = len(header) + len(sep) + 2  # + the two joining newlines
    chunks: list[str] = []
    cur = [header, sep]
    cur_len = head_len
    for row in rows[2:]:
        row_cost = len(row) + 1  # + the joining newline
        if cur_len + row_cost > limit and len(cur) > 2:
            chunks.append("\n".join(cur))
            cur = [header, sep]
            cur_len = head_len
        cur.append(row)
        cur_len += row_cost
    chunks.append("\n".join(cur))
    return chunks


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
    """
    if _FENCE_LINE_RE.search(text):
        return _split_markdown_bounded(text, rendered_limit)
    out: list[str] = []
    for is_table, lines in _table_blocks(text):
        block = "\n".join(lines)
        if is_table:
            if len(block) <= rich_limit:
                out.append(block)
            else:
                out.extend(_split_table_rows(lines, rich_limit))
        elif block.strip():
            out.extend(_split_markdown_bounded(block, rendered_limit))
    return [c for c in out if c.strip()]


def _strip_md(text: str) -> str:
    """Flatten Markdown to clean plaintext for the streaming typewriter frames
    (and as the safe fallback if an HTML final edit is ever rejected) -- avoids
    showing raw ``**``/``##``/``[x](url)`` noise while the answer is forming."""
    text = _FENCE_RE.sub(lambda m: m.group(1), text)
    text = _INLINE_CODE_RE.sub(lambda m: m.group(1), text)
    text = _HEADING_RE.sub(lambda m: m.group(1).strip(), text)
    text = _BOLD_STAR_RE.sub(lambda m: m.group(1), text)
    text = _BOLD_USCORE_RE.sub(lambda m: m.group(1), text)
    text = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}\u2022 ", text)
    return text


class TelegramApprovalDecider:
    """Awaits an inline-button approval for a tool-permission request.

    Process-global Future registry keyed by ``session_key:request_id`` so
    concurrent turns (and users) never resolve each other's prompts. Denies by
    default when the wait elapses.
    """

    _REGISTRY: dict[str, "asyncio.Future[bool]"] = {}
    #: registry key -> the nonce minted for the buttons now showing. A press whose
    #: nonce does not match is refused, which is what stops a button from a previous
    #: run answering a live prompt that reuses its request id.
    _NONCES: dict[str, str] = {}

    def __init__(self, *, session_key: str) -> None:
        self._session_key = session_key

    @staticmethod
    def key(session_key: str, request_id: str | int) -> str:
        return f"{session_key}:{request_id}"

    @classmethod
    def arm(cls, key: str, nonce: str) -> None:
        """Record the nonce for the buttons the renderer is about to post."""
        cls._NONCES[key] = nonce

    async def __call__(self, event: Any) -> bool:
        k = self.key(self._session_key, getattr(event, "request_id", ""))
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        TelegramApprovalDecider._REGISTRY[k] = fut
        try:
            return bool(await asyncio.wait_for(fut, _APPROVAL_TIMEOUT_S))
        except asyncio.TimeoutError:
            return False  # deny-by-default on timeout
        finally:
            TelegramApprovalDecider._REGISTRY.pop(k, None)
            # Retire the nonce with the prompt, so a button for a request id the
            # provider later reuses cannot match a nonce that is no longer live.
            TelegramApprovalDecider._NONCES.pop(k, None)

    @classmethod
    def nonce_matches(cls, key: str, nonce: str) -> bool:
        """Whether *nonce* is the one minted for the prompt currently at *key*.

        The binding a key alone cannot provide. ACP request ids are REUSABLE — a
        provider or gateway restart resets the sequence — and the conversation
        generation only changes on ``/new`` or an idle/daily rotation. So a
        provider that restarts mid-conversation issues request id 1 again, and a
        stale button from before the restart carries that same id: pressing it
        resolves a prompt for an UNRELATED tool the user never read, and on the
        Trust button also hands out standing auto-approve for the conversation.
        Only an unpredictable per-prompt value closes that, so the nonce is the
        thing actually checked and the key is just where it is filed.

        Constant-time compare, and fails closed on a missing or empty value.
        """
        expected = cls._NONCES.get(key)
        if not expected or not nonce:
            return False
        return secrets.compare_digest(nonce, expected)

    @classmethod
    def is_pending(cls, key: str, nonce: str = "") -> bool:
        """Whether a live, unresolved prompt is registered for *key*.

        Asked BEFORE a side effect that a press should only be able to cause
        while its prompt is still live. The registry is empty after a gateway
        restart, so every approval button still sitting in a chat's scrollback
        would otherwise take effect against a session that no longer exists.

        *nonce* is checked when supplied, so a caller asking "may this PRESS act"
        gets the prompt-identity answer rather than the weaker key-identity one.
        """
        if nonce and not cls.nonce_matches(key, nonce):
            return False
        fut = cls._REGISTRY.get(key)
        return fut is not None and not fut.done()

    @classmethod
    def resolve_global(cls, key: str, approved: bool, *, nonce: str = "") -> bool:
        """Resolve a pending approval by key. Returns True iff one was waiting
        AND the pressed button's nonce matches the one minted for that prompt."""
        if not cls.nonce_matches(key, nonce):
            return False  # stale or foreign button — fail closed
        fut = cls._REGISTRY.get(key)
        if fut is not None and not fut.done():
            fut.set_result(bool(approved))
            return True
        return False


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

    # -- lifecycle ----------------------------------------------------------
    async def on_turn_start(self) -> None:
        # Typing indicator only — no draft preview. Idempotent (dispatch + driver
        # both call this).
        if self._typing_task is not None or self._closed:
            return
        self._note_progress()
        self._typing_task = asyncio.create_task(self._typing_loop())

    async def _typing_loop(self) -> None:
        """Keep the 'typing…' chat action alive (it expires after ~5s) for the
        duration of the turn, and surface a stall mark when nothing has moved.

        The stall check rides this existing tick rather than a task of its own:
        the loop already wakes every ``_TYPING_REFRESH_S``, which is finer than
        the soft threshold, and the mark it publishes goes on a frame the
        renderer edits anyway. Cancelled by ``_stop_typing``.
        """
        try:
            while not self._closed:
                try:
                    await self._client.send_typing(self._chat_id, message_thread_id=self._thread_id)
                except Exception:
                    logger.debug("Telegram: typing refresh failed", exc_info=True)
                if not self._closed and self._stall_mark() != self._shown_stall:
                    # force=True so the mark lands now: the throttle exists to
                    # coalesce a token stream, and by definition there is none.
                    try:
                        await self._stream_live(force=True)
                    except Exception:
                        logger.debug("Telegram: stall mark refresh failed", exc_info=True)
                await asyncio.sleep(_TYPING_REFRESH_S)
        except asyncio.CancelledError:
            pass

    def _stop_typing(self) -> None:
        self._closed = True
        task, self._typing_task = self._typing_task, None
        if task is not None and not task.done():
            task.cancel()

    def _note_progress(self) -> None:
        """Reset the stall clock, and end any approval wait. Any provider event is
        progress — and an event arriving after an approval prompt is exactly how
        this renderer learns the decision was made."""
        self._last_progress = time.monotonic()
        self._awaiting_approval = False

    def _stall_mark(self) -> str:
        """The stall mark the live frame should carry right now, or ``""``.

        Read from the clock rather than latched, so the mark clears itself the
        moment output resumes instead of persisting to the end of the turn.

        Suppressed while a tool approval is pending: the turn is blocked on the
        USER, not stalled, and the approval window is 300s against a 45s hard
        mark — so without this every approval that waits a minute reports the
        agent as hung. Slack's controller pauses its own watchdog for the same
        reason (``pause_stall_watchdog`` around its approval wait).
        """
        if self._awaiting_approval:
            return ""
        idle = time.monotonic() - self._last_progress
        if idle >= _HARD_STALL_S:
            return _HARD_STALL_MARK
        if idle >= _SOFT_STALL_S:
            return _SOFT_STALL_MARK
        return ""

    async def on_text_chunk(self, text: str) -> None:
        self._buf.append(text)
        self._note_progress()
        self._tool = ""  # text resumed -> drop the transient tool footer
        # 1) Defensive fallback for callers that bypass TurnDriver and
        #    deliver raw protocol text directly to the renderer.
        await self._rotate_at_markers()
        # 1b) Materialize the pending chip once real post-steer text exists.
        self._materialize_chip()
        # 2) Rotate when a segment would exceed one Telegram message.
        await self._rotate_on_length()
        # 3) Live-stream the current segment: edit the message in place (throttled).
        await self._stream_live()

    def _materialize_chip(self) -> None:
        """Prepend the pending steer chip to the segment — but only when the
        segment carries real text. Keeps an end-of-stream marker from ever
        posting a chip-only ack bubble."""
        if self._pending_chip and self._segment_text().strip():
            body = "".join(self._buf).lstrip("\n")
            self._buf = [f"{self._pending_chip}\n\n{body}"]
            self._pending_chip = ""

    async def on_steer_consumed(self, summary: str = "") -> None:
        """Seal the pre-steer segment at the driver's structured boundary."""
        self._note_progress()
        self._materialize_chip()
        await self._rotate_on_length()
        # A trailing [OPTIONS:] block belongs to the visible PRE-STEER answer,
        # but the steering marker sits after it in the raw buffer, so the
        # end-of-buffer anchor no longer sees it. Extract it here -- BEFORE the
        # seal -- so the choices ship as a keyboard on the sealed message instead of
        # being frozen as literal protocol text the user cannot act on.
        body_raw, opts = _extract_options("".join(self._buf))
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

    async def _rotate_at_markers(self) -> None:
        """Defence for callers that bypass TurnDriver and pass raw markers."""
        while True:
            self._materialize_chip()
            raw = "".join(self._buf)
            marker = _STEER_MARKER_RE.search(raw)
            if marker is None:
                return
            self._buf = [raw[: marker.start()]]
            summary_match = _STEER_SUMMARY_RE.match(raw, marker.start())
            summary = _neutralize_md(summary_match.group(1)) if summary_match else ""
            await self.on_steer_consumed(summary)
            self._buf = [raw[marker.end() :]]

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
                for chunk in _split_markdown_bounded(raw[: spans[0][0]], rendered_cap):
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
            chunks = _split_markdown_table_aware(head, rendered_cap, rich_cap)
            if chunks:
                # Reattach what the line-joining splitter drops: the complete
                # prefix's trailing newlines, then the unterminated line. The
                # tail keeps streaming, so both must survive or the next
                # streamed token glues onto the previous row.
                chunks[-1] += head[len(head.rstrip("\n")) :] + partial
            else:
                chunks = [partial]
        else:
            chunks = _split_markdown_bounded(raw, rendered_cap)
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
        for ch in chunks[:-1]:
            self._buf = [ch]
            # A length rotation never extracts: only a SEMANTIC seal (steer
            # boundary / end of turn) sees the reference in its whole-text fence
            # context, and the guard above has already kept any reference out of
            # these chunks.
            await self._seal_current(extract_uploads=False)
            self._open_new_message()
        self._buf = [(chunks[-1] if chunks else "") + protocol_suffix]

    def _open_new_message(self) -> None:
        """Next render creates a fresh message instead of editing the old one."""
        self._stream_mid = None
        self._shown = ""

    def _segment_text(self) -> str:
        """Current segment's markdown source (chip already seeded into _buf),
        with the steer marker and horizontal-rule noise stripped."""
        return _strip_hr(_strip_steering("".join(self._buf)))

    def _consume_reply_to(self) -> int | None:
        """The reply target for this send, spent so only the FIRST one carries it.

        Every later bubble of the same turn — a rotated frame, an image, a
        follow-up segment — is already attached by adjacency, and a reply quote on
        each one would triple the visual weight of a multi-part answer for no
        added information. Consuming here rather than at the call sites means a
        send path that runs first cannot leave the target for a later one to spend
        a second time; whichever path opens the turn gets it.
        """
        target, self._reply_to = self._reply_to, None
        return target

    async def _stream_live(self, *, force: bool = False) -> None:
        """Throttled in-place edit of the current segment (plaintext, so partial
        markdown never 400s). Sends the message on first render. The transient
        ``🔧 {tool}…`` footer and the stall mark are appended ONLY here (live
        frames) — seals and the final render read ``_segment_text`` and never
        carry either. ``force`` bypasses the throttle so a tool-call event, or a
        stall mark the typing tick noticed, surfaces immediately.

        Serialized by ``_frame_lock``: the typing loop is a SEPARATE task, so
        without it a stall-mark frame could interleave with a token frame and
        leave the older text on screen.
        """
        async with self._frame_lock:
            await self._stream_live_locked(force=force)

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
        if seg == self._safe_src:
            return self._safe_out
        hide = hide_local_refs if self._uploads_enabled() else None

        def _render() -> str:
            return _display_safe(_strip_md(hide(seg) if hide else seg))

        out = await asyncio.to_thread(_render)
        self._safe_src, self._safe_out = seg, out
        return out

    async def _stream_live_locked(self, *, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_edit < _EDIT_THROTTLE_S:
            return
        # Hold back trailing [OPTIONS:] markup (complete or still-streaming
        # partial) from live frames — it is an internal directive, extracted
        # into the inline keyboard at finalization.
        seg, _ = _extract_options(self._segment_text())
        body = await self._safe_body(seg)
        stall = self._stall_mark()
        # The tool footer wins: it names what is happening, which is strictly
        # more informative than "nothing has happened".
        footer = f"🔧 {self._tool}…" if self._tool else stall
        if footer:
            # Keep the footer visible even when it must displace body tail chars.
            room = self._limit() - len(footer) - 2
            text = f"{body[:room]}\n\n{footer}".strip() if room > 0 else footer
        else:
            text = body[: self._limit()]
        # Recorded BEFORE the suppression check: when the frame is unchanged there
        # is nothing to send, but the typing tick compares against this to decide
        # whether to try at all — leaving it stale makes it retry every tick for
        # the rest of the turn.
        self._shown_stall = stall
        if not text or text == self._shown:
            return
        self._last_edit = now
        self._shown = text
        if self._stream_mid is None:
            mid = await self._client.send_message(
                self._chat_id,
                text,
                message_thread_id=self._thread_id,
                reply_to_message_id=self._consume_reply_to(),
            )
            if mid is not None:
                self._stream_mid = mid
        else:
            await self._client.edit_message(self._chat_id, self._stream_mid, text)

    async def _seal_without_rich(self, text: str) -> tuple[str, str]:
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
        message.
        """
        html_text = _seal_table_fallback(text)
        if len(html_text) > self._rendered_limit():
            html_text = _md_to_telegram_html(text)
            if len(html_text) > self._rendered_limit():
                chunks = self._degraded_table_chunks(text)
                for ch in chunks[:-1]:
                    await self._seal_chunk_html(ch)
                if chunks:
                    text = chunks[-1]
                html_text = _seal_table_fallback(text)
                if len(html_text) > self._rendered_limit():
                    html_text = _md_to_telegram_html(text)
        return html_text, text

    # ── Outbound image upload ──────────────────────────────────────────────

    def attach_context_client(self, client: Any) -> None:
        """Supply the provider's ACP client, whose ``context_usage_pct`` the turn
        footer reads. Set after the session is acquired, because the provider does
        not exist before then; absent, the footer reports duration only."""
        self._ctx_client = client

    def authorize_upload_root(self, root: object) -> None:
        """Authorize the provider's resolved cwd as extraction's approved root.

        Anything that is not an ABSOLUTE STRING PATH disables uploads rather than
        widening them: this root is the trust boundary extraction measures every
        reference against — it refuses a path lexically outside the root before
        any metadata probe — so a value it cannot evaluate must be no root at all,
        not a root it guesses at.
        """
        if isinstance(root, str) and os.path.isabs(root):
            self._upload_root = root
            return
        self._upload_root = ""
        if root:
            # A non-empty root we cannot trust is worth a line: it is the
            # difference between "this instance has no provider cwd" and "the cwd
            # changed shape", and both present as uploads silently not happening.
            logger.info("telegram: refusing an untrusted upload root (%s)", type(root).__name__)

    def _uploads_enabled(self) -> bool:
        """Transport capability AND an unrestricted session AND a trusted root."""
        return (
            bool(self.capabilities.files_outbound)
            and self._uploads_allowed
            and bool(self._upload_root)
        )

    async def _extract_uploads(self, text: str) -> tuple[str, list[OutboundFile]]:
        """Pull local image references out of one sealed segment, fail-soft.

        Runs off-loop (extraction reads files) and never raises into the seal: a
        failure here must cost the picture, not the answer.
        """
        try:
            result = await extract_local_refs_off_loop(
                text, within_root=self._upload_root, limits=_UPLOAD_LIMITS
            )
        except Exception:
            logger.warning("telegram: outbound file extraction failed", exc_info=True)
            return text, []
        if result.rejections:
            sel().log_api_access(
                caller=self._session_key or "telegram",
                operation="telegram_renderer.upload_files",
                outcome="denied",
                source="telegram",
                resources=f"{len(result.rejections)} rejection(s)",
                # Only the closed reason CODES — never the LLM-authored destination.
                error=",".join(sorted({item.reason for item in result.rejections})),
            )
        body = result.rewritten_text.strip()
        if not body and not result.files:
            body = text
        if result.rejections:
            body = self._append_rejections(body, result.rejections)
        if result.files:
            sel().log_api_access(
                caller=self._session_key or "telegram",
                operation="telegram_renderer.upload_files",
                outcome="allowed",
                source="telegram",
                resources=f"{len(result.files)} file(s)",
            )
        return body, result.files

    def _append_rejections(self, body: str, rejections: list[Rejection]) -> str:
        """Name every refusal in the answer, as long as the budget permits.

        A file dropped in silence leaves a reply that talks about a picture with
        no picture and no explanation, so the reasons are surfaced; past
        ``_MAX_REJECTION_LINES`` they collapse into a tally rather than crowding
        out the answer.
        """
        for rejection in rejections:
            logger.info("telegram: local image not uploaded (%s)", rejection.reason)
        lines = [f"⚠️ {rejection}" for rejection in rejections[:_MAX_REJECTION_LINES]]
        if len(rejections) > _MAX_REJECTION_LINES:
            lines.append(f"⚠️ …and {len(rejections) - _MAX_REJECTION_LINES} more")
        note = "\n".join(lines)
        if len(body) + len(note) + 2 > self._limit():
            return body
        return f"{body}\n\n{note}"

    async def _send_uploads(self, files: list[OutboundFile]) -> None:
        """Ship the extracted images as their own message, after the text seal.

        Photos deliberately do NOT ride the answer as a caption. A caption is
        capped at 1024 characters against the message's 4096, carries no
        ``reply_markup`` on an album, and the answer has already been rendered
        through this channel's HTML/table machinery — folding a truncated second
        copy into a caption would be strictly worse than one clean bubble followed
        by its pictures. ``disable_notification`` because the answer bubble
        already pinged.

        On failure the REFERENCES are restored, not the segment. Unlike Discord —
        which sends text and files in one multipart call, so recovery has to
        re-post the whole thing — the text bubble here has already landed, so
        re-posting the source would duplicate the entire answer. The markup is
        rebuilt from each ``OutboundFile``'s own alt and path (``path`` is
        provenance for exactly this) so the user learns which picture is missing
        and where it is, without the answer arriving twice.
        """
        if not files:
            return
        try:
            sent = await self._client.send_media_group(
                self._chat_id,
                files,
                message_thread_id=self._thread_id,
                disable_notification=True,
            )
        except Exception:
            logger.warning("telegram: image upload raised", exc_info=True)
            sent = []
        if sent:
            return
        logger.warning("telegram: upload of %d image(s) failed; restoring their markup", len(files))
        # The alt text is LLM-authored and the markup itself can reassemble a
        # credential out of formatting Telegram then hides, so the restored
        # references are redacted against the rendered form. Markup that concealed
        # a secret loses its formatting rather than its redaction — the documented
        # direction of that trade.
        restored = _display_safe(
            "\n".join(f"![{item.alt or 'image'}]({item.path})" for item in files)
        )
        # One truncated bubble used to keep only what fit under the cap — with
        # several failed images the LATER references vanished silently. And the
        # cap itself was measured in code points while Telegram counts UTF-16
        # units, so emoji-dense alt text passed the slice and bounced at the
        # API. Chunk the redacted whole by UTF-16 budget instead (redaction
        # first, so the scanner saw the contiguous text; a chunk is a pure
        # substring of it). Header rides the first bubble only.
        header = "⚠️ Couldn't upload:\n"
        budget = self._limit() - _utf16_len(header)
        for index, chunk in enumerate(_utf16_chunks(restored, budget)):
            await self._client.send_message(
                self._chat_id,
                f"{header}{chunk}" if index == 0 else chunk,
                message_thread_id=self._thread_id,
                disable_notification=True,
            )

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

    async def _retire_live_frame(self) -> None:
        """Remove a live bubble that ended up carrying no answer of its own.

        Deleted rather than blanked: Telegram rejects an empty ``editMessageText``,
        and a placeholder would be one more thing above the real content. Failure
        is non-fatal — a stale footer is worse than nothing, not worse than a
        crash.
        """
        async with self._frame_lock:
            mid, self._stream_mid, self._shown = self._stream_mid, None, ""
        if mid is None:
            return
        try:
            await self._client.delete_message(self._chat_id, mid)
        except Exception:
            logger.debug("Telegram: retiring the live frame failed", exc_info=True)

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
        text = await asyncio.to_thread(_display_safe, text)
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
                    html_text, text = await self._seal_without_rich(text)
                else:
                    # No conforming table, which includes pipe markup GFM rejects -- a
                    # header row whose cell count disagrees with its delimiter. Rich
                    # Markdown renders that as one paragraph with the newlines collapsed,
                    # so it must not take the rich path; the monospace seal shows every
                    # row verbatim on its own line instead.
                    html_text, text = await self._seal_without_rich(text)
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
                        return
                    # Both edits failed — the live message is gone (e.g. the user
                    # deleted it mid-turn). Fall through and SEND the final content so
                    # the completed answer (and its keyboard) is never silently lost.
                    self._stream_mid = None
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
                    await self._client.send_message(
                        self._chat_id,
                        _strip_md(text),
                        reply_markup=keyboard,
                        message_thread_id=self._thread_id,
                    )

            finally:
                # Retire the live message: this segment is final, so nothing
                # may edit it again.
                self._stream_mid = None
                self._shown = ""

    async def on_thinking(self, text: str) -> None:
        """Accumulate the model's reasoning; posted once at ``on_done``.

        Counts as progress: a turn emitting reasoning is working, and a stall mark
        that appeared over it would report the opposite.

        Deliberately NOT streamed. Reasoning arrives as many small chunks and
        Telegram's only streaming primitive is an edit of a message, so streaming
        it would either disturb the answer bubble it must stay out of, or spend an
        edit per chunk on a second bubble — against a per-CHAT rate budget the
        answer is already spending. Off by default (``telegram.show_thinking``),
        and dropped entirely when off so nothing accumulates unread.
        """
        self._note_progress()
        if not self._show_thinking or not text:
            return
        # Bounded: only one message of reasoning is ever posted, and a long
        # agentic turn can emit megabytes of it. Keeping the whole stream would
        # hold it all resident to then discard all but the first few thousand
        # characters. Stop appending once the budget is covered.
        if self._thinking_chars >= self._thinking_budget():
            return
        self._thinking.append(text)
        self._thinking_chars += len(text)

    def _turn_footer(self) -> str:
        """``Finished in 12s · 🟠 ctx 54%``, or ``""`` when neither is worth saying.

        Two facts a user cannot get any other way on this channel: how long the
        turn took, and how close the conversation is to needing ``/compact``. Both
        are only interesting past a threshold (see ``_FOOTER_MIN_SECS`` /
        ``_FOOTER_MIN_CTX_PCT``), and a footer that appears under every reply is
        one the reader learns to skip — including on the turn where the context
        warning finally matters.
        """
        elapsed = max(0.0, time.monotonic() - self._turn_started)
        pct: int | None = None
        reader = getattr(self._ctx_client, "context_usage_pct", None)
        if callable(reader):
            try:
                pct = round(reader())
            except Exception:
                logger.debug("Telegram: context usage read failed", exc_info=True)
        if elapsed < _FOOTER_MIN_SECS and (pct is None or pct < _FOOTER_MIN_CTX_PCT):
            return ""
        if elapsed < 60:
            duration = f"{int(elapsed)}s"
        else:
            mins, secs = divmod(int(elapsed), 60)
            duration = f"{mins}m {secs}s"
        footer = f"Finished in {duration}"
        if pct is None:
            return footer
        icon = next(mark for floor, mark in _CTX_GAUGE if pct >= floor)
        return f"{footer} · {icon} ctx {pct}%"

    def _thinking_budget(self) -> int:
        """Source characters worth accumulating for the one reasoning message."""
        return max(0, self._rendered_limit() - len(_THINKING_SCAFFOLD))

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
        except Exception:
            logger.debug("Telegram: thinking post failed", exc_info=True)

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        self._note_progress()
        # Surface mid-turn tool activity as a transient "🔧 {tool}…" footer on
        # the live bubble (force=True so it shows immediately, not throttled).
        # We deliberately do NOT seal a message here: models interleave tool
        # calls mid-sentence, so sealing at a tool boundary chops a sentence
        # into broken bare messages on a channel with no tool cards (proven on
        # Telegram). The footer lives only on live frames — on_text_chunk clears
        # it and seals/finals never carry it.
        self._last_tool = title or tool_kind or "tool"
        self._tool = self._last_tool
        await self._stream_live(force=True)

    async def on_prompt_choice(  # noqa: D401 - imperative reads wrong for a handler
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
        tool_input: str = "",
    ) -> None:
        # Approve/Deny as a SEPARATE message so ongoing streaming edits to the
        # answer bubble don't clobber the buttons.
        #
        # callback_data is ``a:<request_id>:<nonce>:<1|0|t>`` and stays well under
        # Telegram's 64-byte cap. The NONCE is load-bearing: ACP request ids restart
        # at 1 in every provider process, so a button still sitting in a Telegram
        # chat from a previous run names an id that is live again for a DIFFERENT
        # tool, and pressing it would approve that one. Minted per prompt and
        # compared on resolve, exactly as Discord and Teams do.
        self._note_progress()
        self._awaiting_approval = True
        rid = str(request_id)
        nonce = new_approval_nonce()
        TelegramApprovalDecider.arm(TelegramApprovalDecider.key(self._session_key, rid), nonce)
        # Three choices, matching Slack's ladder: approve this one, trust the rest
        # of this session, or refuse. Without Trust every tool of an agentic turn
        # costs its own round-trip, which is what pushes an operator to global YOLO,
        # a far wider grant than the one they actually wanted.
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"a:{rid}:{nonce}:1"},
                    {"text": "🚫 Deny", "callback_data": f"a:{rid}:{nonce}:0"},
                ],
                [
                    {
                        "text": "🤝 Trust this conversation",
                        "callback_data": f"a:{rid}:{nonce}:t",
                    }
                ],
            ]
        }
        # The tool name and its arguments are LLM-authored and land in a body
        # Telegram RENDERS, so both go through the same display-form scan as the
        # answer rather than relying on the driver's byte-level pass alone: that
        # pass sees `AKIA**...**` as broken while the rendered message shows it
        # whole. Off-loop, because the scan is a full credential/exfil pass.
        #
        # The request's OWN title first: `_last_tool` is the last tool_call seen and
        # is never cleared, so it names the PREVIOUS tool for any permission that
        # arrives without one of its own, and the operator would be consenting to
        # something other than what they read.
        #
        # The name is monospaced, so this goes out as HTML: send_message defaults
        # to plaintext and markdown backticks would arrive literally. Escaped after
        # the scan, and retry_plain re-sends without a parse_mode if the markup is
        # ever rejected.
        tool = await asyncio.to_thread(_display_safe, tool_title or self._last_tool or "this tool")
        body = f"🔐 Approve <code>{html.escape(tool)}</code>?"
        # Show the arguments. "Approve bash?" is not a decision a user can make;
        # which command it wants to run is. Bounded so the prompt stays one message.
        detail = " ".join((tool_input or "").split())
        if detail:
            detail = await asyncio.to_thread(_display_safe, detail)
            if len(detail) > _APPROVAL_INPUT_CHARS:
                detail = detail[: _APPROVAL_INPUT_CHARS - 1].rstrip() + "…"
            body = f"{body}\n<pre>{html.escape(detail)}</pre>"
        await self._client.send_message(
            self._chat_id,
            body,
            parse_mode="HTML",
            reply_markup=keyboard,
            message_thread_id=self._thread_id,
        )

    async def on_compaction(self, context_usage_pct: float) -> None:
        self._note_progress()
        try:
            await self._client.send_message(
                self._chat_id,
                "🗜️ Compacting context…",
                message_thread_id=self._thread_id,
            )
        except Exception:
            logger.debug("Telegram: compaction notice send failed", exc_info=True)

    def note_steer(self, text: str) -> None:
        """Record the user's own mid-turn steer text (their typed words, NOT the
        redacted backend echo). Called by the dispatcher when a steer is actually
        injected; rendered as an inline "↪️ steered: …" chip in on_done. Capped to
        avoid unbounded growth on a pathological steer burst."""
        t = (text or "").strip()
        if t and len(self._steer_texts) < 50:
            self._steer_texts.append(t)

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
        body_raw, opts = _extract_options("".join(self._buf))
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
            return
        await self._seal_current(keyboard=keyboard, footer=self._turn_footer())
        # After the answer, so the answer is what the push notification previews.
        await self._post_thinking()

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

    async def _seal_chunk_html(self, chunk: str) -> None:
        """Seal one leading chunk of an overflowing degraded segment as HTML.

        The first chunk re-uses the streamed bubble (it is the OLDEST message,
        so it must carry the earliest content or the reply reads out of order);
        later chunks are fresh sends. Mirrors the tail seal's degradation
        ladder: HTML edit -> plaintext edit, or HTML send -> plaintext send.
        """
        html_text = _seal_table_fallback(chunk)
        if len(html_text) > self._rendered_limit():
            html_text = _md_to_telegram_html(chunk)
        if self._stream_mid is not None:
            mid = self._stream_mid
            self._stream_mid = None
            ok = await self._client.edit_message(
                self._chat_id, mid, html_text, parse_mode="HTML", retry_plain=False
            )
            if ok:
                return
            if await self._client.edit_message(self._chat_id, mid, _strip_md(chunk)):
                return
        mid2 = await self._client.send_message(
            self._chat_id,
            html_text,
            parse_mode="HTML",
            retry_plain=False,
            message_thread_id=self._thread_id,
        )
        if mid2 is None:
            await self._client.send_message(
                self._chat_id, _strip_md(chunk), message_thread_id=self._thread_id
            )

    def _chip_for_seal(self, i: int) -> str | None:
        """The steer chip (a "> quote" blockquote of the USER's own words) that
        heads the segment opened by the i-th rotation. None when we have no
        recorded text for that rotation (chip is simply omitted)."""
        if 0 <= i < len(self._steer_texts):
            t = _neutralize_md(self._steer_texts[i])
            return f"> {t}" if t else None
        return None

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
