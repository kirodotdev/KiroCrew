"""Render an export bundle as Markdown — the human-readable export format.

``GET /api/chat/slots/{slot}/export?format=md`` answers
``<title-slug>-<stamp>.kcsession.md`` instead of the gzipped JSON bundle.

**Why a second format rather than a second endpoint.** The JSON bundle is a
*transfer* document: ``session_transfer`` validates it on the way back in, so
every byte in it is there for the importer. A person reading a past conversation
needs the opposite document — one a text editor opens, a forge renders, and a
reader skims. Those are two renderings of the same assembled bundle, not two
sources of truth, so this module is a pure renderer over the bundle
``session_export`` already builds: the slot lookup, the app-scope checks, the
restricted-session refusal, the transcript locking and the egress redaction all
stay in one place and are shared.

**It installs nowhere, so it carries no Layer B.** Markdown is a one-way door:
nothing reads it back, and ``session_export`` passes ``include_layer_b=False``
for this format unconditionally — not as an operator choice. The model's context
window is byte-exact and unredacted (``session_transfer._read_layer_b``), and a
document whose whole purpose is being pasted into a review, a ticket or a chat is
the last place it may appear.

**Streamed, never assembled.** :func:`write_markdown_file` writes a message at a
time into a temp file under the export staging directory, the same way
``session_transfer.write_bundle_file`` does, because a long session's rendered
text is far too much to hold resident. :func:`iter_session_markdown` is the pure
generator behind it, which is what the tests drive.

**Message content is written verbatim.** Transcript content is already Markdown —
that is what the dashboard renders — so escaping it would destroy the fenced code
blocks, tables and lists this format exists to preserve.

Two structural risks follow from that, and :func:`_unterminated_blocks` resolves
them at the message boundary. A turn ending inside an unterminated code fence
swallows every later turn into one code block; a turn ending inside an
unterminated HTML construct — a comment, a ``<script``/``<pre``/``<style``/
``<textarea`` block, a ``<?`` instruction, a ``<!`` declaration, ``<![CDATA[``,
an element a browser reads as raw text until its own close tag such as
``<title>`` or ``<iframe>``, or one it parses but does not display such as
``<template>`` or ``<dialog>`` — hides every later turn from the renderer
entirely. Both are *silent* read-time loss — the text is in the file and no
reader sees it — which is why they are closed rather than escaped.

**The guard can cause the very loss it prevents, so nothing here guesses.** A
manufactured closer is itself content, and a bare ````` ``` ````` line is a valid
*opener*: closing a construct that was never open swallows the rest of the
document exactly as leaving a real one open does. A hand-written CommonMark
scanner cannot get this right, because every container rule (list item, quote,
HTML block) changes where a fence ends. So this module does not decide: it
asks. Each message is parsed by a CommonMark reference implementation
(``markdown-it-py``) exactly as it will land in the file — followed by the
separator and a stand-in for the next role heading — and a closer is added only
when that parser reports the heading swallowed, and only the closer the
swallowing block itself names. The rendered HTML is then read by a small HTML
tokenizer for what a *browser* would still have open — a comment, an unfinished
tag, a raw-text element — and that is closed too. Every closer is re-verified
by the same parse before it is accepted, so a closer that would have opened
something is never emitted.

**What verbatim content can still do, and why that is accepted.** A message body
is written at the same heading depth as :func:`_message_heading`, so content
holding a line like ``## Assistant — <ts>`` renders as a turn this session never
produced. Closing that hole means either escaping content — which destroys the
fenced blocks and tables this format exists for — or wrapping each body in a
container, which no Markdown construct provides without the same escaping. The
JSON bundle stays the fidelity document; this one is a reading copy, and a reader
who needs provenance for a specific line has the bundle. The two risks above are
different in kind: they make the file *unreadable* rather than *misleading*, and
they cost nothing to close.
"""

from __future__ import annotations

import html
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterator

from markdown_it import MarkdownIt
from markdown_it.token import Token

from kiro_crew.dashboard.session_transfer import _egress_tmp_dir, _rm_import_temps

#: Extension of an exported Markdown session. Carries the same ``.kcsession``
#: middle as the JSON export so the two sort together and a file that outlived
#: its tab is still identifiable by name alone, with the final ``.md`` telling an
#: editor, a forge and an operator how to open it.
MARKDOWN_FILE_SUFFIX = ".kcsession.md"

