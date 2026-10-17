"""Atlassian Document Format to markdown, for Jira descriptions and comments.

The panel renders every provider's text through its markdown renderer, so ADF is
converted to markdown rather than flattened to plain text. Everything a provider
controls is escaped to stay literal, every URL and attribute passes a redaction
gate before it can become a link or a label, and traversal depth and per-line
expansion are bounded so a hostile document cannot exhaust the stack or memory.
"""

from __future__ import annotations

import json
import re
from typing import Any

from kiro_crew.dashboard.source_providers import projection, sanitize
from kiro_crew.dashboard.source_providers.contract import SourceProviderError

_ADF_MAX_DEPTH = 64

# ADF node types that occupy a line of their own. Everything else is treated as
# an inline run, so an unknown node still contributes its text rather than
# vanishing.
_ADF_BLOCK_TYPES = frozenset(
    {
        "doc",
        "paragraph",
        "heading",
        "codeBlock",
        "blockquote",
        "panel",
        "rule",
        "bulletList",
        "orderedList",
        "taskList",
        "table",
        "expand",
        "nestedExpand",
        "layoutSection",
        "layoutColumn",
        "bodiedExtension",
        "decisionList",
        "mediaSingle",
        "mediaGroup",
        "blockCard",
        "embedCard",
    }
)

# Containers that only GROUP other blocks. Markdown has no columns, so the
# grouping flattens to its children in document order -- but the children must
# still render as BLOCKS. Reaching the inline path instead concatenates them:
# a two-column layout of a paragraph and a heading rendered as `firstsecond`,
# with the separator and the `##` both gone. Measured on each type below.
_ADF_BLOCK_CONTAINER_TYPES = frozenset(
    {
        "layoutSection",
        "layoutColumn",
        "bodiedExtension",
        "decisionList",
        "mediaSingle",
        "mediaGroup",
    }
)

# Block-level cards carry their URL in an attribute and have no content, so the
# inline container fallthrough rendered them as the empty string -- the URL was
# lost outright, which is the same unrecoverable loss this change exists to fix.
_ADF_BLOCK_CARD_TYPES = frozenset({"blockCard", "embedCard"})

# List types, which follow their sibling block without a blank line so the
# nested list stays part of the same (tight) list item.
_ADF_LIST_TYPES = frozenset({"bulletList", "orderedList", "taskList"})

# Inline markdown/HTML syntax openers. The panel renders a source's
# ``description`` and every comment ``body`` through MarkdownRenderer
# (react-markdown + remark-gfm + rehypeRaw), so ADF *text* that merely looks
# like markup would otherwise be re-parsed as markup: a literal ``**`` in a
# Jira description would turn bold, a literal ``<b>`` would be eaten by the
# HTML sanitizer, and a literal ``&copy;`` would be decoded to a copyright sign.
# ``!`` earns its place for a different reason: this converter emits real ``[``
# for a link, a mention card and a media node, so a literal ``!`` landing
# immediately before one would splice into image syntax and the panel would
# auto-fetch a provider-controlled URL -- the beacon the media-as-link form
# exists to avoid. `$` is there because the same renderer runs remark-math, so a
# literal `$$x$$` in a Jira description would render as KaTeX rather than as the
# characters someone typed. Backslash-escaping these keeps ADF text literal,
# leaving the marks and block types below as the only things that become real
# markdown. Every character here is ASCII punctuation, which CommonMark says may
# always be backslash-escaped.
_MD_INLINE_ESCAPE = str.maketrans({ch: "\\" + ch for ch in "\\`*_[]<>|~&!$"})

# A text run that OPENS a line can also start a *block* construct the inline set
# above does not cover (``# heading``, ``- item``, ``1. item``, and a line of
# ``=`` or ``-`` that makes the line ABOVE it a setext heading). Only paragraph
# and list-item text is passed through this: a heading's own ``#`` prefix
# already claims its line, and list markers are added by the list renderer after
# its items are rendered.
_MD_BLOCK_LEAD_RE = re.compile(r"^([ \t]*)(?:([-+#=])|(\d{1,9})([.)]))", re.MULTILINE)

# The characters `_URL_RE` will not cross that can nonetheless appear INSIDE a
# URL. Its path/query class is `[^\s)\"'>]*`, so any of these ends the match and
# puts the rest of the URL -- including its whole query -- outside every
# exfiltration check that follows.
#
# Whitespace is deliberately NOT here even though the class excludes it. A space
# genuinely ends a URL: the renderer's own autolinker stops there too, so text
# after it is prose rather than part of a fetchable address. Encoding it would
# treat the two as one URL and destroy benign content -- `see <url>?id=7
# <40-char sha> for detail` would collapse to `see%20[REDACTED: suspicious URL
# ...]`, losing every word after the URL.
#
# This table is a SHADOW of that scanner's terminator set and exists only while
# the scanner truncates. Retire it once the scanner's own set is fixed rather
# than keeping both: two copies of one set drift, and the copy that matters is
# the scanner's.
_URL_SCAN_ESCAPES = str.maketrans(
    {
        '"': "%22",
        "'": "%27",
        "(": "%28",
        ")": "%29",
        ">": "%3E",
    }
)

