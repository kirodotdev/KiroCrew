"""Which session HOLDS a referenced pull request -- decided on the creator tree.

:mod:`kiro_crew.crew_log.projection` folds ONE log and reads nothing else (RFC
FR-4). :mod:`kiro_crew.crew_log.session_tree` is the reader that looks across logs for
the creator edge. This module is the second such reader, and it exists because
the question consumers actually ask is not answerable from either one alone:
"which session is holding pull request X" needs the references (this module's
scan) AND the lineage (the tree's fold), combined under one rule.

The rule this module owns, and why recency alone is wrong
--------------------------------------------------------

A reader that scans every log for a reference and takes the session with the
NEWEST mention gets the wrong answer whenever a conductor is involved. A
conductor names the pull request every time it checks on the worker it
dispatched through ``session_create``, so by recency it owns every row it
supervises and the worker actually holding the work is never named. The mention
count does not rescue it either: a conductor that patrols on a timer
out-mentions the worker that pushed twice.

So the candidates are ranked only after the lineage has been applied:

1. The candidates for a reference are the sessions whose text NAMED it.
2. Any candidate that is an ANCESTOR of another candidate is dropped. A
   conductor supervising a worker that named the same pull request is that
   worker's ancestor, so it leaves the running. A conductor that named a pull
   request NO descendant of its own named is not dropped -- it is then the only
   session that knows about it, and reporting nobody would be worse than
   reporting the session that actually spoke.
3. The newest mention among the survivors wins, and ties fall to the larger
   count and then to the slot key, so two scans of the same files agree.

Ancestry is the TREE's relation, not a second opinion about it. An edge is
followed only where :func:`~kiro_crew.crew_log.session_tree.fold_tree` followed it --
onto a slot with a log of its own -- and a slot the fold marked as lying on a
cycle is never walked through. That is the whole reason this module imports the
tree rather than re-reading ``session/opened``: two readers with two folds would
disagree about who created whom, and a person switching between the pages that
use them would see two answers.

The answer carries the owner's CITED creator (:attr:`Holder.parent_slot`) even
where the tree could not follow that citation, for the same reason
:class:`~kiro_crew.crew_log.session_tree.TreeNode` retains it: the citation is the
child's own record, not the fold's verdict on it.

What counts as a reference
--------------------------

Text entries only -- ``message/received.text``, ``message/sent.text`` and
``message/chunk.delta``. Tool arguments are hashed in this store, so a pull
request named only inside a tool call is not recoverable from the log and is
deliberately not guessed at.

ONE spelling is read: the full pull-request URL, whose ``/pull/`` path is what
says the number is a pull request. Both short forms are refused. A bare
``#number`` names no repository, and numbers are per-repository, so it would join
a session to whichever repository happened to share it -- the same defect Issue
Radar's per-repository queue shard exists to prevent. ``owner/repo#number`` names
a repository and still does not say what the number IS: the forge spells an issue
and a pull request identically that way, and nothing in a log distinguishes them,
so reading it would hand a holder to an issue. A wrong kind, like a wrong owner,
is worse than no answer, because a caller cannot tell a confident wrong answer
from a right one.

Owner and repository are lowercased in the reference's own constructor, since a
repository's identity is case-insensitive at the forge. A reference is also
stitched across an oversize body's slices: a body too big for one line is written
as a run of ``message/chunk`` entries, and a URL straddling two of them is in
neither, so each slice carries the tail of the one before it. Both rules are
argued where they are implemented (:class:`Reference`, :func:`_scan_entry`).

Bounds
------

Unlike the tree, this reader cannot answer from a log's head: a reference can be
named on any line. It is bounded instead by RESUMING. Each segment's read
position is cached with that segment's identity, so an untouched segment costs
one ``stat`` and a live session's appends cost only the bytes appended --
:data:`SCAN_BYTES_PER_SEGMENT` of them per scan, so a cold log of any size is
absorbed over several scans instead of blocking one. A scan that left bytes
unread says so in the READING it returns (:class:`ReferenceReading`,
:class:`HolderReading`), so a partial answer never
reads as a complete one.

The cache is validated the way the tree's head cache is, and for the same
reasons: the store never rewrites a written line, so bytes already read are
immutable while the segment exists, and ``(st_dev, st_ino)`` alone is not an
identity because a filesystem hands a freed inode number to the next file it
creates. A segment SHORTER than the position we read to is not the file we read,
whatever its inode says.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any, Final

from kiro_crew.crew_log.errors import CrewLogError
from kiro_crew.crew_log.schema import KIND_SESSION, MAX_ENTRY_BYTES, Entry
from kiro_crew.crew_log.session_tree import (
    TREE_UNIT_CAP,
    SessionTree,
    TreeNode,
    header_unreadable,
)
from kiro_crew.crew_log.store import (
    oldest_segment,
    read_head,
    segment_first_seqs,
    segment_paths,
    unit_dir_for,
    unit_dirs,
)
from kiro_crew.jsonl_util import UnreadableRecord, strict_raw_records
from kiro_crew.session_ledger import _store_name
from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN, MAX_SHORT_STRING

logger = logging.getLogger(__name__)

#: Session-log units one scan admits. The tree's bound, deliberately the same
#: constant: the two readers are folded together into one answer, so a unit the
#: tree admitted and this scan did not would be a candidate with no lineage, and
#: the ancestor rule would silently stop applying to it.
REFERENCE_UNIT_CAP: Final[int] = TREE_UNIT_CAP

#: The entry types a reference is read from -- the text a person or a model
#: actually wrote. ``message/queued`` carries a size and no body, and every other
#: entry type carries structure rather than prose.
TEXT_TYPES: Final[frozenset[str]] = frozenset({"message/received", "message/sent", "message/chunk"})

#: The slice type an oversize body is written as. Named because the reader has to
#: tell a slice from the entry that ENDS a run of them: a run with nothing after
#: it is one the store itself calls unreachable and drops, so folding it would
#: report a holder for a message that has no record at all.
CHUNK_TYPE: Final[str] = "message/chunk"

#: The ``data`` fields those types carry text in.
TEXT_FIELDS: Final[tuple[str, ...]] = ("text", "delta")

#: Distinct references RETAINED per segment. A reference is retained for as long
#: as its segment exists, so this bounds the cache and not just one answer. Past
#: it a further distinct reference in that segment is counted in
#: :attr:`_SegmentState.omitted` and dropped, so the cache stays bounded and a
#: reader is told the segment's set is a window rather than all of it.
REFS_PER_SEGMENT_CAP: Final[int] = 512

#: Bytes of NEW record data one scan reads from one segment. The resume position
#: advances by what was read, so the rest arrives on the next scan rather than
#: making the first one pay for the whole file. Sized so an ordinary live session
#: is fully absorbed in one pass while a cold multi-megabyte log cannot stall a
#: sampler tick.
SCAN_BYTES_PER_SEGMENT: Final[int] = 4 * 1024 * 1024

#: Characters of one text field scanned for references. The store writes a body
#: past one line as ``message/chunk`` entries, each scanned in its own right, so
#: this costs a reference only when a SINGLE entry carries more prose than this --
#: and the alternative, running a regex over unbounded text per entry, is the cost
#: this bound exists to refuse.
TEXT_SCAN_LIMIT: Final[int] = 64 * 1024

#: The largest number read as a pull-request number. Past this the digits are not
#: a number any forge issued, and a reference keyed on them would be a key that
#: matches nothing while still costing a cache slot.
MAX_PR_NUMBER: Final[int] = 10_000_000

#: Longest owner and repository names kept in a reference. GitHub's own limits (a
#: login is at most 39 characters, a repository name at most 100), so nothing a
#: forge issued is refused by them.
#:
#: They are a RETENTION bound, not a parsing nicety. A reference is cached for as
#: long as its segment exists, and the cache bounds the COUNT of references per
#: segment; without a bound on each name, one entry of untrusted prose spelling
#: `("a"*30000)/("b"*30000)#1` is a single reference weighing 60 KiB, and a
#: segment's worth of them would pin tens of megabytes in a scanner every reader
#: in the process shares. The check is made before the reference is built, beside
#: the digit bound, so nothing oversized is ever constructed.
MAX_OWNER_CHARS: Final[int] = 39
MAX_REPO_CHARS: Final[int] = 100

#: How many digits :data:`MAX_PR_NUMBER` has. DERIVED, so raising that constant
#: cannot leave this one behind. A run longer than this is refused before
#: ``int()`` sees it: see :func:`parse_references` for why the refusal has to
#: come first rather than after the number comparison.
_MAX_PR_DIGITS: Final[int] = len(str(MAX_PR_NUMBER))

#: A pull request named as a full URL -- the ONE accepted spelling. ``www.`` is
#: accepted because a pasted link carries it. The ``/pull/`` path is what makes
#: the number a pull request rather than some other numbered thing in the same
#: repository: see :func:`parse_references`.
_URL_RE: Final[re.Pattern[str]] = re.compile(
    # The scheme must BEGIN its own token. The pattern is applied to arbitrary
    # prose and matches anywhere in it, so without this a token character glued
    # in front -- `xhttps://github.com/o/r/pull/42`, an ordinary typo -- matches
    # one character in and names a pull request the text does not address. Same
    # refused class as the boundary after the digits, stated once for both ends:
    # a character that CONTINUES a token means the scheme does not start one. A
    # link's real leads are all outside that class -- a space, `(` from a markdown
    # link, `<`, a quote, the start of the text.
    r"(?<![0-9A-Za-z_~%-])"
    # Scheme and host are case-INSENSITIVE per RFC 3986, so `HTTPS://GitHub.com/...`
    # addresses the same pull request and a case-sensitive match drops it from an
    # answer that still calls itself complete. The flag is scoped to that prefix:
    # the path keeps its own semantics, and owner and repo are lowercased for
    # identity where the reference is built, not here.
    r"(?i:https?://(?:www\.)?github\.com/)([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/pull/(\d+)"
    # The digits must END their own path token. Without this, the pattern stopped
    # wherever the digits stopped, so `/pull/123abc` -- which addresses no pull
    # request at all -- read as a reference to 123, and one line of untrusted
    # prose fabricated a holder for somebody else's work. The refused characters
    # are the ones that CONTINUE a token: letters, digits, `_`, `-`, `~` and the
    # `%` of an escape. Everything else ends it and is not read, which keeps
    # `/pull/123/files`, `/pull/123#discussion_r1`, `/pull/123.diff` and a URL at
    # the end of a sentence or inside brackets. Written as a lookahead so the
    # digit run cannot be re-cut shorter to satisfy it: a following digit is
    # refused too, so a run longer than `_MAX_PR_DIGITS` still arrives WHOLE at
    # the bound that refuses it rather than being truncated into a valid number.
    r"(?![0-9A-Za-z_~%-])"
)

#: Characters of one text slice carried forward into the next. A body too large
#: for one line is written as a run of ``message/chunk`` entries, so a URL can
#: straddle any two of them; scanning each slice alone loses it. Longer than the
#: longest reference the bounds above admit -- scheme and host (at most 30),
#: owner, ``/``, repo, ``/pull/``, and the digits -- so no straddling reference
#: can be longer than the overlap that has to catch it.
REFERENCE_STITCH_CHARS: Final[int] = (
    30 + MAX_OWNER_CHARS + 1 + MAX_REPO_CHARS + len("/pull/") + _MAX_PR_DIGITS
)


@dataclass(frozen=True)
class Reference:
    """One pull request, as a repository and a number.

    The repository is part of the identity: numbers are per-repository, so two
    forges' ``#1`` are two references, and folding them together would join a
    session to work it never touched.

    Owner and repository are LOWERCASED on construction. A repository's identity
    is case-insensitive at the forge, so ``Owner/Repo`` and ``owner/repo`` are one
    repository, and a case-sensitive identity gets it wrong in both directions at
    once: two casings in the prose fold as two references with the holder split
    between them, and a caller spelling it a third way finds no holder for a
    reference that was found. Doing it HERE rather than in the parser and again in
    each lookup is what makes it structural -- no call site is left that could
    forget, and the failure it prevents is silent.
    """

    owner: str
    repo: str
    number: int

    def __post_init__(self) -> None:
        """Lowercase the repository halves of the identity.

        ``object.__setattr__`` because the dataclass is frozen. This runs before
        the value is ever hashed or compared, so no set or dict can hold a
        reference in its pre-normalised form.
        """
        object.__setattr__(self, "owner", self.owner.lower())
        object.__setattr__(self, "repo", self.repo.lower())

    @property
    def url(self) -> str:
        """The canonical URL for this reference."""
        return f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}"


@dataclass(frozen=True)
class Mention:
    """What ONE session's log says about one reference: when it last named it,
    and how many times.

    A mention is evidence, not ownership. :func:`fold_holders` is what turns a
    reference's mentions into the session that holds it.
    """

    slot: str
    sid: str
    newest_ms: int
    count: int


@dataclass(frozen=True)
class Holder:
    """The session that holds a reference, and the evidence it was chosen on.

    ``parent_slot`` is the owner's CITED creator, retained even where the tree
    could not follow the citation (an orphan, a cycle) -- it is the owner's own
    record. ``newest_ms`` and ``count`` are the OWNER's, not the reference's
    totals across every session: a consumer showing "last touched" must show when
    the holder last touched it, or a conductor's patrol sets the worker's
    timestamp.
    """

    slot: str
    sid: str
    parent_slot: str | None
    newest_ms: int
    count: int


@dataclass(frozen=True)
class ReferenceReading:
    """One scan's mentions, and whether that scan saw the whole store.

    The two travel together on purpose. ``incomplete`` is true when a per-segment
    byte budget was reached, a segment's reference cap was hit, or the store held
    more session logs than :data:`REFERENCE_UNIT_CAP` admits -- three ways of
    saying the same thing to a consumer, which is that this is not the whole
    store and reading again will see more.

    A scanner instance is shared by every reader in the process, so a flag left on
    the scanner would be readable by a second, unlocked call: another reader's
    scan can land between a caller's read of the data and its read of the flag,
    and the caller would then render a truncated answer as a complete one. Pairing
    them in one immutable value makes that impossible rather than discouraged.
    """

    mentions: dict[Reference, list[Mention]]
    incomplete: bool


@dataclass(frozen=True)
class HolderReading:
    """One scan's owning session per reference, and that scan's completeness.

    ``incomplete`` carries the same meaning, and travels for the same reason, as
    :class:`ReferenceReading`'s.
    """

    holders: dict[Reference, Holder]
    incomplete: bool


def parse_references(text: str) -> set[Reference]:
    """Every pull request *text* names, in the one accepted spelling. Pure.

    Scans at most :data:`TEXT_SCAN_LIMIT` characters. A number past
    :data:`MAX_PR_NUMBER` is not read: see that constant.

    Only the full URL is read, because only its ``/pull/`` path says the number is
    a pull request. The forge spells an issue and a pull request identically in the
    ``owner/repo#number`` short form, and nothing in a log can tell them apart --
    only an API call this reader must not make could. Reading that form would hand
    a holder to an issue, which is a holder for a pull request that does not exist,
    and a wrong kind is worse than no answer for the same reason a bare ``#number``
    is not read: a caller cannot tell a confident wrong answer from a right one.

    The digit RUN is bounded before conversion, not after. ``int()`` on a decimal
    string past CPython's int-str conversion limit raises ``ValueError`` rather
    than returning a large number, and a run of thousands of digits is ordinary
    text a person can paste. The scan is on a code path with no per-record guard
    for that, so the raise would escape to the top of a scan, empty the answer,
    and -- because a read position only advances on records that were consumed --
    meet the same record on every later scan. A digit run longer than
    :data:`MAX_PR_NUMBER` has cannot be a pull request anyway, so the bound costs
    nothing that the number check was not already refusing.
    """
    if not text:
        return set()
    window = text[:TEXT_SCAN_LIMIT]
    found: set[Reference] = set()
    for owner, repo, digits in _URL_RE.findall(window):
        if len(digits) > _MAX_PR_DIGITS:
            continue
        if len(owner) > MAX_OWNER_CHARS or len(repo) > MAX_REPO_CHARS:
            continue
        number = int(digits)
        if 0 < number <= MAX_PR_NUMBER:
            found.add(Reference(owner=owner, repo=repo, number=number))
    return found


def _chunk_key(entry: Entry) -> tuple[int, int] | None:
    """The oversize-body run *entry* belongs to, or ``None`` if it is not part of
    one.

    A ``message/chunk`` entry carries no group id. What identifies its run is that
    the store writes a body's slices as ONE contiguous batch (see
    ``crew-log-core`` section on ``append_many``), all with the same ``turn`` and
    ``step``. So contiguity plus that pair is the run, and the citing entry the
    batch ends with is of another type, which closes the run behind it.

    A chunk whose ``turn`` is missing or not an integer returns ``None``: its run
    cannot be established, and a stitch across an unestablished boundary could
    join two unrelated bodies.
    """
    if entry.type != "message/chunk":
        return None
    turn = entry.data.get("turn")
    if not isinstance(turn, int) or isinstance(turn, bool):
        return None
    step = entry.data.get("step", 0)
    if not isinstance(step, int) or isinstance(step, bool):
        step = 0
    return (turn, step)


def _scan_entry(entry: Entry, state: _SegmentState) -> set[Reference]:
    """Every pull request one entry's TEXT names, stitching across an oversize
    body's slices, and updates *state*'s carry.

    The caller has already refused an entry whose type is not being scanned. That
    set is :data:`TEXT_TYPES`: an entry's ``data`` can hold a string that looks
    like a link without anyone having written it as prose, because tool arguments
    are hashed in this store, so a link found outside a text entry is not read.

    A body too large for one line is written as a run of ``message/chunk``
    entries, so a URL can straddle any two slices and neither slice contains it.
    Each slice therefore also scans the SEAM: the tail carried from the slice
    before, plus this slice's head. Only references that need BOTH sides are taken
    from it -- a reference lying wholly inside either side was already counted by
    that side's own scan, and counting it twice would inflate the mention count the
    fold breaks ties on.

    The carry is reset for every entry that does not continue the run it was taken
    from. Two unrelated bodies joined end to end could spell a reference neither of
    them contains, and a fabricated holder is as wrong as a lost one.
    """
    key = _chunk_key(entry)
    if key is None or key != state.carry_key:
        state.carry = ""
    state.carry_key = key
    found: set[Reference] = set()
    for name in TEXT_FIELDS:
        value = entry.data.get(name)
        if not isinstance(value, str) or not value:
            continue
        window = value[:TEXT_SCAN_LIMIT]
        if key is not None and state.carry:
            head = window[:REFERENCE_STITCH_CHARS]
            straddling = parse_references(state.carry + head)
            straddling -= parse_references(state.carry)
            straddling -= parse_references(head)
            found |= straddling
        found |= parse_references(window)
        if key is not None:
            state.carry = window[-REFERENCE_STITCH_CHARS:]
    return found


def is_ancestor(
    candidate: str, of: str, nodes: Mapping[str, TreeNode], *, depth: int | None = None
) -> bool:
    """Whether *candidate* is an ancestor of *of* in the tree *nodes*.

    Follows an edge under exactly the tree's rules: only onto a slot that HAS a
    node (a log of its own), and never through a slot the fold marked as lying on
    a cycle. A slot is not its own ancestor.

    The walk is bounded by the NUMBER OF NODES, which is the length of the longest
    simple path there can be. A fixed floor is the wrong bound in the one direction
    that matters: a chain deeper than it stops the walk, the candidate is not
    recognised as an ancestor, and it survives the filter to win on recency -- a
    wrong holder on an answer that still calls itself complete. Repeats are already
    refused by ``seen``, so the count cannot be spent circling.
    """
    if candidate == of:
        return False
    cursor = nodes.get(of)
    seen: set[str] = {of}
    for _ in range(len(nodes) if depth is None else depth):
        if cursor is None or cursor.cycle or cursor.parent_slot is None:
            return False
        parent = cursor.parent_slot
        upward = nodes.get(parent)
        if upward is None:
            # A cited creator with no log of its own: a citation, not a place in
            # the tree. The chain ends here, exactly as the fold ends it -- and
            # the test is made BEFORE *parent* is compared to *candidate*,
            # because a slot the tree could not follow onto is not an ancestor
            # even when it is the slot being asked about.
            return False
        if parent == candidate:
            return True
        if parent in seen:
            return False
        seen.add(parent)
        cursor = upward
    return False


def fold_holders(
    mentions: Mapping[Reference, Iterable[Mention]], nodes: Mapping[str, TreeNode]
) -> dict[Reference, Holder]:
    """The owning session per reference. Pure -- this is THE ownership rule.

    See the module docstring for why the lineage is applied before recency. In
    short: drop every candidate that is an ancestor of another candidate, then
    take the newest of what is left, breaking ties on count and then slot so two
    scans of the same files agree.

    A reference nobody mentioned has no entry. Every candidate being dropped
    cannot occur: ancestry is a strict order on a finite set, so at least one
    candidate is maximal.
    """
    out: dict[Reference, Holder] = {}
    for reference, candidates in mentions.items():
        rows = [row for row in candidates if row.slot]
        if not rows:
            continue
        slots = {row.slot for row in rows}
        survivors = [
            row
            for row in rows
            if not any(is_ancestor(row.slot, other, nodes) for other in slots if other != row.slot)
        ]
        if not survivors:  # pragma: no cover -- a strict order always leaves a maximum
            survivors = rows
        winner = max(survivors, key=lambda row: (row.newest_ms, row.count, row.slot))
        node = nodes.get(winner.slot)
        out[reference] = Holder(
            slot=winner.slot,
            sid=winner.sid,
            parent_slot=node.parent_slot if node is not None else None,
            newest_ms=winner.newest_ms,
            count=winner.count,
        )
    return out


@dataclass
class _SegmentState:
    """One segment's cached read position, identity, and what it contributed.

    ``offset`` is always a position at a RECORD BOUNDARY: a frame with no
    terminator is the tail of an append in flight, and counting it would resume
    the next scan inside a line. ``size`` is the floor the append-only guarantee
    gives us -- a shorter file is not this file, whatever its inode says.
    """

    dev: int
    ino: int
    offset: int
    size: int
    refs: dict[Reference, tuple[int, int]] = field(default_factory=dict)
    omitted: int = 0
    #: The highest seq this segment has shown, and the first-seq from its NAME.
    #: Together they tell a gap at the FRONT of the sequence (retention, and the
    #: entries are gone from the store, so an answer without them is complete)
    #: from a gap in the MIDDLE (damage: the entries around it are still here, so
    #: the sequence itself says some are absent). The seq is read from the records
    #: this scan already frames rather than computed from a count, because only
    #: the records know how the writer numbers a header.
    max_seq: int = 0
    first_seq: int = 0
    #: Set when a record in this segment could not be delivered intact. The abort
    #: is repeatable -- it clears and re-faults on every call -- so this changes no
    #: answer; it stops re-reading the bytes before the bad record on every scan
    #: for a file that cannot become readable, the log being append-only.
    unreadable: bool = False
    exhausted: bool = True
    #: The tail of the last oversize-body slice read, and the run it came from.
    #: Kept HERE rather than as a scan-local variable because a scan stops on its
    #: byte budget at a record boundary, and that boundary can fall between two
    #: slices of one body -- a scan-local carry would be dropped there and a URL
    #: straddling exactly that pair lost, on an answer the next scan reports as
    #: complete. A new state is built whenever the segment's identity fails to
    #: match, so a carry never outlives the file it was read from.
    carry: str = ""
    carry_key: "tuple[int, int] | None" = None
    #: An oversize body's slices whose citing entry has not been seen, accumulated
    #: APART from this segment's own references so they are in no answer until it
    #: is. Kept HERE rather than for the duration of one read because a body can be
    #: larger than a scan's whole byte budget -- rewinding to the run's first slice
    #: instead would make every scan re-read the same bytes and never reach the
    #: citing entry, so the group's references would be unreachable for good and
    #: every record after it too. The position advances past a held run; what is
    #: held is what it contributed, which is what must not be answered with.
    pending: "_SegmentState | None" = None

    def add(self, reference: Reference, moment: int) -> None:
        """Record one naming of *reference* at *moment*."""
        existing = self.refs.get(reference)
        if existing is not None:
            newest, count = existing
            self.refs[reference] = (max(newest, moment), count + 1)
        elif len(self.refs) < REFS_PER_SEGMENT_CAP:
            self.refs[reference] = (moment, 1)
        else:
            self.omitted += 1

    def absorb(self, other: "_SegmentState") -> None:
        """Fold a HELD run's accumulator into this state.

        A run of oversize-body slices is accumulated apart from the segment and
        folded only once the entry that cites it has been seen, so it is merged
        rather than undone -- there is no removing a reference from a capped dict
        without also unwinding what the cap refused while it was there.

        The same cap applies to the merge, so a run cannot get past a bound the
        records would have been held to one at a time.
        """
        for reference, (newest, count) in other.refs.items():
            existing = self.refs.get(reference)
            if existing is not None:
                self.refs[reference] = (max(existing[0], newest), existing[1] + count)
            elif len(self.refs) < REFS_PER_SEGMENT_CAP:
                self.refs[reference] = (newest, count)
            else:
                self.omitted += 1
        self.omitted += other.omitted
        self.max_seq = max(self.max_seq, other.max_seq)
        self.carry = other.carry
        self.carry_key = other.carry_key


@dataclass
class _UnitState:
    """One session log's segments, keyed by segment file name, plus the identity
    its header claimed.

    Segments retention removed are dropped whole, which costs the reader those
    entries and costs the format nothing.
    """

    sid: str
    slot: str
    segments: dict[str, _SegmentState] = field(default_factory=dict)


class ReferenceScanner:
    """The reference scan and its per-segment resume cache. BLOCKING: it lists
    directories and reads files, so call it off the event loop.

    One instance per reader. It OWNS a
    :class:`~kiro_crew.crew_log.session_tree.SessionTree` so both halves of an answer are
    folded from the same scan: a caller holding its own tree could hand this one a
    lineage taken at a different moment, and the ancestor rule would then be
    applied to a population that never existed together.
    """

    def __init__(self, tree: SessionTree | None = None) -> None:
        self._lock = threading.Lock()
        self._units: dict[str, _UnitState] = {}
        self._tree = tree if tree is not None else SessionTree()

    @property
    def tree(self) -> SessionTree:
        """The tree this scanner folds lineage from."""
        return self._tree

    def references(
        self,
        *,
        since: int | None = None,
        preferred: Iterable[str] = (),
    ) -> ReferenceReading:
        """Every pull request the store's session logs name, and who named it.

        *since* drops a mention older than that moment in milliseconds. It is
        applied on the way OUT, to a session's merged newest moment, so a session
        whose mentions all predate the window is not a candidate -- which is what
        makes a windowed answer usable for "who is holding this NOW" rather than
        "who ever mentioned it". It deliberately does not narrow what the CACHE
        stores: see :func:`_record`.

        The entry types read are always :data:`TEXT_TYPES` and there is no per-call
        narrowing of them, which is a deliberate subtraction rather than a gap: see
        the note below on why a narrowing would poison the shared cache. A caller
        that wanted to widen past those types would be reading hashed tool
        arguments under the name of prose, which this module's contract refuses
        outright.

        *preferred* names unit ids admitted FIRST, as the tree's scan does, so
        past the cap it is closed sessions' logs that go unread.

        ``incomplete`` comes back WITH the mentions rather than sitting on the
        scanner, and is computed under the same lock that produced them. A flag
        read afterwards would be a second, unlocked read: one instance serves
        every reader in the process, so another reader's scan can land in between
        and a truncated answer would then be handed over labelled complete --
        exactly the state this reader promises never to produce.
        There is deliberately no per-call narrowing of WHICH text entries are
        scanned. The cache stores what the bytes said and one scanner serves every
        reader in the process, so narrowing the scan would advance a read position
        past records the narrow call did not want and no later wider call could
        recover them -- one narrow call would permanently hide those references
        from every other reader and report the result as complete. Narrowing on the
        way out instead would need the cache keyed by entry type, tripling what it
        retains for a filter no consumer asks for. So :data:`TEXT_TYPES` is always
        what is read, and ``since`` -- which needs no extra key, because a moment
        is already stored per reference -- is the one window offered.
        """
        floor = since if isinstance(since, int) and not isinstance(since, bool) else None
        out: dict[Reference, list[Mention]] = {}
        with self._lock:
            admitted, over_cap, listing_faulted = self._admit(preferred)
            incomplete = over_cap or listing_faulted
            for directory in admitted:
                unit, faulted = self._scan_unit(directory, TEXT_TYPES)
                incomplete = incomplete or faulted
                if unit is None:
                    continue
                merged, unit_incomplete = self._merge(unit, floor)
                incomplete = incomplete or unit_incomplete
                for reference, (newest, count) in merged.items():
                    out.setdefault(reference, []).append(
                        Mention(slot=unit.slot, sid=unit.sid, newest_ms=newest, count=count)
                    )
            names = {directory.name for directory in admitted}
            for gone in [name for name in self._units if name not in names]:
                del self._units[gone]
        return ReferenceReading(mentions=out, incomplete=incomplete)

    def holders(
        self,
        *,
        since: int | None = None,
        preferred: Iterable[str] = (),
    ) -> HolderReading:
        """The owning session per referenced pull request, as one call.

        ``incomplete`` is BOTH scans': this module's references, and the tree's
        lineage. The tree half matters more here than anywhere else it is read.
        A lineage read that faulted drops one unit's creator record silently, so
        a conductor stops being recognised as its worker's ancestor, the ancestor
        rule stops dropping it, and the conductor -- which by recency almost
        always wins -- is reported as the holder. That is a confident WRONG owner,
        not a missing one, and without the tree's flag the answer would call
        itself complete while carrying it.

        The two scans take their own locks, so the store can move between them,
        and the direction that matters is a unit RETIRED in that window: its
        mention is already captured while its lineage node is not, which is the
        one shape that defeats the ancestor rule. :func:`_retired_between_scans`
        detects it and the reading says so.

        Reordering the two scans is NOT the fix, though it looks like one. Taking
        the lineage first trades exposure to a retirement for exposure to a unit
        ADDED in the window, which leaves the same mention-without-node shape --
        and a session appearing is constant in a live gateway while a retention
        deletion is rare, so the trade is strictly worse.

        Never raises: this is decoration on the pages that show it, and a store
        fault must not take a page down. The fault is logged and the answer is
        empty, and it is reported INCOMPLETE, because "nothing was readable" and
        "nobody holds anything" are different facts and only the second one is
        safe to render as an answer.
        """
        try:
            preferred_ids = list(islice(preferred, REFERENCE_UNIT_CAP))
            reading = self.references(since=since, preferred=preferred_ids)
            lineage = self._tree.reading(preferred_ids)
            folded = fold_holders(reading.mentions, lineage.nodes)
            return HolderReading(
                holders=folded,
                incomplete=(
                    reading.incomplete
                    or lineage.incomplete
                    or _retired_between_scans(reading.mentions)
                ),
            )
        except Exception:  # pragma: no cover -- defensive; store calls are guarded
            logger.warning("crew log reference scan failed; reporting no holders", exc_info=True)
            return HolderReading(holders={}, incomplete=True)

    def forget(self) -> None:
        """Drop every cached position, so the next scan reads from the start.

        For a caller that changed what it scans FOR -- a different text rule --
        since the cache holds the old rule's verdicts and no amount of appending
        will revise them.
        """
        with self._lock:
            self._units.clear()

    # -- scanning ----------------------------------------------------------- #

    def _merge(
        self, unit: _UnitState, floor: int | None
    ) -> tuple[dict[Reference, tuple[int, int]], bool]:
        """One unit's references across its surviving segments, and whether any of
        them left something unread. Caller holds the lock.

        The flag is RETURNED rather than written onto the scanner so that one
        reading's completeness cannot be observed by another reader's call.
        """
        merged: dict[Reference, tuple[int, int]] = {}
        incomplete = False
        for segment in unit.segments.values():
            for reference, (newest, count) in segment.refs.items():
                if floor is not None and newest < floor:
                    continue
                held = merged.get(reference)
                merged[reference] = (
                    (max(held[0], newest), held[1] + count) if held is not None else (newest, count)
                )
            if not segment.exhausted or segment.omitted:
                incomplete = True
        return merged, incomplete

    def _admit(self, preferred: Iterable[str]) -> tuple[list[Path], bool, bool]:
        """The unit directories this scan reads, live sessions' first, whether the
        store held more than the cap admits, and whether the LISTING faulted.
        Caller holds the lock.

        Over the cap the answer covers only the admitted units, so the second
        value is part of the reading's incompleteness and not a separate fact a
        caller may forget to ask for: a truncated set of units and a truncated set
        of bytes both mean the answer is not the whole store.

        The third is the same distinction one level up. :func:`store.unit_dirs`
        answers an unlistable root with an empty list, which is also its answer for
        a store that genuinely holds nothing -- and only the first means this scan
        saw less than the store holds. The probe runs on the empty path alone, so
        the ordinary case pays nothing for it.
        """
        seen: set[str] = set()
        named: list[Path] = []
        for unit_id in islice(preferred, REFERENCE_UNIT_CAP):
            directory = unit_dir_for(KIND_SESSION, unit_id)
            if directory is not None and directory.name not in seen:
                seen.add(directory.name)
                named.append(directory)
        listed, over_cap, faulted = unit_dirs(
            KIND_SESSION, limit=REFERENCE_UNIT_CAP - len(named), exclude=seen
        )
        # The fault comes from the read that FAILED, not from a probe of our own.
        # `iterdir` yields as it goes, so a failure after the first entry escapes
        # anything that draws one entry and stops, and a full re-read answers for a
        # different moment than the listing did.
        return named + listed, over_cap, faulted

    def _scan_unit(
        self, directory: Path, wanted: frozenset[str]
    ) -> tuple["_UnitState | None", bool]:
        """One unit's state, reading whatever its segments appended since the last
        scan, and whether anything here could not be READ. Caller holds the lock.

        The second value is the difference between "this unit holds nothing for
        you" and "this unit could not be read", which the answer must not blur: a
        fault swallowed silently would let a scan that saw less than the store
        holds come back marked complete.
        """
        unit = self._units.get(directory.name)
        if unit is None:
            identity, faulted = _unit_identity(directory)
            if identity is None:
                return None, faulted
            sid, slot = identity
            unit = _UnitState(sid=sid, slot=slot)
            self._units[directory.name] = unit
        if not unit.slot:
            # A header with no slot has no place in a slot-keyed answer, exactly
            # as the tree drops it. Cached, so the refusal costs one read. Not a
            # fault: the header was read, and it said this.
            return None, False
        try:
            segments = segment_paths(KIND_SESSION, unit.sid)
            firsts = segment_first_seqs(KIND_SESSION, unit.sid)
        except (OSError, CrewLogError):
            # The unit's segments could not be listed, so nothing is known about
            # what it holds -- not even that it holds nothing.
            #
            # `CrewLogError` is the store REFUSING to name the directory, and it
            # belongs in the same branch as a failed read: either way this unit's
            # references cannot be counted. It is reachable two ways. A header can
            # hold an id the store will not address -- `require_unit_id` refuses a
            # path separator, a NUL and `.`/`..`, and `_store_name` folds a
            # separator to `_` before hashing the WHOLE id, so a hand-written
            # directory and header pair can agree while the id stays unusable. And
            # the root itself can be refused, for a data home this process may not
            # read, which has nothing to do with the id. Left uncaught, one such
            # unit raised out of `references` and the outer guard answered with NO
            # holders at all, throwing away every other unit's work to report one
            # unreadable one.
            return None, True
        if len(firsts) != len(segments):
            # Two listings of a directory that changed between them. Pairing a
            # path with another segment's first-seq would invent a gap or hide
            # one, so this scan declines to judge continuity at all.
            return None, True
        names = {segment.name for segment in segments}
        for gone in [name for name in unit.segments if name not in names]:
            del unit.segments[gone]
        faulted = False
        for position, segment in enumerate(segments):
            faulted = self._scan_segment(unit, segment, wanted) or faulted
            held = unit.segments.get(segment.name)
            if held is not None:
                held.first_seq = firsts[position]
        _mark_middle_gaps(unit, segments)
        return unit, faulted

    def _scan_segment(self, unit: _UnitState, segment: Path, wanted: frozenset[str]) -> bool:
        """Read one segment from its cached position; True if it could not be
        read. Caller holds the lock."""
        try:
            stat = segment.stat()
        except FileNotFoundError:
            # Retention deleting a segment off the front is the DESIGNED case and
            # is not a fault: its entries are gone from the store, so an answer
            # without them is complete.
            unit.segments.pop(segment.name, None)
            return False
        except OSError:
            # The stat FAILED on a segment the listing just returned, so the file
            # is there and could not be read. Asking `exists()` instead answers
            # False for this fault as well as for a deletion, reporting a
            # readable-but-unread segment as one retention removed. This branch
            # also drops nothing: the cache it holds was read from bytes that were
            # really there, and a later scan resumes from the same position.
            return True
        state = unit.segments.get(segment.name)
        if state is not None and (
            state.dev != stat.st_dev or state.ino != stat.st_ino or stat.st_size < state.size
        ):
            # Replaced under the same name, or shorter than we read it: not the
            # file whose bytes we cached.
            state = None
        if state is None:
            state = _SegmentState(dev=stat.st_dev, ino=stat.st_ino, offset=0, size=0)
            unit.segments[segment.name] = state
        state.size = stat.st_size
        if state.unreadable:
            # Already judged: re-reading would re-add what it holds before the
            # record that stopped it. Still a fault, every time it is asked.
            return True
        if state.offset >= stat.st_size:
            state.exhausted = True
            return False
        try:
            consumed, exhausted, foreign = _read_from(segment, state, wanted, unit.sid)
        except UnreadableRecord:
            # A record this reader cannot deliver INTACT. The framing layer would
            # otherwise drop it whole, terminator included, and those bytes would
            # never reach this scan: the position would then sit short of the
            # dropped record and every later scan would read the records after it
            # again, adding their mentions a second time and inflating the very
            # count the fold breaks ties on. There is no trustworthy position to
            # cache past it, so this segment contributes nothing and says so. The
            # writer refuses to produce such a record, so reaching here means the
            # file was written by something else.
            state.refs.clear()
            state.omitted += 1
            state.unreadable = True
            return True
        except OSError:
            # The bytes were not seen, so the position does not move: a moment's
            # I/O fault, or retention between the stat and the open. Either way
            # this scan did not see what the segment holds.
            return True
        if foreign:
            # The file's own header names another unit, so nothing here can be
            # attributed to this one. Judged PERMANENTLY, like a record that could
            # not be delivered: the answer is a property of the bytes, not of a
            # moment, so re-reading can only reach the same one. The position does
            # not move either -- there is no trustworthy offset into a file this
            # scan must not fold.
            state.refs.clear()
            state.omitted += 1
            state.unreadable = True
            return True
        state.offset += consumed
        state.exhausted = exhausted
        return False


def _mark_middle_gaps(unit: _UnitState, segments: list[Path]) -> None:
    """Record entries a DELETED MIDDLE segment took with it.

    Retention deletes whole segments off the FRONT, and an answer without those
    entries is complete: they are gone from the store, so no reader can be told
    about them. A segment missing from the MIDDLE is different in kind -- the
    entries around it are still here, so the sequence itself says some are
    absent, and the store puts each segment's first-seq in its NAME precisely so
    a reader can tell the two apart from the listing alone.

    Seq runs contiguously inside one segment, so the segment after one whose
    highest entry is ``max_seq`` must itself start at ``max_seq + 1``. A higher
    start is the hole. The number comes from the records this scan already
    framed -- read from the entries themselves rather than counted, because only
    the records know how the writer numbers a segment's header.

    A segment this scan did not finish is skipped rather than judged: its count
    is short by however much went unread, which would read as a hole that is not
    there. Not finishing already makes the reading incomplete, so nothing is lost
    by staying quiet here.
    """
    ordered = [unit.segments.get(segment.name) for segment in segments]
    for earlier, later in zip(ordered, ordered[1:]):
        if earlier is None or later is None:
            continue
        if not earlier.exhausted or earlier.omitted or not earlier.max_seq:
            continue
        if later.first_seq > earlier.max_seq + 1:
            later.omitted += 1


def _read_from(
    segment: Path, state: _SegmentState, wanted: frozenset[str], sid: str
) -> tuple[int, bool, bool]:
    """Read *segment* from ``state.offset``, recording references into *state*.

    Returns the bytes CONSUMED at record boundaries, whether the segment was read
    to its end, and whether the segment is FOREIGN -- its own header naming a unit
    other than *sid*. A frame with no terminator is the tail of an append in
    flight: it is neither parsed nor counted, so the next scan reads those bytes
    again once they are complete.

    Record 0 is the header, and a read that starts at the beginning CHECKS it
    before skipping it; a resumed read has already passed it. A segment's session
    is otherwise taken from the directory holding it, so a file whose header names
    another session would have its entries folded into this one's mentions and the
    reading would still call itself complete -- a holder for work the session never
    touched. This reader already refuses that disagreement for the OLDEST segment,
    in :func:`_unit_identity`, which is what leaving the rest unchecked made
    uneven: one rule, every segment.

    The check reads the header from the SAME open file as the records, never from a
    second look at the path. A separate read would leave a window in which the file
    the header vouched for is not the file whose records are folded.
    """
    consumed = 0
    run: _SegmentState | None = state.pending
    with open(segment, "rb") as source:
        if state.offset:
            source.seek(state.offset)
        index = 0
        for raw in strict_raw_records(source, segment, cap=MAX_ENTRY_BYTES):
            index += 1
            if not raw.endswith((b"\n", b"\r")):
                # Mid-append. Stop without counting it; nothing here is final.
                # This is also what keeps a header still being written out of the
                # check below: those bytes are not final either.
                state.pending = run
                return consumed, False, False
            consumed += len(raw)
            if state.offset == 0 and index == 1:
                header = _object(raw)
                if header is None or header.get("type") != KIND_SESSION:
                    # Not a header at all, so nothing vouches for these entries.
                    return 0, False, True
                if header.get("id") != sid:
                    return 0, False, True
                continue  # the header, checked
            if run is None:
                run = _SegmentState(
                    dev=state.dev,
                    ino=state.ino,
                    offset=0,
                    size=0,
                    max_seq=state.max_seq,
                    carry=state.carry,
                    carry_key=state.carry_key,
                )
            kind = _record(raw, run, wanted)
            if kind and kind != CHUNK_TYPE:
                # The run this record ends is CITED by construction: the store
                # writes an oversize body's slices and the entry naming their seqs
                # as one group, so slices with anything after them were completed.
                #
                # A SKIPPED record -- no type, because it was damaged or not an
                # entry at all -- must not close a run. It says nothing about
                # whether the citing entry landed, and treating it as the end of the
                # group commits exactly the unreachable slices this hold is for.
                state.absorb(run)
                run = None
            if consumed >= SCAN_BYTES_PER_SEGMENT:
                state.pending = run
                return consumed, False, False
    state.pending = run
    return consumed, True, False


def _object(blob: bytes) -> dict[str, Any] | None:
    """*blob* decoded STRICTLY and parsed as a JSON object, or ``None``.

    Decoding is strict and per record, the posture
    :func:`~kiro_crew.crew_log.store._iter_entries` documents: invalid bytes
    inside a JSON string can decode into still-valid JSON under replacement, so a
    damaged record would be yielded with silently altered values instead of
    skipped -- handing a consumer corrupted data as authority. A record that is
    valid JSON but not an object is not an entry either.
    """
    try:
        parsed = json.loads(blob.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _record(raw: bytes, state: _SegmentState, wanted: frozenset[str]) -> str:
    """Fold one raw record into *state*, skipping anything unparseable.

    Returns the entry TYPE folded, or ``""`` for a record this skipped. The caller
    needs it to tell an oversize-body slice from the entry that ends a run of them,
    and returning it here is what keeps the record parsed once.

    A damaged line is skipped rather than raised on, the posture every reader of
    this store takes: the log is append-only, and one damaged line must not hide
    the history in front of it.

    The ``since`` window is deliberately NOT applied here. The cache stores what
    the bytes said, and :meth:`ReferenceScanner._merge` applies the floor on the
    way out. Filtering here instead would advance the read position past a record
    without caching it -- the position moves on consumed BYTES, not on kept
    records -- so one windowed scan would permanently hide those references from
    every later unwindowed one on the same scanner, and report the result as
    complete. The scanner is shared per process, so a single windowed call would
    poison it for every other reader.

    Every record that is NOT a text entry being scanned clears the oversize-body
    carry, including a record this function skips. A skipped record still lies
    between two slices, so leaving the carry alone would let a stitch reach across
    it and spell a reference from two unrelated bodies. The clear is here, at the
    one place every record passes, rather than at each skip.
    """
    stripped = raw.strip()
    if not stripped:
        return ""
    parsed = _object(stripped)
    if parsed is None:
        state.carry = ""
        state.carry_key = None
        return ""
    entry = Entry.from_dict(parsed)
    if entry is None:
        state.carry = ""
        state.carry_key = None
        return ""
    # Before the type filter: continuity is a property of the SEQUENCE, so every
    # entry's number counts, not just the ones this scan is looking for.
    if isinstance(entry.seq, int) and not isinstance(entry.seq, bool) and entry.seq > 0:
        state.max_seq = max(state.max_seq, entry.seq)
    kind = entry.type if isinstance(entry.type, str) else ""
    if entry.type not in wanted:
        state.carry = ""
        state.carry_key = None
        return kind
    moment = entry.time if isinstance(entry.time, int) and not isinstance(entry.time, bool) else 0
    for reference in _scan_entry(entry, state):
        state.add(reference, moment)
    return kind


def _retired_between_scans(mentions: Mapping[Reference, list[Mention]]) -> bool:
    """Whether a unit that produced a mention was retired before the lineage scan.

    The reference scan and the lineage scan hold their own locks, so the store can
    move between them. A unit RETIRED in that window leaves its mention captured
    and its lineage node missing, and a mention with no node is the one shape that
    defeats the ancestor rule: the candidate cannot be dropped as anyone's
    descendant, so a supervising conductor survives the filter and wins on recency.

    The question is asked of the UNIT, not of its slot. A slot present in the
    lineage says only that SOME unit holds it now, and a slot is reused the moment
    a new session takes it -- so short-circuiting on the slot skips the probe for
    exactly the retirement it is looking for, whenever the vacancy was filled.

    What tells a retirement from the ordinary missing node is whether the unit is
    still THERE. A session whose log carries no ``session/opened`` record has no
    node either -- common, and complete -- but it still has its directory, and one
    retention removed does not. So the probe is one existence check per mention
    that carries a unit id, which on a healthy store is a handful of stats.
    """
    for entries in mentions.values():
        for mention in entries:
            if not mention.sid:
                continue
            directory = unit_dir_for(KIND_SESSION, mention.sid)
            if directory is None or not directory.exists():
                return True
    return False


def _unit_identity(directory: Path) -> tuple["tuple[str, str] | None", bool]:
    """``((sid, slot), False)`` from a unit directory's header, else ``(None, ...)``.

    The second value says whether the header could not be READ, as opposed to
    having been read and refused. Both yield no identity, but only the first means
    this scan saw less than the store holds, and an answer that blurred them would
    report a partial read as complete.

    The refusal is the same one
    :func:`~kiro_crew.crew_log.session_tree.opened_record` makes: a directory carrying
    another unit's id would answer for that unit, so a header whose own id does not
    fold back to this directory is refused. The LENGTH bounds are that function's
    too, and deliberately the same constants: both of these strings are RETAINED,
    one per unit, for as long as the unit is admitted. The unit count is capped but
    each field is only capped by the record size, so without these a store of
    hand-written headers would pin the product of the two in a cache every reader
    in the process shares. A bound the tree applies and this reader did not would
    also be the two folds disagreeing about which units exist, which is the
    disagreement this module was written to end.
    """
    segment = oldest_segment(directory)
    if segment is None:
        # Same probe, same reason, as the tree's own per-unit read:
        # `oldest_segment` answers an unreadable DIRECTORY with None, which is
        # also its answer for a unit that genuinely has no segment. Fixing it in
        # the tree and not here would leave the two folds disagreeing about which
        # units exist, which is the whole defect this module ends.
        try:
            next(iter(directory.iterdir()), None)
        except FileNotFoundError:
            return None, False
        except OSError:
            return None, True
        return None, False
    try:
        header, _entry, _announced = read_head(segment)
    except OSError:
        return None, True
    if header is None:
        # Same rule as the tree's own header read: bytes that produced no header
        # are damage, an empty file is the create-then-announce transient.
        return None, header_unreadable(segment)
    if header.get("type") != KIND_SESSION:
        return None, False
    sid = header.get("id")
    if not isinstance(sid, str) or not 0 < len(sid) <= MAX_ACP_SESSION_ID_LEN:
        return None, False
    if _store_name(sid) != directory.name:
        return None, False
    slot = header.get("slot")
    if not isinstance(slot, str) or len(slot) > MAX_SHORT_STRING:
        slot = ""
    return (sid, slot), False
