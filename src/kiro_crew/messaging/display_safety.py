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
from typing import Callable, Sequence

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

#: One run of whitespace -- the granularity at which a credential completion is cut
#: off the front of a pending message. A credential never straddles a space, so a
#: whole word is the smallest unit that removes a completion OUTRIGHT rather than
#: trimming it until a pattern stops matching and leaving the rest readable.
_WORD_BREAK = re.compile(r"\s+")

#: How many of the most recent delivered messages the grading window keeps WHOLE, each
#: its own piece with its own boundary. Small on purpose: ``severs_a_credential`` builds
#: a reading per boundary and each spans the whole join, so the work is quadratic in the
#: piece count. Four covers the shapes that need separate boundaries -- a head, a message
#: carrying only a link target, the closing bracket, and the text going out.
_WINDOW_RECENT = 4

#: How far back the MERGED tail reaches, in characters of what the reader actually SEES.
#: Everything older than the recent messages is folded into one canonical piece trimmed
#: to this, so no message is dropped while its characters are still within reach -- only
#: the boundaries inside the merged block are given up, and those are the ones furthest
#: from the pending text. Canonical characters rather than raw ones: canonicalising DROPS
#: a link's target, so a raw budget would trim away the short visible text actually
#: adjacent to the pending message while keeping a long invisible one.
_WINDOW_CANON_CHARS = 2048


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


def safe_split_offset(
    text: str,
    limit: int,
    redactor: Callable[[str], str],
    present: Callable[[str], str] | None = None,
) -> int:
    """The largest SAMPLED offset at or below *limit* that severs no credential.

    *present* maps a side to the form the platform will actually DELIVER, and both
    sides go through it before grading. It has to be the caller's own, because a
    renderer that strips steering markers, horizontal rules or surrounding whitespace
    on the way out delivers something shorter than the raw slice: grading the raw
    slice then accepts an offset whose delivered halves sit flush together. Worse than
    accepting it once -- the search is deterministic, so a caller that re-grades the
    answer in delivered form and rejects it gets the SAME answer every time and the
    segment never goes out at all. Default is identity, for a caller that delivers
    its text verbatim.

    Not the largest safe offset: the candidates are sampled, so a safe offset between
    two samples is passed over. Those characters are not lost, only deferred to the
    next delivery.

    Used by a renderer whose message cap forces *text* into two deliveries: cut here
    and :func:`joins_to_a_credential` is false of the delivered halves, so the reader
    cannot rejoin a key across the boundary.

    Candidates step back EXPONENTIALLY (``limit``, then 1, 2, 4, 8 ... characters
    before it), for a cost bound: the nearest safe boundary is not needed, only a safe
    one, and stepping past it merely defers a few more characters to the next
    delivery. A linear walk would be O(*limit*) redaction passes over
    attacker-influenced text on every frame; this is O(log *limit*), and the common
    case -- prose, where any cut is safe -- costs one pass, or none at all when *text*
    already fits.

    ``0`` means every SAMPLED candidate was unsafe -- one matched region covers all of
    them. A safe offset between two samples may still exist; the search does not look
    for it, because the answer it needs is only "is there a safe cut I can take now".
    Callers treat ``0`` as "deliver nothing yet", which is always available to them:
    text withheld now is text the next delivery carries.
    """
    if limit <= 0:
        return 0
    if limit >= len(text):
        # Nothing is severed, so there is no boundary to check.
        return len(text)
    shown = present or (lambda piece: piece)
    offset, step = limit, 0
    while offset > 0:
        if not joins_to_a_credential(shown(text[:offset]), shown(text[offset:]), redactor):
            return offset
        step = 1 if step == 0 else step * 2
        offset = limit - step
    return 0


