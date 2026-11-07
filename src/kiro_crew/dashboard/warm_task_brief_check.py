"""The section 3.9.6 output check's scanners for a warm task brief: the views
of the posted text every scanner reads, and the pull-request, commit SHA and
HH:MM checks over them, plus the unverified tag an unbacked SHA or time gets.

They read only strings, so they live apart from the brief's flow and its
dataclasses; ``warm_task_brief`` imports them under the same names.
"""

from __future__ import annotations

import html
import re
import unicodedata

from kiro_crew.dashboard import warm_task_brief_inputs as inputs

# The section 3.9.6 output check: what the model may only repeat, never make up.
# A commit SHA: 7 to 40 hex digits, not a `#` colour, with a digit and a letter.
_SHA_RE = re.compile(r"(?<![#\w])([0-9a-f]{7,40})(?!\w)", re.IGNORECASE)
_TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")


def _times_in(text: str) -> set[str]:
    return {f"{int(h):02d}:{m}" for h, m in _TIME_RE.findall(text)}


def _unescaped(text: str) -> str:
    """*text* with HTML entities decoded until it stops changing, so a
    double-encoded value reads as the page shows it."""
    for _ in range(8):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


# Emphasis, code and strike marks: drawn as nothing between two halves of a
# fact, so a reference or a time split by bold still reads as one.
_EMPHASIS_RE = re.compile(r"[*_`~]")

# Where a CSS-style declaration can sit: a mermaid fence, a ``<style>`` block,
# or a ``style=`` attribute value.
_STYLE_SPAN_RE = re.compile(
    r"^ {0,3}(`{3,}|~{3,})[ \t]*mermaid\b[^\n]*\n[\s\S]*?(?:^ {0,3}\1[ \t]*$|\Z)"
    r"|<style\b[^>]*>[\s\S]*?(?:</style\s*>|\Z)"
    r"|\bstyle\s*=\s*(?:\"[^\"]*\"|'[^']*')",
    re.IGNORECASE | re.MULTILINE,
)
# Inside a mermaid fence, declarations sit only on these statement lines; a
# node label such as ``A[PR: 42]`` is text, not a declaration.
_MERMAID_STYLE_LINE_RE = re.compile(
    r"^[ \t]*(?:style|classDef|linkStyle)\b[^\n]*", re.IGNORECASE | re.MULTILINE
)
# A CSS-style declaration: ``<name>:`` at a declaration boundary (start, space,
# ``;``, ``,``, ``{`` or a quote), then its value up to the next separator.
_DECLARATION_RE = re.compile(r"(?:^|(?<=[\s;,{\"']))([A-Za-z-]+\s*:\s*)([^;,{}\"'\n]*)")
# A colour value inside a declaration: ``#`` and 3, 4, 6 or 8 hex characters.
_COLOUR_VALUE_RE = re.compile(
    r"#(?:[0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{4}|[0-9a-f]{3})\b", re.IGNORECASE
)


def _without_declared_colours(span: str) -> str:
    return _DECLARATION_RE.sub(lambda d: d.group(1) + _COLOUR_VALUE_RE.sub("", d.group(2)), span)


def _without_colour_values(text: str) -> str:
    """The scan copy of *text* with every colour declaration's value removed.
    A ``#`` + hex value is a colour only inside the value of a CSS-style
    declaration in a mermaid ``style``/``classDef``/``linkStyle`` line, a
    ``<style>`` block or a ``style=`` attribute; everywhere else a ``#N``
    stays a pull-request reference."""

    def strip(match: re.Match[str]) -> str:
        span = match.group(0)
        if match.group(1):
            return _MERMAID_STYLE_LINE_RE.sub(
                lambda line: _without_declared_colours(line.group(0)), span
            )
        return _without_declared_colours(span)

    return _STYLE_SPAN_RE.sub(strip, text)


def _canonical(text: str) -> str:
    """One canonical form of model output for the secret scan: entities
    decoded until stable, then NFKC. Nothing is stripped, so attribute values,
    text and comments all stay in it."""
    # The cap's cut marker stays one character: NFKC would make it three dots,
    # and a number the cap cut (``#261…``) would then read as a whole reference.
    return "…".join(unicodedata.normalize("NFKC", part) for part in _unescaped(text).split("…"))


