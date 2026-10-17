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
swallows every later turn into one code block; a turn ending inside an HTML block
that runs to its own terminator — a comment, a ``<script``/``<pre``/``<style``/
``<textarea`` block, a ``<?`` instruction, a ``<!`` declaration or
``<![CDATA[`` (CommonMark §4.6, conditions 1-5) — swallows them into that block,
and the renderer never sees a heading again. Both are *silent* read-time loss —
the text is in the file and no reader sees it — which is why they are closed
rather than escaped.

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
swallowing block itself names. Every closer is re-verified by the same parse
before it is accepted, so a closer that would have opened something is never
emitted.

**A message the parse cannot settle is shown as plain text, and only that
message.** If the parser names a block this module has no closer for, or a closer
does not end it, :func:`_unterminated_blocks` raises :class:`ValueError` and
:func:`iter_session_markdown` writes that one message inside a backtick fence
longer than any backtick run it holds (:func:`_fenced`). Nothing inside such a
fence can end it, so the message cannot reach the headings after it; it reads as
its source instead of rendered, and every other turn is untouched. The fence is
verified by the same parse as a closer. Only a message even that cannot settle
fails the export, through the ordinary audited failure: a document with a guess in
it would be a reading copy that hides turns without saying so, which is worse than
no document.

**Where the guard stops.** Its boundary is CommonMark's: what the parser itself
reports as swallowing the next heading. What a given HTML renderer does with raw
HTML that CommonMark treats as ended is that renderer's contract — an element
left open on a line of its own, a tag left unfinished, a comment that a list
item or quote ended without its ``-->``. This export names no renderer, so it
models none. The file keeps every byte, and the JSON bundle stays the faithful
record.

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

#: The CommonMark parser every message is checked against, with raw HTML kept so
#: that an HTML block is parsed as one rather than as escaped text. The
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


#: How many closers :func:`_unterminated_blocks` will stack before giving up. A
#: parse names the first block left open, and ending it ends everything inside
#: it, so one closer settles a message. The bound is a backstop for a parser that
#: disagrees with its own terminator, never a count an ordinary message nears.
_MAX_CLOSERS = 8

#: CommonMark §4.6 conditions 1-5: the HTML blocks that run until their own
#: terminator, past any number of blank lines. Each entry is the opener (matched
#: against the block's first line) and the closer that ends it. Conditions 6
#: and 7 end at a blank line, and the separator is preceded by one, so they can
#: never reach the next heading and need no entry.
#:
#: The openers are the parser's own, flags included. The parser reads ``\s`` as
#: Unicode whitespace and folds case by Unicode rules, so ``<pre`` followed by a
#: no-break space opens a block it will not end: a table that stopped at ASCII
#: would find no closer for a block the parser had already opened.
#:
#: For a comment the closer is a whole comment rather than a bare ``-->``: the
#: line is a complete, self-contained comment in Markdown, so it is right
#: wherever it lands.
_HTML_BLOCK_CLOSERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"<(script|pre|style|textarea)(?=\s|>|$)", re.IGNORECASE), "</{tag}>"),
    (re.compile(r"<!--"), "<!-- -->"),
    (re.compile(r"<\?"), "?>"),
    (re.compile(r"<!\[CDATA\["), "]]>"),
    (re.compile(r"<![A-Z]"), ">"),
)


def _probe(content: str, closers: list[str], boundary: str) -> list[Token]:
    """Parse *content* plus *closers* exactly as the file will carry them.

    The same bytes :func:`iter_session_markdown` yields — body, closers, the
    separator — followed by the stand-in heading *boundary*, so what the parser
    reports about the stand-in is what a renderer will do to the real next turn.
    """
    message = content if content.endswith("\n") else content + "\n"
    if closers:
        message += "\n".join(closers) + "\n"
    return _PARSER.parse(f"{message}{_SEPARATOR}## {boundary}\n")


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
                        tag=match.group(1).casefold() if match.groups() else ""
                    )
        break
    return False, None


def _unterminated_blocks(content: str) -> str:
    """The text needed to close whatever *content* leaves open.

    Message content is written verbatim, so a turn that ends inside a code fence
    would make every following role heading render as code, and a turn that ends
    inside an HTML block that runs to its own terminator would swallow them into
    that block. Either way the whole rest of the conversation disappears at read
    time while staying present in the file, which is the failure mode worth
    paying a parse for.

    Decided by parsing, never by scanning (see the module docstring). Each pass
    parses the message with the closers chosen so far, exactly as the file will
    carry them, and asks one question: does the stand-in for the next heading
    parse as a top-level heading? If a fence or a terminator-ended HTML block
    swallowed it, that block's own closer is added (:func:`_markdown_need`), and
    the next pass is what verifies it. A closer is parsed as content like any
    other line, so one that opened something would show up as the next thing to
    close rather than go unnoticed.

    A fence inside a list item or block quote draws no closer, and needs none:
    the separator that follows every message is dedented past the item and
    separated from the quote by a blank line, which ends the container and the
    fence with it — and the parser, seeing the stand-in heading intact, says so.

    Bounded by :data:`_MAX_CLOSERS`. Raises :class:`ValueError` rather than
    returning a closer this module cannot prove closes the block: the parse names
    a block it does not know how to end, or the bound is spent. An unverified
    guess is worse than none, so the caller shows that one message as plain text
    instead (:func:`_fenced`).
    """
    boundary = _boundary(content)
    closers: list[str] = []
    while True:
        closed, closer = _markdown_need(_probe(content, closers, boundary), boundary)
        if closed:
            return "\n".join(closers)
        if closer is None or len(closers) >= _MAX_CLOSERS:
            raise ValueError("session_markdown: a message left a block open that no closer ends")
        closers.append(closer)


def _fenced(content: str) -> str:
    """*content* as plain text: inside one backtick fence longer than any run it holds.

    The fallback for a message :func:`_unterminated_blocks` could not settle. A
    backtick fence ends only on a line of at least its own length of backticks, so
    one longer than the longest run in the message — counted anywhere in it, not
    only at the start of a line — cannot be ended from inside, and the message
    cannot reach the headings after it however it is built. The fence is at least
    three long, the shortest that opens one.

    Verified by the same parse as a closer rather than trusted: the result is
    parsed exactly as the file will carry it, and :class:`ValueError` is raised if
    the stand-in heading does not survive, so an unproven wrapper is never written.
    """
    longest = max(map(len, re.findall(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    body = content if content.endswith("\n") else content + "\n"
    fenced = f"{fence}\n{body}{fence}\n"
    boundary = _boundary(content)
    if not _markdown_need(_probe(fenced, [], boundary), boundary)[0]:
        raise ValueError("session_markdown: a message could not be shown as plain text")
    return fenced


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
        try:
            closing = _unterminated_blocks(content)
        except ValueError:
            # The parse cannot settle this message. Show it as plain text rather
            # than fail the export for every turn that is fine (see _fenced).
            yield _fenced(content)
            continue
        yield content if content.endswith("\n") else content + "\n"
        if closing:
            yield closing + "\n"


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
