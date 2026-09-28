"""Fence-safe markdown splitting, shared by every messaging channel.

Six splitters grew independently — Telegram carries a
``_split_text``/``_split_markdown`` pair, ``messaging/renderer.py`` chunks blind
fixed-width, and Slack, Webex and Weixin have their own — so a fix landed in one
never reached the others. This module is the single engine they converge on, and
Discord is the first channel on it.

Three properties make it safe for the shared path:

**Prefix stability (the streaming contract).** Splitting is greedy
left-to-right and every cut depends only on the text BEFORE it, so re-splitting
a longer prefix of the same stream reproduces every chunk except the last one
byte-for-byte. A streaming caller can therefore send each sealed chunk as it
appears and keep only the final chunk as a live buffer.

**Real fence grammar, not backtick parity.** Counting ``` occurrences misreads
a ``` line inside a ````diff block as a closer and then inverts the open/closed
state for the rest of the message. Here an opener is up to three spaces of
indent plus a run of at least three backticks or tildes, a closer is a run of
the SAME character at least as long with nothing else on the line, and
everything between them is opaque content.

**Self-contained chunks.** A cut inside a fence seals the chunk with a
synthetic closer and reopens the next chunk with the original opener line —
info string and indent included — so ```` ```python ```` survives the split as
```` ```python ````. The FINAL chunk is left open on purpose: callers own final
presentation, and a streaming caller still holds it as a live buffer.

A hard cut is where those properties meet: it splits one line across a chunk
boundary, so both halves start a rendered line and the cut is pulled back until
neither invents a fence. Some fragments admit no such cut — every candidate
lands immediately before indent or a fence character, as in a bare ``` line or
a long backtick run in prose. Those are not cut at all: the line is placed
WHOLE, in a chunk holding it and nothing else, whenever the LINE ITSELF is no
longer than ``limit``. Eligibility deliberately ignores the fence scaffolding
that chunk needs, so the chunk carries its reopener line and synthetic closer on
top of ``limit`` and may pass it by exactly that much; a chunk with no
scaffolding to carry stays within ``limit``. Counting the scaffolding in instead
would refuse a line that fits ``limit`` on its own and cut it into a fence
delimiter its source never contained, which is the worse of the two costs. Only
a line longer than the full ``limit`` cannot be placed that way, and there the
widest prefix-clean cut is taken, so the deferred remainder can still open or
close a fence the source line does not. That residue belongs to the same
degradation regime as a budget too small to hold a line's fence scaffolding:
forward progress and termination come first.

Which of those three a line takes is decided from the line and ``limit`` alone,
before any of the budget arithmetic — remaining room, the reserved closer, what
the chunk already holds — gets a say. So the dirty cut of the third tier is
reachable only through the ``else`` of ``len(line) <= limit``, whatever that
arithmetic works out to. Eligibility written as a guard along one arithmetic
path is what made this ladder bypassable at budgets a fence's scaffolding
consumes whole, and an exhaustive small-space oracle in the tests pins the
property rather than the instances.

Byte-capped platforms wrap the character splitter with
:func:`split_markdown_bytes`, which measures the produced chunks and shrinks the
character budget until they fit. Still out of scope for callers to wrap: pipe
table conversion, rendered-length budgeting for channels that inflate the
source, and UTF-16 length limits.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from kiro_crew.messaging.display_safety import (
    canonicalize_display,
    joins_to_a_credential,
    redact_for_display,
)

__all__ = [
    "split_markdown_safe",
    "split_markdown_safe_with_tier",
    "split_markdown_bytes",
    "chunk_utf8_bytes",
    "repaired_for_delivery",
    "bounded_for_delivery",
    "iter_fence_spans",
    "iter_fence_lines",
    "truncate_utf8",
    "FENCE_OUTSIDE",
    "FENCE_OPEN",
    "FENCE_BODY",
    "FENCE_CLOSE",
]

#: Per-line fence roles yielded by :func:`iter_fence_lines`. Named rather than
#: booleans because a caller distinguishes four cases, not two: outside a fence,
#: the opener, content, and the closer.
FENCE_OUTSIDE = "outside"
FENCE_OPEN = "open"
FENCE_BODY = "body"
FENCE_CLOSE = "close"

# How many times :func:`split_markdown_bytes` may shrink its character budget
# before falling back to byte slicing. Each round strictly reduces the budget,
# so this only bounds the work: the ratio step converges in two or three rounds
# even for all-4-byte input, and the extra headroom absorbs a document whose
# heavy characters are unevenly distributed.
_BYTE_SHRINK_ROUNDS = 6

# Below this the character budget is too small for the fence ladder to make
# meaningful cuts, so shrinking further just degrades every chunk. The byte
# slicer takes over instead.
_MIN_BYTE_SHRINK_LIMIT = 16


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate *text* to at most *max_bytes* UTF-8 bytes without splitting a
    code point.

    The exact guard for a channel whose wire limit is denominated in BYTES. A
    reply can sit under a CHARACTER cap and still be over the byte cap — one CJK
    character is three bytes and an emoji four — and a platform that refuses the
    oversize send gives the user nothing at all, so this is the last thing
    between an authored answer and a rejected frame.

    ``errors="ignore"`` on the decode is what drops a trailing partial sequence
    rather than raising, so the cut lands on the largest whole-code-point prefix
    that fits. A non-positive *max_bytes* disables the guard and returns *text*
    unchanged, matching :func:`split_markdown_safe`'s treatment of a non-positive
    ``limit``: a caller with no budget to enforce must not lose its whole message
    to a zero.

    This TRUNCATES, and therefore loses the tail: it is a backstop, not a
    delivery strategy. A caller with more text than one message may hold splits
    first — ``split_markdown_safe`` against a byte-safe character budget, which
    a byte-capped channel derives as ``<its byte budget> // 4`` so the character
    splitter is byte-safe in the worst case — and reaches this only for a chunk
    that still does not fit.
    """
    if max_bytes <= 0:
        return text
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


