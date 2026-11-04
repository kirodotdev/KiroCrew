"""Convert Slack mrkdwn to the Markdown the notification center renders.

One ``send_message`` call reaches two surfaces with two grammars: Slack renders
mrkdwn, while the dashboard bell renders Markdown (``send_notification``
documents its body as markdown). The grammars disagree exactly where a message
carries meaning — ``<url|label>`` is a link on Slack and unreadable plain text
in the bell, and the emphasis sigils map to different weights (``*x*`` is bold
on Slack, italic in Markdown). This module is the bell's read-side bridge: the
Slack-bound text stays exactly what the caller wrote, and only the bell's copy
converts.

Deliberately conservative, because the input is not always pure mrkdwn and a
wrong conversion corrupts a body a right one merely restyles:

- Emphasis converts only at Slack's own boundaries (delimiter outside a word,
  content not edged with whitespace, no doubled delimiter), so a snake_case
  identifier and an already-Markdown ``**bold**`` pass through unchanged.
- Only the angle form that wraps a URI (``<scheme:...>``, with or without a
  ``|label``) converts; a user/channel mention or ``<!here>`` broadcast has no
  Markdown reading and stays literal. Every other URL-shaped span — an
  existing Markdown link or image, a bare URL — is an atom the emphasis pass
  never reaches, because a ``_`` or ``*`` inside a destination is path, not
  markup, and a body that was already Markdown must round-trip unharmed.
- Code spans are atoms: nothing converts inside an inline or fenced span,
  which is how both renderers read them. An unterminated fence runs to the end
  of the text, so a half-typed fence cannot flip conversion on for the code it
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
# All three are atoms to the emphasis pass — "_" or "*" inside a destination
# is path, not markup, and rewriting it corrupts the link on every renderer.
_MD_LINK = re.compile(rf"!?\[[^\[\]\n]*\]\({md_link_destination(r'[^()\s]')}*\)")
_BARE_URL = re.compile(r"\bhttps?://[^\s<>]+")
_ATOM = re.compile(rf"(?P<slack>{_LINK.pattern})|(?:{_MD_LINK.pattern})|(?:{_BARE_URL.pattern})")
# Slack's emphasis boundaries: delimiter at a word edge, content not edged with
# whitespace and free of the delimiter. The edge guards are what keep a body
# that is already Markdown, and identifiers like snake_case, unchanged.
_BOLD = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_ITALIC = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_STRIKE = re.compile(r"(?<![\w~])~(?!\s)([^~\n]+?)(?<!\s)~(?![\w~])")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_FENCE = "```"


def _markdown_link(match: re.Match[str]) -> str:
    url, label = match.group(1), match.group(2)
    if label is None:
        # A bare <url> becomes a self-labelled link, not a plain URL: Markdown
        # autolinking covers web schemes only, so a bare vscode:// or slack://
        # that Slack rendered clickable would go dead in the bell as text.
        label = url
    # "[" or "]" in a label would close the Markdown link early; "(", ")" or a
    # space in the destination would end it. Escape the label, and spell such a
    # destination with Markdown's own <...> bracketing.
    label = label.replace("[", "\\[").replace("]", "\\]")
    if any(ch in url for ch in "() "):
        return f"[{label}](<{url}>)"
    return f"[{label}]({url})"


def _convert_emphasis(text: str) -> str:
    out = _BOLD.sub(r"**\1**", text)
    out = _ITALIC.sub(r"*\1*", out)
    return _STRIKE.sub(r"~~\1~~", out)


def _convert_non_code(text: str) -> str:
    """Convert one segment that holds no code span.

    URL-shaped spans are atoms: a Slack angle link converts, while an existing
    Markdown link/image and a bare URL pass through verbatim. Emphasis runs
    only on the text BETWEEN atoms, so a delimiter character inside any
    destination (``https://x/_private_``, ``[r](https://x/_r_/1)``) is path,
    never markup.
    """
    pieces: list[str] = []
    cursor = 0
    for atom in _ATOM.finditer(text):
        pieces.append(_convert_emphasis(text[cursor : atom.start()]))
        if atom.group("slack") is not None:
            slack = _LINK.match(atom.group(0))
            assert slack is not None  # the alternative that just matched
            pieces.append(_markdown_link(slack))
        else:
            pieces.append(atom.group(0))
        cursor = atom.end()
    pieces.append(_convert_emphasis(text[cursor:]))
    return "".join(pieces)


def _convert_outside_inline_code(text: str) -> str:
    pieces: list[str] = []
    cursor = 0
    for code in _INLINE_CODE.finditer(text):
        pieces.append(_convert_non_code(text[cursor : code.start()]))
        pieces.append(code.group(0))
        cursor = code.end()
    pieces.append(_convert_non_code(text[cursor:]))
    return "".join(pieces)


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
            pieces.append(_convert_outside_inline_code(text[cursor:start]))
            pieces.append(text[start:])
            return "".join(pieces)
        pieces.append(_convert_outside_inline_code(text[cursor:start]))
        pieces.append(text[start : end + 3])
        cursor = end + 3
    pieces.append(_convert_outside_inline_code(text[cursor:]))
    return "".join(pieces)
