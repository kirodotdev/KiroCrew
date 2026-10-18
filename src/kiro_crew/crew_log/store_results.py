"""What a read of a crew log returns: pages, ref resolutions and resumable positions.

``Page`` is one page, newest first, bounded by ``MAX_PAGE_LIMIT`` and defaulting to
``DEFAULT_PAGE_LIMIT``; ``Resolution`` and its ``STATUS_*`` outcomes answer a followed
``Ref``; ``ReadCursor`` and ``ResumePoint`` are a walk's position a caller continues from
later; ``_as_ref`` and ``_covers_span`` are the checks ``resolve`` makes on a span. Values
only: the reads that produce them are ``CrewLog``'s.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kiro_crew.crew_log.schema import Entry, Ref

#: Largest page a single read may materialize.
MAX_PAGE_LIMIT = 500
DEFAULT_PAGE_LIMIT = 50

#: ``resolve`` outcomes. There is no ``forbidden``: this layer claims no
#: authorization, so it has none to deny (see :meth:`CrewLog.resolve`).
STATUS_OK = "ok"
STATUS_GONE = "gone"
#: The cited span reaches BELOW the oldest surviving segment: retention removed
#: it. A normal answer -- the citing entry stays honest about having pointed there.
STATUS_PRUNED = "pruned"
#: The cited span lies inside a segment that still exists, but lines in it could
#: not be read. That is DAMAGE, not retention, and it must never be reported as
#: either ``gone`` or ``pruned``: a reader told "retention" stops looking, while a
#: reader told "corrupt" knows the file it still has is not intact.
STATUS_CORRUPT = "corrupt"


@dataclass(frozen=True)
class ResumePoint:
    """Where a walk of one unit's log stopped, as a position a later walk can seek to.

    What it buys is the difference between a tail read that costs the TAIL and one
    that costs the LOG. :meth:`CrewLog.iter_from` yields from a seq, and reaching that
    seq means decoding every record below it -- so a reader that polls a growing log
    for its newest entry decodes the whole of it on every poll, however little
    arrived. Handed one of these, the same walk opens the file at the record it
    already knows and decodes only what came after.

    Every field is part of the proof, and a walk that cannot confirm one of them walks
    the log from the top instead:

    ``segment`` is the FILE NAME, not a path, because a unit's history is several
    files and a byte offset means nothing without the one it indexes. A name absent
    from disk -- retention dropped it -- is not an error, it is a resume point that
    has stopped applying.

    ``offset`` is where the record carrying ``seq`` BEGINS. It must be a record
    boundary, and that is checked by reading there and requiring the framed record to
    start at exactly this value: an offset landing inside a record yields the
    remainder, which does not parse, and the next record then starts above the
    offset.

    ``seq`` is what the record at ``offset`` must carry. This is the positive check --
    the one that says the bytes at the offset are the bytes the reader thinks they
    are, rather than merely that something parses there. It also ANCHORS the walk's
    "every seq must advance" rule: the resumed walk's first record is this one, so
    every record after it is still compared against a known predecessor.

    ``cuts`` is :meth:`CrewLog.cuts` as it read when the offset was taken. A cut is
    the only thing in this store that moves a committed record, so an unchanged count
    is what says the bytes BELOW the offset -- which the resumed walk does not read --
    are the bytes they were. ``None`` is not proven and never resumes: a log with no
    counter has no evidence for its prefix, so it gets the full walk.
    """

    segment: str
    offset: int
    seq: int
    cuts: int | None


@dataclass
class ReadCursor:
    """Where the walk a caller is driving has reached, for it to remember.

    Mutable and passed IN, because :meth:`CrewLog.iter_from` is a generator and a
    generator's return value is not reachable by a caller that iterates it. The same
    shape :class:`~kiro_crew.crew_log.projection._SlotStream` already uses for the
    heights it reports: the walk fills it as it goes, and the caller reads it when the
    walk is done.

    ``at`` names the last entry the walk YIELDED, not the last record it framed. That
    is the position a continuation wants: a reader resumes to find what it has not
    folded, and a record it skipped -- an ignorable type it does not know, a damaged
    line -- is one it has not folded either, so a point past it would hide it from
    every later read.
    """

    at: "ResumePoint | None" = None


@dataclass(frozen=True)
class Page:
    """One page of entries, NEWEST first.

    ``next_before`` is the cursor for the following page, or ``None`` when this
    page reached the oldest entry. It is set only when an older entry actually
    exists, so paging never hands back a phantom empty page at the end.
    """

    entries: tuple[Entry, ...]
    next_before: int | None


@dataclass(frozen=True)
class Resolution:
    """The outcome of following a :class:`~kiro_crew.crew_log.schema.Ref`."""

    status: str
    entries: tuple[Entry, ...]

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def _as_ref(ref: Ref | dict[str, Any]) -> Ref:
    """*ref* as a :class:`Ref`, accepting either spelling a caller may hand in.

    Every entry point that takes a ref accepts both a built ``Ref`` and its wire
    form, and coerces here, so the two spellings cannot disagree about what a
    valid ref is: ``Ref.from_dict`` runs the same ``__post_init__`` checks a
    direct constructor call goes through.
    """
    return ref if isinstance(ref, Ref) else Ref.from_dict(ref)


def _covers_span(found: tuple[Entry, ...], from_seq: int, last_seq: int) -> bool:
    """Whether *found* covers ``from_seq..last_seq`` whole, each seq exactly once.

    *found* MUST already be drawn from that span -- the caller's walk bounds it --
    so seq being contiguous inside a file makes the two counts settle it, and this
    does not re-check membership.

    The test is COVERAGE of the span, and a duplicate is damage in its own right.
    Seq is unique under the append lock, so two lines claiming one seq cannot both
    be the writer's -- and a tally of LINES would let that duplicate fill the place
    of a line that is gone, reading a span with a hole in it as intact. Counting
    distinct seqs catches that substitution; requiring the two counts to agree
    catches the duplicate even when nothing is missing, which is a file a reader
    must not be told is intact.
    """
    covered = {entry.seq for entry in found}
    return len(covered) >= max(0, last_seq - from_seq + 1) and len(covered) == len(found)
