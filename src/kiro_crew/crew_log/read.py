"""Reads over a crew log that more than one consumer needs: a page, a listing, lineage.

All functions here are BLOCKING and belong off the event loop. They are in the
storage package rather than in a route module because two consumers now want the
same answers -- the dashboard's own routes and the ``kirocrew-crew-log`` MCP
server's proxy leg -- and a second copy of a page reader is how two callers come
to disagree about what a page is.

No function here makes an authorization claim, the dispatch pair included. That is
the same split ``crew_log.store`` states for itself: this layer has no caller
identity to derive a decision from, so the first caller with a permission model
owns the decision, and for both consumers here that caller is a dashboard route.
:func:`dispatch_view` and :func:`recorded_class` report recorded FACTS -- which
session dispatched which, and what kind of session a log belongs to -- and a route
decides what those facts permit. Keeping them here rather than in the route is why
the page, the listing and the scope test read one record the same way.
"""

from __future__ import annotations

import heapq
import json
import logging
import os
import threading
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from kiro_crew.crew_log import projection as projections
from kiro_crew.crew_log import session_tree
from kiro_crew.crew_log.errors import CrewLogError
from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.session_tree import TreeNode
from kiro_crew.crew_log.store import LOG_FILE, crew_log_root, log_exception_text
from kiro_crew.crew_log.store import now_ms as store_now_ms
from kiro_crew.crew_log.store import segment_first_seqs

logger = logging.getLogger(__name__)

#: Distinct refs a single page resolves. Each resolution opens the cited unit and
#: walks to the span, so the work is bounded per request rather than left to
#: however many citations a page happens to carry; identical refs on one page are
#: resolved once. Past the budget the entry keeps its ``ref`` and carries no
#: resolution, and the page says how many it left, so a reader is never shown a
#: silently unresolved citation.
MAX_PAGE_REFS: Final[int] = 25

#: Unit directories one listing is willing to OPEN. A listing walks the kind root
#: and folds each unit it accepts, so the cost is bounded here rather than by how
#: many sessions the host has ever run. The walk visits newest-mtime first and
#: reports ``scanned``/``truncated``, so a caller reading a truncated list knows
#: it is the newest window and not the whole tree.
MAX_LISTED_SCAN: Final[int] = 400

#: Unit directories one listing is willing to RETAIN while choosing which to open.
#: Separate from ``MAX_LISTED_SCAN`` and deliberately larger: the walk skips a
#: directory that is outside an ``active_within`` window or carries no readable
#: unit id WITHOUT counting it as scanned, so the candidate set has to be able to
#: absorb some skips before the scan cap is reached. It is a bound on MEMORY, not
#: on what a caller may see -- the newest this many are kept by mtime in a
#: fixed-size heap, so a host with a hundred thousand session logs holds this many
#: paths rather than all of them, and a set that had to be cut reports
#: ``truncated``.
MAX_LISTED_CANDIDATES: Final[int] = MAX_LISTED_SCAN * 2

#: Rows one listing returns, whatever a caller asks for.
MAX_LIST_LIMIT: Final[int] = 200

#: Class bundles held for incremental re-folding, keyed by unit id. A read of the
#: class asks a question about the log's whole life, so it cannot be answered from
#: the head -- and re-folding every entry on each request would put a whole-file
#: read on the path of a scope test a route runs per request. The bundle is the
#: registry's own resumable checkpoint, so a second read of an unchanged log
#: consumes no entries and a grown one consumes only what grew.
MAX_CLASS_BUNDLES: Final[int] = 256

#: The held bundles, newest use last. Module-level for the same reason the tree
#: scanner is: the saving only exists if it survives the request.
_class_bundles: "OrderedDict[str, projections.SessionProjections]" = OrderedDict()

#: Guards :data:`_class_bundles`. Folds run off the loop and concurrently.
_class_lock: Final[threading.Lock] = threading.Lock()


