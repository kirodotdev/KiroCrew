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
from collections.abc import Iterator
from dataclasses import dataclass

__all__ = [
    "split_markdown_safe",
    "split_markdown_bytes",
    "chunk_utf8_bytes",
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


def split_markdown_safe(text: str, limit: int, *, reserve: int = 0) -> list[str]:
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
    line is placed whole whenever the LINE ITSELF is no longer than ``limit``,
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
    """
    if not text:
        return []
    cap = limit - reserve
    if limit <= 0 or cap <= 0 or len(text) <= cap:
        return [text]

    out: list[str] = []
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
        # arithmetic branch that skipped this was how the ladder below used to be
        # bypassed at budgets a fence's scaffolding consumes whole. Both read
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
            cut = width

        if not cut:
            # Take the fragment whole: either it fits, or no cut is clean on both
            # sides and the line it came from is no longer than the caller's full
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
    return out


def chunk_utf8_bytes(text: str, max_bytes: int) -> list[str]:
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
    """
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
