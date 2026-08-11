"""Redaction against what a chat platform will DISPLAY, not the bytes it is sent.

Every channel scans outbound text for credentials, but a scan of the literal
bytes is not enough on any platform that renders markup away: ``AKIA**REST**``
and ``[AKIA](https://x)REST`` match no credential pattern as written, yet the
reader sees an intact key once the delimiters are stripped at render time. The
transformation happens AFTER the scan, so the scan has to anticipate it.

This lives in ``messaging`` rather than in one channel package because the
hazard is not Slack-specific: Telegram (MarkdownV2) and Discord (Markdown)
collapse the same emphasis, code-span and link syntax. It was written for
Slack first and hoisted here when :func:`kiro_crew.messaging.renderer.
format_overflow` began putting LLM-authored choice text into the message BODY
on every widget-capable channel -- the shared sink cannot depend on each
renderer remembering to canonicalise, which is the same reasoning that put the
``max_buttons`` cap in shared code.

Stdlib-only leaf: it takes the redactor as a parameter rather than importing
``kiro_crew.security``, so it stays importable from anywhere and each caller
keeps its own (possibly session-scoped) redactor.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable

_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")

# Delimiter runs the platforms consume at render time. ``||`` is Discord's
# spoiler: the reader clicks it and the delimiters vanish, joining the halves --
# the same splitter property as ``**``, which is why it belongs in this run
# rather than in a pass of its own.
#
# The pipe counts only in PAIRS. A lone ``|`` is literal text on every channel
# here (Telegram's body goes out as HTML, where ``||`` is not spoiler markup
# either, and Slack renders it as-is), so collapsing single pipes would only
# widen the canonical form for no display that matches it. Slack link internals
# (``<url|label>``) are already consumed by ``_SLACK_LINK`` before this runs.
_EMPHASIS_RUN = re.compile(r"(?:[*_~`]|\|\|)+")
# ``[label](url)`` (Markdown) and ``<url|label>`` (Slack mrkdwn). Both DISPLAY only
# the label, so the url is invisible to a reader and the label joins whatever
# surrounds it -- which makes them a splitter, exactly like ``**``.
#
# The opening delimiter is excluded from every inner class (no ``[`` inside the
# Markdown label, no ``<`` inside the Slack one). That is not cosmetic: with ``[``
# allowed, input like ``[[[[[[...`` makes each start position consume the whole
# remaining string before failing to find ``]``, so the scan is quadratic in the
# length of attacker-supplied text (CodeQL ``py/polynomial-redos``). Excluding it
# makes a failed start fail immediately, which matters because this runs on every
# outbound message. A label containing a literal ``[`` is simply not collapsed --
# safe, since the fallback is to scan the text as written.
_MD_LINK = re.compile(r"\[([^\[\]\n]*)\]\(([^()\n]*)\)")
_SLACK_LINK = re.compile(r"<([^<>|\n]*)\|([^<>\n]*)>")


def strip_ansi(text: str) -> str:
    """Remove SGR colour escapes.

    Public because redaction call sites need it: this strip can *reassemble* a
    credential that escape sequences had broken up, so a caller that redacts
    around a conversion has to normalise with the SAME function first, or the
    secret slips through the regex and is put back together afterwards.
    """
    return _ANSI_SGR.sub("", text)


def _strip_format_chars(text: str) -> str:
    """Drop Unicode *format* characters (category ``Cf``) and soft hyphens.

    The delimiter families above are visible markup a platform consumes. This is
    the invisible half of the same hazard, and it is strictly worse: a
    zero-width space, joiner, bidi mark or BOM between two halves of a key is
    rendered as NOTHING, so the reader sees an intact credential with no click
    and no markup, while every literal scan sees it broken. ``Cf`` is the
    principled set -- it is exactly Unicode's "format" category (ZWSP, ZWNJ,
    ZWJ, word joiner, bidi controls, BOM, soft hyphen) and contains nothing a
    reader can see.

    The ASCII fast path is sound, not an approximation: the ASCII range holds no
    ``Cf`` code point at all (the C0 controls are ``Cc``), so ordinary traffic
    never pays for the per-character walk.
    """
    if text.isascii():
        return text
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def canonicalize_display(text: str) -> str:
    """Reduce *text* to what the platform will actually SHOW a reader.

    Three families, one property: the platform removes them at render time, so a
    credential broken across them is whole on screen while every literal scan
    sees it broken.

    * **links** collapse to their label -- ``[AKIA](https://x)REST`` displays as
      the joined key, with the url nowhere in sight;
    * **emphasis / code / spoiler delimiters** vanish -- ``AKIA**REST**`` and
      Discord's ``AKIA||REST||`` likewise;
    * **invisible format characters** were never rendered at all -- see
      :func:`_strip_format_chars`.

    Links are reduced FIRST: a url can itself contain ``_`` or ``~``, and dropping
    those before the url is removed would corrupt the label boundaries. Format
    characters are dropped LAST, so a delimiter run that a zero-width character
    had split (``*``+ZWSP+``*``) is still recognised as the run it renders as.
    """
    out = _MD_LINK.sub(r"\1", text)
    out = _SLACK_LINK.sub(r"\2", out)
    out = _EMPHASIS_RUN.sub("", out)
    return _strip_format_chars(out)


def redact_for_display(text: str, redactor: Callable[[str], str]) -> tuple[str, bool]:
    """Redact *text* against what the platform will DISPLAY, not just the bytes.

    Two normalisations, for the same underlying reason: a transformation applied
    *after* the scan can reassemble a credential the scanner saw as broken.

    1. **ANSI escapes** -- stripped outright, because they are display noise with
       no meaning to preserve.
    2. **Link markup and emphasis/code delimiters** -- these DO carry meaning, so
       they cannot simply be deleted. Instead the canonical (display) form is
       scanned as well. Neither ``AKIA**<rest>**`` nor ``[AKIA](https://x)<rest>``
       matches a credential pattern as written, yet the platform renders the
       markup away and shows the reader an intact key.

    When the canonical form reveals a secret that the literal form hid, the
    canonical text is emitted -- so the message loses that markup. That is
    deliberate and one-directional: formatting is worth less than a credential, and
    the downgrade only happens on a message that actually contains one.

    Returns:
        ``(safe_text, redacted)``. ``redacted`` is True when the redactor changed
        anything, by either route -- callers that already published the text rely
        on it to go back and replace what is visible.
    """
    stripped = strip_ansi(text or "")
    safe = redactor(stripped)
    changed = safe != stripped

    canonical = canonicalize_display(safe)
    if canonical != safe:
        canonical_safe = redactor(canonical)
        if canonical_safe != canonical:
            # The markup was hiding a credential from the scan. Emit the canonical,
            # redacted form: losing formatting beats leaking the key.
            return canonical_safe, True
    return safe, changed
