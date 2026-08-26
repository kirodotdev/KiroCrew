"""Extract ``<mcwidget>`` spans from assistant text (Python port of the frontend parser).

This is the backend half of a two-language pair. The frontend
(``website/src/hooks/useBlockAssembler.ts`` -> ``parseBlocks``) splits a chat
message into blocks and assigns each widget block a 0-based ``widgetIndex``;
``WidgetFrame`` then derives its artifact slug from ``(message_ts, widgetIndex)``.
The backend auto-registers widget artifacts at message-finalize time and must
compute the SAME indices, or every registered artifact lands on a slug the
frontend never looks up.

So this module deliberately mirrors ``parseBlocks``'s widget detection rather
than using a simpler regex:

- Tags are matched per line, **after** inline-code spans are masked, so a
  ``<mcwidget>`` quoted in backticks while documenting the syntax is not
  extracted as a real widget.
- A fenced code block outside a widget swallows widget tags (they are literal
  code, not markup).
- A fence *inside* a widget body keeps the body opaque: a ``</mcwidget>`` inside
  that fence does not close the widget until the fence closes.
- An unterminated widget at end of input **is** emitted. This mirrors
  ``parseBlocks``'s ``flushWidget(!streaming)``: on final text the frontend marks
  such a widget ``complete: true`` and renders a real ``WidgetFrame`` for it (only
  a *streaming* partial gets the placeholder). Dropping it here would leave a
  rendered widget with no artifact behind it — the parity test caught exactly
  this.

Only ``streaming=False`` semantics are implemented: the backend only ever parses
final, complete message text (a streaming partial has no stable identity to
register yet).

``test/test_widget_parse.py`` and ``website/src/test/widgetSlug.test.ts`` share
the same fixture strings so drift between the two parsers fails both suites.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

# ── Character classes: ASCII-only, ON PURPOSE ────────────────────────────────
#
# The frontend regexes these mirror use JS `\w` and `\s`, which are ASCII-only
# (`\w` == [A-Za-z0-9_]). Python's `\w`/`\s` are UNICODE-aware, so writing the
# visually identical pattern here makes the two parsers disagree on non-ASCII
# input — and because a widget's artifact slug is keyed by its INDEX, a
# disagreement doesn't just misparse, it binds the wrong artifact to the widget.
#
# Concrete case this prevents: an agent answering in Japanese writes a fenced
# example with a non-ASCII info string (```例) and then emits the real widget.
# With Unicode `\w`, Python sees a fence (so the example is inert code) and
# returns the REAL widget at index 0, while JS sees plain markdown (so the
# example IS a widget) and returns the EXAMPLE at index 0. Same slug, different
# content: the frontend then links/pins an artifact holding HTML from a widget
# the user never touched.
#
# `\s` diverges in BOTH directions (Python matches U+001C-U+001F and U+0085,
# which JS does not; JS matches U+FEFF, which Python does not), so it is spelled
# out as an explicit class mirroring the ECMAScript definition. A fence line
# ending in one of those characters would otherwise close on one side only.
#
# Keep every class here ASCII-literal / explicit; do NOT "simplify" back to
# `\w`/`\s` — they look right and are wrong.
_ASCII_WORD = "[A-Za-z0-9_]"
#: ECMAScript `\s`, verbatim (see the note above on why Python's differs).
_JS_SPACE = (
    "[\\f\\n\\r\\t\\v\\u0020\\u00a0\\u1680\\u2000-\\u200a"
    "\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]"
)

#: The exact character set JS ``String.prototype.trim`` removes. Differs from
#: Python's ``str.strip()`` in both directions — most visibly U+FEFF, which JS
#: trims and Python does not. Used for the widget body so the artifact persisted
#: by the backend is byte-identical to what the frontend renders.
_JS_TRIM_CHARS = (
    "\f\n\r\t\v\u0020"
    "\u00a0\u1680\u2000\u2001\u2002\u2003"
    "\u2004\u2005\u2006\u2007\u2008\u2009"
    "\u200a\u2028\u2029\u202f\u205f\u3000"
    "\ufeff"
)


def _js_trim(s: str) -> str:
    """Strip leading/trailing whitespace exactly as JS ``.trim()`` does."""
    return s.strip(_JS_TRIM_CHARS)


#: Open tag with its full attribute run captured, so attribute order is free.
_WIDGET_OPEN_RE = re.compile(rf'<mcwidget((?:{_JS_SPACE}+{_ASCII_WORD}+="[^"]*")*){_JS_SPACE}*>')
_WIDGET_ATTR_RE = re.compile(rf'({_ASCII_WORD}+)="([^"]*)"')
_WIDGET_CLOSE_RE = re.compile(r"</mcwidget>")
_FENCE_OPEN_RE = re.compile(rf"^(`{{3,}})({_ASCII_WORD}*){_JS_SPACE}*$")

#: Fence languages where a nested fence example is expected. Mirrors
#: ``NESTABLE_LANGS`` in ``useBlockAssembler.ts``.
_NESTABLE_LANGS = frozenset(
    {
        "",
        "markdown",
        "md",
        "mdx",
        "rst",
        "txt",
        "text",
        "html",
        "xml",
        "svg",
        "asciidoc",
        "adoc",
    }
)


@dataclass(frozen=True)
class ParsedWidget:
    """One complete ``<mcwidget>`` span found in a message.

    ``index`` is the 0-based ordinal among the message's widget blocks — the
    value the frontend passes as ``widgetIndex``. ``slug`` is the explicit
    ``slug=`` attribute when the agent re-emitted a known artifact, else ``""``
    (the caller derives one from ``message_ts`` + ``index``).

    ``truncated`` marks a widget whose ``</mcwidget>`` never arrived. The
    frontend still renders it on final text, so it is registered like any other,
    but the flag lets callers treat a half-written body differently if needed.
    """

    index: int
    content: str
    title: str
    slug: str
    truncated: bool = False


def mask_inline_code(line: str) -> str:
    """Blank out balanced backtick runs (and their delimiters) in one line.

    Port of ``maskInlineCode`` in ``useBlockAssembler.ts`` with
    ``streamingTrailingLine=False``: an *unbalanced* run on a final line is
    literal text per CommonMark and is left alone. Length is preserved so match
    offsets from the masked line index correctly into the original.
    """
    out = list(line)
    i = 0
    n = len(line)
    while i < n:
        if line[i] != "`":
            i += 1
            continue
        run_start = i
        while i < n and line[i] == "`":
            i += 1
        run_len = i - run_start
        # Look ahead for a closing run of exactly the same length.
        j = i
        match_start = -1
        while j < n:
            if line[j] != "`":
                j += 1
                continue
            r2_start = j
            while j < n and line[j] == "`":
                j += 1
            if j - r2_start == run_len:
                match_start = r2_start
                break
        if match_start >= 0:
            for k in range(run_start, match_start + run_len):
                out[k] = " "
            i = match_start + run_len
        # else: unbalanced on a final line — leave as-is.
    return "".join(out)


def _parse_attrs(attr_str: str) -> tuple[str, str]:
    """Return ``(title, slug)`` from a captured attribute run."""
    title = ""
    slug = ""
    for m in _WIDGET_ATTR_RE.finditer(attr_str or ""):
        if m.group(1) == "title":
            title = m.group(2)
        elif m.group(1) == "slug":
            slug = m.group(2)
    return title, slug


def parse_widgets(raw: str) -> List[ParsedWidget]:
    """Return every widget span in ``raw``, in document order.

    Indices match the ``widgetIndex`` the frontend assigns to the same text.
    Includes a trailing unterminated widget (flagged ``truncated``) because
    ``parseBlocks`` renders one on final text — see the module docstring.
    """
    if not raw or "<mcwidget" not in raw:
        return []

    lines = raw.split("\n")
    widgets: List[ParsedWidget] = []
    state = "outside"
    widget_buf: List[str] = []
    widget_title = ""
    widget_slug = ""
    widget_fence_tick = ""
    fence_tick = ""
    fence_len = 0
    fence_nestable = False
    inner_fence_depth = 0

    def flush_widget(truncated: bool = False) -> None:
        nonlocal widget_buf, widget_title, widget_slug, widget_fence_tick
        widgets.append(
            ParsedWidget(
                index=len(widgets),
                # _js_trim, not .strip(): the frontend's flushWidget uses JS
                # .trim(), whose whitespace set differs (U+FEFF et al). A
                # mismatch would persist a body that differs from what renders.
                content=_js_trim("\n".join(widget_buf)),
                title=widget_title or "Widget",
                slug=widget_slug,
                truncated=truncated,
            )
        )
        widget_buf = []
        widget_title = ""
        widget_slug = ""
        widget_fence_tick = ""

    for line in lines:
        if state == "outside":
            masked = mask_inline_code(line)
            w_open = _WIDGET_OPEN_RE.search(masked)
            if w_open:
                widget_title, widget_slug = _parse_attrs(w_open.group(1) or "")
                after_tag = line[w_open.end() :]
                # Close tag on the same line -> single-line widget.
                w_close = _WIDGET_CLOSE_RE.search(mask_inline_code(after_tag))
                if w_close:
                    widget_buf.append(after_tag[: w_close.start()])
                    flush_widget()
                else:
                    if after_tag:
                        widget_buf.append(after_tag)
                    state = "widget"
                continue
            fence = _FENCE_OPEN_RE.match(line)
            if fence:
                fence_tick = fence.group(1)
                fence_len = len(fence.group(1))
                fence_nestable = (fence.group(2) or "").lower() in _NESTABLE_LANGS
                inner_fence_depth = 0
                state = "fence"
            continue

        if state == "fence":
            if re.match(rf"^{re.escape(fence_tick)}{_JS_SPACE}*$", line):
                if inner_fence_depth > 0:
                    inner_fence_depth -= 1
                else:
                    state = "outside"
            elif fence_nestable:
                inner = _FENCE_OPEN_RE.match(line)
                if inner and len(inner.group(1)) == fence_len and inner.group(2):
                    inner_fence_depth += 1
            continue

        if state == "widget":
            masked = mask_inline_code(line)
            w_close = _WIDGET_CLOSE_RE.search(masked)
            if w_close:
                before = line[: w_close.start()]
                if before:
                    widget_buf.append(before)
                flush_widget()
                state = "outside"
                continue
            fence = _FENCE_OPEN_RE.match(line)
            if fence:
                widget_buf.append(line)
                widget_fence_tick = fence.group(1)
                state = "widget-fence"
                continue
            widget_buf.append(line)
            continue

        if state == "widget-fence":
            widget_buf.append(line)
            if re.match(rf"^{re.escape(widget_fence_tick)}{_JS_SPACE}*$", line):
                state = "widget"
            continue

    # End of input: an open widget still flushes, matching parseBlocks's
    # flushWidget(!streaming) on final text (see module docstring). An open plain
    # fence flushes as a code block there, which is never a widget — nothing to do.
    if state in ("widget", "widget-fence"):
        flush_widget(truncated=True)
    return widgets