# A URL as any consumer delimits one: it runs to the first whitespace. Terminator
# encoding is applied only INSIDE these spans so surrounding prose keeps its own
# punctuation.
_URL_SPAN_RE = re.compile(r"https?://\S*", re.IGNORECASE)

# The largest ordered-list start CommonMark accepts. Ten digits is not a list
# marker, so a longer number renders as a literal paragraph.
_MD_MAX_LIST_START = 999999999

# One highlighter token, and nothing that could leave the fence's own line. The
# anchors are deliberately absent: `$` also matches just before a TRAILING
# newline, so `re.match` accepted `"python\n"` and the fence emitted a blank
# first line inside the block. `fullmatch` is the check that means what this
# comment says.
_MD_CODE_LANGUAGE_RE = re.compile(r"[A-Za-z0-9+#._-]{1,32}")


def _md_redact_untruncated(text: str, *, single_url: bool = False) -> str:
    """Redact *text* with the URL scan able to see whole URLs.

    `_redact_provider_data` inherits `_URL_RE`, whose path/query class stops at
    whitespace, ``)``, ``"``, ``'`` or ``>``. A URL carrying any of them is
    scanned only as far as that character, so its query -- the part that would
    carry exfiltrated data -- is never inspected, and `_exfil_url_warning`
    returns clean on a truncated match with no ``?`` left in it. Scanning a form
    with those characters percent-encoded is what lets the checks see all of it.

    Every place this converter emits provider text goes through here, because
    the gap is per-CALL-SITE, not per-node-type: when only the link destination
    was covered, an expand title, a mention label, an inline card and a media URL
    each still leaked a high-entropy query, measured one by one.

    The rule is to scan the form that will actually be EMITTED, which is why
    whitespace is handled differently per site rather than uniformly:

    * ``single_url`` -- a link DESTINATION is one address, and the angle-bracket
      form emits it with whitespace percent-encoded, so a space does NOT end it.
      The scan encodes whitespace too, or it would stop where the emitted URL
      does not.
    * otherwise -- in prose a space really does end the URL. Verified against the
      renderer: for `https://host/a?data= <blob>` the emitted anchor's href is
      `https://host/a?data=`, so following text cannot ride along in a fetchable
      address. Encoding whitespace here treated a URL and the next word as one
      address: a URL followed by a commit SHA scanned as a query carrying the SHA,
      the entropy heuristic fired, and the WHOLE paragraph was replaced.

    When nothing is found the ORIGINAL text comes back, so no encoding is ever
    visible in the ordinary case. When something IS found the redacted encoded
    form is returned: the marker replaces the URL wholesale, and a stray percent
    escape beside a redaction marker is a better outcome than emitting a URL that
    was only scanned up to its first parenthesis.
    """
    if single_url:
        scanned = re.sub(r"\s+", "%20", text).translate(_URL_SCAN_ESCAPES)
    else:
        scanned = _URL_SPAN_RE.sub(lambda m: m.group(0).translate(_URL_SCAN_ESCAPES), text)
    redacted = str(sanitize._redact_provider_data(scanned))
    return text if redacted == scanned else redacted


def _md_escape_inline(text: str) -> str:
    """Backslash-escape the markdown syntax characters in literal ADF text."""
    return text.translate(_MD_INLINE_ESCAPE)


def _adf_attr_label(value: Any) -> str:
    """Prepare an ADF *attribute* string for emission as an inline label.

    Redact, collapse whitespace, then escape -- in that order, and all three for
    the same reason.

    An attribute is a label, not prose. Where a text node's own newline is
    content, a newline inside an attribute would end the construct the attribute
    sits in and let the remainder become document structure, so whitespace is
    collapsed first. Escaping then keeps the rest literal -- and because escaping
    inserts a backslash, it would hide a credential from the payload-level
    ``_redact_provider_data`` pass that runs afterwards, so redaction has to
    happen here, before the backslash lands.

    Doing it here rather than in a pre-pass over the whole tree is what keeps the
    work bounded: this runs inside the converter's own depth-capped traversal,
    while ``_redact_provider_data`` recurses without a cap and raises
    ``RecursionError`` on a document a few hundred levels deep.
    """
    collapsed = re.sub(r"\s+", " ", _md_redact_untruncated(str(value or ""))).strip()
    return _md_escape_inline(collapsed)


def _md_code_language(value: Any) -> str:
    """The fence info string for an ADF code block's ``language`` attribute.

    A fence's info string runs to the end of its line, so a newline-bearing (or
    merely space-bearing) attribute would close the fence early and turn
    provider-controlled text into real document structure. Only a single
    highlighter token is admitted: letters, digits, and the punctuation real
    language names carry (``c++``, ``c#``, ``objective-c``, ``asp.net``,
    ``shell_session``). Anything else drops to a bare fence, which costs syntax
    highlighting and nothing else.
    """
    language = str(value or "")
    return language if _MD_CODE_LANGUAGE_RE.fullmatch(language) else ""


