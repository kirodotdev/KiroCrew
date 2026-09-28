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
from typing import Callable, Iterator, Sequence

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

#: How many consecutive messages one INTERIOR reading may span in
#: :func:`severs_a_credential`. A credential framed by a spoiling message on each
#: side appears in no prefix, suffix or whole reading, so those runs are read too --
#: but capped, because reading every contiguous run is quadratic in a piece count
#: the author controls. Four covers a credential the split broke into three
#: fragments, which is what markup spanning a length cut produces; the cap is what
#: keeps the reading count linear in the number of messages.
_INTERIOR_RUN_PIECES = 4


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


def severs_a_credential(
    pieces: Sequence[str],
    redactor: Callable[[str], str],
    present: Callable[[str], str] | None = None,
) -> bool:
    """Would a reader shown *pieces* in order see a key that none of them holds?

    The n-piece case of :func:`joins_to_a_credential`. A renderer whose cap forces
    one buffer into several messages redacts each message ALONE, so a credential the
    split severed matches nothing in any single message -- and the reader's client
    renders the markup away and reads the pieces one under the other, in order, as
    one text.

    Two readings, because neither subsumes the other:

    * **the whole sequence**, every piece redacted alone and then put together. This
      is what catches a key whose MARKUP spans an entire piece rather than whose
      characters do: cut ``AKIA[KEYTAIL](http://host/<long>)`` into three and no
      neighbouring pair canonicalises to anything (the link needs its closing
      bracket, which is in the third piece), while the full join collapses the url
      to the label and puts the label against ``AKIA``. Piece length is no defence
      here -- canonicalising DROPS a link's target, so a piece of any size can
      vanish entirely.
    * **each boundary read BOTH ways round** -- everything after it, and everything
      before it. This is the reading that survives a pattern anchored at its edges:
      the reader sees a message break where the whole-sequence join sees the next
      character, so a key completed at the end of one piece must be caught even when
      later text would spoil the match. Both ways, because the patterns anchor on
      BOTH sides: reading only forwards misses a key finished at a boundary whose
      NEXT piece opens on a character the trailing class rejects, which every
      forward reading carries.

    Every piece is put through the same redaction the sender will apply and then
    reduced to what the platform SHOWS, exactly as :func:`joins_to_a_credential`
    does for a single cut. Soundness rests on the same property:
    ``redact_for_display`` is already a fixed point for one string, so whatever
    either reading finds is produced by putting pieces together and by nothing else.
    One piece therefore answers False: there is no boundary.

    *present* maps a piece to the form the platform will actually DELIVER, and when
    it is given BOTH forms are graded -- a sever in either answers True. Both,
    because neither form dominates the other and a caller that picked one would miss
    the half the other sees:

    * trimming REVEALS a join. ``AKIAIOSF\\n\\n`` and ``   ODNN7EXAMPLE`` are kept
      apart by whitespace no credential pattern tolerates, and sit flush on screen
      once the sink strips it.
    * trimming CONCEALS one. ``-----BEGIN `` and ``RSA PRIVATE KEY-----`` need that
      trailing space to match, so the trimmed pair reads clean while the delivered
      pair -- on a sink that does not trim, or trims less than modelled -- is an
      intact PEM anchor.

    So "model more trimming and the grade only gets stricter" is FALSE, and grading
    the delivered form alone is not the safe direction. Grading both is.

    True means the split is unusable and the caller must cut somewhere else (see
    :func:`safe_split_offset`) or deliver nothing at all.
    """
    forms = [list(pieces)]
    if present is not None:
        forms.append([present(piece) for piece in pieces])
    return any(_sequence_severs(form, redactor) for form in forms)


def _sequence_severs(pieces: Sequence[str], redactor: Callable[[str], str]) -> bool:
    """One reading pass over *pieces* as given. See :func:`severs_a_credential`."""
    safe = [redact_for_display(piece, redactor)[0] for piece in pieces]
    return any(redactor(reading) != reading for reading in _sequence_readings(safe))