#: Heading shown for each role. Only ``user`` and ``assistant`` reach a bundle
#: (``session_transfer._VISIBLE_ROLES``); an unrecognised role is titled rather
#: than dropped, because losing a turn from a transcript is worse than printing a
#: role name this module did not expect.
_ROLE_LABELS = {"user": "User", "assistant": "Assistant"}

#: Provenance rows, in display order: the bundle/``source`` key and its label. A
#: field the source gateway could not read is absent from ``source`` rather than
#: empty (``session_transfer.build_source_record``), so an absent row means "not
#: known" and is skipped rather than printed blank.
_SOURCE_ROWS = (
    ("exported_at", "Exported"),
    ("model", "Model"),
    ("reasoning_effort", "Reasoning effort"),
    ("approval_policy", "Approval policy"),
    ("workspace", "Workspace"),
    ("project", "Project"),
    ("producer", "Produced by"),
)

#: What ``approval_policy`` reads as in the table. The empty string is a VALUE
#: there and not an absence — it is the interactive policy, the same spelling the
#: session object uses — so it is rendered rather than skipped, and named in
#: words because "" would read as a gateway that had nothing to report.
_INTERACTIVE_POLICY = "interactive"

#: What every message is followed by in the document: the separator, then the
#: next role heading. :func:`iter_session_markdown` yields exactly these bytes.
_SEPARATOR = "\n---\n\n"


def _single_line(value: str) -> str:
    """*value* with every run of whitespace collapsed to one space.

    An ATX heading is one line by definition, so a title carrying a newline does
    not render as a two-line heading — it renders as a heading plus whatever
    followed, as its own block. A title of ``Notes`` followed by a line that opens
    an HTML comment therefore hides the entire transcript beneath it, and the
    title is the one part of this document a user types freely: the rename
    endpoint trims and truncates but keeps newlines, so this is where that has to
    be handled.

    Collapsed rather than escaped, because a heading is a display line and a title
    that wanted to be two lines has nothing to express here. The provenance table
    escapes its cells instead (:func:`_escape_cell`): there the delimiter is
    structural and the text around it is data worth keeping intact.
    """
    return " ".join(value.split())


def _escape_cell(value: str) -> str:
    """Make *value* safe inside a Markdown table cell.

    A table cell is the one place in this document where content cannot be
    verbatim: an unescaped ``|`` ends the cell and shifts every later column, and
    a newline ends the row. Only provenance strings pass through here — a
    workspace name, a model id — never message content, which is written as
    ordinary block text where neither character means anything.
    """
    markdown_safe = (
        value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    )
    return html.escape(markdown_safe, quote=False)


# ── the structural guard ───────────────────────────────────────────────────────

#: The CommonMark parser every message is checked against, with raw HTML kept
#: so that what a forge would emit is what the HTML tokenizer below reads. The
#: ``commonmark`` preset is the spec and nothing more: no tables, no strikethrough,
#: no extensions a particular forge may or may not have — the file is promised to
#: render under CommonMark, and that is what is verified.
_PARSER = MarkdownIt("commonmark", {"html": True})

#: Stand-in for the next role heading. It is parsed exactly where the real
#: heading will be, after the separator, and a message is "closed" when the
#: parser sees it as a top-level ``h2`` whose text is this and nothing else.
#: Message content is untrusted and can hold anything — including this text —
#: so it is only the BASE of the stand-in: :func:`_boundary` lengthens it until
#: the message does not contain it, and every check uses that per-message text.
_SENTINEL_BASE = "kcsession-turn-boundary-6f3a9c1e"


def _boundary(content: str) -> str:
    """A stand-in heading text that occurs nowhere in *content*.

    The checks below find the stand-in by its text, so a message that carried
    the same text could pose as the boundary: a paragraph holding it would be
    found first, read as "the heading survived", and whatever the message really
    left open would hide every later turn. A text the message provably does not
    contain cannot be impersonated, whatever the message says.
    """
    boundary = _SENTINEL_BASE
    suffix = 0
    while boundary in content:
        suffix += 1
        boundary = f"{_SENTINEL_BASE}-{suffix}"
    return boundary


#: How many verification passes :func:`_unterminated_blocks` runs. Each pass
#: adds the closer(s) for one layer — a fence, then a comment it hid, then
#: every element open inside what the comment hid — and the NEXT pass is what
#: verifies that batch, by re-parsing it as content. This bounds nesting
#: depth, not how many closers one pass may emit: a single pass closes every
#: currently-open element in one go (one ``</tag>`` each), however many there
#: are, and the loop still re-verifies the result rather than trusting a batch
#: just because it was emitted.
_MAX_CLOSERS = 8