def _md_escape_block_leads(text: str) -> str:
    """Escape a line-leading ``-``/``+``/``#``/``1.`` so it stays literal text.

    The backslash goes before the punctuation, never before the digit: ``\\1`` is
    not a valid CommonMark escape and would render as a visible backslash.
    """

    def _sub(match: re.Match[str]) -> str:
        if match.group(2):
            return f"{match.group(1)}\\{match.group(2)}"
        return f"{match.group(1)}{match.group(3)}\\{match.group(4)}"

    return _MD_BLOCK_LEAD_RE.sub(_sub, text)


def _md_backtick_fence(text: str, minimum: int) -> str:
    """A backtick fence long enough to survive the backticks inside *text*."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(minimum, longest + 1)


def _md_inline_code(text: str) -> str:
    """Wrap *text* in an inline code span, unescaped (code spans are literal).

    CommonMark cannot open a span whose content starts or ends with a backtick,
    and it strips one leading and one trailing character from a span whose
    content both begins and ends with a space or newline (unless the content is
    nothing but whitespace, which is left alone). One space of padding -- which
    that same rule then removes -- is what keeps such content intact.

    A line break inside the content is collapsed to a space FIRST. A code span is
    inline, so it cannot span a paragraph: a newline in the content ends the
    enclosing paragraph and everything after it is parsed as fresh markdown --
    outside the fence, and so past :func:`_md_escape_inline`,
    :func:`_md_link_target` and the redaction gate. Collapsed rather than emitted
    as a fenced block, because a block would change the document structure at
    every call site while the escape is what actually has to hold.
    """
    text = re.sub(r"\r\n?|\n", " ", text)
    fence = _md_backtick_fence(text, 1)
    first, last = text[:1], text[-1:]
    edge_stripped = first in (" ", "\n") and last in (" ", "\n") and text.strip() != ""
    pad = " " if text.startswith("`") or text.endswith("`") or edge_stripped else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _md_link_target(url: str) -> str | None:
    """Render *url* as a markdown link destination, or None to omit the link.

    The bare form covers the common case; the angle-bracket form takes over when
    the URL carries whitespace or parentheses, which would otherwise end the
    destination early and spill the rest of the URL into the document.

    None means this URL must not become a destination at all. The old plain-text
    walker dropped every href, so a provider-controlled destination reaching the
    payload is new here, and the payload-level scan cannot be relied on to clean
    one up afterwards: ``_URL_RE``'s path group excludes ``)``, so a URL with a
    ``)`` in its path is matched only up to that point. Everything past it --
    including the whole query -- is then outside every check that follows, and
    ``_exfil_url_warning`` returns clean on a truncated match with no ``?`` left
    in it. Measured: a high-entropy query blob is redacted without the paren and
    NOT redacted with it, and no credential-pattern pass covers a bare blob.

    So the scan here is run against a form with every character that terminates
    that match percent-encoded -- not just the parenthesis, which is one member
    of that class. The encoded form only ever informs the DECISION: a URL that
    passes is emitted exactly as it arrived.
    Encoding a handful of characters cannot trip the heavy-encoding rule, which
    needs 20 consecutive octets, and a URL pathological enough to reach that is
    dropped rather than leaked. A link that fails is dropped rather than emitted
    partly redacted; the label still carries the text, so nothing silently
    vanishes.

    This shields the hrefs THIS converter emits. The truncation itself is in the
    shared scanner and every other caller still has it, so it is tracked against
    that scanner rather than left recorded only here; fixing a shared security
    regex is a different blast radius than a Jira rendering change.
    """
    if not url:
        return None
    if _md_redact_untruncated(url, single_url=True) != url:
        return None
    if not re.search(r"[\s()<>]", url):
        return url
    inner = re.sub(r"\s+", "%20", url).replace("<", "%3C").replace(">", "%3E")
    return f"<{inner}>"


def _md_one_line(text: str) -> str:
    """Fold *text* onto a single line, collapsing ONLY line breaks.

    A heading and a GFM table cell each occupy exactly one line, but the repeated
    spaces and tabs inside one are content, not layout: a code span's whitespace
    is literal by definition, and the padding that protects its boundary spaces
    would be eaten by a blanket whitespace collapse. Only a newline has to go,
    and a code span's own padding sits inside its backticks where no newline can
    reach it.

    Split on the line breaks rather than matched with a regex. The pattern this
    replaces made the leading whitespace run and the newline anchor compete for
    the same characters, so on a long newline-FREE whitespace run -- content a
    provider controls, bounded only by the 8MiB fetch cap -- the engine retried
    every split of that run at every starting offset before failing. A split,
    strip and join reads each character a fixed number of times. The collapsed
    set is unchanged: a maximal whitespace run containing at least one line break
    becomes one space, and the whitespace class strip uses is the one the pattern
    matched.
    """
    return " ".join(seg for seg in (line.strip() for line in text.split("\n")) if seg)


def _md_guard_line_expansion(text: str, per_line: int) -> None:
    """Refuse a per-line expansion that would blow the payload ceiling.

    Marking or indenting adds *per_line* characters to EVERY line, and a provider
    controls both numbers. Newlines embedded in a single text node cost about
    three bytes of payload each, while sixty levels of nesting adds a hundred and
    twenty characters to every one of them, so a document well inside the 8MiB
    fetch cap can project past three hundred MiB of output. The payload gate runs
    only AFTER conversion, so without this check the allocation happens first:
    measured before it, a 2.3MiB payload rendered 93MiB of markdown with a 224MiB
    peak, and the 8MiB cap extrapolates to roughly 780MiB.

    Raising here matches how an oversized response is already refused, and it
    lives inside the two expanders rather than at their call sites so no new
    caller can forget it.
    """
    if len(text) + per_line * (text.count("\n") + 1) > sanitize._MAX_PAYLOAD_BYTES:
        raise SourceProviderError("Jira issue content is too large to render.")


def _md_prefix_lines(text: str, prefix: str) -> str:
    """Prefix every line of *text*, keeping blank lines inside the same block."""
    _md_guard_line_expansion(text, len(prefix))
    stripped = prefix.rstrip()
    return "\n".join(prefix + line if line else stripped for line in text.split("\n"))


def _md_hang_indent(body: str, marker: str) -> str:
    """Put *marker* on the first line and align continuation lines under it."""
    if not body:
        return ""
    _md_guard_line_expansion(body, len(marker))
    pad = " " * len(marker)
    head, *rest = body.split("\n")
    lines = [marker + head]
    lines.extend(pad + line if line else "" for line in rest)
    return "\n".join(lines)


def _adf_to_markdown(node: Any, *, _depth: int = 0) -> str:
    """Convert an Atlassian Document Format tree to markdown.

    ADF is the JSON document model Jira Cloud v3 returns for rich-text fields
    (descriptions and comment bodies). The panel renders those fields through
    MarkdownRenderer, and every other provider puts real markdown in the same
    payload field (a GitHub issue ``body``, a GitLab ``description``), so
    emitting markdown here restores headings, lists, link URLs, code fences and
    tables that a plain-text walk would drop.

    Traversal is depth-limited (max 64 levels) to prevent stack exhaustion from a
    malformed or maliciously deep document, and literal text is escaped so a
    description cannot smuggle markup into the panel.

    Best-effort by design: ADF tables may carry merged cells and nested blocks
    that GFM cannot express (rendered as a flat approximation), and a ``media``
    node without a public ``url`` attribute has no fetchable address, so it
    contributes nothing.
    """
    if _depth > _ADF_MAX_DEPTH or not isinstance(node, dict):
        return ""
    if str(node.get("type") or "") in _ADF_BLOCK_TYPES:
        return _adf_block_to_markdown(node, _depth=_depth)
    return _adf_inline_to_markdown(node, _depth=_depth)


def _adf_block_to_markdown(node: dict[str, Any], *, _depth: int) -> str:
    """Render one ADF block node. Only called for a type in _ADF_BLOCK_TYPES."""
    node_type = str(node.get("type") or "")
    attrs = projection._as_dict(node.get("attrs"))
    if node_type == "doc":
        return _adf_join_blocks(node, _depth=_depth)
    if node_type in _ADF_BLOCK_CONTAINER_TYPES:
        return _adf_join_blocks(node, _depth=_depth)
    if node_type in _ADF_BLOCK_CARD_TYPES:
        return _adf_url_link(str(attrs.get("url") or ""))
    if node_type == "paragraph":
        return _md_escape_block_leads(_adf_inline_run(node, _depth=_depth))
    if node_type == "heading":
        level = min(max(projection._int_or_zero(attrs.get("level")) or 1, 1), 6)
        # A heading occupies one line, and only its first line carries the `#`
        # prefix. A hardBreak inside it would push the rest onto a second line
        # where a leading `-` or `#` is neither inline- nor block-lead-escaped and
        # would render as a spurious list or heading.
        text = _md_one_line(_adf_inline_run(node, _depth=_depth))
        return f"{'#' * level} {text}" if text else ""
    if node_type == "codeBlock":
        body = _adf_plain_text(node, _depth=_depth)
        fence = _md_backtick_fence(body, 3)
        language = _md_code_language(attrs.get("language"))
        # The newline before the closing fence SEPARATES the body from it, so a
        # body that already ends with one does not need another: adding it
        # unconditionally turned a source ending in `\n` into content ending in
        # `\n\n`, and an empty body into a block holding one blank line.
        if not body:
            return f"{fence}{language}\n{fence}"
        separator = "" if body.endswith("\n") else "\n"
        return f"{fence}{language}\n{body}{separator}{fence}"
    if node_type in ("blockquote", "panel"):
        # An ADF panel (info/note/warning) has no markdown equivalent; a
        # blockquote keeps it visually set apart from the surrounding prose.
        #
        # A chain of single-child quotes is collapsed and prefixed ONCE. Marking
        # at every level re-copies text the level below already marked, which is
        # quadratic in the nesting depth for the number of lines it carries: on a
        # 6.7MB document nested 60 deep that measured 2.57s of blocking work
        # against 0.47s for the same content unnested. Each collapsed level still
        # consumes depth, so the traversal cap applies exactly as before.
        marks = 1
        inner_node = node
        while True:
            only = projection._as_list(inner_node.get("content"))
            if len(only) == 1 and str(only[0].get("type") or "") in ("blockquote", "panel"):
                inner_node = only[0]
                marks += 1
                _depth += 1
            else:
                break
        inner = _adf_join_blocks(inner_node, _depth=_depth)
        return _md_prefix_lines(inner, "> " * marks) if inner else ""
    if node_type == "rule":
        return "---"
    if node_type in ("bulletList", "orderedList"):
        return _adf_list_to_markdown(node, _depth=_depth, ordered=node_type == "orderedList")
    if node_type == "taskList":
        return _adf_task_list_to_markdown(node, _depth=_depth)
    if node_type == "table":
        return _adf_table_to_markdown(node, _depth=_depth)
    # expand / nestedExpand: a collapsed section, whose title is the only part
    # markdown cannot express as a container.
    title = _adf_attr_label(attrs.get("title"))
    inner = _adf_join_blocks(node, _depth=_depth)
    return "\n\n".join(part for part in (f"**{title}**" if title else "", inner) if part)


def _adf_inline_to_markdown(node: Any, *, _depth: int, _scanned: bool = False) -> str:
    """Render one ADF inline node, recursing into an unrecognised container."""
    if _depth > _ADF_MAX_DEPTH or not isinstance(node, dict):
        return ""
    node_type = str(node.get("type") or "")
    attrs = projection._as_dict(node.get("attrs"))
    if node_type == "text":
        return _adf_apply_marks(str(node.get("text") or ""), projection._as_list(node.get("marks")))
    if node_type == "hardBreak":
        # Two trailing spaces: the panel renders with CommonMark soft-break
        # collapse, so a bare newline would become a space.
        return "  \n"
    if node_type == "mention":
        name = _adf_attr_label(attrs.get("text") or attrs.get("id"))
        if not name:
            return ""
        return name if name.startswith("@") else f"@{name}"
    if node_type == "emoji":
        return _adf_attr_label(attrs.get("text") or attrs.get("shortName"))
    if node_type == "inlineCard":
        return _adf_url_link(str(attrs.get("url") or ""))
    if node_type == "media":
        # A link, not an image: the URL stays recoverable (the loss this fix is
        # about) without the panel auto-fetching a provider-controlled address
        # the moment someone opens the issue.
        return _adf_url_link(str(attrs.get("url") or ""), _adf_attr_label(attrs.get("alt")))
    # An unrecognised inline container contributes only its children, so it gets
    # the same span treatment they would get at the top level -- otherwise two
    # equally marked text nodes one level down still emit `**a****b**`. The
    # ancestor's scan already covered this subtree, so it is not repeated.
    return _adf_inline_sequence(
        projection._as_list(node.get("content")), _depth=_depth + 1, _scanned=_scanned
    )


_ADF_EMPHASIS_MARKS = frozenset({"strong", "em"})


def _adf_emphasis_item(node: dict[str, Any], *, _depth: int) -> tuple[str, frozenset[str]]:
    """Split one inline node into (text, marks that can be factored).

    Only ``strong`` and ``em`` are factorable, because they are the only two
    marks that share a delimiter CHARACTER and so the only two whose delimiter
    runs can merge with a neighbour's. A node carrying anything else -- a code
    mark, strike, a link -- is rendered whole and treated as opaque: its own
    backtick, tilde or bracket sits at the boundary and keeps the asterisks
    apart, which was verified per pair rather than assumed.
    """
    text = str(node.get("text") or "")
    kinds = {str(mark.get("type") or "") for mark in projection._as_list(node.get("marks"))}
    if str(node.get("type") or "") != "text" or not text or not kinds <= _ADF_EMPHASIS_MARKS:
        return _adf_inline_to_markdown(node, _depth=_depth, _scanned=True), frozenset()
    return _md_escape_inline(text), frozenset(kinds)


def _md_wrap_emphasis(mark: str, inner: str, before: str, after: str) -> str:
    """Wrap *inner* for one emphasis mark with a delimiter that survives.

    ``before`` and ``after`` are the single characters on either side, not the
    surrounding text: only the adjacent character decides either rule, and
    passing the accumulated output instead made the emitter quadratic.

    ``strong`` has only ``**``, and an asterisk on the INSIDE of it is fine: a
    closing ``***`` resolves correctly. ``em`` needs both of its spellings,
    because neither works everywhere -- ``*`` is re-lexed when it touches
    another asterisk run, and ``_`` will not open or close INTRAWORD. When
    neither is safe at both ends the mark is dropped and the text kept: that
    loses an italic, where emitting the delimiter anyway would show it as
    content and lose the italic as well.
    """
    if not inner:
        return ""
    if mark == "strong":
        return f"**{inner}**"
    if before != "*" and after != "*":
        return f"*{inner}*"
    if not before.isalnum() and not after.isalnum():
        return f"_{inner}_"
    return inner


def _md_emit_emphasis(items: list[tuple[str, frozenset[str]]], *, before: str = "") -> str:
    """Emit a run of (text, marks) items, factoring a shared mark out ONCE.

    Wrapping each node independently is what corrupts overlapping marks. A
    strong node, then strong+em, then em emitted ``**a*****b****c*``, which a
    CommonMark parser reads as strong(a), a LITERAL ``***b***``, then em(c): the
    delimiters become visible text and the middle node loses both its marks.
    Emitting a mark shared by neighbours ONCE, around all of them, is what keeps
    the runs unambiguous -- ``**a*b***_c_`` parses as intended.

    The scan is greedy from the left, taking the mark that spans the longest run
    at each position. A different factorisation can occasionally keep a mark this
    one drops; greedy is chosen because its fallback is lossy, never wrong.

    ``before`` is the one character preceding this run. Recursion is bounded by
    the number of factorable marks -- each level removes one, so it is at most
    two deep and needs no depth guard of its own.
    """
    out: list[str] = []
    last = before
    index = 0
    while index < len(items):
        text, marks = items[index]
        if not marks:
            out.append(text)
            last = text[-1:] or last
            index += 1
            continue
        best_mark, best_end = "", index
        for mark in ("strong", "em"):
            if mark not in marks:
                continue
            end = index
            while end < len(items) and mark in items[end][1]:
                end += 1
            if end > best_end:
                best_mark, best_end = mark, end
        inner = _md_emit_emphasis(
            [(t, ms - {best_mark}) for t, ms in items[index:best_end]], before=last
        )
        if best_end < len(items):
            next_text, next_marks = items[best_end]
            # A marked neighbour opens with a delimiter, which is punctuation for
            # the underscore rule and an asterisk for the run-merging one.
            following = "*" if next_marks else next_text[:1]
        else:
            following = ""
        piece = _md_wrap_emphasis(best_mark, inner, last, following)
        out.append(piece)
        last = piece[-1:] or last
        index = best_end
    return "".join(out)


def _adf_apply_marks(text: str, marks: list[dict[str, Any]]) -> str:
    """Wrap literal *text* in the markdown for each ADF mark, innermost first.

    A ``code`` mark is exclusive: a code span is literal by definition, so the
    emphasis marks are not applied inside one and the text is not escaped.
    Empty text takes no mark wrapping at all, since a bare ``****`` or ``` `` ```
    would render as those literal characters rather than as nothing.
    """
    kinds = {str(mark.get("type") or "") for mark in marks}
    if not text:
        out = ""
    elif "code" in kinds:
        out = _md_inline_code(text)
    else:
        out = _md_escape_inline(text)
        if "strong" in kinds:
            out = f"**{out}**"
        if "em" in kinds:
            # Asterisk, not underscore: CommonMark refuses to open or close an
            # underscore emphasis INTRAWORD, so an italic node between two plain
            # ones would render as `a_b_c` with the underscores visible and the
            # italic lost. Asterisk has no such restriction, and `***x***` still
            # nests correctly when a strong mark wraps the same text.
            out = f"*{out}*"
        if "strike" in kinds:
            out = f"~~{out}~~"
    for mark in marks:
        if str(mark.get("type") or "") != "link":
            continue
        href = str(projection._as_dict(mark.get("attrs")).get("href") or "")
        if href:
            target = _md_link_target(href)
            if target:
                out = f"[{out or _md_escape_inline(href)}]({target})"
            else:
                # No destination, but keep the text: the label is what the reader
                # was shown, and the href is the part that failed the scan.
                out = out or _adf_attr_label(href)
        break
    return out


def _adf_inline_run(node: dict[str, Any], *, _depth: int) -> str:
    """Concatenate a block's inline children into one line of markdown."""
    return _adf_inline_sequence(projection._as_list(node.get("content")), _depth=_depth + 1).strip()


