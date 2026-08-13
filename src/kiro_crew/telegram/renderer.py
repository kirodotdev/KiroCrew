"""Layer 2b -- Telegram ``Renderer`` + interactive approval decider.

``TelegramRenderer`` maps the channel-neutral ``OutputEvent`` stream (routed by
the base :class:`Renderer`'s ``dispatch``) onto Telegram's Bot API:

* ``on_turn_start`` -- typing indicator + a "🤔 …" placeholder message.
* ``on_text_chunk`` -- throttled ``editMessageText`` streaming (typewriter),
  with any trailing ``[OPTIONS:]`` markup held back from the visible stream.
* ``on_tool_call`` -- a transient ``🔧 {tool}…`` footer.
* ``on_prompt_choice`` -- inline Approve/Deny buttons as a SEPARATE message
  (so streaming edits don't clobber them); byte-safe ``callback_data``.
* ``on_compaction`` -- a lightweight "compacting…" note.
* ``on_done`` -- the final edit, splitting long output at the capability's
  char cap and attaching the ``[OPTIONS:]`` inline keyboard to the last chunk.

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
import re
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.constants import OPTIONS_RE_TRAILER, split_trailing_protocol_suffix
from kiro_crew.messaging.renderer import Renderer, apply_options_cap
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.telegram.client import TELEGRAM_RICH_MAX_CHARS

if TYPE_CHECKING:
    from kiro_crew.telegram.client import TelegramClient

logger = logging.getLogger(__name__)

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
_APPROVAL_TIMEOUT_S = 300.0

# Fallback placeholder for a turn that failed without a user-safe reason. The
# retry wording is only correct for transient failures; a permanent failure
# (e.g. the account lacks the selected model) passes its own bounded reason via
# ``close(failure_reason=...)`` so the user is never told to retry an error
# that says retrying will not help.
_GENERIC_ERROR_TEXT = "⚠️ Error — please try again"

# Trailing "[OPTIONS: a | b | c]" -- extracted for inline-keyboard rendering.
# Matched only at the very END of the message, so use the DOTALL/trailer
# canonical parser. Defined once in constants.py (shared with the Slack/
# dashboard/Discord/WeCom surfaces) so the ReDoS-hardened grammar can never
# drift; see OPTIONS_RE_TRAILER for the full rationale. Per-choice whitespace is
# stripped by the caller.
_OPTIONS_RE = OPTIONS_RE_TRAILER


def _extract_options(text: str) -> tuple[str, list[str]]:
    """Split text into (body, options). Handles the streamed partial too."""
    m = _OPTIONS_RE.search(text)
    if m:
        body = text[: m.start()].rstrip()
        options = [o.strip() for o in m.group(1).split("|") if o.strip()]
        return body, options
    # Hold back an incomplete "[OPTIONS…" fragment mid-stream.
    idx = text.rfind("[OPTIONS")
    if idx != -1 and "]" not in text[idx:]:
        return text[:idx].rstrip(), []
    return text, []


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


def build_inline_keyboard(options: list[str]) -> dict | None:
    """Build an InlineKeyboardMarkup from ``[OPTIONS:]`` labels.

    ``callback_data`` is the index only (``opt:<i>``) -- Telegram caps it at
    64 BYTES, so a multi-byte (CJK/emoji) label there could overflow and make
    the whole send fail. The label is recovered from the button text at
    callback time. Two buttons per row (mobile friendly).
    """
    if not options:
        return None
    buttons: list[list[dict]] = []
    row: list[dict] = []
    for i, opt in enumerate(options):
        row.append({"text": opt[:64], "callback_data": f"opt:{i}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return {"inline_keyboard": buttons}


def _split_text(text: str, limit: int) -> list[str]:
    """Split text into <=``limit`` chunks, preferring paragraph boundaries.

    The continuation strips only NEWLINES at the boundary, never horizontal
    whitespace: a bare ``lstrip()`` ate the leading indentation of the first line
    of every continuation chunk, silently re-indenting split code blocks. Render-
    aware splitting makes chunking fire on more shapes, so that pre-existing
    corruption became much easier to hit.
    """
    if limit <= 0 or len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n\n", 0, limit)
        if split_at < limit // 2:
            split_at = text.rfind("\n", 0, limit)
        if split_at < limit // 4:
            split_at = limit
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip("\n")
    return chunks


def _split_markdown(text: str, limit: int) -> list[str]:
    """Split markdown into <=``limit`` chunks, keeping fenced code blocks balanced.

    ``_split_text`` can cut inside a ``` fence (a long code block has internal
    newlines it splits on). An unbalanced fence in a chunk means the per-chunk
    HTML pass never matches it -- the literal ``` shows and, worse, the code body
    is sent unescaped and 400s the HTML request. Rebalance by closing a dangling
    fence at a chunk's end and reopening it at the next chunk's start, so every
    chunk is self-contained markdown.
    """
    chunks = _split_text(text, limit)
    if len(chunks) <= 1:
        return chunks
    out: list[str] = []
    carry_open = False
    for ch in chunks:
        if carry_open:
            ch = "```\n" + ch  # reopen the fence carried from the previous chunk
        if ch.count("```") % 2 == 1:
            ch = ch.rstrip() + "\n```"  # close the fence left dangling here
            carry_open = True
        else:
            carry_open = False
        out.append(ch)
    return out


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
    """True if *line* is a GFM separator row (``| --- |``, ``---|---``)."""
    stripped = line.strip()
    if not stripped or not set(stripped) <= _TABLE_SEP_CHARS:
        return False
    return "-" in stripped and "|" in stripped


def _has_table(text: str) -> bool:
    """True if *text* contains a GFM pipe table.

    A table is a line holding at least one ``|`` immediately followed by a
    separator row. Outer pipes are optional on BOTH rows, because GFM accepts
    ``a | b`` / ``--- | ---`` with no leading or trailing pipe -- anchoring on a
    leading ``|`` silently missed those and rendered them as literal pipes.

    The separator row must contain a dash (so it is a separator, not more data)
    and a pipe (so a bare ``-----`` horizontal rule under a pipe-bearing
    sentence is not mistaken for a table).

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
        "|" in header and _is_table_separator(sep) for header, sep in zip(lines, lines[1:])
    )


