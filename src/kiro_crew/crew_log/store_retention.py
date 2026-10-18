"""Retention, and the session tree's reads of a unit's log: tree edges and spawned children.

``sweep_expired`` removes a session unit only when the newest of its lifecycle pair
(``_LIFECYCLE_TYPES``, ``_last_lifecycle_entry``) is a close whose reason proves the id can
never be resumed (``_TERMINAL_CLOSE_REASONS``) and that close is older than the window
(``_expired_unit_id``), and it decides again under the unit's lease (``_still_expired``).
The session tree's reads of a unit's log sit beside it: ``read_last_tree_edge`` takes the
newest adoption or release from the tail window the sweep reads, through the same walk
(``_last_entry_of_types``); ``find_last_tree_edge`` reads every segment when that window
cannot answer (``_scan_whole_for_types``); ``tree_edge_scan_identity`` says when an earlier
complete answer still holds; and ``find_spawned_entries`` returns the ``session/spawned``
entries a creator recorded over its whole log.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Container
from functools import partial
from pathlib import Path

from kiro_crew.crew_log.errors import CrewLogError
from kiro_crew.crew_log.schema import KIND_SESSION, MAX_ENTRY_BYTES, Entry
from kiro_crew.crew_log.store_paths import _checked_crew_log_root, _segment_first_seq, logger
from kiro_crew.crew_log.store_removal import REMOVE_FAILED, REMOVE_REMOVED, _is_held, remove_unit
from kiro_crew.crew_log.store_segment_reads import (
    _TAIL_WINDOW,
    _parses_to_object,
    _read_header_line,
    _scan_tail,
    _Tail,
)
from kiro_crew.jsonl_util import UnreadableRecord, strict_raw_records
from kiro_crew.session_ledger import _store_name, is_link

#: The teardown marker. A unit whose newest lifecycle entry is not this one is
#: OPEN, whatever its age.
_TYPE_SESSION_CLOSED = "session/closed"
#: The revival marker. It is written on create AND on a re-attach to an existing
#: conversation, so one appearing after a close means the session came back.
_TYPE_SESSION_OPENED = "session/opened"
#: The two entries that move a session between open and closed. Everything else --
#: a turn, a tool, an in-flight closer landing after a teardown -- says nothing
#: about which state the unit is in.
_LIFECYCLE_TYPES = frozenset({_TYPE_SESSION_OPENED, _TYPE_SESSION_CLOSED})

#: The ONE close reason that is positive proof the session's ACP id can never be
#: resumed: ``destroy`` deletes that id's mapping outright
#: (``_session_map.delete``), unconditionally, inside the registry lock and before
#: this entry is written. An id absent from the map cannot be resumed by anything.
#:
#: ``reset`` is deliberately NOT here, and the reason is worth stating because the
#: opposite reading is intuitive: a reset does cold-start its successor on a new
#: id, but its own ``clear_sid`` is guarded by ``if clear_conversation and session
#: is not None``, so a reset that keeps the conversation emits
#: ``session/closed {reset}`` while LEAVING the old id mapped -- still resumable,
#: and its log still needed. ``discarded`` clears the sid unconditionally and would
#: qualify on that test, but no path writes that reason into a crew log today, so
#: admitting it would be a rule about a file nothing produces. Every other reason --
#: a shutdown, a crash, an eviction, or a spelling this build does not know -- ends
#: the gateway's SERVICE of the session without ending the id's life.
#:
#: This reason is the whole authorization for deleting a unit, and it is read from
#: the crew log rather than from anything outside it. The obvious alternative -- ask
#: which sessions are still mapped in ``session_map.json`` and keep those -- was
#: implemented and then removed, because that file is agent-WRITABLE while this tree
#: is bind-masked, and a VALID EMPTY map is not a failed read: it reads as "nothing
#: is revivable" and hands a trusted sweep a positive answer that authorizes
#: deleting a fenced unit the writer of that file cannot touch directly. Absence of
#: protection must never be authorization.
_TERMINAL_CLOSE_REASONS = frozenset({"destroyed"})


def _is_terminal_close(entry: "Entry") -> bool:
    """Whether *entry*'s reason proves this session's id can never be resumed."""
    data = entry.data
    reason = data.get("reason") if isinstance(data, dict) else None
    return isinstance(reason, str) and reason in _TERMINAL_CLOSE_REASONS


def sweep_expired(retention_days: int, *, now: float | None = None) -> "tuple[int, int]":
    """Remove CLOSED session logs older than *retention_days*. ``(removed, failed)``.

    The same switch and the same sweep that already expire transcript archives:
    ``history._cleanup_old_archives`` calls this with the retention it resolved
    from ``session.archive_retention_days``, inside the hourly throttle it
    already has. A negative value disables both halves, so a user who turned
    archive expiry off has turned this off too and there is no second setting to
    find. Bodies live in these files now, which is why one switch has to govern
    both: expiring the transcript while its crew log grew forever is the gap this
    closes.

    A missing root, or one holding no unit directories, costs a directory listing
    and answers ``(0, 0)``. That is also what makes this de facto gated by
    ``KIROCREW_CREW_LOG`` without reading it: only the emitter creates
    session units, and the emitter is inert without the flag. Reading the flag
    HERE would be worse than not reading it -- turning it off would strand every
    crew log already written, permanently, since nothing else collects them.

    Crew logs are out of scope and are never scanned: this walks
    ``crew-log/sessions`` alone. A crew's log IS written -- the crew emitter
    appends a dispatch and the reports that answer it -- and it is still not aged
    here, because nothing in that file says when its history stops being wanted. A
    session's does: ``session/closed`` names the moment the unit's life ended. A
    crew outlives every item it dispatched, so ageing one needs a rule of its own
    rather than a session's lifecycle applied to a unit that has none.

    A unit is collectable only when its own log PROVES the session is finished:
    the newest lifecycle entry is a ``session/closed`` whose reason is one of
    :data:`_TERMINAL_CLOSE_REASONS`, meaning the gateway cleared that ACP id's
    mapping during the teardown so it can never be resumed. Nothing outside the
    crew log tree takes part in that decision -- see that constant for why the
    session map was tried for it and removed.
    """
    if retention_days < 0:
        return (0, 0)
    try:
        # The CHECKED root, the same one ``crew_log_dir`` resolves under. A linked or
        # out-of-home kind directory is refused here rather than one unit at a
        # time: the removal itself is already refused downstream, but only after
        # this walk had read a header and a tail from every file under whatever the
        # link named. One refusal reads nothing.
        children = list(_checked_crew_log_root(KIND_SESSION).iterdir())
    except CrewLogError:
        # A root that EXISTS and is wrong. Not a unit failure -- there is no
        # legitimate unit here to have failed -- so it is reported and the pass
        # ends rather than being counted as work.
        logger.warning("crew log retention: refusing to sweep the session log root", exc_info=True)
        return (0, 0)
    except OSError:
        # Includes the ordinary case of a root that was never created.
        return (0, 0)
    cutoff_ms = int(((time.time() if now is None else now) - retention_days * 86400) * 1000)
    removed = 0
    failed = 0
    for child in children:
        try:
            if is_link(child) or not child.is_dir():
                continue
            unit_id = _expired_unit_id(child, cutoff_ms)
            if unit_id is None:
                continue
            # Re-read the SAME decision inside the removal's own lease hold. The
            # scan above is a snapshot, and ownership ends between turns by
            # design, so a session can be revived, append its ``session/opened``,
            # finish its turn and release the lease in the window between the two
            # -- after which an unguarded removal would take a live conversation's
            # log while contending with nobody.
            status = remove_unit(
                KIND_SESSION, unit_id, guard=partial(_still_expired, cutoff_ms, unit_id)
            )
        except (CrewLogError, OSError):
            # One unreadable unit must not stop the pass over the others, and it
            # is not a removal: the unit keeps its history and the next pass sees
            # it again.
            logger.debug("crew log retention: skipped %s", child.name, exc_info=True)
            failed += 1
            continue
        if status == REMOVE_REMOVED:
            removed += 1
        elif status == REMOVE_FAILED:
            failed += 1
    if removed or failed:
        logger.info(
            "crew log retention: removed %d expired session log(s) (>%dd), %d could not be removed",
            removed,
            retention_days,
            failed,
        )
    return (removed, failed)


def _still_expired(cutoff_ms: int, expected: str, directory: Path) -> bool:
    """The sweep's re-decision, re-derived from the unit as it stands NOW.

    Bound to its first two arguments and handed to :func:`remove_unit` as the
    ``guard`` it calls under the lease. Re-deriving the ID as well as the age is
    deliberate: it refuses a directory that changed identity in the same window,
    so the removal can only proceed against the unit the scan actually chose.
    """
    return _expired_unit_id(directory, cutoff_ms) == expected


def _expired_unit_id(directory: Path, cutoff_ms: int) -> "str | None":
    """The raw id of the unit in *directory* when it is closed and expired.

    ``None`` means leave it alone, and every path to ``None`` is deliberate:

    * **No segment, or a header this directory does not answer to.** The id is
      read from the OLDEST surviving segment, which is where ``CrewLog.open``
      resolves it, and it is accepted only if it folds BACK to this directory's
      own name. A directory no id addresses is not removed, because the removal
      would be aimed by id and would resolve somewhere else.
    * **A trash hold** (:data:`TRASH_HOLD_FILE`). The unit is live while the rest of
      its session waits in the trash, so the user can still restore it whole.
    * **A torn tail.** Unterminated trailing bytes are what
      ``open(repair=True)`` truncates, and the sweep cannot tell a dead writer's
      crash artifact from an append that has not yet reached its fsync -- the
      bytes are identical. Deleting the unit would destroy the history the repair
      exists to recover, so the repair gets it and retention does not.
    * **The newest lifecycle entry is not a ``session/closed``.** An OPEN session
      is never touched, whatever its age, and this is the check that decides it.
      The pair ``session/opened`` / ``session/closed`` is what moves a unit
      between the two states, so the newest of the PAIR is the answer -- not the
      newest close on its own. A resumed session appends to the crew log it already
      had, so ``... closed ... opened ...`` is a legitimate file whose session is
      running right now. The read is bounded, so the deciding entry could in
      principle sit further back than the window -- but only entries written after
      it can push it out, and once a unit is closed the emitter writes nothing but
      a handful of in-flight closers unless the session was revived, which appends
      its own ``session/opened``. So a lifecycle entry outside the window means a
      live session, which is exactly the unit that must be kept: the bound fails
      closed rather than needing a full scan of every crew log on every pass.
    * **A segment that vanished mid-read.** Another remover -- the delete funnel,
      or a second pass -- got there first, which is the outcome this sweep wanted.
      It reads as "nothing to do" rather than as a failure, because the caller's
      failure tally is what an operator reads as "these units still hold
      history", and counting a race that already collected one would make that
      number a lie.

    The age comes from the LAST ``session/closed`` entry's own ``time``, falling
    back to the newest segment's mtime only when that field is unusable. The
    entry wins because it is the writer's own record of when the session ended
    and nothing rewrites it, while mtime is metadata a copy, a restore or a
    backup tool resets -- a restored tree would read as freshly closed and never
    expire. The fallback is kept rather than skipping the unit because a damaged
    ``time`` still proves the session ended, and mtime is then the best available
    bound on when writing stopped; it can only be at or after the real close, so
    it errs toward keeping the file.
    """
    try:
        segments = [
            (first, child)
            for child in directory.iterdir()
            if (first := _segment_first_seq(child)) is not None
        ]
    except OSError:
        return None
    if not segments or _is_held(directory):
        return None
    segments.sort(key=lambda pair: pair[0])
    oldest = segments[0][1]
    newest = segments[-1][1]
    try:
        raw_header = _read_header_line(oldest)
        if raw_header is None:
            return None
        parsed = _parses_to_object(raw_header)
        unit_id = parsed.get("id") if parsed else None
        if not isinstance(unit_id, str) or not unit_id:
            return None
        if _store_name(unit_id) != directory.name:
            return None
        tail = _scan_tail(newest)
    except FileNotFoundError:
        # A segment listed a moment ago is gone: another remover -- the delete
        # funnel, or a second pass -- got there first. That is the outcome this
        # sweep wanted, so it reads as "nothing to do" and is deliberately NOT
        # counted as a failure; the caller's ``failed`` tally is what an operator
        # reads as "these units still hold history", and a race that already
        # collected one would make that number a lie.
        return None
    except (OSError, ValueError):
        return None
    if tail.empty or tail.torn_offset is not None:
        return None
    closed = _last_lifecycle_entry(tail)
    if closed is None or closed.type != _TYPE_SESSION_CLOSED:
        return None
    if not _is_terminal_close(closed):
        return None
    closed_ms = closed.time
    if not isinstance(closed_ms, int) or closed_ms <= 0:
        try:
            closed_ms = int(newest.stat().st_mtime * 1000)
        except OSError:
            return None
    return unit_id if closed_ms < cutoff_ms else None


def _last_lifecycle_entry(tail: _Tail) -> "Entry | None":
    """The newest ``session/opened`` or ``session/closed`` in *tail*'s window.

    Whether a unit is closed is decided by the newest of the PAIR, not by the
    newest close on its own. A resumed session appends to the crew log it already
    had, so a file legitimately reads ``... closed ... opened ...`` -- one close
    followed by the revival that outlived it -- and a scan that stopped at the
    close would call a session that is running right now expired and delete a
    live conversation's log.

    :data:`_LIFECYCLE_TYPES` holds those two types and no others, and nothing that
    moves a session in the TREE belongs in it: this answer authorizes a DELETION,
    so a type added here makes a unit whose newest such entry is not a close
    immortal in retention. The tree's own types are read by
    :func:`read_last_tree_edge`, which shares the window walk below and keeps its
    own set.
    """
    return _last_entry_of_types(tail, _LIFECYCLE_TYPES)


#: The two entry types that move a session in the TREE, read from a unit's tail.
#: Deliberately NOT in :data:`_LIFECYCLE_TYPES`: that set decides open versus
#: closed and therefore authorizes retention's delete, so an adoption sitting at
#: the end of a log would keep that log forever.
_TREE_EDGE_TYPES = frozenset({"session/adopted", "session/released"})


def read_last_tree_edge(segment: Path) -> "Entry | None":
    """The newest tree-edge entry in *segment*'s tail window, or ``None``.

    An adoption or a release lands after the session opened, so a reader of a
    unit's HEAD cannot see it. This is the counterpart read, and it is the same
    bounded window retention already reads -- one open, at most ``_TAIL_WINDOW``
    bytes, no walk of the file.

    BOUNDED, and that bound is why this is not the whole answer: only entries written
    AFTER a decision can push it out of the window, and a session that keeps working
    keeps appending. :func:`find_last_tree_edge` is the complete read a cold rebuild
    needs; this one is the cheap first look it starts from, and the one a warm scan of a
    live unit uses because a live unit's decision is at its end.

    Raises whatever the read raises, like :func:`read_head`: a caller distinguishes
    "this unit has no decision" from "its bytes were not seen", and only the first
    is something to cache.
    """
    tail = _scan_tail(segment)
    if tail.empty:
        return None
    return _last_entry_of_types(tail, _TREE_EDGE_TYPES)


def tree_edge_scan_identity(directory: Path) -> "tuple[int, int, int, int] | None":
    """What must be unchanged for an earlier COMPLETE tree-edge read of this unit to
    still be its answer, or ``None`` when the unit has no segments.

    :func:`find_last_tree_edge` is the only honest read for a rebuild and it is
    proportional to the unit's whole log, because a unit that records no decision is
    only proved to record none by reading all of it. Paying that on every process start
    for every unit is what this exists to avoid: the verdict is a pure function of the
    bytes, so it can be cached across boots as long as something can say the bytes are
    the same ones.

    Four numbers, each closing a different way the answer could go stale:

    * the directory's own mtime, which moves when a segment is ADDED or REMOVED -- a
      rotation, or retention taking the oldest;
    * the number of segments, because two changes inside one mtime granularity can leave
      the directory looking untouched while its contents are not;
    * the newest segment's size and mtime, which move on an APPEND -- the one change that
      does not touch the directory at all, and the one that can add a decision.

    Deliberately NOT a content hash: this runs per unit on a boot path, and the whole
    point is to be cheaper than reading the bytes. It is a staleness check, not proof of
    identity -- so it is used only to skip re-deriving a NEGATIVE verdict, where being
    wrong costs a decision that is re-read on the next change, and never to admit an edge
    that no read produced.

    Raises what the stats raise. A caller that cannot get an identity must do the read.
    """
    newest: tuple[int, Path] | None = None
    count = 0
    for child in directory.iterdir():
        first = _segment_first_seq(child)
        if first is None:
            continue
        count += 1
        if newest is None or first > newest[0]:
            newest = (first, child)
    if newest is None:
        return None
    stat = newest[1].stat()
    return (directory.stat().st_mtime_ns, count, stat.st_size, stat.st_mtime_ns)


def find_last_tree_edge(directory: Path) -> "Entry | None":
    """The newest tree-edge entry in the WHOLE of *directory*'s surviving log, or
    ``None``.

    What a cold rebuild needs, and the reason it cannot use the tail window alone: a
    decision is silently lost when the log grew past that window after it was written,
    and the consequence of losing one is not a missing edge but a WRONG one -- the
    sidebar restores the creating edge and shows a session under a parent that gave it
    up, with nothing to correct it. A bounded read is the right cost for a live unit and
    the wrong answer for a rebuild that has no checkpoint to fall back on.

    Segments are searched NEWEST FIRST, and the first one that answers wins: a decision
    in a newer segment supersedes anything an older one holds, so the walk stops at the
    first hit rather than reading the rest. A unit that has never rotated therefore
    costs exactly what the tail read costs, which is the ordinary case; a rotated unit
    costs one read per segment until its newest decision is found, and a unit that
    records none costs one read per segment once.

    Inside a segment the search is still the windowed one for the LAST window, then the
    whole file when the window did not reach its start -- so a decision anywhere in a
    rotated log is found, and the common case pays the cheap read first.

    Raises what the reads raise, for the reason :func:`read_head` does: "no decision"
    and "the bytes were not seen" are different answers and only the first may be
    cached. Narrowed to ``OSError`` and ``ValueError``, which is what every caller here
    guards -- a reader exception outside those two would escape all of them and reach
    the projection's boot guard, which seeds no records at all and then refuses lineage
    for the rest of the process.
    """
    # NOT guarded. A listing that fails has not established that this unit records no
    # decision -- it has established nothing -- and the difference matters because both
    # callers CACHE the answer: the projection's seed would install the creating edge for
    # a session that was moved, and the scanner would store that as its verdict for the
    # segment's whole stat identity, so one moment's fault becomes the tree's standing
    # answer. Each caller already catches ``OSError`` and marks its scan incomplete,
    # which is the honest reading and the one the docstring above promises.
    found = [
        (first, child)
        for child in directory.iterdir()
        if (first := _segment_first_seq(child)) is not None
    ]
    if not found:
        return None
    found.sort(key=lambda pair: pair[0], reverse=True)
    for _first, segment in found:
        entry = read_last_tree_edge(segment)
        if entry is not None:
            return entry
        # The window did not answer. When it did not reach the start of the file there
        # are earlier lines in THIS segment it never saw, so they are read before moving
        # on to an older segment -- otherwise a decision in the middle of a busy log is
        # exactly what goes missing.
        entry = _scan_whole_for_types(segment, _TREE_EDGE_TYPES)
        if entry is not None:
            return entry
    return None


#: The entry a creator's log carries for each session it created (see
#: ``entry_types``). Read separately from :data:`_TREE_EDGE_TYPES`: a unit holds at most
#: one decision that matters -- its newest -- but one ``session/spawned`` per child.
_SPAWNED_TYPE = "session/spawned"
#: The marker a raw record must contain before it is parsed. Every other line in a
#: busy log is skipped without a JSON parse, which is most of the cost of this read.
_SPAWNED_NEEDLE = b'"session/spawned"'


def _spawned_child_slot(entry: "Entry") -> "str | None":
    """The child slot a ``session/spawned`` entry names, or ``None`` when it is shaped
    wrong. Mirrors the field :func:`session_tree.spawned_record` keys the edge on, so
    the two agree about which child an entry is about."""
    child = entry.data.get("child")
    if not isinstance(child, dict):
        return None
    slot = child.get("slot")
    return slot if isinstance(slot, str) and slot else None


def find_spawned_entries(
    directory: Path, limit: int, self_parented: "Container[str]" = frozenset()
) -> "tuple[list[Entry], bool, bool]":
    """The NEWEST *limit* ``session/spawned`` entries in *directory*'s surviving log,
    oldest first, plus two flags: whether any older LOAD-BEARING one was left out
    (``cut``), and whether the per-unit cap was hit at all (``hit_cap``).

    The whole log, every segment, because a creator records its children over its
    whole life and any of them may still be waiting for a first turn. The newest are
    kept because the newest children are the ones most likely to have no log of their
    own yet. A reader holds what it returns, so the bound is the caller's.

    The held set is ALWAYS the newest *limit* spawns of every child, independent of
    *self_parented* -- a pure function of this log, so a caller may cache it by this
    log's identity.

    *self_parented* names child slots the caller already knows have a parent edge of
    their OWN -- the child opened a log that names a parent, which the fold applies
    instead of this spawn (:func:`session_tree.fold_tree`: a spawn fills a child's
    citation "only when none of the child's own records names a parent"). It affects
    ONLY the ``cut`` flag: an evicted spawn whose child is self-parented is not counted
    lost, since the fold would discard it anyway. Without this, a long-running creator
    that evicted more than *limit* children over its life -- all long since started and
    self-recorded -- would report ``cut`` on every scan, which the fold's reader turns
    into "lineage unknown" and uses to refuse adopt/release authority checks
    gateway-wide until retention pruned the log, though no evicted spawn was
    load-bearing. The default empty set keeps the plain "every eviction counts".

    ``hit_cap`` is the RAW fact that the cap evicted at least one spawn, self-parented
    or not. A caller caches a scan by this log's identity, but a self-parented child
    whose spawn was evicted can later have its OWN log removed WITHOUT changing this
    creator's identity -- at which point that evicted spawn becomes load-bearing and the
    cached "complete" answer is wrong. So a caller must refuse to cache (and re-read
    next time) whenever ``hit_cap`` is set, even when ``cut`` was suppressed.

    Both flags are load-bearing. A cut answer is not the unit's whole answer, so a
    caller must not report a reading built from it as complete, and a capped scan must
    not be cached as the unit's whole answer.

    Raises what the reads raise, as ``OSError`` or ``ValueError``, for the reason
    :func:`find_last_tree_edge` does: "no child recorded" and "the bytes were not seen"
    are different answers, and only the first may be cached.
    """
    found = [
        (first, child)
        for child in directory.iterdir()
        if (first := _segment_first_seq(child)) is not None
    ]
    found.sort(key=lambda pair: pair[0])
    kept: "deque[Entry]" = deque(maxlen=max(limit, 0))
    # The held set is the newest *limit* spawns of EVERY child, independent of
    # *self_parented*: it is cached by the creator log's own identity, so it must stay a
    # pure function of that log. Filtering the held set by the children's logs would let
    # a later change to a child's log (its deletion, a retracted parent) leave a stale
    # cache missing a now-needed edge. *self_parented* instead relaxes only the cut flag:
    # a dropped spawn is counted lost only when its child is NOT self-parented, since the
    # fold would discard a self-parented child's spawn anyway.
    cut = False
    hit_cap = False
    for _first, segment in found:
        if segment.stat().st_size == 0:
            continue
        with open(segment, "rb") as handle:
            try:
                for raw in strict_raw_records(handle, segment, cap=MAX_ENTRY_BYTES):
                    if _SPAWNED_NEEDLE not in raw:
                        continue
                    parsed = _parses_to_object(raw.strip())
                    if parsed is None:
                        continue
                    entry = Entry.from_dict(parsed)
                    if entry is not None and entry.type == _SPAWNED_TYPE:
                        # Appending to a full maxlen deque evicts the oldest. The cap
                        # was hit either way (hit_cap, which gates CACHING); the eviction
                        # is only a lost answer -- cut, which gates the INCOMPLETE
                        # signal -- when the evicted child is not self-parented.
                        if kept.maxlen and len(kept) == kept.maxlen:
                            hit_cap = True
                            if _spawned_child_slot(kept[0]) not in self_parented:
                                cut = True
                        kept.append(entry)
            except UnreadableRecord as exc:
                # Same conversion :func:`_scan_whole_for_types` makes, so every caller
                # guards one pair of exception types.
                raise ValueError(
                    f"crew log segment {segment.name} holds an unreadable record"
                ) from exc
    return list(kept), cut, hit_cap


def _scan_whole_for_types(segment: Path, types: "frozenset[str]") -> "Entry | None":
    """The newest entry of *types* anywhere in *segment*, or ``None``.

    Only reached when the bounded window did not answer and could not prove it saw the
    whole file. It streams the records rather than holding the file, and keeps the last
    match instead of stopping at the first: the newest one is the answer, and a forward
    stream meets it last.

    Raises what the stat and the read raise, as ``OSError`` or ``ValueError`` and nothing
    else -- byte damage from the framing reader is converted below, because the callers
    that guard this one guard exactly those two. ``None`` therefore means the file holds
    no such entry, never that it could not be looked at -- the distinction the tree-edge
    caller turns into a CACHED verdict, which nothing afterwards re-reads or corrects.
    The ``open`` below already propagates, so guarding only the stat made one function
    answer two different ways about the same file.
    """
    size = segment.stat().st_size
    if size == 0:
        return None
    if size <= _TAIL_WINDOW:
        # The window already covered this whole file, so there is nothing new to read.
        return None
    newest: Entry | None = None
    with open(segment, "rb") as handle:
        try:
            for raw in strict_raw_records(handle, segment, cap=MAX_ENTRY_BYTES):
                parsed = _parses_to_object(raw.strip())
                if parsed is None:
                    continue
                entry = Entry.from_dict(parsed)
                if entry is not None and entry.type in types:
                    newest = entry
        except UnreadableRecord as exc:
            # Byte damage -- an over-cap record is the reachable case, and the framing
            # reader ABORTS on it, so everything after it in this file is unseen.
            # Reported as a failed READ rather than returned as ``newest``, because the
            # unseen part is where a later decision would be: answering with the newest
            # readable one would be a WRONG edge served as complete, which is the whole
            # failure this function exists to prevent.
            #
            # Converted to ``ValueError`` rather than propagated as-is. Every caller of
            # this file's tree-edge reads guards ``(OSError, ValueError)`` and turns a
            # raise into "incomplete"; ``UnreadableRecord`` is neither, so it escaped all
            # of them into the projection's own boot guard, which marks the projection
            # seeded with no records at all and refuses lineage for the process lifetime.
            # One conversion here, rather than a class added to three separate tuples
            # that would then have to stay in step.
            raise ValueError(f"crew log segment {segment.name} holds an unreadable record") from exc
    return newest


def _last_entry_of_types(tail: _Tail, types: "frozenset[str]") -> "Entry | None":
    """The newest entry in *tail*'s window whose type is in *types*.

    Searched from the END, so a file with many turns costs one comparison per
    trailing entry rather than a parse of the whole window. Entries outside *types*
    are skipped, and each caller brings its own set: the question "is this unit
    closed" and the question "where does this slot hang" are answered from the same
    bytes and must not share a vocabulary, because the first one authorizes a
    delete.

    Reuses the window the tail scan already read, so this costs no second read.
    """
    lines = tail.window.split(b"\n")
    if not tail.at_start:
        # The window may begin mid-line; that first fragment is not a record.
        lines = lines[1:]
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        parsed = _parses_to_object(stripped)
        if parsed is None:
            continue
        entry = Entry.from_dict(parsed)
        if entry is not None and entry.type in types:
            return entry
    return None