def _adf_inline_sequence(
    children: list[dict[str, Any]], *, _depth: int, _scanned: bool = False
) -> str:
    """Render a run of inline nodes.

    Redaction is checked ONCE, over the whole run, against the plain-text
    rendition a seamless walk would produce -- every node's own text in order,
    with no markup between any of it. That string is exactly what the
    payload-level ``_redact_provider_data`` pass sees, so checking it is what
    preserves a catch this converter would otherwise break: escaping puts a
    backslash inside ``ghp_``, and marks put delimiters between the halves of a
    secret split across siblings, so a credential contiguous in the plain-text
    rendition is not contiguous in the marked-up one.

    Checking the WHOLE run rather than some span of it is deliberate. Any
    narrower boundary has to answer "which nodes contribute text seamlessly", and
    that question kept having a wider answer than the last one -- a bold sibling,
    then an unrecognised container, then a mention or emoji label, each of which
    contributes text with no delimiter of its own. The run has no such boundary
    to get wrong.

    When the check fires the run is emitted as that redacted string: it loses its
    formatting, but no node's text is lost with it.

    ``_scanned`` says an ancestor already scanned this subtree and found it clean.
    That scan covered every descendant's text, since ``_adf_plain_text`` recurses,
    so re-scanning inside a nested container is provably redundant -- and it is
    not free: rescanning at each level is depth-times-text work, which measured
    7.0s for 1MiB under 60 unrecognised containers and 27.8s for 4MiB.
    """
    if not _scanned:
        plain = "".join(_adf_plain_text(child, _depth=_depth) for child in children)
        redacted = _md_redact_untruncated(plain)
        if redacted != plain:
            return _md_escape_inline(redacted)
    return _md_emit_emphasis(
        [_adf_emphasis_item(node, _depth=_depth) for node in _adf_merge_marked_text(children)]
    )