def severs_a_credential(pieces: Sequence[str], redactor: Callable[[str], str]) -> bool:
    """Would a reader shown *pieces* in order see a key that none of them holds?

    A renderer whose cap forces one buffer into several messages redacts each message
    ALONE, so a credential the split severed matches nothing in any single message --
    and the reader's client renders the markup away and reads the pieces one under
    the other, in order, as one text.

    Two readings, because neither subsumes the other:

    * **the whole sequence**, every piece redacted alone and then put together. This
      is what catches a key whose MARKUP spans an entire piece rather than whose
      characters do: cut ``AKIA[KEYTAIL](http://host/<long>)`` into three and no
      neighbouring pair canonicalises to anything (the link needs its closing
      bracket, which is in the third piece), while the full join collapses the url
      to the label and puts the label against ``AKIA``. Piece length is no defence
      here -- canonicalising DROPS a link's target, so a piece of any size can
      vanish entirely.
    * **each boundary against everything after it**, which is the reading that
      survives a pattern needing a trailing boundary: the reader sees a message
      break where the whole-sequence join sees the next character, so a key
      completed at the end of one piece must be caught even when later text would
      spoil the match.

    Every piece is put through the same redaction the sender will apply and then
    reduced to what the platform SHOWS, exactly as :func:`joins_to_a_credential`
    does for a single cut -- of which this is the n-piece case. Soundness rests on
    the same property: ``redact_for_display`` is already a fixed point for one
    string, so whatever either reading finds is produced by putting pieces together
    and by nothing else. One piece therefore answers False: there is no boundary.

    Callers must pass the pieces AS DELIVERED, not as the splitter produced them. A
    renderer that strips steering markers, horizontal rules or surrounding
    whitespace on the way out delivers something shorter than it graded, and two
    pieces whose facing edges are whitespace become adjacent on screen -- where
    ``AKIAIOSF`` and ``    ODNN7EXAMPLE`` read as one key that no credential pattern
    tolerating no whitespace would have matched.

    True means the split is unusable and the caller must cut somewhere else (see
    :func:`safe_split_offset`) or deliver nothing at all.
    """
    safe = [redact_for_display(piece, redactor)[0] for piece in pieces]
    readings = [
        canonicalize_display("".join(safe)),
        "".join(canonicalize_display(piece) for piece in safe),
    ]
    for index in range(len(safe) - 1):
        rest = "".join(safe[index + 1 :])
        readings.append(canonicalize_display(safe[index] + rest))
        readings.append(canonicalize_display(safe[index]) + canonicalize_display(rest))
    return any(redactor(reading) != reading for reading in readings)


def delivered_window(window: Sequence[str], landed: str) -> list[str]:
    """*window* with *landed* added as the reader's nearest message, bounded.

    The screen tail a later delivery is graded against. One predecessor is not
    enough: canonicalising is non-local, so a key can span a head, a message
    holding only a link target and a message holding only the closing bracket,
    and every adjacent PAIR of those reads clean (see
    :func:`severs_a_credential`). Only a window that holds all of them at once
    sees it.

    Bounded, because the alternative is a renderer that accumulates every message
    of a long turn and a predicate whose cost grows with it -- each reading in
    ``severs_a_credential`` spans the whole join, and there is one per boundary, so
    the work is quadratic in the number of pieces.

    Nothing is DROPPED to meet that bound, because a dropped message is one the reader
    can still see and a key can still finish against. The recent messages are kept
    whole, each its own piece with its own boundary, and everything older is MERGED
    into a single piece holding the trailing characters of what those messages put on
    screen. Merging costs only the boundaries INSIDE the merged block, which are the
    ones furthest from the pending text; the characters themselves stay in every
    reading. A message-count cap that evicted instead would throw away exactly the
    fragment a run of near-invisible deliveries pushes out -- the construction this
    module defends against, and one the model on the other side can drive.

    The merged piece is stored in canonical form, which is what makes it a rolling
    tail: canonicalising is idempotent, so folding the next message into it and
    trimming to the budget can be repeated without the representation drifting. The
    budget counts those canonical characters rather than raw ones because
    canonicalising DROPS a link's target, so a message of any raw size can contribute
    almost nothing to what the reader reads, and a raw budget would trim away the
    short text actually adjacent to the pending message.

    Every message entered is counted once per delivery and never collapsed against its
    neighbour. Two consecutive deliveries CAN be byte-identical -- an edit pair that
    returns not-modified falls through to a duplicate send, and a degraded segment can
    repeat a header row -- and the boundary between those two is as real as any other:
    a message whose tail ends a key's head and whose own head completes it hands the
    reader that key across its own repeat. Not entering the same message twice is the
    caller's job, where message identity is known.

    The bound is a bound, so it is also a limit: a credential whose canonical form
    spans more characters of screen than the budget reaches is not caught here, and
    no finite budget closes that -- the bare-secret-run pattern has no upper length.
    What it takes is for the retained screen tail to be a property of the transport
    rather than of one renderer's field, and that is tracked separately.
    """
    pieces = [piece for piece in window if piece]
    if landed:
        pieces.append(landed)
    if len(pieces) <= _WINDOW_RECENT:
        return pieces
    older, recent = pieces[:-_WINDOW_RECENT], pieces[-_WINDOW_RECENT:]
    tail = canonicalize_display("".join(older))[-_WINDOW_CANON_CHARS:]
    return [tail, *recent] if tail else recent