# An opener is <=3 spaces of indent + a run of >=3 backticks/tildes + an info
# string. A backtick fence's info string may not contain a backtick (otherwise
# ``` `` x `` ``` would open a block); a tilde fence's may contain anything.
_BACKTICK_OPEN_RE = re.compile(r"^ {0,3}(`{3,})[^`]*$")
_TILDE_OPEN_RE = re.compile(r"^ {0,3}(~{3,}).*$")
# A closer carries nothing but its own run (trailing whitespace is allowed).
_CLOSE_RE = re.compile(r"^ {0,3}((?:`{3,})|(?:~{3,}))[ \t]*$")

# Every delimiter line starts with indent or the run itself, so a rendered line
# opening with any other character can never be one — the test a hard cut applies
# to the remainder it defers.
_DELIM_LEAD = frozenset(" `~")

# Characters a GFM separator row may contain (``| --- |``, ``|:--|--:|``).
_TABLE_SEP_CHARS = set("-:| \t")


@dataclass(frozen=True)
class _Fence:
    """The fence open at some position in the source."""

    char: str  # "`" or "~"
    length: int  # backtick/tilde run length of the OPENER
    opener: str  # the verbatim opener line (no line ending), reused to reopen

    @property
    def closer(self) -> str:
        """A synthetic closer for this fence: same char, matching length."""
        return self.char * self.length

    @property
    def seal_cost(self) -> int:
        """Capacity to hold back so the synthetic closer always fits.

        ``+1`` for the newline that must precede it — reserved unconditionally
        because a hard cut can land mid-line, where the chunk does not already
        end in one. Held back everywhere a line is accumulated or cut; the
        whole-line placement in ``split_markdown_safe`` deliberately does not,
        which is why that one chunk can pass ``limit`` by this much.
        """
        return self.length + 1


#: One unit of work: a text fragment, the full logical line it came from, and
#: whether it ends that line. Fence state advances only on a TERMINATED line's
#: tail, so neither a line hard-cut across chunks nor a last line still arriving
#: can flip the state halfway through itself.
_Frag = tuple[str, str, bool]

#: Any whitespace run. A cut consumes the run it lands on -- the character
#: splitter trims it when it seals, and a platform drops it when it renders the
#: message either way -- so the form with EVERY run gone is what some cut could
#: produce. Used only by the repair, never to decide whether one is needed.
_WHITESPACE_RUN = re.compile(r"\s+")

#: How many budgets below the caller's own are tried one at a time before the
#: walk starts doubling its step. Bounded because each probe costs a redaction
#: pass over the whole text, and chosen so the worst case stays near a second at
#: the largest message sizes this module cuts.
_DENSE_PROBES = 128


def _rejoins_a_key(chunks: list[str], redactor: Callable[[str], str]) -> bool:
    """Does the delivered sequence show a key that the text on its own does not?

    Graded on the RENDERED SEQUENCE, not the characters as stored. A platform
    drops the whitespace at a message's edges, so the last visible character of
    one message sits against the first visible character of the next: neither a
    trailing newline nor a trailing space separates anything once the two are on
    screen. Stripping every chunk's edges before the grade is what makes this
    predicate see what a reader sees, and it is the difference between the two
    splitters -- the character one trims the whitespace itself when it seals,
    while the byte one is lossless and carries the run into the chunk, where the
    client drops it anyway.

    The whole sequence at once, not boundary by boundary, because a key needs no
    more than a narrow budget to span three chunks. Split ``AKIA``-then-sixteen
    over four chunks and every neighbouring pair holds a fragment that matches
    nothing, while the screen holds the key whole; the same gap swallows a chunk
    that renders to nothing at all, which the lossless byte splitter can place
    between the two halves. A fragment is only ever part of a key, so no pair of
    neighbours can be asked about it -- the reading that sees it is the sequence.

    Both readings
    :func:`~kiro_crew.messaging.display_safety.joins_to_a_credential` names are
    kept, for the reasons it gives: canonicalising the join is the wider reading
    for runs of delimiters, and canonicalising each side first is the wider one
    wherever rendering DROPS text, as a link does to its target. Either one
    finding something refuses the cut.

    Scanning each reading ONCE is what keeps the search affordable, and it is
    sound because the caller redacts the whole text before cutting it: that text
    is a fixed point of this same scan, so anything found here is produced by
    delivering it in pieces and by nothing else.
    """
    if len(chunks) < 2:
        return False
    rendered = [chunk.strip() for chunk in chunks]
    readings = (
        canonicalize_display("".join(rendered)),
        "".join(canonicalize_display(part) for part in rendered),
    )
    return any(redactor(reading) != reading for reading in readings)


def _collapses_to_a_key(text: str, redactor: Callable[[str], str]) -> bool:
    """Does *text* read as a key once its whitespace goes?"""
    bare = _WHITESPACE_RUN.sub("", text)
    return redactor(bare) != bare


def _collapse_reads_as_a_key(text: str, redactor: Callable[[str], str]) -> bool:
    """Does *text* read as a key once RENDERED and then collapsed?

    The reading a caller re-cutting this text is exposed to, and the one
    :func:`_made_collapse_clean` guarantees against. Wider than
    :func:`_collapses_to_a_key`, which asks about the characters as stored: a key
    written inside code spans is assembled by the rendering first and only then by
    removing the break, so a gate that skips the canonical step passes text whose
    collapse still holds a key.
    """
    return _collapses_to_a_key(canonicalize_display(text), redactor)


def _made_collapse_clean(text: str, redactor: Callable[[str], str]) -> str:
    """*text*, or a form of it whose COLLAPSE holds no key.

    The guarantee every caller that cuts this module's answer again depends on: a
    text whose collapse reads clean cannot produce a key under any later cut,
    because removing a boundary's whitespace is the most a cut can do to bring two
    characters together, and no substring of a clean collapse holds a key either.
    Establishing it HERE is what lets a caller re-cut without a second grade.

    Three steps, least destructive first. Text whose collapse is already clean is
    returned unchanged, which is every reply that holds no credential at all and
    costs one scan. Next the canonical form is redacted, which is
    :func:`~kiro_crew.messaging.display_safety.redact_for_display`'s own
    one-directional trade and reaches a key that only the rendering assembles.

    Only where that still collapses into a key is whitespace given up, and only the
    whitespace of the SPAN that hides it: the canonical form's smallest span that
    collapses into a key is found the same way the literal reading finds one, closed
    up, and redacted. Everything outside that span keeps its breaks and its markup,
    so a long reply carrying such a key is not flattened to protect one span of it.
    """
    if not _collapse_reads_as_a_key(text, redactor):
        return text
    canonical = redact_for_display(canonicalize_display(text), redactor)[0]
    if not _collapse_reads_as_a_key(canonical, redactor):
        return canonical
    narrowed = _redact_only_the_rejoined_span(canonicalize_display(text), redactor)
    if narrowed is not None and not _collapse_reads_as_a_key(narrowed, redactor):
        return narrowed
    return redact_for_display(_WHITESPACE_RUN.sub("", canonicalize_display(text)), redactor)[0]