def _adf_mark_key(node: dict[str, Any]) -> list[tuple[str, str]]:
    """An order-insensitive signature for a text node's marks."""
    return sorted(
        (
            str(mark.get("type") or ""),
            json.dumps(projection._as_dict(mark.get("attrs")), sort_keys=True),
        )
        for mark in projection._as_list(node.get("marks"))
    )


def _adf_merge_marked_text(span: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge neighbouring text nodes whose marks are identical.

    A hardBreak between two text nodes stops the merge, since the break has to
    survive between them.

    Each run's text is collected in a list and joined ONCE at the end. Rebuilding
    the accumulator as ``previous + current`` per node is superlinear -- measured
    at 0.26s for 100k adjacent nodes, 0.75s for 200k and 1.48s for 300k, and 300k
    single-character text nodes fit inside the 8MiB fetch cap -- so a provider
    could buy seconds of synchronous work on the event loop. Only the traversal
    DEPTH is capped; nothing bounds a node's breadth.
    """
    merged: list[dict[str, Any]] = []
    runs: list[list[str]] = []
    previous_key: list[tuple[str, str]] | None = None
    for node in span:
        is_text = str(node.get("type") or "") == "text"
        key = _adf_mark_key(node) if is_text else None
        if merged and is_text and previous_key is not None and key == previous_key:
            runs[-1].append(str(node.get("text") or ""))
            continue
        merged.append(node)
        runs.append([str(node.get("text") or "")] if is_text else [])
        previous_key = key
    return [{**node, "text": "".join(run)} if run else node for node, run in zip(merged, runs)]


def _adf_plain_text(node: Any, *, _depth: int) -> str:
    """The plain text a node contributes, with no markup of any kind.

    This is the markup-free rendition of a node, and it serves two callers for
    the same reason -- both want the characters, not the markup: a code block's
    literal body, and the redaction gate in ``_adf_inline_sequence``, which has
    to see what the payload-level redactor sees.

    A label-bearing node contributes its label WITHOUT the markup this converter
    would wrap it in -- a mention's bare name, not ``@name``; a card's URL, not
    ``[url](url)``. That is deliberate: the gate must never see less contiguity
    than the rendered output has, and dropping the prefix can only make it see
    more, which errs toward redacting.
    """
    if _depth > _ADF_MAX_DEPTH or not isinstance(node, dict):
        return ""
    node_type = str(node.get("type") or "")
    attrs = projection._as_dict(node.get("attrs"))
    if node_type == "text":
        return str(node.get("text") or "")
    if node_type == "hardBreak":
        return "\n"
    if node_type == "mention":
        return str(attrs.get("text") or attrs.get("id") or "")
    if node_type == "emoji":
        return str(attrs.get("text") or attrs.get("shortName") or "")
    if node_type == "media":
        # The ALT before the URL, because the alt is what the reader is shown and
        # what can JOIN a neighbour's text. When the URL fails the destination
        # scan the link is dropped and the alt is emitted with no brackets around
        # it, so a credential split across a text node and an alt becomes one
        # contiguous token -- measured, with the backslash from escaping `ghp_`
        # then defeating the payload pass. Returning the URL here made the gate
        # blind to exactly that. The URL is the fallback for a media node with no
        # alt, which is still emitted as the label.
        return str(attrs.get("alt") or attrs.get("url") or "")
    if node_type == "inlineCard":
        # A card has no alt: its label IS the redacted URL, so the URL is what
        # the gate must see.
        return str(attrs.get("url") or "")
    return "".join(
        _adf_plain_text(child, _depth=_depth + 1)
        for child in projection._as_list(node.get("content"))
    )


def _adf_url_link(url: str, label: str = "") -> str:
    """Render a provider URL as a link, or as its label alone if the URL fails.

    The single place a provider URL becomes a markdown destination, so the scan
    in `_md_link_target` cannot be skipped by adding another node type that
    carries a URL. `_adf_attr_label` is the matching chokepoint for attribute
    text; between them, no provider attribute reaches the document unscanned.
    """
    if not url:
        return ""
    text = label or _adf_attr_label(url)
    target = _md_link_target(url)
    return f"[{text}]({target})" if target else text


def _adf_join_blocks(node: dict[str, Any], *, _depth: int) -> str:
    """Render a container's children as markdown blocks, blank-line separated."""
    rendered = (
        _adf_to_markdown(child, _depth=_depth + 1)
        for child in projection._as_list(node.get("content"))
    )
    return "\n\n".join(block for block in rendered if block)


def _adf_item_body(item: dict[str, Any], *, _depth: int) -> str:
    """Render one list item, which may mix inline text with nested blocks."""
    blocks: list[tuple[str, bool]] = []
    run: list[dict[str, Any]] = []

    def flush() -> None:
        # Through _adf_inline_sequence, so an item's own inline children get the
        # same split-credential guarantee a paragraph's do.
        text = _adf_inline_sequence(list(run), _depth=_depth + 1).strip()
        run.clear()
        if text:
            blocks.append((_md_escape_block_leads(text), False))

    for child in projection._as_list(item.get("content")):
        child_type = str(child.get("type") or "")
        if child_type in _ADF_BLOCK_TYPES:
            flush()
            # Through _adf_to_markdown, never straight to the block renderer: a
            # nested list would otherwise re-enter its own renderer past the
            # depth cap and exhaust the stack on a deeply nested document.
            rendered = _adf_to_markdown(child, _depth=_depth + 1)
            if rendered:
                blocks.append((rendered, child_type in _ADF_LIST_TYPES))
        else:
            run.append(child)
    flush()

    if not blocks:
        return ""
    parts = [blocks[0][0]]
    for text, is_list in blocks[1:]:
        parts.append("\n" if is_list else "\n\n")
        parts.append(text)
    return "".join(parts)


def _adf_list_to_markdown(node: dict[str, Any], *, _depth: int, ordered: bool) -> str:
    """Render a bullet or ordered list, honouring an explicit start number."""
    items = projection._as_list(node.get("content"))
    raw_order = projection._as_dict(node.get("attrs")).get("order")
    # `or 1` collapsed an explicit `order: 0`, which ADF allows and CommonMark
    # honours as `<ol start="0">`. Only an absent, non-integer or negative value
    # falls back to 1. `bool` is excluded because it is an `int` in Python and
    # `order: true` is not a start number.
    start = (
        raw_order
        if isinstance(raw_order, int) and not isinstance(raw_order, bool) and raw_order >= 0
        else 1
    )
    # More than nine digits is not a list start at all: CommonMark renders
    # `1000000000. a` as a PARAGRAPH, so an out-of-range order would turn the
    # whole list into literal text carrying visible numbers. The LAST item's
    # marker is the one that has to fit, since a start of 999999999 overflows on
    # its second item.
    if start + max(len(items) - 1, 0) > _MD_MAX_LIST_START:
        start = 1
    lines: list[str] = []
    for index, item in enumerate(items):
        marker = f"{start + index}. " if ordered else "- "
        rendered = _md_hang_indent(_adf_item_body(item, _depth=_depth + 1), marker)
        if rendered:
            lines.append(rendered)
    return "\n".join(lines)


def _adf_task_list_to_markdown(node: dict[str, Any], *, _depth: int) -> str:
    """Render an ADF task list as a GFM checklist."""
    lines: list[str] = []
    for item in projection._as_list(node.get("content")):
        state = str(projection._as_dict(item.get("attrs")).get("state") or "").upper()
        marker = "- [x] " if state == "DONE" else "- [ ] "
        rendered = _md_hang_indent(_adf_item_body(item, _depth=_depth + 1), marker)
        if rendered:
            lines.append(rendered)
    return "\n".join(lines)


def _adf_table_to_markdown(node: dict[str, Any], *, _depth: int) -> str:
    """Render an ADF table as a GFM table, using its first row as the header.

    GFM requires a header row and cannot express merged cells or block content
    inside a cell, so cell text is flattened to a single line and any
    colspan/rowspan is ignored.

    Each BODY row emits its own cells and nothing more, because GFM inserts empty
    cells for a row shorter than the header. The HEADER and separator are widened
    to the widest row, because the other direction is not symmetric: GFM fixes the
    table's width at the header and a row with MORE cells than the header has the
    excess dropped -- the text is gone, not wrapped. Widening two lines is linear
    in the width; padding every row is what made this quadratic before, since a
    table with one wide row and many narrow ones emitted rows x width cells for
    the handful it actually carried, and a provider controls both numbers.
    """
    rows: list[list[str]] = []
    for row in projection._as_list(node.get("content")):
        if str(row.get("type") or "") != "tableRow":
            continue
        cells = [
            _adf_cell_text(cell, _depth=_depth + 1)
            for cell in projection._as_list(row.get("content"))
            if str(cell.get("type") or "") in ("tableHeader", "tableCell")
        ]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    header, *body = rows
    width = max(len(row) for row in rows)
    lines = [
        "| " + " | ".join(header + [""] * (width - len(header))) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _adf_cell_text(cell: dict[str, Any], *, _depth: int) -> str:
    """Flatten one table cell to a single line (a GFM cell cannot wrap).

    Only line breaks are folded: repeated spaces and tabs survive, because a code
    span's whitespace is literal and a blanket collapse would silently rewrite
    ``a  b`` as ``a b``.

    Any pipe still unescaped after rendering is escaped here. A text pipe is
    already escaped by ``_md_escape_inline``, but a code span is emitted
    literally by definition, so ``a|b`` inside one would split the cell in two.
    GFM honours ``\\|`` inside a code span for exactly this case. The one thing
    this cannot express is a literal backslash-pipe pair inside a code span in a
    table, which GFM has no spelling for.
    """
    text = _md_one_line(_adf_join_blocks(cell, _depth=_depth))
    return re.sub(r"(?<!\\)\|", r"\\|", text)