#: CommonMark §4.6 conditions 1-5: the HTML blocks that run until their own
#: terminator, past any number of blank lines. Each entry is the opener (matched
#: against the block's first line) and the closer that ends it. Conditions 6
#: and 7 end at a blank line, and the separator is preceded by one, so they can
#: never reach the next heading and need no entry.
#:
#: For a comment the closer is a whole comment rather than a bare ``-->``: a
#: browser reads its own ``-->`` as the terminator of the still-open comment
#: (comments do not nest), and the line is a complete, self-contained comment in
#: Markdown too, so it is right wherever it lands.
_HTML_BLOCK_CLOSERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"<(script|pre|style|textarea)(?=[\s>]|$)", re.IGNORECASE | re.ASCII), "</{tag}>"),
    (re.compile(r"<!--"), "<!-- -->"),
    (re.compile(r"<\?"), "?>"),
    (re.compile(r"<!\[CDATA\["), "]]>"),
    (re.compile(r"<![A-Z]"), ">"),
)

#: Elements an HTML parser reads as raw text or RCDATA until their own close
#: tag (HTML §13.2.5), whatever Markdown construct emitted the opener. An
#: unterminated one swallows every later heading as element text. Wider than
#: CommonMark's condition-1 set because the two answer different questions:
#: ``<title>`` opens only a blank-terminated block in Markdown, but a browser
#: still needs the ``</title>``.
_RAW_TEXT_ELEMENTS = frozenset(
    {"script", "style", "textarea", "title", "iframe", "xmp", "noembed", "noframes", "noscript"}
)
#: Elements whose children are fallback content, which a browser never lays out
#: (``datalist`` and ``rp`` are ``display: none``). Their end tag is read by the
#: "any other end tag" rule of the "in body" insertion mode, which ignores it
#: while a special element such as a paragraph is open inside. A closer written on
#: the line after a paragraph merges into that paragraph, so :meth:`_RenderedState.closers`
#: starts these on a block of their own.
_FALLBACK_ELEMENTS = frozenset({"audio", "video", "canvas", "meter", "progress", "datalist", "rp"})
#: Elements whose content is parsed normally but which are still closed when
#: left open, because leaving them open changes how every later turn reads.
#: ``pre`` shows the turns as preformatted text. The rest are elements a browser
#: parses and then does not display, which makes every later turn unreadable:
#: ``template`` keeps its content inert, a closed ``details`` shows only its
#: summary, ``dialog`` is ``display: none`` and ``select`` shows only its options,
#: besides the fallback-content elements above.
#:
#: Whether a container's end tag (``</p>``, ``</li>``) ends one of these depends
#: on the element, and the tokenizer does not model that, so it closes every one
#: left open. A browser ignores an end tag with nothing to close: a needless
#: closer costs one line, a missing one costs every later turn.
#:
#: Ceiling: this is a tokenizer, not a tree builder. A browser ignores the end tag
#: of ``details`` or ``dialog`` while a ``table``, ``td``, ``marquee`` or ``object``
#: is open inside it, and that of a fallback-content element while a block element
#: such as ``<p>`` is, so an element left open together with one of those still
#: hides the turns after it. Closing those too means tracking every open element.
_TRACKED_ELEMENTS = (
    frozenset({"pre", "template", "dialog", "details", "select"}) | _FALLBACK_ELEMENTS
)
_TAG_NAME_RE = re.compile(r"</?([A-Za-z][A-Za-z0-9-]*)")
#: Completes a tag the message left unfinished (``<script`` with no ``>``, or a
#: ``<script src="`` with its quote still open). Read by a browser: the ``"``
#: and ``'`` end whichever quoted value was open and are name characters
#: otherwise, and the final ``>`` ends the tag. Read by CommonMark: a line that
#: begins with ``<!--`` is an HTML block wherever it lands, so it is always
#: emitted raw. Read when no tag was open at all: an ordinary comment.
_TAG_COMPLETER = "<!-- \" ' -->"


