"""The warm welcome's secret scan: the views of the posted text it reads, and
the whole-string redaction every ledger string gets before any cut.

They read only strings, so they live apart from the welcome's flow;
``warm_task_brief`` imports them under the same names.
"""

from __future__ import annotations

import html
import re
import unicodedata

from kiro_crew.dashboard import warm_task_brief_inputs as inputs


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


def redact_whole(text: str) -> str:
    """*text* with any credential that a scan view of the WHOLE string shows
    redacted, before anything cuts it: a cut first can split a key (or move an
    entity-encoded header off its body) into pieces no redactor recognises. Text
    with nothing to redact comes back unchanged."""
    for _ in range(3):  # each pass redacts the first view that still holds one
        changed = next((r for v in _scan_views(text) if (r := inputs.redact(v)) != v), None)
        if changed is None:
            break
        text = changed
    return text