def _span_whose_whitespace_hides_a_key(
    text: str, redactor: Callable[[str], str]
) -> tuple[int, int] | None:
    """The smallest span of *text* that still reads as a key once collapsed.

    Found by SHRINKING, not by a window of characters guessed to be near a key.
    A span known to collapse into a key is trimmed from each end by half its
    width, the trim kept whenever what remains still collapses into one, and the
    step halved when neither end can give ground. What that converges on is a span
    no half-step shorter at either end, which holds one key and little else.

    Shrinking is what keeps the repair honest when a message carries TWO keys. The
    region between them collapses into a key only because the keys do, so a span
    bracketing both also brackets the prose between them, and closing that up
    would destroy paragraphs the credentials have nothing to do with. One key at a
    time, each in its own span, leaves the text between untouched.

    A span chosen from a hand-written character set instead would stop at the first
    character the set does not know, which is the objection
    :func:`~kiro_crew.messaging.display_safety.joins_to_a_credential` makes to
    windows: the scan then runs on text the key's prefix was never inside.

    ``None`` when nothing is hidden, which is the whole point of asking.
    """
    if not _collapses_to_a_key(text, redactor):
        return None
    low, high = 0, len(text)
    step = (high - low) // 2
    while step > 0:
        if low + step < high and _collapses_to_a_key(text[low + step : high], redactor):
            low += step
        elif high - step > low and _collapses_to_a_key(text[low : high - step], redactor):
            high -= step
        else:
            step //= 2
    return low, high


def _redact_only_the_rejoined_span(text: str, redactor: Callable[[str], str]) -> str | None:
    """*text* with only the key-hiding spans closed up and redacted, or ``None``.

    The least destructive repair: the whitespace INSIDE a span that reads as a key
    is what gets removed, which makes that key contiguous and lets the ordinary
    redaction take it. Every other space, every line break and all the markup of
    the message survive, so a long reply keeps its shape and loses the credential
    rather than its formatting.

    It makes a promise to callers, and keeps it by test rather than by assertion:
    the loop returns only once the form with EVERY run removed reads clean, so a
    caller that cuts these chunks again under its own platform limit cannot
    uncover a key at a boundary this module never chose. ``None`` says the repair
    cannot make that promise -- the case being a key that only the canonical
    reading sees, which collapsing whitespace does not assemble and so no span of
    the literal text names.

    One span per pass, so a message carrying two keys keeps the prose between
    them: each pass closes up one key's own span and redacts, and the next pass
    starts from the result.

    The pass count is capped at :data:`_DENSE_PROBES`, for the same reason the
    budget search is: each pass costs a logarithmic number of whole-text redaction
    passes, so a reply whose every line break splits a key-shaped token would cost
    thousands of them and the send would never finish. Past the cap the answer is
    ``None``, which is not a failure -- the caller's own last resort is to decline
    to cut, which gives up nothing at all.
    """
    repaired = text
    for _ in range(min(len(_WHITESPACE_RUN.findall(text)) + 1, _DENSE_PROBES)):
        span = _span_whose_whitespace_hides_a_key(repaired, redactor)
        if span is None:
            collapsed = _WHITESPACE_RUN.sub("", canonicalize_display(repaired))
            return repaired if redactor(collapsed) == collapsed else None
        start, end = span
        closed = repaired[:start] + _WHITESPACE_RUN.sub("", repaired[start:end]) + repaired[end:]
        if closed == repaired:
            return None
        repaired = redact_for_display(closed, redactor)[0]
    return None


def repaired_for_delivery(
    source: str, pieces: list[str], redactor: Callable[[str], str]
) -> str | None:
    """``None`` when *pieces* are safe to deliver one message each, else a repair.

    For a caller that cuts *source* into *pieces* AFTER this module graded it. A
    fixed-width slice of an oversized chunk is a boundary nothing graded, and each
    slice is posted as its own message, so the seam the splitter closed reopens at
    the very last step. Grading the sequence here means the caller does not have to
    know how the grade works, only that it has one to run.

    ``source`` is the text the pieces were cut FROM, and the repair is made on it.
    A repair rebuilt from ``"".join(pieces)`` would not be the reply: sealing a
    chunk outside a fence trims the whitespace that ended it, so the join deletes a
    break at every seam, and a fence spanning a seam contributes its synthetic
    closer and the next piece's reopener, which meet as one longer delimiter run
    that reads as a new fence. The pieces are therefore GRADED and never
    reassembled -- every caller of this function still holds what it cut.

    The returned text is safe to cut AGAIN by any rule the caller likes, and that
    is a guarantee rather than a hope: what comes back is a collapse fixed point,
    so no later cut can bring two characters together into a key.
    :func:`_made_collapse_clean` establishes it, and the splitter establishes it on
    its own answers too, so a caller re-cutting either needs no second grade.

    When no span of the literal text names the key -- the case of one only the
    rendering assembles -- the canonical form is what answers, giving up markup and
    keeping every space and every break. That is
    :func:`~kiro_crew.messaging.display_safety.redact_for_display`'s own
    one-directional trade, not a wider one invented here: no valid span loses its
    whitespace to protect a credential somewhere else in the message.
    """
    if not _rejoins_a_key(pieces, redactor):
        return None
    repaired = _redact_only_the_rejoined_span(source, redactor)
    if repaired is not None:
        return repaired
    return _made_collapse_clean(source, redactor)


