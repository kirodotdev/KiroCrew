"""Outbound rendering for the WhatsApp channel.

WhatsApp renders its own formatting dialect, not Markdown: ``*bold*``,
``_italic_``, ``~strikethrough~``, ```` ```monospace``` ```` and `` `inline` ``.
Headings, links, tables and diagrams have no native form. :func:`to_whatsapp_text`
converts the agent's Markdown into that dialect (conservatively -- anything it
cannot map degrades to plain text, never to visible ``**`` litter), and
:func:`render_chunks` splits oversized payloads with code fences kept intact.

**What has no native form is REDUCED, not passed through.** This is the most
mobile surface in the product, so a construct that needs a monospace grid or an
image is unreadable here rather than merely plain: the shared reductions in
``messaging.markup`` turn a pipe table into labelled bullets and a mermaid fence
into arrows, and they emit Markdown so ``_convert_line`` below is still the one
place that knows the dialect. ``<thinking>`` blocks go the same way -- WhatsApp
offers nothing to fold one behind, so the model's scratchpad would simply be part
of the answer.

**Fence grammar is not ours.** Both functions read line roles from
``messaging.split.iter_fence_lines`` and split with
``messaging.split.split_markdown_safe``, because a channel-local backtick
counter gets the grammar wrong in ways that corrupt the message: a run of four
backticks does not open a block for it, so ```` ````md ```` fell through as
prose and every ``**bold**`` INSIDE the code block was rewritten to WhatsApp
``*bold*``; a ``~~~`` fence did the same; and hard-splitting a long block left
chunks holding an odd number of delimiters, which WhatsApp renders as a
monospace block that never closes. The shared machine is also where the
language-tag carry and the prefix-stability contract live, so a streaming caller
can seal chunks as they appear.

WhatsApp has exactly one code marker and no info string, so the delimiter lines
are REWRITTEN to a bare ```` ``` ```` while the content between them stays
byte-exact -- which is why this reads per-line roles rather than character spans.
The one info string that survives as MEANING rather than as text is
``mermaid``: a dropped one leaves the reader raw diagram source in monospace with
nothing that says it was a diagram.

**This module is a redaction sink, and the screen that carries the guarantee runs
LAST.** ``TurnDriver`` scans the provider stream as literal bytes, which cannot
see a credential that markup has split: ``AKIA**I**OSFODNN7EXAMPLE`` matches no
credential pattern as written, and this channel is the one that then rewrites it
to ``AKIA*I*OSFODNN7EXAMPLE`` for a client that strips the delimiters and shows
the reader an intact key. So :func:`to_whatsapp_text` screens through
``messaging.display_safety.redact_for_display`` **after** every rewrite below,
which is the only form whose safety does not depend on what those rewrites did:
each reduction DELETES a span, and deleting a span joins what sat on either side
of it. Screening before the transform is the bypass -- a scan of
``AKIA<thinking>x</thinking>IOSFODNN7EXAMPLE`` sees nothing, and the reduction
then hands the reader the key. There is a second screen before the conversion as
well; it is a belt rather than the mechanism, and it is the only scan that reads
the authored text as one piece, so a reduction made lossier later cannot take the
whole guarantee with it. ANSI is stripped ahead of both, which is load-bearing for
a different reason: the conversion reads LINE structure, so an escape in front of
a heading marker hides the heading from it.

:func:`display_safe_text` is the same screen without the conversion, for the sinks
that put already-dialect text on the wire.
"""

from __future__ import annotations

import asyncio
import re
from typing import Callable