class _RenderedState:
    """What a browser would still have open at the end of a rendered message."""

    __slots__ = ("pending_quote", "comment_open", "open_elements", "script_double_escaped")

    def __init__(self) -> None:
        #: ``None`` when no tag runs past the end; otherwise the quote character
        #: of the attribute value that was open, or ``""`` for none.
        self.pending_quote: str | None = None
        #: An HTML comment, or a ``<!``/``<?`` bogus comment, is still open.
        self.comment_open = False
        #: Raw-text and tracked elements still open, outermost first.
        self.open_elements: list[str] = []
        #: True when an open ``script`` (always the last entry, if present —
        #: nothing else can open while raw-text scanning is in progress) ran out
        #: of text in the *script-data-double-escaped* sub-state (HTML
        #: §13.2.5.4–5): one ``</script>`` there only drops back to escaped and
        #: does not close the element, so :meth:`closers` must emit a second one.
        self.script_double_escaped = False

    @property
    def clean(self) -> bool:
        return self.pending_quote is None and not self.comment_open and not self.open_elements

    def closers(self) -> list[str]:
        """The lines that close this state, in the order a browser needs them.

        An unfinished tag is completed first and alone: until its ``>`` arrives
        nothing after it is an element at all, so what the completed tag opens
        is only known on the next pass (:func:`_unterminated_blocks` loops).

        When a fallback-content element is open the lines start with a blank
        one, so the first closer begins a block of its own instead of merging
        into the paragraph above it, where the browser would ignore it.
        """
        if self.pending_quote is not None:
            return [_TAG_COMPLETER]
        closers = ["<!-- -->"] if self.comment_open else []
        for name in reversed(self.open_elements):
            closers.append(f"</{name}>")
            if name == "script" and self.script_double_escaped:
                # The first </script> above only de-escalates double-escaped
                # to escaped (HTML §13.2.5.4–5); only a SECOND one, now read
                # from the escaped sub-state, actually closes the element.
                closers.append("</script>")
        if _FALLBACK_ELEMENTS.intersection(self.open_elements):
            closers.insert(0, "")
        return closers


#: HTML's ASCII whitespace (§13.2) — the only characters a parser accepts as a
#: tag-name delimiter. Python's ``\s`` is wider (it matches U+00A0 and other
#: Unicode spaces), so a ``</script\u00a0>`` end tag ``\s`` treats as closed is
#: in fact raw text to a browser; spell the set out to stay in step with HTML.
_HTML_WHITESPACE = " \t\n\r\f"


def _scan_tag_tail(text: str, position: int, length: int) -> tuple[int, str]:
    """Scan from inside a tag to its ``>``, honouring quoted attribute values.

    *position* starts just past the tag name. Returns ``(end, quote)``: when the
    ``>`` is found, *end* is its index and *quote* is ``""``; when the text runs
    out first, *end* is *length* and *quote* is the still-open quote character
    (``""`` if none) so the caller can treat the tag as unfinished. A ``>`` that
    sits inside a quoted value is passed over, never read as the terminator.
    """
    quote = ""
    #: ``before`` — before an attribute name (tag name, between attributes,
    #: after a value); ``name`` — inside an attribute name; ``after_name`` —
    #: past a name's end but before any ``=`` (ASCII whitespace can appear
    #: here with no effect, HTML §13.2.5.34), where a ``=`` still starts that
    #: name's value; ``value`` — just past that ``=``, where the next char
    #: decides quoted vs. unquoted; ``unquoted`` — inside an unquoted value,
    #: where a later ``=`` or ``"`` is an ordinary value character, not a new
    #: name or a quote (HTML §13.2.5.36). Entering a quoted value sets
    #: *quote* instead.
    #:
    #: A ``=`` seen from ``before`` is a parse error (HTML §13.2.5.32) but a
    #: browser still consumes it as the first character of an attribute
    #: *name*, not a value — only the ``=`` that follows a name (directly, or
    #: across whitespace via ``after_name``) starts a value. Treating a
    #: leading ``=``, or whitespace after a name, as resetting straight to
    #: ``before`` loses that the name is still waiting for its ``=`` — the
    #: very next character (quote included) then gets read as starting a
    #: fresh name instead of the value this attribute is still owed, pairing
    #: quotes with the wrong partner and reporting the tag closed while a
    #: browser would not.
    attr_state = "before"
    while position < length:
        char = text[position]
        if quote:
            if char == quote:
                quote = ""
                attr_state = "before"
        elif char == ">":
            return position, ""
        elif attr_state == "unquoted":
            # An unquoted value ends only at ASCII whitespace; ``=`` and ``"``
            # inside it are value characters and must not restart value parsing.
            if char in _HTML_WHITESPACE:
                attr_state = "before"
        elif char in _HTML_WHITESPACE:
            if attr_state == "name":
                attr_state = "after_name"
            # ``before`` and ``after_name`` both stay put on whitespace.
        elif attr_state in ("before", "name", "after_name"):
            if char == "=" and attr_state in ("name", "after_name"):
                attr_state = "value"
            else:
                # Any other character — including a bare ``=`` seen from
                # ``before``, or any character other than ``=`` seen from
                # ``after_name`` (that name took no value) — is (or starts)
                # an attribute name.
                attr_state = "name"
        elif attr_state == "value":
            if char in "\"'":
                quote = char
                attr_state = "before"
            else:
                attr_state = "unquoted"
        position += 1
    return length, quote