def offset_clear_of_a_sent_tail(
    sent_tail: str, remainder: str, redactor: Callable[[str], str]
) -> int:
    """Smallest offset into *remainder* whose suffix completes no key after *sent_tail*.

    ``0`` when the pair is already clean, which is the common answer. Otherwise the
    offset covers exactly the leading characters of *remainder* that finish a
    credential begun in text ALREADY DELIVERED, and everything from it onward is
    the reply as written.

    A streaming caller cannot grade this at the moment it seals. It seals a chunk
    whose tail is a credential PREFIX -- matching nothing, so every scan passes it
    -- and the characters completing the key arrive afterwards. The sealed message
    cannot be recalled, so the only side still open to repair is the one not yet
    sent, and the honest repair is to give up the span that completes the key while
    keeping every character after it.

    Candidates step back EXPONENTIALLY, the same bound
    :func:`~kiro_crew.messaging.display_safety.safe_split_offset` takes and for the
    same reason: the smallest clearing offset is not needed, only a clearing one,
    and each candidate costs a redaction pass over attacker-influenced text.
    ``len(remainder)`` is the last candidate, which gives up the remainder rather
    than delivering a key across the seam.
    """
    if not sent_tail or not remainder:
        return 0
    head = sent_tail.rstrip()
    if not joins_to_a_credential(head, remainder.lstrip(), redactor):
        return 0
    offset = 1
    while offset < len(remainder):
        if not joins_to_a_credential(head, remainder[offset:].lstrip(), redactor):
            return offset
        offset *= 2
    return len(remainder)


def bounded_for_delivery(
    chunks: list[str],
    budget: int,
    redactor: Callable[[str], str],
    cut: Callable[[str, int], list[str]] | None = None,
) -> list[str]:
    """*chunks*, each graded once more and cut back inside *budget* if it repairs.

    Every caller whose transport CAPS what it accepts needs this, and the reason is
    :func:`_cut_where_no_key_rejoins`'s last step: when no budget cuts without
    rejoining a key the splitter declines to cut and answers with the text whole,
    which is fail-closed but is one chunk over the caller's budget. A transport that
    truncates a larger payload would drop the tail of that answer with no notice,
    and the truncation happens AFTER every scan, so nothing sees it.

    Each chunk is bounded and graded ON ITS OWN, and a chunk that repairs is the
    repair's subject, because a chunk is a string this function holds while the
    sequence's concatenation is not the reply -- the seam whitespace is already
    trimmed out of it. The boundaries BETWEEN the chunks handed in are the
    splitter's own, graded where the whole source was still available; the ones
    created here are the new slices inside a chunk, which is exactly what each
    chunk's own grade sees.

    Re-cutting the repair needs no redactor and must not take one: the repair is only
    returned once the form with every whitespace run removed reads clean, so any cut
    of it is safe, while a credential-aware cut could decline to cut again and leave
    the budget unmet. ``cut`` defaults to the character splitter; a byte-capped
    transport passes :func:`chunk_utf8_bytes`.

    The budget is applied BEFORE the grade, not after, and that order is the whole
    point. The splitter's fail-closed answer is a list of ONE chunk, and a
    one-element list holds no boundary, so a grade asked about it answers that
    nothing rejoins and the oversized chunk travels on unbounded -- the exact case
    this function exists for. Cutting to the budget first makes the grade see the
    sequence the transport will really deliver, so a key across one of those new
    boundaries is found and repaired. A chunk already inside the budget is returned
    unchanged by its own cutter, so the pass costs nothing.
    """
    cutter = cut if cut is not None else split_markdown_safe
    delivered: list[str] = []
    for chunk in chunks:
        pieces = cutter(chunk, budget) or [chunk]
        repaired = repaired_for_delivery(chunk, pieces, redactor)
        if repaired is not None:
            pieces = cutter(repaired, budget) or [repaired]
        delivered.extend(pieces)
    return delivered


def _under_a_safe_budget(
    cut: Callable[[str, int], list[str]],
    text: str,
    budget: int,
    redactor: Callable[[str], str],
    floor: int = 0,
) -> list[str] | None:
    """Chunks of *text* whose every boundary is clean, or ``None`` if no budget is.

    Moves the CUT rather than the text, which costs nothing: every character is
    still delivered, fences still reopen, and a key that straddles no boundary
    travels whole inside one chunk -- where the space or newline between its
    halves is on screen, which is the same reading the scanner already accepts
    when it passes such text.

    Budgets step back ONE AT A TIME for the first :data:`_DENSE_PROBES`, then
    exponentially, and each candidate is verified rather than assumed. The dense
    prefix is what stops a safe budget a few bytes below the caller's from being
    stepped over: an exponential walk alone jumps from 4 to 8 to 16, so a budget
    clean at 6950 sits unseen between samples at 6968 and 6936 and the text goes
    to the last resort with a safe cut available. The exponential tail keeps the
    reach, because text needing a much smaller budget must still be found without
    a probe per byte -- a scan of every budget costs a redaction pass each, which
    at this module's message sizes is minutes rather than milliseconds.

    ``floor`` is the smallest budget worth trying -- below the caller's ``reserve``
    the splitter returns the text whole, which is not a safe answer but an unsplit
    one, and the caller would cut it again under its own limit.
    """
    room, step = budget, 0
    while room > floor:
        chunks = cut(text, room)
        if not _rejoins_a_key(chunks, redactor):
            return chunks
        step = step + 1 if step < _DENSE_PROBES else step * 2
        room = budget - step
    return None


