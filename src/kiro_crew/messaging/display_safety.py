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
from typing import Callable

from kiro_crew.preview_text import drop_format_chars

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

    Delegates to :func:`kiro_crew.preview_text.drop_format_chars`, the one
    implementation of the Cf drop (its docstring carries the fast-path
    soundness argument), so the display-safety canonicalizer and the preview
    stripper cannot drift apart on which characters count as invisible.
    """
    return drop_format_chars(text)


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


def joins_to_a_credential(head: str, tail: str, redactor: Callable[[str], str]) -> bool:
    """Would a reader shown *head* and then *tail* see a key neither half holds?

    A cap that cuts text into two messages is applied to the RAW string, while the
    reader sees the CANONICAL rendering of each piece. So a credential the model
    split with markup can be severed by the cut: each piece is scrubbed on its own
    and matches nothing, and the reader's client renders the markup away and
    rejoins the halves on screen.

    This answers the question directly rather than guessing which characters could
    hide such a split. Each side is put through the same redaction the sender will
    actually apply (:func:`redact_for_display`), then reduced to what the platform
    SHOWS (:func:`canonicalize_display`), and the result of putting the two sides
    together is scanned.

    Soundness, which is the whole point: ``redact_for_display`` already emits the
    canonical form whenever canonicalising reveals something the literal form hid,
    so ``redactor(canonicalize_display(redact_for_display(x)[0]))`` is a fixed
    point for any single string ``x``. Whatever this scan finds is therefore
    produced by putting the two sides together and by nothing else. No character
    class, no window and no
    anchor list: a search window built from a hand-written set of characters cannot
    be closed, because the next character the set does not know about is one more
    place a split can hide -- the walk that finds the window's edge stops there, the
    check runs on a span the credential's prefix was never inside, and it passes
    vacuously.

    BOTH readings a reader can produce are scanned, because neither one contains
    the other:

    * **canonicalise the join** models a COPY of both messages, and a client
      lenient about where one message ends. It is the wider reading for runs of
      delimiters, which concatenation can only extend: ``AKIA**`` beside
      ``**REST`` is a run only once the halves sit together.
    * **canonicalise each side, then join** models the screen -- two messages
      rendered separately, read one after the other. This is the wider reading
      wherever canonicalising DROPS text rather than just deleting delimiters,
      which is exactly what a link does to its target. A cut one character inside
      ``[l](https://x/AKIA`` + ``REST)`` completes the link only in the join,
      where the url then collapses to the label and the key vanishes from the
      scan -- while on screen each half is an unfinished link whose url stays
      visible, and the reader reads straight through it.

    So the join alone would pass a cut through a credential in a url, and the
    per-side reading alone would miss a credential split by markup at the
    boundary. Either reading finding something is enough to refuse the cut, and
    ``test_display_split_safety.py`` pins one shape per reading.
    """
    head_safe = redact_for_display(head, redactor)[0]
    tail_safe = redact_for_display(tail, redactor)[0]
    readings = (
        canonicalize_display(head_safe + tail_safe),
        canonicalize_display(head_safe) + canonicalize_display(tail_safe),
    )
    return any(redactor(reading) != reading for reading in readings)


def safe_split_offset(text: str, limit: int, redactor: Callable[[str], str]) -> int:
    """The largest SAMPLED offset at or below *limit* that severs no credential.

    Not the largest safe offset: the candidates are sampled, so a safe offset
    between two samples is passed over. Those characters are not lost, only
    deferred to the next delivery.

    Used by a renderer whose message cap forces *text* into two deliveries: cut
    here and :func:`joins_to_a_credential` is false, so the reader cannot rejoin a
    key across the boundary.

    Candidates step back EXPONENTIALLY (``limit``, then 1, 2, 4, 8 ... characters
    before it), for a cost bound: the nearest safe boundary is not needed, only a
    safe one, and stepping past it merely defers a few more characters to the next
    delivery. A linear walk would be O(*limit*) redaction passes over
    attacker-influenced text on every frame; this is O(log *limit*), and the common
    case -- prose, where any cut is safe -- costs one pass, or none at all when
    *text* already fits.

    ``0`` means every SAMPLED candidate was unsafe -- one matched region covers all
    of them. A safe offset between two samples may still exist; the search does not
    look for it, because the answer it needs is only "is there a safe cut I can take
    now". Callers treat ``0`` as "deliver nothing yet", which is always available to
    them: text withheld now is text the next delivery carries.
    """
    if limit <= 0:
        return 0
    if limit >= len(text):
        # Nothing is severed, so there is no boundary to check.
        return len(text)
    offset, step = limit, 0
    while offset > 0:
        if not joins_to_a_credential(text[:offset], text[offset:], redactor):
            return offset
        step = 1 if step == 0 else step * 2
        offset = limit - step
    return 0


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

    literal = _strip_format_chars(safe)
    if literal != safe:
        literal_safe = redactor(literal)
        if literal_safe != literal:
            safe, changed = literal_safe, True
    canonical = canonicalize_display(safe)
    if canonical != safe:
        canonical_safe = redactor(canonical)
        if canonical_safe != canonical:
            # The markup was hiding a credential from the scan. Emit the canonical,
            # redacted form: losing formatting beats leaking the key.
            return canonical_safe, True
    return safe, changed