def recorded_class(session_id: str) -> dict[str, Any] | None:
    """What kind of session this unit belongs to, over its log's whole life, or ``None``.

    The ``class`` projection, folded through the registry: ``memory`` always, ``app``
    when an app owns the session, ``channel`` when its conversation is published to a
    messaging channel, each held at the most restrictive value the log ever recorded.
    ``complete`` says the history has a beginning -- an opening entry that stated a
    class -- and ``stated`` counts the entries that stated one.

    The whole life, not the first instant, and that is why this folds rather than
    reading the head. A session can be given a channel surface, an app owner or a
    different memory mode while it runs, and the content that arrives afterwards is
    in this same log; a reader that consulted only the opening entry would be told
    what the session was before the surface it acquired, which for a closed session
    is the only thing anyone can still check.

    ``None`` is the answer that matters. It means this log does not say what kind of
    session it is -- nothing recorded, no unit, or a log that cannot be read -- and a
    caller deciding whether another session may read it must REFUSE on it. A missing
    record is not evidence that nothing applies.

    A fold whose entries were CUT SHORT also answers ``None``. ``iter_from`` stops at
    an entry type it does not know, which a log written by a newer build can carry,
    and stopping is safe for a fold that accumulates totals but not for this one: a
    restrictive move recorded after the unknown entry would simply be unseen, and the
    fold would report the log as more readable than it is. So the seq the fold reached
    is compared against the seq the file holds, and a short fold refuses.

    A log that does not hold its own BEGINNING refuses for the same reason at the
    other end. Retention deletes whole segments off the front, and the first seq is in
    each segment's name, so an oldest survivor starting past seq 1 says so for the
    price of a directory listing. Without that check the fold would read the earliest
    SURVIVING opener as the log's first and date the log by an entry written after the
    part retention took.

    A log with a hole in its MIDDLE refuses too, and that one the fold reports rather
    than the reader measuring it. The store skips an unreadable interior line on purpose
    -- one damaged record must not make a whole file unreadable -- and it checks seq
    continuity only at segment boundaries, so a damaged line that carried a restriction
    disappears while the fold still reaches the file's tail. Neither check above sees
    that: nothing stopped early and the beginning is intact. The class fold therefore
    keeps its own contiguity, and reports a record it could not read as a hole rather
    than as silence.

    The one hole none of the three covers is a damaged LAST line: it lowers the tail seq
    too, so the fold and that seq agree, and nothing distinguishes it from an append in
    flight -- which is frequent, so refusing on it would refuse ordinary reads. It
    carries no exposure, and that is a property of the writer rather than luck: a class
    is recorded at the START of the turn that runs under it, so a move with nothing
    after it is a move no turn has run under, and no content in the log is governed by
    what was lost.

    BLOCKING. The fold is incremental: the bundle for each unit is kept and handed
    back as ``since=``, so a second read of an unchanged log consumes no entries, and
    ``fold_session`` itself refuses to reuse a bundle whose log has been recreated.
    """
    if not session_id:
        return None
    try:
        handle = projections.open_session_log(session_id)
        if handle is None:
            return None
        held = handle.last_seq
        with _class_lock:
            since = _class_bundles.get(session_id)
        bundle = projections.fold_session(session_id, ("class",), since=since, log=handle)
        _keep_class_bundle(session_id, bundle)
    except (CrewLogError, OSError):
        log_exception_text(
            logger, logging.DEBUG, "no class record for %r: its log could not be folded", session_id
        )
        return None
    if bundle.last_seq < held:
        logger.debug(
            "the class fold for %r stopped at seq %d of %d; refusing rather than "
            "reporting a class read from part of the log",
            session_id,
            bundle.last_seq,
            held,
        )
        return None
    try:
        firsts = segment_first_seqs(KIND_SESSION, session_id)
    except OSError:
        return None
    if not firsts or firsts[0] > 1:
        # The same refusal as the short fold above, at the other end. Retention
        # deletes whole segments off the FRONT, so a log whose oldest survivor starts
        # past seq 1 does not hold its own beginning -- and the fold would then read
        # the earliest surviving opener as the log's first one and date the log by it.
        # Costs a directory listing, because the first seq is in the segment's name.
        #
        # An EMPTY list refuses on the same ground rather than being read as "no
        # trimming": it means no segment name could be parsed, so the question of
        # where this log starts has no answer here, and an unanswerable question about
        # a log's beginning is what this check refuses on.
        logger.debug(
            "the class fold for %r cannot show it still holds seq 1 (segment starts: "
            "%r); refusing rather than dating a log whose beginning may be gone",
            session_id,
            firsts[:3],
        )
        return None
    folded = bundle.projection("class").value
    if folded.get("damaged"):
        # A hole in the middle. The store skips an unreadable interior line on purpose
        # and does not check interior seq continuity, so a damaged line that carried a
        # restriction disappears while the fold still reaches the file's tail -- the
        # short-fold check above cannot see it, because nothing stopped early. The fold
        # reports the hole and this refuses, which keeps byte damage from raising an
        # authorization ceiling.
        logger.debug(
            "the class history for %r has a hole in it; refusing rather than "
            "answering from a record that may be missing a restriction",
            session_id,
        )
        return None
    if not folded.get("recorded"):
        return None
    return dict(folded)