def _cut_where_no_key_rejoins(
    cut: Callable[[str, int], list[str]],
    text: str,
    budget: int,
    redactor: Callable[[str], str],
    floor: int = 0,
) -> list[str]:
    """Cut *text* so that no boundary hands the reader a key neither chunk holds.

    Four steps, cheapest and least destructive first:

    1. redact against the rendered form, which closes a cut landing mid-line, and
       establish the COLLAPSE guarantee on that text through
       :func:`_made_collapse_clean`, so every chunk returned below holds no key
       under any later cut and a caller re-cutting one needs no second grade;
    2. search for a budget whose boundaries are all clean, which keeps every
       character and every break;
    3. if none is, close up and redact only the span that reads as a key, which
       keeps every other space, break and markup in the message;
    4. if no span names it either, DO NOT CUT. The text goes back whole.

    Each repair is graded by the same search that rejected the text, because a
    repair returned without a grade is a repair nobody checked.

    Step 4 is the honest answer to a budget this module cannot meet. A boundary is
    the only thing that rejoins a key ACROSS messages, so declining to create one
    is fail-closed, and it costs the caller a chunk over its limit rather than
    costing the reader a message stripped of its whitespace and its markup. A key
    the model wrote with a space or emphasis inside it then stays exactly as
    written inside ONE message, which is the reading the whole-text pass already
    accepts. What to do with an oversized chunk is the caller's own decision, and a
    caller that bounds it again has :func:`repaired_for_delivery` to grade what it
    produced.
    """
    safe = redact_for_display(text, redactor)[0]
    chunks = _under_a_safe_budget(cut, safe, budget, redactor, floor)
    if chunks is not None:
        return chunks
    repaired = _redact_only_the_rejoined_span(safe, redactor)
    if repaired is None:
        return [_made_collapse_clean(safe, redactor)]
    chunks = _under_a_safe_budget(cut, repaired, budget, redactor, floor)
    return chunks if chunks is not None else [_made_collapse_clean(repaired, redactor)]


def split_markdown_safe(
    text: str,
    limit: int,
    *,
    reserve: int = 0,
    redactor: Callable[[str], str] | None = None,
    stable: bool = False,
) -> list[str]:
    """Split *text* into chunks of at most ``limit - reserve`` characters.

    ``reserve`` holds back capacity for something the caller appends to every
    chunk (a page counter, a continuation marker). Empty text yields ``[]``;
    text that already fits, a non-positive ``limit``, and a ``reserve`` that
    consumes the whole budget all yield ``[text]`` unchanged.

    Cut preference outside a fence follows Discord's long-standing ladder: a
    paragraph break if one sits at least halfway into the budget, else a line
    break if it sits at least a quarter in, else a hard cut filling the budget.
    The thresholds keep a short leading line from stranding most of the budget.
    Inside a fence only line boundaries are used, and a line is hard-cut only
    when it cannot fit a chunk at all.

    Leading whitespace is never stripped — doing so silently re-indents split
    code. Trailing whitespace is trimmed only when sealing outside a fence,
    where it cannot be content.

    A budget too small to hold a line's own fence scaffolding yields chunks over
    the budget rather than not terminating; callers pass a realistic ``limit``.
    One further chunk may exceed ``limit`` itself, and only by its fence
    scaffolding, when a logical line admits no cut clean on both sides: such a
    line is placed whole whenever the LINE ITSELF fits within ``limit``,
    rather than cut into a fence delimiter its source never contained. Eligibility
    measures the line alone, so the chunk holding it adds the reopener line and
    the synthetic closer on top and may pass ``limit`` by exactly that
    scaffolding; with no scaffolding to carry it stays within ``limit``. Such a
    chunk holds that one line and nothing else. Only a line longer than ``limit``
    is cut without a clean boundary, and there the deferred remainder can still
    read as a delimiter the source line does not. That choice reads the line and
    ``limit`` alone — never the remaining room, the reserved closer, or what the
    chunk already holds — so the cut without a clean boundary is reachable only
    for a line longer than ``limit``.

    ``redactor`` makes the cut itself credential-aware, and a caller that
    delivers each chunk as its own message passes one. The text is redacted
    against the rendered form first, which closes a cut landing mid-line; the
    boundaries are then graded as the reader sees them, and a budget whose
    boundaries are all clean is searched for before anything in the text is given
    up, so ordinary content -- a long wrapped line in a fence, say -- comes back
    exactly as written. The parameter stays optional, because a caller whose
    chunks land inside one message severs nothing a reader can rejoin across
    messages, and the redaction is idempotent, so a caller that already redacted
    its text pays one scan and keeps its bytes.
    """
    if redactor is not None:
        if stable:
            # PREFIX-STABLE: redact the whole text, then cut at the caller's own
            # budget and nowhere else. A streaming caller re-splits its growing
            # body every frame and treats all but the last chunk as delivered, so
            # it needs chunk i to be decided by the text before it and nothing
            # later. Searching for a safer budget reads the WHOLE body, so text
            # arriving later can move a boundary under a message already sent --
            # which a count of delivered chunks cannot detect and no later frame
            # can take back. Such a caller grades its own seam before it treats a
            # chunk as final, and holds one it cannot yet vouch for.
            return split_markdown_safe(
                redact_for_display(text, redactor)[0], limit, reserve=reserve
            )
        # The recursive cut passes no redactor, so this runs one level deep.
        # ``reserve`` travels with it, and is the floor of the budget search: a
        # budget it consumes whole would return the text unsplit.
        return _cut_where_no_key_rejoins(
            lambda body, room: split_markdown_safe(body, room, reserve=reserve),
            text,
            limit,
            redactor,
            reserve,
        )
    chunks, _ = split_markdown_safe_with_tier(text, limit, reserve=reserve)
    return chunks