def _seal_table_fallback(text: str) -> str:
    """Render *text* for the no-Rich-Messages path: tables monospace, prose rich.

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
    lines = text.split("\n")
    parts: list[str] = []
    prose: list[str] = []
    i = 0

    def _flush_prose() -> None:
        if prose:
            rendered = _md_to_telegram_html("\n".join(prose))
            if rendered:
                parts.append(rendered)
            prose.clear()

    while i < len(lines):
        # A table starts on a pipe-bearing line whose successor is a separator.
        # No fence can appear here -- a fenced segment returned above.
        if (
            "|" in lines[i]
            and i + 1 < len(lines)
            and _is_table_separator(lines[i + 1])
        ):
            _flush_prose()
            block = [lines[i], lines[i + 1]]
            i += 2
            # Body rows run until the first line that is not part of the table.
            while i < len(lines) and "|" in lines[i]:
                block.append(lines[i])
                i += 1
            parts.append(f"<pre>{html.escape(chr(10).join(block))}</pre>")
            continue
        prose.append(lines[i])
        i += 1

    _flush_prose()
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
        # Shrink proportionally to the observed inflation, capped so a
        # barely-over chunk still makes real progress.
        scaled = int(src_limit * (rendered_limit / worst) * 0.95)
        new_limit = max(_MIN_SPLIT_LIMIT, min(scaled, src_limit - 128))
        if new_limit >= src_limit:
            new_limit = _MIN_SPLIT_LIMIT  # guarantee progress to the floor
        src_limit = new_limit
        chunks = _split_markdown(text, src_limit)


def _table_blocks(text: str) -> list[tuple[bool, list[str]]]:
    """Partition *text* into alternating ``(is_table, lines)`` blocks.

    Uses the same run detection as ``_seal_table_fallback``: a table starts on
    a pipe-bearing line whose successor is a separator row and extends while
    lines carry a pipe. Callers must screen out fenced text first (see
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

    def __init__(self, *, session_key: str) -> None:
        self._session_key = session_key

    @staticmethod
    def key(session_key: str, request_id: str | int) -> str:
        return f"{session_key}:{request_id}"

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

    @classmethod
    def resolve_global(cls, key: str, approved: bool) -> bool:
        """Resolve a pending approval by key. Returns True iff one was waiting."""
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
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._chat_id = chat_id
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

    # -- lifecycle ----------------------------------------------------------
    async def on_turn_start(self) -> None:
        # Typing indicator only — no draft preview. Idempotent (dispatch + driver
        # both call this).
        if self._typing_task is not None or self._closed:
            return
        self._typing_task = asyncio.create_task(self._typing_loop())

    async def _typing_loop(self) -> None:
        """Keep the 'typing…' chat action alive (it expires after ~5s) for the
        duration of the turn. Cancelled by ``_stop_typing``."""
        try:
            while not self._closed:
                try:
                    await self._client.send_typing(self._chat_id, message_thread_id=self._thread_id)
                except Exception:
                    logger.debug("Telegram: typing refresh failed", exc_info=True)
                await asyncio.sleep(_TYPING_REFRESH_S)
        except asyncio.CancelledError:
            pass

    def _stop_typing(self) -> None:
        self._closed = True
        task, self._typing_task = self._typing_task, None
        if task is not None and not task.done():
            task.cancel()

    async def on_text_chunk(self, text: str) -> None:
        self._buf.append(text)
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
        keyboard = build_inline_keyboard(opts) if opts else None
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
            await self._seal_current()
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

    async def _stream_live(self, *, force: bool = False) -> None:
        """Throttled in-place edit of the current segment (plaintext, so partial
        markdown never 400s). Sends the message on first render. The transient
        ``🔧 {tool}…`` footer is appended ONLY here (live frames) — seals and
        the final render read ``_segment_text`` and never carry it. ``force``
        bypasses the throttle so a tool-call event surfaces immediately."""
        now = time.monotonic()
        if not force and now - self._last_edit < _EDIT_THROTTLE_S:
            return
        # Hold back trailing [OPTIONS:] markup (complete or still-streaming
        # partial) from live frames — it is an internal directive, extracted
        # into the inline keyboard at finalization.
        seg, _ = _extract_options(self._segment_text())
        body = _strip_md(seg)
        footer = f"🔧 {self._tool}…" if self._tool else ""
        if footer:
            # Keep the footer visible even when it must displace body tail chars.
            room = self._limit() - len(footer) - 2
            text = f"{body[:room]}\n\n{footer}".strip() if room > 0 else footer
        else:
            text = body[: self._limit()]
        if not text or text == self._shown:
            return
        self._last_edit = now
        self._shown = text
        if self._stream_mid is None:
            mid = await self._client.send_message(
                self._chat_id, text, message_thread_id=self._thread_id
            )
            if mid is not None:
                self._stream_mid = mid
        else:
            await self._client.edit_message(self._chat_id, self._stream_mid, text)

    async def _seal_current(self, *, keyboard: dict | None = None) -> None:
        """Finalize the current segment: replace its live plaintext with the
        formatted HTML (and optional keyboard). Edits the streamed message in
        place, or sends one if the segment never streamed (e.g. throttled out).
        Empty segments are skipped so a bare steer doesn't post a blank bubble."""
        text = self._segment_text().strip()
        if not text:
            if keyboard is None:
                return
            text = "…"

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
            #
            # The segment was already sized against the plain HTML render, and
            # <pre> wrapping only ADDS characters, so on a near-limit reply the
            # wrapped form can overflow _rendered_limit() and have its tail cut
            # by _cap_text(). Losing the end of the answer is worse than losing
            # column alignment, so fall back to the plain render when it spills.
            logger.debug("sendRichMessage failed for chat %s, falling back to HTML", self._chat_id)
            html_text = _seal_table_fallback(text)
            if len(html_text) > self._rendered_limit():
                html_text = _md_to_telegram_html(text)
                if len(html_text) > self._rendered_limit():
                    # The segment was sized against the RICH budget, so it can
                    # be several HTML messages long -- letting the client's
                    # truncation backstop cap it would drop content. Re-split
                    # against the HTML budget instead: header repetition keeps
                    # every chunk of the table detected, so the whole table
                    # degrades uniformly to <pre> rather than half-rich,
                    # half-pipes. All chunks but the last ship here; the last
                    # flows into the normal seal below so the keyboard lands on
                    # the final message.
                    chunks = self._degraded_table_chunks(text)
                    for ch in chunks[:-1]:
                        await self._seal_chunk_html(ch)
                    if chunks:
                        text = chunks[-1]
                    html_text = _seal_table_fallback(text)
                    if len(html_text) > self._rendered_limit():
                        html_text = _md_to_telegram_html(text)
        else:
            html_text = _md_to_telegram_html(text)
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
        )
        if mid is None:
            await self._client.send_message(
                self._chat_id,
                _strip_md(text),
                reply_markup=keyboard,
                message_thread_id=self._thread_id,
            )

    async def on_thinking(self, text: str) -> None:
        # Telegram does not surface reasoning inline (parity with prior behavior).
        return None

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
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

    async def on_prompt_choice(self, options: list[dict[str, Any]], request_id: str | int) -> None:
        # Approve/Deny as a SEPARATE message so ongoing streaming edits to the
        # answer bubble don't clobber the buttons. callback_data stays well
        # under Telegram's 64-byte cap (a:<request_id>:<1|0>); the callback
        # handler resolves the decider Future by reconstructing the key.
        rid = str(request_id)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"a:{rid}:1"},
                    {"text": "🚫 Deny", "callback_data": f"a:{rid}:0"},
                ]
            ]
        }
        tool = self._last_tool or "this tool"
        await self._client.send_message(
            self._chat_id,
            f"🔐 Approve `{tool}`?",
            reply_markup=keyboard,
            message_thread_id=self._thread_id,
        )

    async def on_compaction(self, context_usage_pct: float) -> None:
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
        keyboard = build_inline_keyboard(opts) if opts else None
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
            return
        await self._seal_current(keyboard=keyboard)

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
            scaled = int(src_cap * (rendered_cap / worst) * 0.95)
            new_cap = max(_MIN_SPLIT_LIMIT, min(scaled, src_cap - 128))
            if new_cap >= src_cap:
                new_cap = _MIN_SPLIT_LIMIT  # guarantee progress to the floor
            src_cap = new_cap
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

    # -- helpers ------------------------------------------------------------
    def _options(self) -> list[str]:
        raw = "".join(self._buf).strip()
        _, opts = _extract_options(raw)
        return opts