#: Characters that may follow ``</script`` / ``<script`` for a browser to treat
#: the preceding name as a complete tag name rather than ordinary raw text.
_SCRIPT_TAG_DELIMS = _HTML_WHITESPACE + "/>"


def _find_script_end(text: str, cursor: int) -> tuple[int, bool]:
    """``(index, double_escaped)`` of the ``</script`` that actually closes a ``<script>`` element.

    ``<script>`` is RAWTEXT with escape states (HTML §13.2.5.4–5.21). After a
    ``<!--`` the parser is *script-data-escaped*; a ``<script`` seen while
    escaped makes it *script-data-double-escaped*, and in that state a
    ``</script>`` only drops back to escaped — it does **not** close the outer
    element. So ``<script><!--<script></script>`` has its inner ``</script>``
    ignored, and the outer ``<script>`` stays open. A plain ``</script`` search
    would stop at that inner tag and wrongly report the element closed, hiding
    every later turn in the rendered preview. Returns the index of the real
    closing ``</script`` and ``False`` (where :func:`_scan_tag_tail` resumes),
    or ``-1`` and whether the scan ran out of text still *double-escaped* —
    the caller needs that bit because a script left open from double-escaped
    needs a second ``</script>`` closer, not the one closer every other open
    element gets.

    A single forward pass tracks whether a ``<!--`` escape is open and, within
    it, whether an inner ``<script`` has opened the double-escaped state. Only in
    the base and single-escaped states does ``</script`` close the element;
    matching is ASCII-case-insensitive to mirror HTML tag-name folding.
    """
    length = len(text)
    escaped = False
    double_escaped = False
    position = cursor
    while position < length:
        char = text[position]
        if char == "-" and text.startswith("-->", position):
            # A ``-->`` leaves the escaped state (and so the double-escaped one).
            escaped = False
            double_escaped = False
            position += 3
            continue
        if char == "<":
            if text.startswith("<!--", position):
                escaped = True
                position += 4
                continue
            if _match_script_tag(text, position + 1, length, closing=True):
                if double_escaped:
                    double_escaped = False
                    position += len("</script")
                    continue
                return position + len("</script"), False
            if _match_script_tag(text, position + 1, length, closing=False):
                if escaped:
                    double_escaped = True
                position += len("<script")
                continue
        position += 1
    return -1, double_escaped


def _match_script_tag(text: str, position: int, length: int, *, closing: bool) -> bool:
    """Whether ``text[position:]`` is a complete ``script`` / ``/script`` tag name.

    ASCII-case-insensitive, and a real delimiter must follow the name: ``scripty``
    is not a match, and a ``</script`` cut off at the end of the text is script
    data (HTML §13.2.5.17), not a tag, so it does not close the element either.
    """
    if closing:
        if not text.startswith("/", position):
            return False
        position += 1
    name = "script"
    if text[position : position + len(name)].lower() != name:
        return False
    after = position + len(name)
    return after < length and text[after] in _SCRIPT_TAG_DELIMS