def _keep_class_bundle(session_id: str, bundle: projections.SessionProjections) -> None:
    """Hold *bundle* as the ``since=`` for this unit's next class fold.

    Bounded and least-recently-used, because the key space is every unit that has
    ever been asked about. Each held bundle is one checkpoint whose state is six
    small fields, so the cap bounds real memory rather than standing in for a bound.
    """
    with _class_lock:
        _class_bundles[session_id] = bundle
        _class_bundles.move_to_end(session_id)
        while len(_class_bundles) > MAX_CLASS_BUNDLES:
            _class_bundles.popitem(last=False)


def reset_class_bundles() -> None:
    """Drop every held class bundle. For tests that rebuild a unit's log in place."""
    with _class_lock:
        _class_bundles.clear()


@dataclass(frozen=True)
class DispatchView:
    """One scan of the store, folded so a dispatch question can be asked of it.

    Two maps from the same scan: which SLOT each unit's log belongs to, and each
    slot's creator. Both come from :mod:`kiro_crew.crew_log.session_tree` -- its scanner
    reads every log's head once and caches it, and its pure fold decides the
    parent edges, the orphans and the cycles -- so this holds no slot map of its
    own and cannot disagree with the tree a reader sees on screen.

    Keyed by SLOT, not by unit, and that is the point. A slot outlives its ACP
    session: a gateway restart gives the same tab a new session id and therefore a
    new unit, so a fence keyed on the recorded ``parent.sid`` would hand a
    conductor a chain naming one of its own earlier units and lock it out of the
    children it dispatched. The slot key carries its own creation stamp and is not
    recycled, so the slot a child records is the slot that dispatched it for as
    long as that tab exists.
    """

    #: unit id -> the slot key its log's header declares. A unit missing here has
    #: no provable log in this scan, which refuses.
    slot_of_unit: Mapping[str, str]
    #: slot -> its node in the folded tree, carrying the creator it cites.
    nodes: Mapping[str, TreeNode]
    #: Whether the scan these maps came from saw the whole store. True when a unit's
    #: bytes could not be read, when the population ran past the scanner's cap, or
    #: when the scan failed outright.
    #:
    #: It is on the view because every consumer here DECIDES on an edge, and
    #: :meth:`dispatched_by` answers False for a missing one -- so a lost edge and a
    #: real absence of lineage are the same answer from that method and can only be
    #: told apart here. Without it a scan that could not read part of the store
    #: refuses a conductor its own child's log and words the refusal as "out of
    #: scope", which is a confident wrong answer rather than a missing one; with it,
    #: an OPERATOR gets that distinction. The caller deliberately does not: the
    #: refusal text is identical either way, because the per-unit door passes the
    #: requested unit as the scan's ``preferred``, so a caller-visible difference
    #: would tell a guessed id that exists-and-is-unreadable apart from one that does
    #: not exist. This is the same reason
    #: :class:`~kiro_crew.crew_log.session_tree.TreeReading` carries the flag, and it
    #: is carried rather than re-derived for the same reason too: one tree serves
    #: every reader in the process, so a flag read in a second, unlocked call can
    #: belong to another reader's scan.
    incomplete: bool = False

    def slot_of(self, unit: str) -> str:
        """The slot *unit*'s log belongs to, or ``""`` when this scan has no log for it."""
        return self.slot_of_unit.get(unit, "") if unit else ""

    def dispatched_by(self, unit: str, root_slot: str) -> bool:
        """Whether *unit*'s session is *root_slot* or one it dispatched, transitively.

        Membership is what "this session was dispatched by that one" means: a
        conductor's slot appears above its child's slot and above its grandchild's.
        The walk stops at the first slot citing no creator or on a repeat, and it
        refuses to cross a slot the fold marked as being on a CYCLE -- a cycle is
        unreachable through honest writes, since a creator exists before its child, so
        a slot on one carries a forged or damaged record and is not evidence of
        anything.

        The repeat check is what ends the walk, and it ends it on any input: each pass
        either returns or adds one slot to ``seen``, and ``seen`` only ever holds slots
        drawn from the finite node map, so the walk cannot run longer than that map. A
        counted depth cap on top of that would not make termination any safer -- it
        would only make a deep-but-honest lineage answer "no creator above this" and
        refuse a grant the tree actually supports.
        """
        if not root_slot:
            return False
        slot = self.slot_of(unit)
        if not slot:
            return False
        if slot == root_slot:
            return True
        seen = {slot}
        while True:
            node = self.nodes.get(slot)
            if node is None or node.cycle:
                return False
            parent = node.parent_slot
            if not parent or parent in seen:
                return False
            if parent == root_slot:
                return True
            seen.add(parent)
            slot = parent