from kiro_crew.messaging.display_safety import redact_for_display, strip_ansi
from kiro_crew.messaging.markup import (
    MERMAID_INFO,
    flatten_mermaid_body,
    flatten_pipe_tables,
    strip_thinking_tags,
)
from kiro_crew.messaging.split import (
    FENCE_BODY,
    FENCE_CLOSE,
    FENCE_OPEN,
    iter_fence_lines,
    split_markdown_safe,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

#: WhatsApp's own per-message ceiling. Also declared as
#: ``TransportCapabilities.max_message_chars`` on ``WHATSAPP_CAPABILITIES``;
#: this module owns the number and the transport reads it.
WHATSAPP_CHUNK_LIMIT = 4096

#: The only code delimiter WhatsApp renders. Every opener/closer normalizes to
#: it, dropping any info string -- WhatsApp has no language tags, so a retained
#: ``python`` would show up as the block's first line of "code".
WHATSAPP_FENCE = "```"

#: Announces a fenced diagram, in Markdown bold so ``_convert_line`` maps it to
#: the dialect. WhatsApp renders no image from text, so a diagram arrives either
#: as flattened arrows or as its own source; either way the info string was the
#: only thing saying it was a diagram, and it does not survive the delimiter
#: rewrite.
_DIAGRAM_HEADING = "**Diagram**"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_BOLD_US_RE = re.compile(r"__(.+?)__")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+")
#: A single-backtick inline-code span, matching the Telegram renderer's shape.
#: The dialect has ONE code marker, so a longer run carries no distinct meaning
#: here, and a span containing a backtick cannot be expressed at all.
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
#: The info string of a fence opener: whatever follows the delimiter run. The run
#: is greedy, so an all-delimiter line (a 5,000-backtick opener) yields ``""``.
_FENCE_INFO_RE = re.compile(r"^ {0,3}(?:`{3,}|~{3,})[ \t]*(\S*)")


def _redact_all(text: str) -> str:
    """The same redactor pair ``TurnDriver`` streams provider text through.

    Composed here rather than imported so this module owns its own scanner set,
    matching the sibling channels; ``security`` is pure regex, so it costs no
    import-time work and touches no event loop.
    """
    out, _ = redact_exfiltration_urls(text or "")
    out, _ = redact_credentials(out)
    return out


def display_safe_text(text: str) -> str:
    """*text* screened for what WhatsApp SHOWS, with no dialect conversion.

    For the sinks that put text on the wire without passing through
    :func:`to_whatsapp_text`: a rejection note and an image caption are already in
    the dialect, and re-converting them would rewrite characters inside a path.
    They still need the screen -- both are built from agent-authored text, and
    WhatsApp collapses the same emphasis, code-span and link markup in a caption
    as in a message body.
    """
    safe, _ = redact_for_display(strip_ansi(text or ""), _redact_all)
    return safe


def _fence_info(opener: str) -> str:
    """The lowercased info string of a fence *opener* line, or ``""``."""
    match = _FENCE_INFO_RE.match(opener)
    return match.group(1).lower() if match else ""


def _sub_outside_code(
    pattern: re.Pattern[str], repl: Callable[[re.Match[str]], str], line: str
) -> str:
    """Apply *pattern* to *line*, but never treat a DELIMITER inside inline code.

    An inline-code span is the only way to show text WhatsApp must not reformat,
    because the dialect has no escape character for its own delimiters. So
    ``__`` inside `` `/tmp/__init__.py` `` is a filename, and rewriting it to
    ``*init*`` defeats the span that exists for exactly that. ``files.py``'s
    rejection note is built on this guarantee.

    The test is on the delimiter POSITIONS, not on overlap: emphasis legitimately
    spans a code span (``**a `b` c**``), and skipping the whole match there would
    leave visible ``**`` litter, which this module promises never to emit. Only a
    match that OPENS or CLOSES inside a span is left alone. Spans are recomputed
    per pass because a substitution shifts every later offset; the spans
    themselves never move, since nothing here rewrites inside one.
    """
    spans = [match.span() for match in _INLINE_CODE_RE.finditer(line)]
    if not spans:
        return pattern.sub(repl, line)

    def _inside(index: int) -> bool:
        return any(start <= index < end for start, end in spans)

    def _guard(match: re.Match[str]) -> str:
        if _inside(match.start()) or _inside(match.end() - 1):
            return match.group(0)
        return repl(match)

    return pattern.sub(_guard, line)


def _convert_line(line: str) -> str:
    heading = _HEADING_RE.match(line)
    if heading:
        # WhatsApp has no headings; bold the text instead.
        line = f"*{heading.group(2).strip()}*"
    line = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", line)
    line = _sub_outside_code(_BOLD_RE, lambda m: f"*{m.group(1)}*", line)
    line = _sub_outside_code(_BOLD_US_RE, lambda m: f"*{m.group(1)}*", line)
    line = _sub_outside_code(_STRIKE_RE, lambda m: f"~{m.group(1)}~", line)
    # [label](url) -> "label (url)"; bare label when the label IS the url.
    line = _sub_outside_code(
        _LINK_RE,
        lambda m: m.group(2) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})",
        line,
    )
    return line