# Markup that can sit between two halves of a key without showing: an HTML
# tag or comment, a markdown link's brackets and target (its label stays),
# and emphasis and code marks. Not ``_``, which credential prefixes (``ghp_``)
# contain.
_JOINER_RE = re.compile(r"<!--[\s\S]*?-->|<[^<>]*>|!?\[|\]\([^)\n]*\)|[*`~]")


def _scan_views(text: str) -> tuple[str, ...]:
    """The one set of views every section 3.9.6 scanner reads, all of the
    final posted *text*: its canonical form (entities decoded until stable,
    then NFKC) as written, with markup removed so the halves around it join,
    and that joined form with emphasis marks also removed. Colour
    declaration values are removed in every view. The stored markdown is not
    changed."""
    written = _without_colour_values(_canonical(text))
    # Until stable, so markup nested in markup (``<im<img>g>``) joins too.
    joined = written
    while (once := _JOINER_RE.sub("", joined)) != joined:
        joined = once
    return written, joined, _EMPHASIS_RE.sub("", joined)


def _names_an_unknown_pr(posted: str, known: set[int]) -> bool:
    """Whether *posted*, read as the page shows it (entities decoded), names a
    pull request outside *known*: one read live, or named by the input or the
    owner's request."""
    named: set[int] = set()
    for view in _scan_views(posted):
        named |= inputs.pr_numbers(view)
    return bool(named - known)


def _unbacked_facts(posted: str, grounding: str, clocks: set[str]) -> list[str]:
    """The commit SHAs and HH:MM times in *posted* that the input never gave
    (section 3.9.6); unknown `#N` references are checked by
    :func:`_names_an_unknown_pr`. *grounding* is the snapshot the prompt
    carried; *clocks* the "as of" times the prompt stated."""
    views = _scan_views(posted)
    lowered = grounding.lower()
    times = _times_in(grounding) | clocks
    # Each SHA is named once, however many views or fields carry it.
    shas = dict.fromkeys(sha for view in views for sha in _SHA_RE.findall(view))
    bad = [
        sha
        for sha in shas
        if any(c.isdigit() for c in sha)
        and any(c.isalpha() for c in sha)
        and sha.lower() not in lowered
    ]
    found = set().union(*(_times_in(view) for view in views))
    bad += [t for t in sorted(found) if t not in times]
    return bad


def _tagged_re(tag: str) -> re.Pattern[str]:
    """A SHA or time with *tag* right after it, as :func:`_mark` writes it."""
    return re.compile(rf"(?:{_SHA_RE.pattern}|{_TIME_RE.pattern}) {re.escape(tag)}", re.IGNORECASE)


# Where a tag may go: a value with only whitespace (or the field's start)
# before it and whitespace, the end or plain sentence punctuation after it.
# Anywhere else (``17:05:30``, ``**abc1234**def``, a link target, inline
# code) the value stays untagged, so the recheck counts it as a miss.
_BEFORE = r"(?<!\S)"
_AFTER = r"(?=\s|$|[,;!?)]|\.(?!\w))"
_SHA_TAG_RE = re.compile(_BEFORE + _SHA_RE.pattern + _AFTER, re.IGNORECASE)
_TIME_TAG_RE = re.compile(_BEFORE + _TIME_RE.pattern + _AFTER)


def _mark(text: str, bad: set[str], tag: str) -> str:
    """*text* with *tag* after each SHA or HH:MM time in *bad* (lower case)
    that sits at a clean boundary."""
    text = _SHA_TAG_RE.sub(lambda m: f"{m[0]} {tag}" if m[1].lower() in bad else m[0], text)
    return _TIME_TAG_RE.sub(
        lambda m: f"{m[0]} {tag}" if f"{int(m[1]):02d}:{m[2]}" in bad else m[0], text
    )


# A fence or a widget anywhere in the body: its source a tag would break.
_PICTURE_RE = re.compile(r"```|~~~|<mcwidget\b", re.IGNORECASE)


def _mark_body(body: str, bad: set[str], tag: str) -> str:
    """:func:`_mark` on a body with no picture in it. A body that holds one
    stays untagged, so an unbacked value in it is a miss."""
    return body if _PICTURE_RE.search(body) else _mark(body, bad, tag)