#: The scanner the dispatch view is folded from. Module-level because it holds a
#: per-unit head cache validated against each segment's identity, and the cache is
#: what keeps a scan one ``stat`` per untouched unit rather than one read -- which
#: is the difference between a scope test a route can run per request and one it
#: cannot. Its own lock makes concurrent scans safe.
_TREE: Final[session_tree.SessionTree] = session_tree.SessionTree()


def dispatch_view(preferred: Iterable[str] = ()) -> DispatchView:
    """One scan of the store as a :class:`DispatchView`. BLOCKING; runs off the loop.

    *preferred* names unit ids to admit FIRST, which the caller uses to put the
    units a request is actually about ahead of the scanner's cap.

    Through :meth:`~kiro_crew.crew_log.session_tree.SessionTree.reading`, never
    :meth:`~kiro_crew.crew_log.session_tree.SessionTree.records`, and the difference
    is the whole point: ``records`` drops whether the scan faulted, and every
    consumer of this view DECIDES on an edge. ``reading`` is also what
    :class:`~kiro_crew.crew_log.holders.ReferenceScanner` uses for the same reason,
    so the fault bit reaches a decider through ONE mechanism rather than two
    spellings of it. The ``sid -> slot`` map is built from that same reading's own
    records, so the map, the nodes and the flag all describe one scan under one take
    of the tree's lock.

    Never raises: a store fault must leave the caller able to refuse rather than
    500, so a failed scan reports an empty view -- in which no unit is placeable and
    every dispatch test is therefore false, and which says so through
    ``incomplete`` instead of reading as a store that holds no lineage.
    """
    try:
        reading = _TREE.reading(preferred)
    except Exception:
        logger.warning("dispatch scope scan failed; reporting no lineage", exc_info=True)
        return DispatchView(slot_of_unit={}, nodes={}, incomplete=True)
    slots = {record.sid: record.slot for record in reading.records if record.sid and record.slot}
    return DispatchView(slot_of_unit=slots, nodes=reading.nodes, incomplete=reading.incomplete)


def read_page(session_id: str, start: int, end: int) -> dict[str, Any]:
    """One range of entries with their refs resolved. Blocking; runs off the loop."""
    handle = projections.open_session_log(session_id)
    if handle is None:
        return {
            "session_id": session_id,
            "exists": False,
            "from": start,
            "to": end,
            "last_seq": 0,
            "entries": [],
            "next_from": None,
            "refs_unresolved": 0,
        }
    # No vocabulary: a page renders history, so an unfamiliar line is shown
    # rather than made to refuse the lines around it. ``strict_seq=False`` for
    # the same reason: a non-advancing seq is damage a FOLD must refuse, but a
    # page that raised on it would take every intact line of the unit away from
    # the operator exactly when damage makes the history most worth reading.
    #
    # ``handle.last_seq`` is this instance's own cached figure -- the store's
    # docstring says it is authoritative only for its OWN appends -- and a reader
    # handle never appends, so a writer that grows the file after this handle
    # opened is invisible to it. The iteration below reads the file live and walks
    # the whole tail from ``start``, discarding what is past ``end`` rather than
    # never seeing it, so the true tail is observable here for free. Deriving
    # ``next_from`` from the cached figure instead would let a page return rows up
    # to ``end`` and still report that nothing follows, and a client that believes
    # it stops paging with entries left unread.
    observed_last = handle.last_seq
    entries: list[Any] = []
    for entry in handle.iter_from(start, strict_seq=False):
        if entry.seq > observed_last:
            observed_last = entry.seq
        if entry.seq <= end:
            entries.append(entry)
    resolutions: dict[tuple[Any, ...], dict[str, Any]] = {}
    unresolved = 0
    rows: list[dict[str, Any]] = []
    for entry in entries:
        row = entry.to_dict()
        if entry.ref is not None:
            key = (entry.ref.unit, entry.ref.id, entry.ref.from_seq, entry.ref.to_seq)
            found = resolutions.get(key)
            if found is None:
                if len(resolutions) >= MAX_PAGE_REFS:
                    unresolved += 1
                    rows.append(row)
                    continue
                outcome = handle.resolve(entry.ref)
                found = {
                    "status": outcome.status,
                    "entries": len(outcome.entries),
                    "first_seq": outcome.entries[0].seq if outcome.entries else None,
                    "last_seq": outcome.entries[-1].seq if outcome.entries else None,
                }
                resolutions[key] = found
            # The citation's VERDICT and span, not its bytes: the cited lines are
            # a page of their own unit, which this route already serves, and
            # inlining them would make one page carry up to MAX_REF_SPAN lines
            # per entry.
            row["ref_resolution"] = dict(found)
        rows.append(row)
    last_seq = observed_last
    return {
        "session_id": session_id,
        "exists": True,
        "from": start,
        "to": end,
        "last_seq": last_seq,
        "entries": rows,
        "next_from": end + 1 if end < last_seq else None,
        "refs_unresolved": unresolved,
    }


