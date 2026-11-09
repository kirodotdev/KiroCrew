"""Convert Slack mrkdwn to the Markdown the notification center renders.

One ``send_message`` call reaches two surfaces with two grammars: Slack renders
mrkdwn, while the dashboard bell renders Markdown (``send_notification``
documents its body as markdown). The grammars disagree exactly where a message
carries meaning — ``<url|label>`` is a link on Slack and unreadable plain text
in the bell, and the emphasis sigils map to different weights (``*x*`` is bold
on Slack, italic in Markdown). This module is the bell's read-side bridge: the
Slack-bound text stays exactly what the caller wrote, and only the bell's copy
converts.

The scan is one left-to-right pass per fence-free region, over TOKENS — inline
code spans (any backtick-run length) and URL-shaped spans — competing by start
position, earliest wins. That single rule decides both nestings the same way
Slack reads them: a backtick inside a link's label belongs to the link (the
``<`` opened first), and a link inside a code span belongs to the code (the
backticks opened first). Emphasis then converts over the WHOLE region with
each token reduced to an inert sentinel, so a pair of delimiters enclosing a
link or a code span still pairs: ``*Review <url|R-1> now*`` comes out bold,
with the token contents untouched by the emphasis pass.

Deliberately conservative, because the input is not always pure mrkdwn and a
wrong conversion corrupts a body a right one merely restyles:

- Emphasis converts only at Slack's own boundaries (delimiter outside a word,
  content not edged with whitespace, no doubled delimiter), so a snake_case
  identifier and an already-Markdown ``**bold**`` pass through unchanged.
- Only the angle form that wraps a URI (``<scheme:...>``, with or without a
  ``|label``) converts; a user/channel mention or ``<!here>`` broadcast has no
  Markdown reading and stays literal. Every other URL-shaped span — an
  existing Markdown link or image, a bare URL — is a token the emphasis pass
  never reaches, because a ``_`` or ``*`` inside a destination is path, not
  markup, and a body that was already Markdown must round-trip unharmed.
- Code spans are atoms: nothing converts inside an inline span (single or
  multi-backtick) or a fenced block. An unterminated fence runs to the end of
  the text, so a half-typed fence cannot flip conversion on for the code it
  guards.
- HTML entities (``&amp;``, ``&lt;``, ``&gt;``) pass through outside code
  spans: a Markdown renderer decodes them, which is also their mrkdwn reading.
  Inside a code span Markdown shows them literally — the one divergence this
  bridge accepts rather than rewriting code bytes.
"""

from __future__ import annotations

import re

from kiro_crew.constants import md_link_destination

# Slack wraps a URI in angle brackets, optionally with a reader-facing label
# after "|". The scheme class excludes the sigils of Slack's non-URI angle
# forms (user "@", channel "#", broadcast "!"), so a mention never converts.
_LINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]*:[^<>|\n]*)(?:\|([^<>\n]+))?>")
# The other URL-shaped spans a body can carry: an existing Markdown link or
# image (whose destination is NOT this module's to rewrite), and a bare URL.
_MD_LINK = re.compile(rf"!?\[[^\[\]\n]*\]\({md_link_destination(r'[^()\s]')}*\)")
_BARE_URL = re.compile(r"\bhttps?://[^\s<>]+")
# An inline code span: a backtick run of any length, closed by an equal run
# (so ``code with `nested` ticks`` is one span, as both renderers read it).
_INLINE_CODE = re.compile(r"(?<!`)(?P<ticks>`+)(?!`).+?(?<!`)(?P=ticks)(?!`)")
# One pass, earliest-start-wins: a code span and a link compete positionally,
# which resolves a backtick inside a label (link opened first) and a link
# inside a code span (backticks opened first) without ordering two passes.
_TOKEN = re.compile(
    rf"(?P<code>{_INLINE_CODE.pattern})"
    rf"|(?P<slack>{_LINK.pattern})"
    rf"|(?:{_MD_LINK.pattern})"
    rf"|(?:{_BARE_URL.pattern})"
)
# Slack's emphasis boundaries: delimiter at a word edge, content not edged with
# whitespace and free of the delimiter. The edge guards are what keep a body
# that is already Markdown, and identifiers like snake_case, unchanged.
_BOLD = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_ITALIC = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_STRIKE = re.compile(r"(?<![\w~])~(?!\s)([^~\n]+?)(?<!\s)~(?![\w~])")
_FENCE = "```"
# Token sentinels for the emphasis pass: control characters carry no emphasis
# sigil, no word character and no whitespace, so a sentinel neither opens nor
# closes emphasis and never trips a boundary guard. The same characters are
# STRIPPED from the input first (below), so a sentinel can only be one of ours.
_SENTINEL = re.compile("\x00(\\d+)\x01")
_SENTINEL_CHARS = re.compile("[\x00\x01]")