def redact_across_delivery(
    delivered: str | Sequence[str], pending: str, redactor: Callable[[str], str]
) -> str:
    """*pending*, with whatever completes a key against *delivered* taken out.

    *delivered* is the message ALREADY on screen, or several of them in delivery
    order with the reader's nearest last. A window rather than a single message,
    for two reasons that are the same reason. Canonicalising is non-local, so a
    key can span THREE messages -- a head, a message holding only a link target,
    and the closing bracket -- and every adjacent PAIR of those reads clean while
    the join does not; one predecessor cannot see that. And a live frame whose
    edit FAILED is still on the reader's screen even though the next payload is
    sent rather than edited, so on that path the frame is a neighbour too, sitting
    between the last closed message and the text going out.

    A seam is graded when the cut is made, over the text present THEN. That is not a
    decision that keeps: characters arriving later can change how earlier ones
    render. A message ending ``...AKIA`` beside a live tail of ``[KEYTAIL`` grades
    clean -- the link has no closing bracket yet -- and once it is sent it cannot be
    recalled; the bracket then arrives and the reader reads one key down the screen.

    So the boundary is graded again at every delivery, against what is already on
    screen. When the join reveals a key, the remedy has to act on the PENDING side,
    because that is the only side still in hand.

    Everything this returns is built from *pending* ALONE, and that is the whole
    design rather than a detail. Redacting the JOIN and slicing the result at the
    point where it stops matching *delivered* cannot work: the redaction of a join
    may emit its canonical form, which differs from *delivered* from the first piece
    of markup onward -- before the match, not at it -- so the slice falls at a
    representation boundary and carries a re-rendered copy of prose the reader can
    already see. Duplicated prose and a truncated placeholder are the two shapes
    that produces.

    Withholding is NOT an alternative either. The text must be delivered eventually,
    and a later seal redacts only its own segment -- it cannot see the half that is
    already gone -- so deferring would ship the completion untouched.

    So *pending* is redacted on its own terms first. When that is still not enough,
    the key's completion is what sits at the front of it, and leading WORDS are
    dropped until the predicate agrees.

    Words, not characters, and that is the point. A character-at-a-time cut stops at
    the first length the pattern fails to match, which for a twenty-character key
    means nineteen of its characters stay on screen -- safe by the regex and plainly
    readable by a human. A credential does not straddle a space, so the completion
    lies inside the first word; removing whole words removes it outright. The empty
    string is always safe -- *delivered* alone is already a fixed point -- so the walk
    terminates.

    The predicate throughout is the one that fired, :func:`severs_a_credential`, and
    that matters: :func:`redact_for_display` scans a strictly SMALLER set of readings
    -- it has no per-piece canonical reading -- so its rewrite alone can leave a
    match that only the wider reading sees, and a cut inside a link's target is
    exactly such a match.
    """
    pieces = [delivered] if isinstance(delivered, str) else list(delivered)
    window = [piece for piece in pieces if piece]
    if not window or not severs_a_credential([*window, pending], redactor):
        return pending
    candidate = redact_for_display(pending, redactor)[0]
    while candidate and severs_a_credential([*window, candidate], redactor):
        head = candidate.lstrip()
        gap = len(candidate) - len(head)
        cut = _WORD_BREAK.search(candidate, gap + 1)
        candidate = candidate[cut.end() :] if cut else ""
    return candidate


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