def _scan_rendered(text: str) -> _RenderedState:
    """Tokenize rendered HTML the way a browser does, as far as this module cares.

    Tracks comments (including the abruptly-closed ``<!-->`` and the
    ``--!>`` terminator a browser accepts), bogus comments, tags with quoted
    attribute values, and the raw-text elements whose content is opaque until
    ``</name`` followed by whitespace, ``/`` or ``>``. Everything else is data.

    Tracked elements nest, and an end tag closes the innermost one only when it
    names it. A browser ignores an end tag that cannot reach its element (one
    that crosses an open ``template`` or ``select``, say), so an end tag for an
    outer element is not trusted to close it: the closer left behind is ignored
    if the browser did honour the tag, and needed if it did not.
    """
    state = _RenderedState()
    length = len(text)
    cursor = 0
    raw_text_of = ""
    while cursor < length:
        if raw_text_of:
            if raw_text_of == "script":
                end_start, double_escaped = _find_script_end(text, cursor)
            else:
                # ``re.ASCII`` keeps the tag-name match ASCII-case-insensitive, as
                # HTML is (§13.2.5): without it ``re.IGNORECASE`` folds Unicode, so
                # ``</ſtyle>`` (U+017F) wrongly matches ``</style`` though a browser
                # — which only ASCII-folds tag names — reads it as raw text and
                # leaves the element open.
                match = re.compile(
                    rf"</{raw_text_of}(?=[{_HTML_WHITESPACE}/>])", re.IGNORECASE | re.ASCII
                ).search(text, cursor)
                end_start, double_escaped = (-1 if match is None else match.end()), False
            if end_start < 0:
                # Only a script can leave with ``double_escaped`` set; record it
                # so :meth:`_RenderedState.closers` knows one ``</script>`` would
                # merely de-escalate rather than close.
                state.script_double_escaped = double_escaped
                return state
            close, quote = _scan_tag_tail(text, end_start, length)
            if close >= length:
                # The end tag has an unfinished attribute: nothing after it is an
                # element until its ``>`` arrives, so the raw-text run stays open.
                state.pending_quote = quote
                return state
            cursor = close + 1
            state.open_elements.pop()
            raw_text_of = ""
            continue
        start = text.find("<", cursor)
        if start < 0:
            return state
        cursor = start
        if text.startswith("<!--", cursor):
            if text.startswith("<!-->", cursor) or text.startswith("<!--->", cursor):
                cursor = text.find(">", cursor) + 1
                continue
            ends = [
                k for k in (text.find("-->", cursor + 4), text.find("--!>", cursor + 4)) if k >= 0
            ]
            if not ends:
                state.comment_open = True
                return state
            comment_end = min(ends)
            cursor = comment_end + (3 if text.startswith("-->", comment_end) else 4)
            continue
        if text.startswith("<!", cursor) or text.startswith("<?", cursor):
            bogus_end = text.find(">", cursor)
            if bogus_end < 0:
                state.comment_open = True
                return state
            cursor = bogus_end + 1
            continue
        match = _TAG_NAME_RE.match(text, cursor)
        if match is None:
            cursor += 1
            continue
        name = match.group(1).lower()
        closing = text[cursor + 1] == "/"
        position, quote = _scan_tag_tail(text, match.end(), length)
        if position >= length:
            state.pending_quote = quote
            return state
        cursor = position + 1
        if closing:
            if state.open_elements and state.open_elements[-1] == name:
                state.open_elements.pop()
        elif name in _RAW_TEXT_ELEMENTS:
            state.open_elements.append(name)
            raw_text_of = name
        elif name in _TRACKED_ELEMENTS:
            state.open_elements.append(name)
    return state


def _probe(content: str, closers: list[str], boundary: str) -> tuple[list[Token], str]:
    """Parse *content* plus *closers* exactly as the file will carry them.

    The same bytes :func:`iter_session_markdown` yields — body, closers, the
    separator — followed by the stand-in heading *boundary*, so what the parser
    reports about the stand-in is what a renderer will do to the real next turn.
    Returns the parse of that document and the rendered HTML of the message alone.
    """
    message = content if content.endswith("\n") else content + "\n"
    if closers:
        message += "\n".join(closers) + "\n"
    tokens = _PARSER.parse(f"{message}{_SEPARATOR}## {boundary}\n")
    # The HTML is rendered WITHOUT the separator and stand-in: a tag the message
    # left unfinished would otherwise be completed by the ">" of the separator's
    # own <hr>, and read as closed while a browser is still inside it.
    return tokens, _PARSER.render(message)


def _markdown_need(tokens: list[Token], boundary: str) -> tuple[bool, str | None]:
    """``(closed, closer)``: whether the stand-in heading survived, and if not, what closes the block that ate it.

    A top-level ``h2`` whose text is the stand-in *boundary* means the message's
    Markdown is closed. Otherwise the token holding the stand-in's text is the
    open block: a fence names its own closer (its exact marker run, so a longer
    opener gets a long-enough closer), and a terminator-ended HTML block names
    its terminator. ``(False, None)`` is a block this module does not know how
    to close — nothing is emitted, since a wrong closer is worse than none.

    Both searches are by text, which is sound only because *boundary* is a
    text the message does not contain (:func:`_boundary`): the one token that
    holds it is the one this module appended.
    """
    for index, token in enumerate(tokens[:-1]):
        if (
            token.type == "heading_open"
            and token.tag == "h2"
            and token.level == 0
            and tokens[index + 1].content == boundary
        ):
            return True, None
    for token in tokens:
        if boundary not in (token.content or ""):
            continue
        if token.type == "fence":
            return False, token.markup
        if token.type == "html_block":
            first_line = token.content.split("\n", 1)[0].lstrip(" ")
            for opener, closer in _HTML_BLOCK_CLOSERS:
                match = opener.match(first_line)
                if match is not None:
                    return False, closer.format(
                        tag=match.group(1).lower() if match.groups() else ""
                    )
        break
    return False, None