def _markdown_link(match: re.Match[str]) -> str:
    url, label = match.group(1), match.group(2)
    if label is None:
        # A bare <url> becomes a self-labelled link, not a plain URL: Markdown
        # autolinking covers web schemes only, so a bare vscode:// or slack://
        # that Slack rendered clickable would go dead in the bell as text.
        label = url
    # Slack renders a label as literal text, so every Markdown reading inside
    # one must be escaped: "[" / "]" would close the link early, and _ * ~ `
    # would restyle the label while the destination keeps the raw characters
    # (a self-labelled <https://x/_r_/1> showing "r" italic is the worst case,
    # because the visible text then disagrees with the URL it points at).
    # Backslashes first: a label's own trailing "\" would otherwise fuse with
    # a later escape (or the link's closing "]") into one escape and un-link
    # it. The DESTINATION is never escaped — only bracketed when "(", ")" or
    # a space would end it, with Markdown's own <...> spelling.
    label = label.replace("\\", "\\\\")
    for ch in "[]_*~`":
        label = label.replace(ch, "\\" + ch)
    if any(ch in url for ch in "() "):
        return f"[{label}](<{url}>)"
    return f"[{label}]({url})"


def _convert_emphasis(text: str) -> str:
    out = _BOLD.sub(r"**\1**", text)
    out = _ITALIC.sub(r"*\1*", out)
    return _STRIKE.sub(r"~~\1~~", out)


def _convert_segment(text: str) -> str:
    """Convert one fence-free region.

    Tokens (code spans and URL-shaped spans) are lifted out behind sentinels,
    emphasis converts over the remaining text as ONE string — so a delimiter
    pair enclosing a token still pairs — and each token then lands back:
    a Slack angle link converted, everything else byte-for-byte.
    """
    # Control characters the sentinels are built from never carry meaning in a
    # message body; stripping them first is what makes a sentinel unforgeable.
    text = _SENTINEL_CHARS.sub("", text)
    tokens: list[str] = []
    carrier: list[str] = []
    cursor = 0
    for token in _TOKEN.finditer(text):
        carrier.append(text[cursor : token.start()])
        if token.group("slack") is not None:
            slack = _LINK.match(token.group(0))
            assert slack is not None  # the alternative that just matched
            tokens.append(_markdown_link(slack))
        else:
            tokens.append(token.group(0))
        carrier.append(f"\x00{len(tokens) - 1}\x01")
        cursor = token.end()
    carrier.append(text[cursor:])
    emphasized = _convert_emphasis("".join(carrier))

    out = ""
    cursor = 0
    for sentinel in _SENTINEL.finditer(emphasized):
        out += emphasized[cursor : sentinel.start()]
        landed = tokens[int(sentinel.group(1))]
        if landed.startswith("[") and out.endswith("!"):
            # On Slack the "!" is plain text before the link; on the Markdown
            # side "![" would read the converted link as an IMAGE. Escape the
            # bang, never the link. (A verbatim token keeps its own bytes, and
            # an existing Markdown image is already a token of its own.)
            # Parity first: a bang behind an ODD backslash run is already
            # Markdown-escaped, and escaping it again ("\\!") frees the "!"
            # to pair with the link as an image after all.
            stem = out[:-1]
            if (len(stem) - len(stem.rstrip("\\"))) % 2 == 0:
                out = stem + "\\!"
        out += landed
        cursor = sentinel.end()
    return out + emphasized[cursor:]


def mrkdwn_to_markdown(text: str) -> str:
    """The Markdown reading of Slack-mrkdwn *text*, code spans kept verbatim."""
    pieces: list[str] = []
    cursor = 0
    while True:
        start = text.find(_FENCE, cursor)
        if start < 0:
            break
        end = text.find(_FENCE, start + 3)
        if end < 0:
            # Unterminated fence: everything after it reads as code on both
            # renderers, so it is kept verbatim rather than converted.
            pieces.append(_convert_segment(text[cursor:start]))
            pieces.append(text[start:])
            return "".join(pieces)
        pieces.append(_convert_segment(text[cursor:start]))
        pieces.append(text[start : end + 3])
        cursor = end + 3
    pieces.append(_convert_segment(text[cursor:]))
    return "".join(pieces)