def split_markdown_safe_with_tier(
    text: str, limit: int, *, reserve: int = 0
) -> tuple[list[str], bool]:
    """Split like :func:`split_markdown_safe` but declare the tier.

    Returns ``(chunks, degraded)`` where ``degraded`` is True iff the split
    entered the context-degrading tier — a logical line longer than ``limit``
    was cut without a clean boundary on both sides, so the deferred remainder
    can read as a delimiter the source line never contained. Discord's
    ``_rotate_on_length`` consumes this instead of probing with a synthetic
    ``![x](/tmp/x.png)`` reference.

    The plain :func:`split_markdown_safe` keeps its ``list[str]`` signature so
    every other channel stays unchanged. The redactor/stable credential-aware
    paths belong to :func:`split_markdown_safe` and never reach the tier form.
    """
    if not text:
        return [], False
    cap = limit - reserve
    if limit <= 0 or cap <= 0 or len(text) <= cap:
        return [text], False

    out: list[str] = []
    degraded = False
    work: list[_Frag] = _lines(text)
    pos = 0  # index of the next fragment to place
    buf: list[_Frag] = []  # fragments accumulated for the current chunk
    states: list[_Fence | None] = []  # fence state AFTER each buffered fragment
    fence: _Fence | None = None  # fence open at the current source position
    reopen = ""  # synthetic opener line starting the current chunk
    used = 0  # characters already committed to the current chunk

    while pos < len(work):
        frag, line, is_tail = work[pos]
        # A last line with no newline yet is UNCLASSIFIED, not classified-so-far.
        # One more character can invert it — "```x" opens a block and "```x`"
        # does not, and inside a ````block a "```" run closes nothing until a
        # fourth backtick arrives — so a seal taken from that state would be
        # rewritten by the rest of the line.
        settled = is_tail and line.endswith("\n")
        after = _advance(fence, line) if settled else fence
        # Such a line also reserves NOTHING, not even the current fence's closer:
        # the chunk holding it is the live tail, which is never sealed, and any
        # reservation would shrink once its newline arrived — loosening the fit
        # and merging back a chunk already sealed before it. Reserving nothing
        # can only tighten as the line grows, which merely seals sooner.
        hold = _seal_cost(after) if settled else 0
        if used + len(frag) + hold <= cap:
            buf.append(work[pos])
            states.append(after)
            used += len(frag)
            fence = after
            pos += 1
            continue

        # How much of this fragment the chunk can still take. At least one
        # character even where the scaffolding already spent the whole budget:
        # forward progress outranks the budget in that regime, which is the one
        # the module documents as over-budget rather than non-terminating.
        take = max(1, cap - used - _seal_cost(fence))
        fits = len(frag) <= take
        # The widest cut at or below ``take``, and whether it is clean on BOTH
        # sides (``clean`` is 0 when no width is). Consulted for EVERY fragment
        # that does not fit, whatever the arithmetic above worked out to: an
        # arithmetic branch that skips this bypasses the ladder below at budgets
        # a fence's scaffolding consumes whole. Both read
        # only ``frag[: take + 1]``, which is already complete whenever a cut is
        # on the table, so neither answer moves as a still-arriving line grows.
        width = 0 if fits else _safe_cut(fence, frag, take)
        clean = width if width and frag[width] not in _DELIM_LEAD else 0
        if buf and (fence is not None or used >= cap // 4 or fits or not clean):
            # Seal at a boundary. This empties the buffer, so the next iteration
            # must consume or hard-cut — two seals can never run back to back,
            # which is what makes the loop terminate.
            #
            # ``not clean`` seals for a fragment no cut fits cleanly, handing it
            # the whole budget of a fresh chunk before any dirty cut is
            # considered. That trigger reads cut cleanliness alone, never how
            # long the line turns out to be: a seal keyed on the length of a line
            # still arriving would land elsewhere once the rest of it did,
            # rewriting a chunk already sent.
            keep = len(buf) if fence is not None else _boundary(buf, states, cap, len(reopen))
            chunk = _seal(reopen, buf[:keep], states[keep - 1])
            if chunk:
                out.append(chunk)
            work[pos:pos] = buf[keep:]  # defer what this chunk did not take
            fence = states[keep - 1]
            reopen = f"{fence.opener}\n" if fence else ""
            used = len(reopen)
            buf, states = [], []
            continue

        # THE ELIGIBILITY LADDER. Which of the three placements this fragment
        # takes is decided here, from the LINE and the caller's ``limit`` alone,
        # so none of the arithmetic above can route around it. Every earlier
        # defect in this cluster was exactly that: eligibility wired as a guard
        # along one arithmetic path, and another path reaching a dirty cut
        # without passing it.
        if len(line) <= limit:
            # The line is deliverable in ONE chunk, so it is cut only where a cut
            # is clean on both sides and placed WHOLE (``cut`` 0) where none is.
            # No dirty cut is reachable from this branch, at any ``take``.
            cut = clean
        else:
            # A line longer than the caller's full ``limit`` fits no chunk whole,
            # so it must be cut: cleanly where a clean width exists, else at the
            # widest prefix-clean width — the documented residue, where the
            # deferred remainder can still read as a delimiter the source line
            # never contained. This is the ONLY dirty cut in the function.
            if not clean:
                degraded = True
            cut = width

        if not cut:
            # Take the fragment whole: either it fits, or no cut is clean on both
            # sides and the line it came from fits within the caller's full
            # ``limit``. That second test measures the LINE alone — not the fence
            # scaffolding it needs, and not what the chunk already holds, which
            # the seal above reduced to the reopen line. So the chunk becomes
            # reopen + line + closer and may pass ``limit`` by exactly that
            # scaffolding, which is the cheaper of the two costs: measuring the
            # scaffolding in would refuse a line that fits ``limit`` on its own
            # and cut it into a fence delimiter the source never contained.
            # Either way the fragment's fence transition travels with it —
            # splitting an opener line from the state it opens would strand the
            # reopen and unbalance every later chunk.
            buf.append(work[pos])
            states.append(after)
            used += len(frag)
            fence = after
            pos += 1
            continue

        # Cut the line mid-way, at the width the ladder chose.
        buf.append((frag[:cut], line, False))
        states.append(fence)
        used += cut
        work[pos] = (frag[cut:], line, is_tail)

    tail = reopen + "".join(f for f, _, _ in buf)
    if tail:
        # No synthetic closer: the final chunk keeps an unclosed fence open so a
        # streaming caller can keep appending to it.
        out.append(tail)
    return out, degraded


def chunk_utf8_bytes(
    text: str, max_bytes: int, *, redactor: Callable[[str], str] | None = None
) -> list[str]:
    """Split *text* into chunks of at most *max_bytes* UTF-8 bytes.

    Lossless and code-point-safe: the concatenation of the result always equals
    the input, and no chunk ends mid-sequence. Slicing the ENCODED bytes and
    re-decoding with ``errors="ignore"`` finds the largest whole-code-point
    prefix; the loop then resumes from exactly the characters consumed.

    This is the byte-limit primitive, with no markdown awareness at all — it
    will happily cut through a fence. Callers wanting fence-safe chunks under a
    byte cap use :func:`split_markdown_bytes`, which only falls back here for a
    fragment that admits no clean cut. A non-positive *max_bytes* disables
    chunking, matching ``chunk_text``.

    ``redactor`` carries the same meaning as in :func:`split_markdown_safe`, for
    the same reason: a byte budget knows nothing about credentials either, so a
    caller delivering each chunk as its own message passes one. This splitter
    keeps the whitespace a cut lands on rather than trimming it, but the client
    drops it when it renders the message, which is why the boundary grade reads
    the stripped pair. Losslessness then holds whenever a budget cuts safely,
    which is what the search tries before anything in the text is given up.
    """
    if redactor is not None:
        return _cut_where_no_key_rejoins(chunk_utf8_bytes, text, max_bytes, redactor)
    if not text:
        return []
    if max_bytes <= 0:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        encoded = remaining.encode("utf-8")
        if len(encoded) <= max_bytes:
            chunks.append(remaining)
            break
        piece = encoded[:max_bytes].decode("utf-8", errors="ignore")
        if not piece:
            # max_bytes is smaller than this single code point. Emitting it
            # whole overshoots the budget by a couple of bytes; dropping it
            # would lose content and looping would never terminate.
            piece = remaining[0]
        chunks.append(piece)
        remaining = remaining[len(piece) :]
    return chunks


def split_markdown_bytes(text: str, max_bytes: int, *, reserve: int = 0) -> list[str]:
    """Fence-safe split under a UTF-8 BYTE budget rather than a character one.

    :func:`split_markdown_safe` counts characters, which is the right measure
    for most platforms and the wrong one for a platform whose limit is bytes
    (Webex caps a message at 7439 bytes): a chunk of CJK text can sit well under
    a character budget and still be rejected, and a send path that truncates the
    overflow loses the tail silently.

    Measure, don't predict. A budget of ``max_bytes`` characters cannot overflow
    for ASCII, so the first attempt is the common case and costs one pass. When
    a chunk does measure over, the character budget is shrunk by the observed
    overflow ratio and the split is retried — reading the real encoded length
    beats reasoning about worst-case bytes per character, which would divide the
    budget by four and fragment every ASCII answer into quarters.

    A chunk that still does not fit after the ladder is byte-sliced through
    :func:`chunk_utf8_bytes`, and only that chunk: a fence spanning a cut is a
    rendering defect, but a lost tail is data loss, so the byte cap wins when
    the two conflict. This is reachable for input the character splitter itself
    documents as over-budget — a single line longer than the whole budget, or a
    budget too small to hold a line's fence scaffolding.

    ``reserve`` holds back bytes for something the caller appends to every
    chunk, matching :func:`split_markdown_safe`. Empty text yields ``[]``; text
    that already fits, a non-positive *max_bytes*, and a *reserve* consuming the
    whole budget all yield ``[text]`` unchanged.
    """
    if not text:
        return []
    budget = max_bytes - reserve
    if max_bytes <= 0 or budget <= 0:
        return [text]
    if len(text.encode("utf-8")) <= budget:
        return [text]

    limit = budget
    chunks = split_markdown_safe(text, limit)
    for _ in range(_BYTE_SHRINK_ROUNDS):
        widest = max(len(c.encode("utf-8")) for c in chunks)
        if widest <= budget:
            return chunks
        if limit <= _MIN_BYTE_SHRINK_LIMIT:
            break
        # Scale the character budget by how far the worst chunk overshot, and
        # always make progress: integer rounding on a near-miss could otherwise
        # reproduce the same limit and spin out the rounds for nothing.
        scaled = limit * budget // widest
        limit = max(_MIN_BYTE_SHRINK_LIMIT, min(scaled, limit - 1))
        chunks = split_markdown_safe(text, limit)

    out: list[str] = []
    for chunk in chunks:
        if len(chunk.encode("utf-8")) <= budget:
            out.append(chunk)
        else:
            out.extend(chunk_utf8_bytes(chunk, budget))
    return out


def _lines(text: str) -> list[_Frag]:
    """Split *text* on ``\\n`` into fragments that rejoin to it exactly.

    Only ``\\n`` is a boundary. ``str.splitlines`` would also break on ``\\v``,
    ``\\f`` and ``\\u2028``, offering cut points no chat platform renders as a
    line break. A ``\\r\\n`` line keeps its ``\\r``; fence matching strips it.
    """
    frags: list[_Frag] = []
    start = 0
    while start < len(text):
        end = text.find("\n", start)
        line = text[start:] if end < 0 else text[start : end + 1]
        frags.append((line, line, True))
        start += len(line)
    return frags


def _advance(fence: _Fence | None, line: str) -> _Fence | None:
    """The fence state after *line*, given *fence* was open before it."""
    body = line[:-1] if line.endswith("\n") else line
    if body.endswith("\r"):
        body = body[:-1]
    if fence is not None:
        # Fence content is opaque: only a long-enough run of the SAME character
        # closes the block, so a ``` line inside a ````diff block stays content.
        m = _CLOSE_RE.match(body)
        if m and m.group(1)[0] == fence.char and len(m.group(1)) >= fence.length:
            return None
        return fence
    m = _BACKTICK_OPEN_RE.match(body) or _TILDE_OPEN_RE.match(body)
    if m:
        return _Fence(char=m.group(1)[0], length=len(m.group(1)), opener=body)
    return None


def iter_fence_spans(text: str) -> Iterator[tuple[int, int]]:
    """Yield ``(start, end)`` character spans of *text* inside a fenced code block.

    Each span covers the opener line through the end of the closer line, so
    everything a fence encloses -- and the delimiter lines themselves -- falls
    inside one. A fence left open runs to the end of *text*: content after a
    dangling opener renders as code. Spans are yielded in order and never overlap.

    This is the whole-text view of the same machine :func:`split_markdown_safe`
    runs on: both drive :func:`_advance`, so the open/close rule -- which run
    length closes which fence character -- exists once. A consumer that needs to
    know "is this offset inside code?" uses this instead of re-deriving the rule,
    because a second spelling of it diverges on the next CommonMark fix. Line
    boundaries come from :func:`_lines` for the same reason.
    """
    fence: _Fence | None = None
    open_at = 0
    pos = 0
    # _lines rejoins to `text` exactly, so `pos` tracks true character offsets
    # and reaches len(text) on the final line -- no clamping needed.
    for line, _full, _terminated in _lines(text):
        line_start = pos
        pos += len(line)
        after = _advance(fence, line)
        if fence is None and after is not None:
            open_at = line_start
        elif fence is not None and after is None:
            yield open_at, pos
        fence = after
    if fence is not None:
        yield open_at, len(text)


def iter_fence_lines(text: str) -> Iterator[tuple[str, str]]:
    """Yield ``(line, role)`` for every logical line of *text*.

    ``line`` is the line without its terminator (``\\n``, and a ``\\r`` before
    it). ``role`` is one of :data:`FENCE_OUTSIDE`, :data:`FENCE_OPEN`,
    :data:`FENCE_BODY`, :data:`FENCE_CLOSE`.

    This is the per-LINE view of the machine :func:`iter_fence_spans` views as
    character spans, and it exists for the one thing spans cannot answer: which
    lines are the delimiters. A channel whose own markup has a single code
    marker and no info string (WhatsApp: always ```````, never
    `````python``) has to REWRITE the delimiter lines while
    leaving the content between them byte-exact, and telling those apart from a
    span plus offsets is ambiguous for a fence left open -- its last line is
    content, but it sits where a closer would. Deriving it from a second fence
    regex is what this module exists to prevent.

    A fence left open yields no :data:`FENCE_CLOSE`, which is how a caller
    detects it: the block is unterminated and the caller owns what to append.
    """
    fence: _Fence | None = None
    for line, _full, _terminated in _lines(text):
        body = line[:-1] if line.endswith("\n") else line
        if body.endswith("\r"):
            body = body[:-1]
        before = fence
        fence = _advance(fence, line)
        if before is None and fence is not None:
            role = FENCE_OPEN
        elif before is not None and fence is None:
            role = FENCE_CLOSE
        elif fence is not None:
            role = FENCE_BODY
        else:
            role = FENCE_OUTSIDE
        yield body, role


def _safe_cut(fence: _Fence | None, frag: str, room: int) -> int:
    """The widest cut at or below *room* that invents no fence line on EITHER side.

    A hard cut splits one logical line across a chunk boundary, and BOTH halves
    then start a rendered line the receiver applies the fence grammar to:

    * the prefix ends its own chunk — ``"```abc"`` cut out of ``"```abc`rest"``
      is a valid opener while the whole line is not, and a ``"```"`` cut out of
      longer content closes an open block early, leaving the chunk's own
      synthetic closer to read as a fresh opener;
    * the remainder opens the NEXT chunk — ``"aaaaa```x"`` cut at five emits a
      remainder ``"```x"`` that opens a block the source line never contained
      (its run is mid-line prose), and inside a fence a remainder that is
      nothing but a long enough run closes a block its own line does not.

    So a candidate is accepted only when neither half moves the fence state, and
    the widest such candidate wins. The reference is the state BEFORE the line,
    never the line's own transition: for a line still arriving that transition is
    revocable, so keying the cut on it would move an already-sealed boundary once
    the rest of the line landed.

    The remainder is judged by its FIRST character rather than parsed, and that
    is what keeps prefix stability. A delimiter line must begin with indent or
    the run itself, so any other character rules the remainder out for good,
    whatever arrives after it. Parsing it would instead read text that is still
    growing, where the verdict flips as it grows — ``"```x"`` is an opener until
    a fourth backtick disqualifies it, and ``"``"`` is inert until a third
    backtick completes a run — which would move a cut already sealed one
    character earlier.

    A run of fewer than three backticks or tildes can never be a delimiter, so
    widths 1 and 2 always clear the prefix test, and the fallback therefore lands
    at one or more characters: the caller's forward-progress guarantee survives.
    The caller tells the two answers apart by re-testing the returned width's
    remainder, and reaches for the fallback only for a line too long to place
    whole. *room* is below ``len(frag)``, so the remainder always has a first
    character to test.
    """
    widest = 0
    for width in range(room, 0, -1):
        if _advance(fence, frag[:width]) is not fence:
            continue
        widest = widest or width
        if frag[width] not in _DELIM_LEAD:
            return width
    return widest


def _seal_cost(fence: _Fence | None) -> int:
    return fence.seal_cost if fence else 0


def _seal(reopen: str, buf: list[_Frag], fence: _Fence | None) -> str:
    """Render the buffered fragments as one finished chunk."""
    body = reopen + "".join(f for f, _, _ in buf)
    if fence is None:
        return body.rstrip()
    # Inside a fence trailing whitespace is content, so it survives; only the
    # closer is added, on its own line.
    if not body.endswith("\n"):
        body += "\n"
    return body + fence.closer


def _boundary(buf: list[_Frag], states: list[_Fence | None], cap: int, base: int) -> int:
    """How many buffered fragments to seal when cutting outside a fence.

    Returns at least 1. Prefers the last paragraph break sitting at least
    halfway into the budget; otherwise seals everything buffered, minus a
    trailing pipe-bearing line when an earlier cut is nearby.

    That last rule reads only buffered text on purpose. Checking whether the
    NEXT line is a separator row would identify table headers exactly, but it
    would make this cut depend on text after it — and a prefix arriving
    mid-line would then produce a different chunk, breaking the streaming
    contract that outranks table cosmetics. Pulling back any table-ish trailing
    line keeps a header with its separator, at the price of an occasionally
    early cut on prose that merely contains a pipe.
    """
    chars = base
    para = 0
    for i in range(len(buf) - 1):
        chars += len(buf[i][0])
        # A blank line inside a fence is code, not a paragraph break.
        if not buf[i][0].strip() and states[i] is None and chars >= cap // 2:
            para = i + 1
    if para:
        return para
    last = buf[-1][0]
    if len(buf) > 1 and "|" in last and not _is_table_separator(last):
        head = base + sum(len(f) for f, _, _ in buf[:-1])
        if head >= cap // 4:
            return len(buf) - 1
    return len(buf)


def _is_table_separator(line: str) -> bool:
    """True if *line* is a GFM table separator row (``| --- |``, ``---|---``).

    Deliberately loose: this only nudges a cut point, so over-matching costs a
    slightly earlier cut and never corrupts output.
    """
    s = line.strip()
    return bool(s) and set(s) <= _TABLE_SEP_CHARS and "-" in s and "|" in s