def _unterminated_blocks(content: str) -> str:
    """The text needed to close whatever *content* leaves open.

    Message content is written verbatim, so a turn that ends inside a code fence
    would make every following role heading render as code, and a turn that ends
    inside an HTML comment, an HTML block or a raw-text element would hide them
    from the renderer altogether. Either way the whole rest of the conversation
    disappears at read time while staying present in the file, which is the
    failure mode worth paying a parse for.

    Decided by parsing, never by scanning (see the module docstring). Each pass
    parses the message with the closers chosen so far, exactly as the file will
    carry them, and asks two questions in order:

    1. **Markdown.** Does the stand-in for the next heading parse as a top-level
       heading? If a fence or a terminator-ended HTML block swallowed it, that
       block's own closer is added (:func:`_markdown_need`).
    2. **HTML.** With the Markdown closed, does a browser reading the rendered
       output still have a comment, an unfinished tag or a raw-text element
       open? Those are closed in browser order (:class:`_RenderedState`).

    A closer added by one pass is verified by the next: it is parsed as content,
    so one that opened something would show up as the next thing to close, and
    one that fixed nothing is followed by the next layer rather than repeated.
    The order matters and falls out of the loop: a fence is closed before the
    comment it hid, and an element opened inside ``<pre>`` is closed before the
    ``</pre>`` that ends both the element's container and the Markdown block.

    A fence inside a list item or block quote draws no closer, and needs none:
    the separator that follows every message is dedented past the item and
    separated from the quote by a blank line, which ends the container and the
    fence with it — and the parser, seeing the stand-in heading intact, says so.

    Bounded by passes, not by how many closers one pass emits (:data:`_MAX_CLOSERS`):
    a pass can close any number of simultaneously-open elements at once, and the
    loop never trusts a batch just because it was the one that reached the
    budget. The last pass is always a verification, whether or not it added
    anything: raises :class:`ValueError` rather than returning a closer this
    module cannot prove closes everything, since an unverified guess the
    browser still reads as open is the one outcome worse than failing the export.
    """
    boundary = _boundary(content)
    closers: list[str] = []
    for _ in range(_MAX_CLOSERS):
        tokens, rendered = _probe(content, closers, boundary)
        closed, closer = _markdown_need(tokens, boundary)
        if not closed and closer is None:
            break
        if closer is not None and not closer.startswith("</"):
            # A fence, a comment, an instruction, a declaration or CDATA: the
            # block's own terminator is the whole answer.
            closers.append(closer)
            continue
        # Either the Markdown is closed, or a condition-1 HTML block is open
        # whose terminator is also an element's close tag. Let the browser's
        # view decide what to close first: an element opened INSIDE that block
        # must be closed before the block's own tag, or the tag is its text.
        state = _scan_rendered(rendered)
        html_closers = state.closers()
        if html_closers:
            closers.extend(html_closers)
            continue
        if closer is not None:
            closers.append(closer)
            continue
        break
    # Re-parse once more with whatever was accumulated, win or give up: a batch
    # added by the loop's own last iteration is only ever checked here, and an
    # exhausted pass budget must not ship its last, unexamined guess either.
    tokens, rendered = _probe(content, closers, boundary)
    closed, closer = _markdown_need(tokens, boundary)
    if not closed or closer is not None or _scan_rendered(rendered).closers():
        raise ValueError(
            "session_markdown: a message left HTML open that "
            f"{_MAX_CLOSERS} closer passes could not close"
        )
    return "\n".join(closers)


# ── the document ───────────────────────────────────────────────────────────────