def _unit_id_in(directory: Path) -> str:
    """The raw unit id the log in *directory* declares, or ``""``.

    Read straight off the first line rather than derived from the directory
    NAME, because the name is a readable-plus-digest fold the store documents as
    not reversible. Nothing here has to trust the line either: the id is handed
    back to :func:`~kiro_crew.crew_log.store.CrewLog.open`, which re-derives the
    directory from it and refuses when it does not arrive at this one -- so a
    header naming another unit reads as an unopenable unit rather than as that
    other unit's log.
    """
    try:
        with open(directory / LOG_FILE, "r", encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
    except OSError:
        return ""
    if not first.strip():
        return ""
    try:
        parsed = json.loads(first)
    except ValueError:
        return ""
    unit_id = parsed.get("id") if isinstance(parsed, dict) else None
    return unit_id if isinstance(unit_id, str) else ""


def _written_at(directory: Path | str) -> float:
    """When *directory*'s log was last APPENDED to, as an mtime.

    The log file, never the directory. A write to ``log.jsonl`` does not move the
    mtime of the directory holding it -- a directory's mtime moves when its entry
    SET changes, so for a unit created once and appended to forever it stays the
    creation time. Reading the directory instead would silently turn "newest
    first" into "most recently created first" and drop a long-lived active session
    out of a recency window, which is the opposite of what both callers want.

    A unit with no readable log sorts oldest rather than raising: it is rejected a
    moment later by :func:`_unit_id_in` anyway, and an unreadable member of the
    root must not be able to fail the whole listing.
    """
    try:
        return os.stat(os.path.join(str(directory), LOG_FILE)).st_mtime
    except OSError:
        return 0.0


def _candidate_dirs(kind: str) -> tuple[list[Path], bool]:
    """The newest unit directories under *kind*'s root by write time, and whether cut.

    An mtime rather than a fold of each file, because the ordering has to exist
    BEFORE anything is opened -- it is what makes a truncated listing the newest
    window rather than an arbitrary one. It is a proxy for the newest entry's
    time and is honest about being one: the rows carry ``last_ts`` read from the
    entries themselves, and a caller that needs exact recency ordering sorts on
    that.

    The bound is applied HERE, while iterating, not by the caller's scan cap
    downstream: ``scandir`` streams, and a fixed-size heap holds at most
    ``MAX_LISTED_CANDIDATES`` entries, so a host with any number of session logs
    costs this function a bounded amount of memory. Returning whether the set was
    cut lets the listing report ``truncated`` for a cut IT did not perform.
    """
    kept: list[tuple[float, str]] = []
    cut = False
    try:
        with os.scandir(crew_log_root(kind)) as entries:
            for entry in entries:
                try:
                    if entry.is_symlink() or not entry.is_dir():
                        continue
                except OSError:
                    continue
                stamp = _written_at(entry.path)
                if len(kept) < MAX_LISTED_CANDIDATES:
                    heapq.heappush(kept, (stamp, entry.path))
                    continue
                cut = True
                # The heap's root is the OLDEST retained candidate, so this keeps
                # the newest window and drops the loser immediately rather than
                # growing the list and sorting at the end.
                if stamp > kept[0][0]:
                    heapq.heapreplace(kept, (stamp, entry.path))
    except OSError:
        # Includes the ordinary case of a root that was never created, which is
        # every host with the flag off.
        return [], False
    kept.sort(key=lambda pair: pair[0], reverse=True)
    return [Path(path) for _, path in kept], cut


def _listed_row(unit_id: str, *, with_type_counts: bool) -> dict[str, Any] | None:
    """One unit's row, folded in a single pass, or ``None`` when it cannot be read.

    ONE pass over the entries, feeding the ``status`` fold and counting types at
    the same time. Counting in a second walk would double the read of every file
    in the listing, and re-deriving lifecycle here instead of folding would put a
    second implementation of "is this session open" next to the one the panel
    already shows.

    No vocabulary is passed to ``iter_from``: a listing is a listing, and a unit
    written by a newer writer must appear in it rather than take the whole list
    down. The fold simply ignores a type it does not branch on.
    """
    try:
        handle = projections.open_session_log(unit_id)
    except (CrewLogError, OSError):
        return None
    if handle is None:
        return None
    counts: Counter[str] = Counter()
    seen: dict[str, int] = {"ts": 0, "seq": 0}

    def _tapped(entries: Any) -> Any:
        for entry in entries:
            counts[entry.type] += 1
            seen["ts"] = entry.time
            seen["seq"] = entry.seq
            yield entry

    try:
        checkpoint = projections.advance(
            projections.initial("status"), _tapped(handle.iter_from(1))
        )
    except (CrewLogError, OSError):
        return None
    status = projections.projection_of(checkpoint).value
    header = handle.header
    row: dict[str, Any] = {
        "unit": unit_id,
        "slot": getattr(header, "slot", None) or status.get("slot") or "",
        "agent": getattr(header, "agent", "") or status.get("agent") or "",
        "model": status.get("model") or "",
        "first_ts": getattr(header, "created_at", 0) or 0,
        "last_ts": seen["ts"] or status.get("last_time") or 0,
        "last_seq": seen["seq"],
        "open": status.get("lifecycle") == "open",
        "lifecycle": status.get("lifecycle") or "unknown",
        "entries": status.get("entries") or 0,
    }
    if with_type_counts:
        row["type_counts"] = dict(sorted(counts.items()))
    return row


def list_session_units(
    *,
    slot_contains: str = "",
    active_within_ms: int = 0,
    now_ms: int = 0,
    with_type_counts: bool = False,
    limit: int = 50,
    scope_unit: str = "",
    scope_slot: str = "",
    admit_dispatched: "Callable[[str], bool] | None" = None,
) -> dict[str, Any]:
    """The session crew logs on this host, newest first, with one row each.

    ``active_within_ms`` prunes by the LOG's mtime BEFORE the file is opened, which
    is the whole point of filtering rather than folding everything and cutting:
    the cheap signal answers "not recently written" without a read, and a unit it
    keeps is then folded and reports its real ``last_ts``. The log's mtime, never
    the directory's -- see :func:`_written_at` for why the directory answers a
    different question than the one asked here.

    ``slot_contains`` is a substring match over the row's slot, applied after the
    header is read because the slot lives in the header rather than in the
    directory name.

    Reports ``scanned`` and ``truncated`` rather than only the rows: a caller that
    cannot tell a short list from a cut one would read "3 sessions" off a host
    running three hundred. ``truncated`` covers both causes -- a cut this function
    made, and a lineage scan that could not read the whole store, which drops rows
    before they are counted so ``scanned`` never sees them. The two are not
    distinguished on the wire because no reader wants them apart; the second is
    recorded for an operator by the PRODUCER --
    :meth:`~kiro_crew.crew_log.session_tree.SessionTree._read` warns once, naming the
    unit, at the arm that judges a permanent fault -- because this path reaches no
    refusal door and so nothing that door writes could account for it.

    ``scope_unit`` and ``scope_slot`` narrow the listing, and between them they
    express the three scopes a route hands down. Both empty is NO narrowing, the
    owner's own view of every unit. ``scope_unit`` alone is exactly that one unit --
    the scope of a caller entitled to its own record and to nothing past it, and it
    costs no scan at all. Both together are that unit plus every unit whose session
    ``scope_slot`` dispatched, transitively, which is the tree a conductor may read.
    The unit is named separately rather than derived from the slot because a
    caller's own record stays readable however its lineage folds, including on a
    store the scan could not read.

    The narrowing is applied BEFORE the unit is opened and before it counts as
    scanned, so an out-of-scope unit costs a map lookup rather than a fold, and both
    figures describe the in-scope set rather than the whole tree. A route decides
    which scope to pass, and this function does not infer one from the absence of
    the other.

    ``admit_dispatched`` is asked about each row admitted by LINEAGE, and a false
    answer drops the row exactly as the scope filter does -- before it is opened and
    before it counts as scanned, so a dropped row neither appears nor shifts the
    figures. It is a callable rather than more scope data because the question it
    answers is a POLICY one: the route already owns the per-unit rules, and spelling
    them a second time here would put two implementations of one authorization rule
    in the tree. It is asked only of lineage rows, so a caller's own row and an
    unscoped owner listing never pay for it.
    """
    capped = max(1, min(int(limit), MAX_LIST_LIMIT))
    cutoff_ms = 0
    if active_within_ms > 0:
        cutoff_ms = (now_ms or store_now_ms()) - active_within_ms
    needle = slot_contains.casefold()
    rows: list[dict[str, Any]] = []
    scanned = 0
    # One scan for the whole listing, and only when a dispatch tree is in scope:
    # every candidate is tested against the same fold, so the scope test costs one
    # pass over the store rather than one walk per candidate.
    view = dispatch_view() if scope_slot else None
    global_counts: Counter[str] = Counter()
    candidates, truncated = _candidate_dirs(KIND_SESSION)
    for directory in candidates:
        if len(rows) >= capped or scanned >= MAX_LISTED_SCAN:
            truncated = True
            break
        if cutoff_ms:
            if _written_at(directory) * 1000 < cutoff_ms:
                continue
        unit_id = _unit_id_in(directory)
        if not unit_id:
            continue
        if scope_unit or scope_slot:
            own = bool(scope_unit) and unit_id == scope_unit
            dispatched = view is not None and view.dispatched_by(unit_id, scope_slot)
            if not own and not dispatched:
                continue
            # A row admitted by LINEAGE gets the caller's per-unit test as well, because
            # a listing discloses a unit's slot, model and activity and a lineage grant
            # alone does not bound which workspace those belong to. Only the lineage
            # rows: the caller's OWN row is its own record, which the per-unit door also
            # answers before any target test, and an unscoped owner listing never
            # reaches this branch at all.
            if not own and admit_dispatched is not None and not admit_dispatched(unit_id):
                continue
        scanned += 1
        row = _listed_row(unit_id, with_type_counts=with_type_counts)
        if row is None:
            continue
        if needle and needle not in str(row["slot"]).casefold():
            continue
        if cutoff_ms and row["last_ts"] and row["last_ts"] < cutoff_ms:
            continue
        if with_type_counts:
            global_counts.update(row["type_counts"])
        rows.append(row)
    rows.sort(key=lambda item: (item["last_ts"], item["unit"]), reverse=True)
    # A lineage scan that could not read the whole store drops rows this listing
    # would otherwise have admitted, and it drops them BEFORE they are counted --
    # ``scanned`` never sees them. So it sets ``truncated``: every caller reads that
    # field as "this is not the whole set", and leaving it false would tell all of
    # them the list is complete while the tree was short an edge.
    #
    # It gets no field of its own. A separate ``lineage_incomplete`` key would let a
    # caller tell a cut this function made from a population it could not test, which
    # is a real distinction -- but no reader in the product wants it, and a response
    # key with no consumer is a contract to keep for nobody. The fold above is what
    # carries the safety property; the cause is recorded for an operator by
    # :meth:`session_tree.SessionTree._read`, which warns once, naming the unit, at
    # the arm that judges a permanent fault. That record is at the producer, so a
    # caller that only ever LISTS is covered by it: this path reaches no refusal door,
    # and a permanent fault would otherwise latch ``truncated`` with nothing anywhere
    # saying why.
    lineage_incomplete = view is not None and view.incomplete
    out: dict[str, Any] = {
        "kind": KIND_SESSION,
        "units": rows,
        "scanned": scanned,
        "truncated": truncated or lineage_incomplete,
        "limit": capped,
    }
    if with_type_counts:
        out["type_counts"] = dict(sorted(global_counts.items()))
    return out