def to_whatsapp_text(content: str) -> str:
    """The text WhatsApp will show: dialect-converted and display-screened.

    Content inside a fenced block is passed through untouched; the delimiter
    lines are normalized to :data:`WHATSAPP_FENCE`. An unterminated block is
    closed, since a dangling opener would render the rest of the conversation
    as code. A ``mermaid`` fence is the exception -- see :data:`_DIAGRAM_HEADING`.

    The order is the security-critical part (see the module docstring): the screen
    runs once on the authored form and once on the delivered form, with every
    rewrite in between.
    """
    # Normalise BEFORE anything reads line structure. Everything below is
    # line-shaped or anchored, so a colour escape in front of a ``#`` hides the
    # heading from ``_convert_line`` and the reader gets a literal hash; the strip
    # also reassembles a credential the escapes had split, which is why it belongs
    # on the scanned side of a screen rather than after one.
    #
    # ``redact_for_display`` strips ANSI itself, so the explicit call is belt: it
    # keeps the ordering visible here instead of resting on another function's
    # internals. The screen is what the ANSI guarantee is PINNED against.
    text = strip_ansi(content or "")
    # The belt half of the screen (see the module docstring): the only scan that
    # reads the authored text as one piece, before a reduction removes a span from
    # it. It also carries the normalisation the conversion below depends on.
    text, _ = redact_for_display(text, _redact_all)
    # No closing tag arrives mid-stream, so an opener owns the remainder: on this
    # channel a live frame is a real send, and its notification would carry the
    # model's scratchpad.
    text, _ = strip_thinking_tags(text, strip_whitespace=False, hide_partial=True)

    out: list[str] = []
    prose: list[str] = []
    fence_body: list[str] = []
    fence_info = ""
    open_fence = False

    def flush_prose() -> None:
        """Convert the buffered run of non-fenced lines.

        Tables are flattened over the whole RUN rather than per line, because a
        row's labels come from the header row above it. Buffering by run is also
        what keeps fenced content out of the reduction: a code sample full of
        pipes never reaches it.
        """
        if not prose:
            return
        for line in flatten_pipe_tables("\n".join(prose)).split("\n"):
            out.append(_convert_line(line))
        prose.clear()

    def flush_fence() -> None:
        """Emit the block that just closed, as a diagram or as a code block."""
        if fence_info == MERMAID_INFO:
            prose.append(_DIAGRAM_HEADING)
            diagram = flatten_mermaid_body("\n".join(fence_body))
            if diagram:
                prose.extend(diagram.split("\n"))
                fence_body.clear()
                flush_prose()
                return
        # A diagram grammar the reduction does not read keeps its source, under
        # the heading: showing it as code is honest, inventing a reading is not.
        flush_prose()
        out.append(WHATSAPP_FENCE)
        out.extend(fence_body)
        out.append(WHATSAPP_FENCE)
        fence_body.clear()

    for line, role in iter_fence_lines(text):
        if role == FENCE_OPEN:
            flush_prose()
            open_fence = True
            fence_info = _fence_info(line)
        elif role == FENCE_BODY:
            fence_body.append(line)
        elif role == FENCE_CLOSE:
            open_fence = False
            flush_fence()
        else:
            prose.append(line.rstrip())
    flush_prose()
    if open_fence:
        flush_fence()  # an unterminated fence would swallow the rest
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    # THE screen. Every reduction above deletes a span, and deleting a span joins
    # what sat on either side of it, so this is the only scan whose result does not
    # depend on which reductions ran -- and it reads the form the client renders.
    text, _ = redact_for_display(text, _redact_all)
    return text.strip()


def render_chunks(content: str, limit: int = WHATSAPP_CHUNK_LIMIT) -> list[str]:
    """Delivery-ready chunks of WhatsApp-dialect text (see module docstring).

    Every rewrite -- the display screen, the reductions, dialect conversion --
    runs BEFORE splitting, so the splitter measures the characters that are
    actually delivered. That ordering is what lets a step GROW the text without
    any chunk outgrowing the budget it was cut to, and several of them do: a
    flattened table row carries its column labels, a diagram gains a heading, and
    a redacted credential becomes a marker longer than the key it replaces.
    """
    text = to_whatsapp_text(content)
    if not text:
        return []
    return split_markdown_safe(text, limit)


async def render_chunks_off_loop(content: str, limit: int = WHATSAPP_CHUNK_LIMIT) -> list[str]:
    """:func:`render_chunks` on a worker thread.

    The shared splitter terminates on pathological delimiter input but its CPU
    work is unbounded in the message size, and the gateway runs every channel,
    every turn and the liveness heartbeat on one event loop. Discord offloads
    the same call for the same reason.
    """
    return await asyncio.to_thread(render_chunks, content, limit)