def _message_heading(message: dict[str, Any]) -> str:
    """``## User`` / ``## Assistant``, with the message's timestamp when it has one.

    The timestamp is printed as the transcript recorded it — an ISO 8601 instant
    with its offset — rather than reformatted for a locale: this file is read on
    whatever machine it lands on, and an unambiguous instant survives that trip
    where a localised one does not. A row with no ``ts`` gets a bare heading
    rather than a placeholder.

    Both the label and the timestamp go through :func:`_single_line` for the reason
    the title does: a heading is one line, so a newline in either would render as a
    heading plus a separate block, and a block opening an HTML comment hides every
    turn beneath it. The gateway's own writer only ever stamps ISO instants here
    (``history.monotonic_transcript_ts``), but an IMPORTED bundle's ``ts`` is
    accepted as any string, and an unexpected role reaches ``label`` as its own
    text — so neither is this module's to trust.
    """
    role = str(message.get("role", "") or "")
    label = html.escape(
        _single_line(_ROLE_LABELS.get(role, role.title() or "Message")), quote=False
    )
    ts = html.escape(_single_line(str(message.get("ts", "") or "")), quote=False)
    return f"## {label} — {ts}\n" if ts else f"## {label}\n"


def iter_session_markdown(bundle: dict[str, Any]) -> Iterator[str]:
    """Yield the Markdown document for *bundle*, a chunk at a time. Pure.

    One chunk per heading, per content body and per separator rather than one per
    document, so :func:`write_markdown_file` never holds more than a single
    message's text. The caller joins or writes them; nothing here touches disk.

    *bundle* is the already-assembled, already-redacted export bundle — its
    ``title`` and non-user ``content`` went through ``session_transfer``'s egress
    scrubbers on the way in, and this renderer adds no new source of text.
    """
    title = html.escape(_single_line(str(bundle.get("title", "") or "")), quote=False)
    yield f"# {title}\n" if title else "# Session\n"

    source = bundle.get("source")
    rows: list[tuple[str, str]] = []
    agent = str(bundle.get("agent", "") or "")
    if agent:
        rows.append(("Agent", agent))
    if isinstance(source, dict):
        for key, label in _SOURCE_ROWS:
            if key not in source:
                continue
            value = source[key]
            if key == "approval_policy" and value == "":
                value = _INTERACTIVE_POLICY
            text = str(value)
            if text:
                rows.append((label, text))
    messages = bundle.get("messages") or []
    rows.append(("Messages", str(len(messages))))

    yield "\n| Field | Value |\n| --- | --- |\n"
    for label, value in rows:
        yield f"| {_escape_cell(label)} | {_escape_cell(value)} |\n"

    for message in messages:
        yield _SEPARATOR
        yield _message_heading(message)
        content = str(message.get("content", "") or "")
        if not content.strip():
            continue
        yield "\n"
        yield content if content.endswith("\n") else content + "\n"
        try:
            closing = _unterminated_blocks(content)
            if closing:
                yield closing + "\n"
        except ValueError:
            # If the message leaves constructs open that we cannot close
            # within the budget, wrap the entire content in a longer fence.
            # Find the longest run of backticks in the content.
            max_backticks = 0
            for line in content.split('\n'):
                current_run = 0
                for char in line:
                    if char == '`':
                        current_run += 1
                        max_backticks = max(max_backticks, current_run)
                    else:
                        current_run = 0
            # Use one more backtick than the longest run (min 3 for valid fence)
            fence = '`' * (max(3, max_backticks + 1))
            yield fence + "\n"


def write_markdown_file(bundle: dict[str, Any]) -> Path:
    """Render *bundle* to a temp Markdown file and return its path. **Blocking.**

    Staged in the export directory beside the JSON path's own temp file, so both
    formats are swept by the same orphan reclaim, and written through
    ``mkstemp`` so two concurrent exports cannot collide on a name. The caller
    owns the file and removes it with ``_rm_import_temps``; a failure mid-write
    removes it here rather than leaving a partial document behind.

    ``newline=""`` keeps the ``\\n`` line endings this renderer emits on every
    platform: Python's text mode would otherwise translate them to CRLF on
    Windows, which changes the bytes of a document whose content was captured
    with LF endings.

    ``errors="backslashreplace"`` is what keeps a transcript holding a lone
    surrogate exportable. Such a character reaches a transcript through the JSON
    wire format, which can carry an unpaired ``\\uD800`` that UTF-8 cannot encode;
    the JSON export survives it because ``ensure_ascii`` re-escapes it, so
    defaulting to ``strict`` here would make Markdown the one format that fails on
    a session the other exports. The escape is visible and reversible in the
    document, which a replacement character would not be.
    """
    fd, name = tempfile.mkstemp(dir=str(_egress_tmp_dir()), suffix=MARKDOWN_FILE_SUFFIX)
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="", errors="backslashreplace") as out:
            for chunk in iter_session_markdown(bundle):
                out.write(chunk)
    except BaseException:
        _rm_import_temps(path)
        raise
    return path