def _sequence_readings(safe: Sequence[str]) -> Iterator[str]:
    """Every reading of *safe* a reader can assemble. Lazy, so a sever exits early."""
    alone = [canonicalize_display(piece) for piece in safe]
    yield canonicalize_display("".join(safe))
    yield "".join(alone)
    # Each boundary read BOTH ways round, because the patterns anchor at BOTH ends
    # and a reading that runs past either one is spoiled there:
    #
    # * everything AFTER the boundary -- a key whose first character opens a
    #   message, where the message before it ends in a character the leading
    #   ``(?<![A-Za-z0-9_.-])`` class rejects.
    # * everything BEFORE the boundary -- a key COMPLETED at the end of a message,
    #   where the next message opens with a character the trailing
    #   ``(?![A-Za-z0-9_-])`` class rejects. EVERY reading that runs to the end of
    #   the sequence carries that character and reports clean, so without this half
    #   a key finished across messages 1 and 2, with message 3 starting on a
    #   letter, is one key on screen and invisible here.
    #
    # Both halves in both forms -- joined, and each message canonicalised alone --
    # for the reason the whole-sequence pair is read twice.
    #
    # One reading per boundary, each over a prefix or a suffix. That is a linear
    # NUMBER of readings; the bytes they copy are not linear, because a prefix and
    # a suffix are rebuilt at every boundary, so the pass is quadratic in total
    # bytes on a sequence with many pieces. The bound that matters for an author-
    # controlled input is the piece count, and the interior windows below are what
    # keep the reading count linear in it.
    for index in range(len(safe) - 1):
        rest = "".join(safe[index + 1 :])
        yield canonicalize_display("".join(safe[: index + 1]))
        yield canonicalize_display(rest)
        yield "".join(alone[: index + 1])
        yield "".join(alone[index + 1 :])
        # The head against the whole remainder as ONE lump, which per-message
        # canonicalisation does not subsume: a lump collapses markup spanning two
        # messages inside it that each message alone leaves intact.
        yield alone[index] + canonicalize_display(rest)
    # INTERIOR runs, bounded. A credential can also sit with a spoiling message on
    # EACH side of it, and no reading above reaches that: every prefix, suffix and
    # whole reading carries at least one of the two frames, where the anchor classes
    # reject the match. A rotation of model text makes both frames ordinary rather
    # than contrived -- the splitter rstrips each chunk, so an alphanumeric edge is
    # the common case, not a crafted one.
    #
    # Reading EVERY contiguous run would catch a credential spread over any number
    # of pieces, and is quadratic in the piece count -- which an author controls, a
    # degenerate rotation splitting into thousands of pieces (2451 for a
    # 5000-backtick run at a 100 budget). So the run LENGTH is capped instead: a
    # window of at most ``_INTERIOR_RUN_PIECES`` messages, slid across the interior,
    # is a linear number of readings over bounded-size strings. A credential spread
    # thinner than the cap, with a spoiling frame on each side, is the residual and
    # is tracked rather than read here.
    for start in range(1, len(safe) - 1):
        for stop in range(start + 2, min(start + _INTERIOR_RUN_PIECES, len(safe) - 1) + 1):
            yield canonicalize_display("".join(safe[start:stop]))
            yield "".join(alone[start:stop])


def safe_split_offset(
    text: str,
    limit: int,
    redactor: Callable[[str], str],
    present: Callable[[str], str] | None = None,
) -> int:
    """The largest SAMPLED offset at or below *limit* that severs no credential.

    *present* maps a side to the form the platform will actually DELIVER, and an
    offset is accepted only when NEITHER the raw pair nor the presented pair severs.
    Both, for the reason :func:`severs_a_credential` sets out: trimming reveals a
    join where whitespace kept the edges apart, and conceals one where the pattern
    needs that whitespace to match, so neither form dominates. It has to be the
    caller's own, because only the caller knows what its sink strips. Default is
    identity, for a caller that delivers its text verbatim.

    Not the largest safe offset: the candidates are sampled, so a safe offset
    between two samples is passed over. Those characters are not lost, only
    deferred to the next delivery.

    Used by a renderer whose message cap forces *text* into two deliveries: cut
    here and :func:`joins_to_a_credential` is false of both forms, so the reader
    cannot rejoin a key across the boundary.

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
        if not severs_a_credential([text[:offset], text[offset:]], redactor, present):
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
