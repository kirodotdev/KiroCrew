"""``CrewLog``: one unit's append-only crew log, the handle every caller holds.

``create`` and ``open`` (with ``repair``), the appends under write ownership, and the reads
-- ``iter_from``, ``page``, ``resolve`` and the walks a caller resumes -- are methods of the
class, which composes the store owners below it: paths and locks, segment reads and writes,
the cut counter, the tail repair and the read results. A new operation on one open unit
goes here.
"""

from __future__ import annotations

import hashlib
import logging
import weakref
from collections import deque
from collections.abc import Callable, Collection, Iterator
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.crew_log.entry_types import validate_data
from kiro_crew.crew_log.errors import (
    CODE_ALREADY_EXISTS,
    CODE_BAD_DATA,
    CODE_BAD_HEADER,
    CODE_BAD_SEGMENT,
    CODE_BAD_THREAD,
    CODE_NO_LEDGER,
    CODE_SEGMENT_GAP,
    CODE_UNKNOWN_ENTRY_TYPE,
    CrewLogError,
)
from kiro_crew.crew_log.lease import LEASE_FILE
from kiro_crew.crew_log.lease import acquire as acquire_lease
from kiro_crew.crew_log.lease import release as release_lease
from kiro_crew.crew_log.schema import (
    MAX_ENTRY_BYTES,
    Entry,
    Header,
    Ref,
    build_header,
    check_ownership,
    parse_header,
    require_data,
    require_entry_line,
    require_kind,
    serialize,
)
from kiro_crew.crew_log.store_cuts import _seed_cuts, read_cuts
from kiro_crew.crew_log.store_paths import (
    _lock_path,
    _mkdir_private,
    _open_lock,
    _segment_first_seq,
    crew_log_path,
    log_exception_text,
    logger,
    now_ms,
    segment_first_seqs,
    segment_paths,
)
from kiro_crew.crew_log.store_results import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    STATUS_CORRUPT,
    STATUS_GONE,
    STATUS_OK,
    STATUS_PRUNED,
    Page,
    ReadCursor,
    Resolution,
    ResumePoint,
    _as_ref,
    _covers_span,
)
from kiro_crew.crew_log.store_segment_reads import (
    _anchor_exists,
    _has_content,
    _iter_entries_located,
    _parses_to_object,
    _read_first_entry_seq,
    _read_header_line,
    _scan_tail,
    _Tail,
)
from kiro_crew.crew_log.store_segment_writes import _append_line, _append_lines, _truncate
from kiro_crew.crew_log.store_tail_repair import _close_interrupted_tail
from kiro_crew.jsonl_util import bounded_raw_records


class CrewLog:
    """One unit's append-only crew log.

    Construct through :meth:`create` or :meth:`open`, never directly: both do
    the file-level work (existence, header, torn-tail repair, seq recovery) that
    the instance then assumes has happened.
    """

    __slots__ = (
        "_kind",
        "_id",
        "_path",
        "_header",
        "_last_seq",
        "_needs_newline",
        "_lease_key",
        # The ``weakref.finalize`` that releases this handle's lease when the
        # object is dropped. Held so :meth:`release_ownership` can run it
        # DETERMINISTICALLY -- dropping the last reference frees the lease only
        # once the cyclic collector runs the finalizer, and a caller retiring
        # this handle (a test boundary, a cache eviction) cannot wait for a GC
        # pass it does not control. See :meth:`_adopt_lease`.
        "_lease_finalizer",
        # Weak-referenceable so this handle being dropped is what releases its
        # write ownership. See :meth:`_claim`.
        "__weakref__",
    )

    def __init__(
        self,
        *,
        kind: str,
        unit_id: str,
        path: Path,
        header: Header,
        last_seq: int,
        needs_newline: bool,
        lease_key: str | None = None,
    ) -> None:
        self._kind = kind
        self._id = unit_id
        self._path = path
        self._header = header
        self._last_seq = last_seq
        self._needs_newline = needs_newline
        self._lease_key: str | None = None
        self._lease_finalizer: "weakref.finalize | None" = None
        if lease_key is not None:
            # A reference already taken on this handle's behalf -- the repair an
            # ``open`` ran before the instance existed. Adopting it here is what
            # binds its release to this handle's lifetime.
            self._adopt_lease(lease_key)

    # -- identity ----------------------------------------------------------- #

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def id(self) -> str:
        return self._id

    @property
    def path(self) -> Path:
        return self._path

    @property
    def header(self) -> Header:
        return self._header

    @property
    def last_seq(self) -> int:
        """The newest seq this instance knows of. Authoritative only for its own
        appends -- the file is re-read under the lock on every write."""
        return self._last_seq

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"CrewLog(kind={self._kind!r}, id={self._id!r}, last_seq={self._last_seq})"

    # -- lifecycle ---------------------------------------------------------- #

    @classmethod
    def exists(cls, kind: str, unit_id: str) -> bool:
        """Whether *unit_id* has a crew log with content. Raises on an invalid id.

        Empty means absent, the same answer ``create`` and ``open`` give, so all
        three agree about a file that carries nothing.
        """
        return any(_has_content(p) for p in segment_paths(kind, unit_id))

    @classmethod
    def create(cls, kind: str, unit_id: str, **header_fields: Any) -> CrewLog:
        """Write a new crew log's header. Refuses if the file already exists.

        The header is PUBLISHED atomically -- temp file, fsync, rename -- while
        every later line is a plain append. Creation is the one moment the file
        holds no history, so nothing is lost by writing it whole, and an
        interrupted create must not be able to leave a partial header behind: a
        short write or ENOSPC mid-header would otherwise wedge the unit forever,
        since ``open`` would read the fragment as a torn tail, truncate it to an
        empty file, and refuse -- while a retried ``create`` refused the very
        file it had just produced. After the rename the file is append-only for
        the rest of its life.

        A ZERO-BYTE file counts as absent for the same reason: it carries no
        header and no entries, so there is nothing to protect and refusing would
        only strand the unit. Anything longer is real content and is refused.

        Existence is checked twice, the second time under the lock: the first
        check is the cheap answer for the ordinary caller, the second is what
        makes "create refuses an existing crew log" true when two processes race.
        """
        kind = require_kind(kind)
        path = crew_log_path(kind, unit_id)
        if _has_content(path):
            raise CrewLogError(
                f"{kind} crew log {unit_id!r} already exists", code=CODE_ALREADY_EXISTS, field="id"
            )
        header = build_header(kind, unit_id, now_ms(), header_fields)
        line = require_entry_line(serialize(header.to_dict()))
        with _open_lock(_lock_path(kind, unit_id)):
            if _has_content(path):
                raise CrewLogError(
                    f"{kind} crew log {unit_id!r} already exists",
                    code=CODE_ALREADY_EXISTS,
                    field="id",
                )
            _mkdir_private(path.parent)
            atomic_write(path, f"{line}\n", fsync=True, newline="\n")
            _seed_cuts(path.parent)
        return cls(
            kind=kind,
            unit_id=unit_id,
            path=path,
            header=header,
            last_seq=0,
            needs_newline=False,
        )

    @classmethod
    def open(
        cls,
        kind: str,
        unit_id: str,
        *,
        repair: bool = False,
        child_gone: "Callable[[str], bool] | None" = None,
    ) -> CrewLog:
        """Open an existing crew log, repairing a torn tail if there is one.

        ``child_gone`` narrows what ``repair`` is allowed to conclude, and is the
        only thing that lets it close an unmatched ``subagent/spawned``. A child
        outlives its asking turn by design, so an unmatched opener does NOT mean
        the child is finished -- a writer torn down inside a live process can leave
        one still running and still able to file its own real terminal, which would
        put two outcomes for one ``agent_id`` in a file nothing rewrites. No fact
        derivable from the file answers this, and no flag does either: what settles
        it is the live subagent registry of the process doing the repair, so the
        caller that holds one passes a predicate over ``agent_id``. Omitted, no
        child is closed -- the direction that leaves a reader behind rather than
        wrong.

        A zero-byte file answers ``no_ledger``, the same answer ``create`` gives
        it. One meaning for an empty file across both paths is what keeps them
        from disagreeing: a create that could not finish leaves nothing behind,
        and a retry succeeds instead of being refused by the fragment.

        ``repair`` is what closes an INTERRUPTED TURN, and it is opt-in because
        the callers of ``open`` want opposite things. A RESUME -- the gateway
        finding a session log whose writer is gone -- wants the open turn
        closed, and passes ``repair=True``. A live writer RECONNECTING to its own
        crew log must not: its turn is still running, and closing it would append a
        ``turn/completed {interrupted}`` in the middle of a turn that then keeps
        writing, so the record would claim an outcome the turn never had. A
        reconnect happens for reasons that have nothing to do with the writer's
        health -- a handle evicted from a bounded cache is enough -- so repair
        cannot be inferred from the fact that an ``open`` is happening at all. The
        third caller is a SUPERSEDE, which wants the same thing a resume wants but
        for a store that is not its own, and reaches it through
        :meth:`repair_interrupted_turn` rather than this flag so it can report how
        many closers landed. See :func:`_close_interrupted_tail`; the torn-tail
        truncation below is unconditional because trailing bytes that are not a
        complete line are not a record, so nothing can be reading them.
        """
        kind = require_kind(kind)
        # The NEWEST segment is the one a writer appends to, and with nothing
        # rotating yet that is ``log.jsonl`` in every crew log that exists. Taking
        # it from the segment list rather than the fixed name is what lets a log
        # whose oldest segment was pruned still open: retention removes files off
        # the front, and the header travels on each segment so the survivor carries
        # it.
        segments = [
            candidate for candidate in segment_paths(kind, unit_id) if _has_content(candidate)
        ]
        if not segments:
            raise CrewLogError(
                f"no {kind} crew log for {unit_id!r}", code=CODE_NO_LEDGER, field="id"
            )
        path = segments[-1]
        header_path = segments[0]
        # Under the lock, always: an append holds it from its write through its fsync
        # and any rollback, so a tail read under it sees only lines the writer has made
        # durable or has already taken back. A read without it can land between the
        # flush and a failed fsync and hand a fold a line that is truncated a moment
        # later. The same pass drops a torn tail.
        tail = cls._settle_tail(kind, unit_id, path)
        raw = _read_header_line(header_path)
        parsed = None if not raw else _parses_to_object(raw)
        if parsed is None:
            raise CrewLogError(
                f"{kind} crew log {unit_id!r} has no readable header line",
                code=CODE_BAD_HEADER,
                field="type",
            )
        header = parse_header(parsed, kind=kind, unit_id=unit_id)
        lease_key: str | None = None
        if repair:
            # Ownership BEFORE the closers, and the reason repair is the one thing
            # ``open`` claims it for: the closers are appends, and a repair running
            # while a writer in ANOTHER process still holds the turn is what makes
            # the file state two outcomes for one turn. Refused here, this open
            # raises and nothing is written -- the live writer's log is left as it
            # stands.
            _mkdir_private(path.parent)
            lease_key = acquire_lease(path.parent / LEASE_FILE, kind=kind, unit_id=unit_id)
            try:
                if _close_interrupted_tail(kind, unit_id, path, child_gone=child_gone):
                    # The closers moved the tail, so this object's cached seq has to
                    # be re-read or its first append would collide with them.
                    tail = _scan_tail(path)
            except BaseException:
                release_lease(lease_key)
                raise
        return cls(
            kind=kind,
            unit_id=unit_id,
            path=path,
            header=header,
            last_seq=tail.last_seq,
            needs_newline=tail.needs_newline,
            lease_key=lease_key,
        )

    @staticmethod
    def _settle_tail(kind: str, unit_id: str, path: Path) -> _Tail:
        """Re-read *path*'s tail under the lock, dropping torn trailing bytes."""
        with _open_lock(_lock_path(kind, unit_id)):
            tail = _scan_tail(path)
            if tail.torn_offset is not None:
                dropped = path.stat().st_size - tail.torn_offset
                _truncate(path, tail.torn_offset)
                logger.warning(
                    "dropped %d torn trailing byte(s) from %s crew log %r",
                    dropped,
                    kind,
                    unit_id,
                )
        return tail

    def repair_interrupted_turn(self, *, child_gone: "Callable[[str], bool] | None" = None) -> int:
        """Close an open turn on this crew log. Returns how many closers landed.

        The method form of ``open(repair=True)``, for a caller that already holds
        a handle. Two callers reach it: a RESUME, and a SUPERSEDE closing the tail
        of the store its slot was writing before. Same rule for both, and it is
        about the WRITER rather than about which caller asks -- never call this for
        a turn that is still running, whoever is asking. And the
        same ``child_gone`` meaning: without a predicate an unmatched
        ``subagent/spawned`` is left open, because nothing else can rule out a live
        child that is still about to report its own outcome. A supersede passes
        none, because the children it would be answering for were dispatched by a
        session that is gone.
        """
        # The closers are appends, so this takes write ownership exactly as one
        # does, and is refused the same way when another process holds the log.
        self._claim()
        written = _close_interrupted_tail(self._kind, self._id, self._path, child_gone=child_gone)
        # Refreshed unconditionally: the repair also TRUNCATES an unreachable chunk
        # group, which changes the tail without writing a closer, and a cached
        # ``last_seq`` past the end of the file would be served to a reader that only
        # wants to look.
        tail = _scan_tail(self._path)
        self._last_seq = tail.last_seq
        self._needs_newline = tail.needs_newline
        return written

    # -- write ownership ---------------------------------------------------- #

    def _claim(self) -> None:
        """Take write ownership of this unit before the first write of this handle.

        Lazy, and never taken by ``open`` itself, because ``open`` also serves
        READERS: ``iter_from``, ``page`` and ``resolve`` need no ownership, and
        making a reader contend with the writer would be a regression in exchange
        for nothing. The first write is the moment ownership starts to mean
        something, so that is where it is claimed.

        Raises the ``already_owned`` refusal when another PROCESS owns the log.
        Another handle in THIS process is not contention -- it shares the same
        kernel lock through the reference count -- which is what lets the emitter's
        cached handle and a session claim's handle overlap.
        """
        if self._lease_key is not None:
            return
        # The directory of the segment this handle appends to, not one resolved
        # from the data home again: a handle writes the file it was opened on, and
        # the lock has to name that file even if the home was repointed since.
        _mkdir_private(self._path.parent)
        self._adopt_lease(
            acquire_lease(self._path.parent / LEASE_FILE, kind=self._kind, unit_id=self._id)
        )

    def _adopt_lease(self, key: str) -> None:
        """Bind an acquired lease reference to this handle's lifetime.

        Released when this object is dropped, so a holder keeps ownership for
        exactly as long as it can still append through it. That places one
        requirement on whatever holds a handle: it must not drop one belonging to
        a LIVE turn, or ownership ends while that turn is still producing entries
        and a successor's repair closes a turn that completes for real a moment
        later. Both of the emitter's release paths honour it -- capacity eviction
        and session teardown each skip a session with a live turn -- so ownership
        ends between turns, where there is no live turn for a repair to damage.

        Binding release to the object rather than to an explicit call is also what
        keeps a queued write that still holds the handle inside the ownership it
        needs: an eager release at eviction would take it out from under a job
        that is about to append.
        """
        self._lease_key = key
        self._lease_finalizer = weakref.finalize(self, release_lease, key)

    def release_ownership(self) -> None:
        """Release this handle's write lease NOW, without waiting for a GC pass.

        Dropping the last reference to a handle frees its lease through the
        ``weakref.finalize`` :meth:`_adopt_lease` armed -- but only once the
        cyclic collector RUNS that finalizer, and a handle reachable only through
        a reference cycle (the member event-log service holds one: its projection
        registry keeps a bound method back to it) is never freed by refcount, so
        the release waits for a generational GC pass. That wait is invisible on
        POSIX -- an advisory lock on a torn-down directory harms nothing and the
        next acquire against a fresh path just works -- but on Windows the still-open
        descriptor keeps a mandatory lock on the file and pins its directory open,
        so the directory cannot be removed and a later write under the same
        inherited process fails. A caller retiring this handle deterministically
        (a test boundary, a cache eviction that is not GC-driven) runs the
        finalizer itself instead.

        ``weakref.finalize`` is idempotent and atomic: calling the finalizer here
        runs ``release_lease`` exactly once and marks it dead, so the later GC-time
        call it would have made is a no-op. Safe to call more than once and safe on
        a handle that never took a lease (a pure reader), where there is nothing to
        run.
        """
        finalizer = self._lease_finalizer
        if finalizer is not None:
            finalizer()
        self._lease_finalizer = None
        self._lease_key = None

    # -- write -------------------------------------------------------------- #

    def append(
        self,
        type: str,
        data: dict[str, Any],
        *,
        src: str,
        thread: int | None = None,
        ref: Ref | dict[str, Any] | None = None,
        ignorable: bool = False,
    ) -> Entry:
        """Append one entry and return it, with ``seq`` and ``time`` filled in.

        Every refusal happens before any byte is written, so a rejected append
        leaves the file identical. ``thread`` is checked against the seq read
        back under the lock, which is also what assigns this entry's own seq.

        ``ignorable`` promises that nothing later in the file depends on this
        entry being understood, so a reader that does not know the type may skip
        it instead of stopping. Only the writer can make that promise -- it is
        the one that knows whether the entry is a sample or a fact -- which is
        why it lives on the append and not on the read.
        """
        entry = self._append(
            type,
            data,
            src=src,
            thread=thread,
            ref=ref,
            ignorable=ignorable,
            max_tail_seq=None,
        )
        assert entry is not None  # no seq bound, so nothing can decline
        return entry

    def append_if(
        self,
        type: str,
        data: dict[str, Any],
        *,
        src: str,
        max_tail_seq: int,
        thread: int | None = None,
        ref: Ref | dict[str, Any] | None = None,
        ignorable: bool = False,
    ) -> Entry | None:
        """:meth:`append`, written only while the file's tail is still *max_tail_seq*.

        The tail is read AFTER write ownership and the per-append lock are both held,
        and the entry is written only when that tail is at or below *max_tail_seq*.
        Returning ``None`` means it declined: no entry was appended. The file is not
        guaranteed byte-identical across a decline, because the tail read happens
        first and a torn trailing record is repaired as soon as it is seen -- that
        repair is the store's, owed to the file rather than to this append, and it is
        the one change a decline can leave behind.

        This exists because a decision made BEFORE the append is not ordered against
        another PROCESS. The log has more than one writer, so an entry committed
        between a caller's decision and its own write lands FIRST and the caller's
        entry lands after it -- and for a last-wins projection that ordering is the
        whole outcome, so a decision that was true when it was made is applied to a
        state that has since moved. The caller cannot close that window from outside:
        every check it makes is still outside the ownership that decides the order.
        *max_tail_seq* is the seq the caller's decision was made against, and
        comparing it here is what proves nothing was committed in between.

        A seq rather than a callback, deliberately: an int cannot parse the file or
        write to it, so a caller cannot turn this hold -- which refuses every other
        process's append to the unit while it lasts -- into a long one. A caller that
        needs to read the log to decide does so BEFORE calling, and passes the tail
        that read reached.
        """
        return self._append(
            type,
            data,
            src=src,
            thread=thread,
            ref=ref,
            ignorable=ignorable,
            max_tail_seq=max_tail_seq,
        )

    def _append(
        self,
        type: str,
        data: dict[str, Any],
        *,
        src: str,
        thread: int | None,
        ref: Ref | dict[str, Any] | None,
        ignorable: bool,
        max_tail_seq: int | None,
    ) -> Entry | None:
        """The one append body, shared so the two entry points cannot drift apart."""
        require_data(data)
        check_ownership(self._kind, type, src)
        validate_data(self._kind, type, data)
        pointer = None if ref is None else _as_ref(ref)
        if thread is not None and (
            not isinstance(thread, int) or isinstance(thread, bool) or thread < 1
        ):
            raise CrewLogError(
                f"thread must be a positive seq in this crew log: {thread!r}",
                code=CODE_BAD_THREAD,
                field="thread",
            )
        # After the format checks, so a malformed entry is refused without this
        # process claiming anything, and before the write lock, so ownership is
        # settled before any byte is at stake.
        self._claim()
        with _open_lock(_lock_path(self._kind, self._id)):
            tail = _scan_tail(self._path)
            if tail.empty:
                raise CrewLogError(
                    f"no {self._kind} crew log for {self._id!r}",
                    code=CODE_NO_LEDGER,
                    field="id",
                )
            if tail.torn_offset is not None:
                _truncate(self._path, tail.torn_offset)
            if thread is not None and (
                thread > tail.last_seq or not _anchor_exists(self._path, thread, tail)
            ):
                raise CrewLogError(
                    f"thread {thread} names no parseable entry in this crew log "
                    f"(newest seq is {tail.last_seq})",
                    code=CODE_BAD_THREAD,
                    field="thread",
                )
            # Compared against the tail just read, under the ownership that decides
            # this entry's order, and before any byte is written -- so a decline
            # appends nothing. It is not the same as a format refusal above: those
            # happen before the tail is read, while the torn-tail repair just above
            # is unconditional, so a decline can still leave that repair behind.
            if max_tail_seq is not None and tail.last_seq > max_tail_seq:
                return None
            entry = Entry(
                type=type,
                seq=tail.last_seq + 1,
                time=now_ms(),
                src=src,
                data=data,
                thread=thread,
                ref=pointer,
                ignorable=bool(ignorable),
            )
            line = require_entry_line(serialize(entry.to_dict()))
            _append_line(self._path, line, needs_newline=tail.needs_newline)
        self._last_seq = entry.seq
        self._needs_newline = False
        return entry

    def append_many(
        self,
        items: "list[dict[str, Any]]",
        *,
        src: str,
        cite: "Callable[[list[int]], dict[str, Any]] | None" = None,
    ) -> "list[Entry]":
        """Append a GROUP of entries as one write, and return them.

        For a group whose members are meaningless apart: a body too large for one
        line becomes several ``message/chunk`` entries plus the entry that CITES
        their seqs, and appending those one at a time leaves a window where a hard
        kill puts the chunks on disk with nothing pointing at them -- the body is
        stored and unreachable, and the citing entry that would have explained it
        never exists. Written together, a crash leaves either the whole group or a
        torn tail, and the tail is what the next append truncates.

        *cite* is how a citation is made unable to disagree with the allocation. The
        caller passes ONLY the cited entries in *items* and a callable; this method
        calls it INSIDE the lock, with the seqs it has just allocated for those
        entries, and appends what it returns as the group's last member. Allocating
        outside the lock and citing the result cannot be made correct: the lock is a
        cross-process one, so between a caller's read of the tail and this method's
        read another handle -- a resumed session, a second process -- can append and
        shift the whole run, after which the citation names seqs belonging to the
        intruder. Nothing detects that later: the seqs exist and parse.

        Every refusal still happens before any byte is written -- each item is
        validated up front, and a *cite* result is validated before the write it
        joins -- so a rejected group leaves the file identical. One lock, one
        ``write()``, one ``fsync``.

        ``thread`` is deliberately not accepted here. A group is self-contained by
        construction, and an anchor check per member would have to re-read the tail
        it is being allocated from.
        """
        if not items:
            return []
        for item in items:
            # ``item["data"]`` itself, not ``item.get("data") or {}``: the fallback
            # made a MISSING key and a falsey non-dict both validate as an empty
            # object, so a caller that forgot the body -- or passed None -- would
            # have its entry written with no data at all. A group is refused whole,
            # and this is where that promise is kept.
            require_data(item.get("data"))
            check_ownership(self._kind, str(item.get("type") or ""), src)
            validate_data(self._kind, str(item.get("type") or ""), item.get("data"))
        self._claim()
        with _open_lock(_lock_path(self._kind, self._id)):
            tail = _scan_tail(self._path)
            if tail.empty:
                raise CrewLogError(
                    f"no {self._kind} crew log for {self._id!r}",
                    code=CODE_NO_LEDGER,
                    field="id",
                )
            if tail.torn_offset is not None:
                _truncate(self._path, tail.torn_offset)
                tail = _scan_tail(self._path)
            members = items
            if cite is not None:
                # The seqs the cited entries are about to get, from the allocation
                # this call is committing to -- not from a read the caller made
                # earlier and hoped still held.
                allocated = [tail.last_seq + 1 + offset for offset in range(len(items))]
                citing = cite(allocated)
                # Refused, not raised through. An entry shape this cannot use is a
                # deterministic caller bug, and letting it surface as a TypeError or
                # an AttributeError would put it in the "might clear" class the
                # write-behind RETRIES -- spending the whole attempt budget, and
                # holding every later entry of this session behind it, on a verdict
                # that cannot change. A refusal is a loss now, which is the honest
                # outcome and the one the retention policy already handles.
                if not isinstance(citing, dict):
                    raise CrewLogError(
                        f"cite must return an entry mapping, got {type(citing).__name__}",
                        code=CODE_BAD_DATA,
                        field="cite",
                    )
                require_data(citing.get("data"))
                check_ownership(self._kind, str(citing.get("type") or ""), src)
                validate_data(self._kind, str(citing.get("type") or ""), citing.get("data"))
                # LAST, so a torn tail can only cost the citing entry -- the orphan
                # shape the repair drops -- never a chunk it already named.
                members = [*items, citing]
            stamped = now_ms()
            entries: "list[Entry]" = []
            for offset, item in enumerate(members):
                pointer = item.get("ref")
                entries.append(
                    Entry(
                        type=str(item["type"]),
                        seq=tail.last_seq + 1 + offset,
                        time=stamped,
                        src=src,
                        data=item["data"],
                        thread=None,
                        ref=None if pointer is None else _as_ref(pointer),
                        ignorable=bool(item.get("ignorable")),
                    )
                )
            lines = [require_entry_line(serialize(entry.to_dict())) for entry in entries]
            _append_lines(self._path, lines, needs_newline=tail.needs_newline)
        self._last_seq = entries[-1].seq
        self._needs_newline = False
        return entries

    # -- read --------------------------------------------------------------- #

    def cuts(self) -> int | None:
        """This unit's settled cut count, or ``None`` when nothing is settled.

        The cheap answer to the question :meth:`raw_prefix_digest` answers expensively:
        are the records a fold already consumed still the records on disk? A cut is the
        only thing in this store that can change them, so a reader that holds the count
        it saw last time and finds the same count again knows they are -- in one read of
        a file a few bytes long, whatever the log's size.

        A value comes back only with the bytes SETTLED. The store brackets each cut with
        a raise on either side, and the odd value in between answers ``None``, so no
        reader can hold a count that was taken while the file was being rewritten.

        ``None`` is NOT PROVEN, never zero: see :func:`read_cuts`. A caller that gets it
        has to fall back to the digest, which is what a log created before this counter
        existed does until its first cut.

        Read from DISK on every call, like :func:`log_origin` and for the same reason: a
        value captured when the handle was opened keeps answering for the file that
        existed then, which is exactly the staleness this is here to detect.
        """
        return read_cuts(self._path.parent)

    def raw_prefix_digest(self, records: int) -> tuple[str, int]:
        """SHA-256 of the first *records* raw entry records, and count hashed.

        Segments are walked oldest first with the store's normal binary framing.
        Each segment's header is excluded: log identity is checked separately,
        while this digest proves that the entry bytes already consumed by a fold
        have not changed. No JSON is decoded on this path.

        *records* is a RAW record count and must come from
        :meth:`raw_records_through`, never from a fold's entry span. The two differ
        by every blank or unparseable interior line, and an entry span therefore
        stops this walk short of the records the fold actually consumed.

        Never raises. An invalid count, unreadable segment, framing failure, or
        prefix shorter than requested returns a count other than *records*, which
        makes a savepoint comparison fail closed.
        """
        if not isinstance(records, int) or isinstance(records, bool) or records < 0:
            return ("", 0)
        digest = hashlib.sha256()
        hashed = 0
        if records == 0:
            return (digest.hexdigest(), hashed)
        try:
            paths = segment_paths(self._kind, self._id)
            for path in paths:
                with open(path, "rb") as source:
                    for index, raw in enumerate(
                        bounded_raw_records(source, path, cap=MAX_ENTRY_BYTES, label="crew log")
                    ):
                        if index == 0:
                            continue
                        digest.update(raw)
                        hashed += 1
                        if hashed == records:
                            return (digest.hexdigest(), hashed)
        except Exception:
            log_exception_text(
                logger, logging.DEBUG, "crew log raw prefix for %s could not be read", self._id
            )
        return (digest.hexdigest(), hashed)

    def raw_records_through(self, seq: int) -> int | None:
        """How many raw entry records end at the entry numbered *seq*, or ``None``.

        This is the boundary :meth:`raw_prefix_digest` has to be given, and it
        exists because the two ways of measuring "how much of this file did a fold
        consume" are NOT the same number. A fold counts ENTRIES, so its span is
        ``last_seq - first_seq + 1``. The digest walks RAW records, and a blank or
        unparseable interior line is a record to that walk while the fold skips it.
        Handed the entry span, the walk therefore stops one record short per such
        line, and the trailing records a fold did consume fall outside the digest --
        where later damage to them passes verification, which is the one thing the
        digest exists to prevent.

        So the boundary is resolved here, once, by the same walk the digest uses,
        and the record's own ``seq`` decides where it lies. That costs a JSON decode
        per record, which is why only the WRITE path calls it: a savepoint is
        written rarely, and the reader is handed the resolved count so its own
        verification stays decode-free.

        ``None`` when *seq* is not a real boundary in this log: a bad argument, an
        unreadable or unframeable segment, or a file whose records do not reach it.
        A savepoint is then not written, which costs a cold fold rather than
        recording a boundary nothing can check.
        """
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            return None
        seen = 0
        try:
            for path in segment_paths(self._kind, self._id):
                with open(path, "rb") as source:
                    for index, raw in enumerate(
                        bounded_raw_records(source, path, cap=MAX_ENTRY_BYTES, label="crew log")
                    ):
                        if index == 0:
                            continue  # the header
                        seen += 1
                        stripped = raw.strip()
                        if not stripped:
                            continue
                        parsed = _parses_to_object(stripped)
                        if parsed is None:
                            continue
                        own = parsed.get("seq")
                        if not isinstance(own, int) or isinstance(own, bool):
                            continue
                        if own == seq:
                            return seen
                        if own > seq:
                            # Past the boundary without landing on it, so the entry
                            # is not in this log and no count describes it.
                            return None
        except Exception:
            log_exception_text(
                logger, logging.DEBUG, "crew log prefix boundary for %s could not be read", self._id
            )
            return None
        return None

    def iter_from(
        self,
        seq: int = 1,
        *,
        known: Collection[str] | None = None,
        strict_seq: bool = True,
        resume: "ResumePoint | None" = None,
        cursor: "ReadCursor | None" = None,
    ) -> Iterator[Entry]:
        """Every entry from *seq* onward, OLDEST first -- the shape a fold wants.

        *known* is the reader DECLARING the types it can interpret, and passing
        it is what turns this read into a reconstruction rather than a listing.
        Then an entry whose type is not in *known* either is skipped, when the
        writer marked it ``ignorable``, or raises ``unknown_entry_type`` when it
        did not: a required entry the reader cannot interpret may change the
        meaning of every entry after it, so a fold that continued past one would
        produce a confident wrong answer instead of an admitted failure.

        The default, ``None``, means "I understand everything" and is exactly the
        behaviour every caller had before the marker existed -- no refusal, every
        entry yielded. The gate is opt-in because only a caller that FOLDS state
        is harmed by a line it skipped; :meth:`page` deliberately has no such
        parameter, since paging renders history for a human and showing an
        unfamiliar line is not a wrong answer.

        Reads across SEGMENTS, oldest first, and requires seq to stay contiguous
        over each boundary -- a gap between segments is damage or a partial copy,
        and yielding across it would hand a fold a hole it cannot see. A gap at the
        FRONT is not damage: that is retention, so a first segment starting above 1
        is read as it stands.

        Every WALKED entry -- including the ones below *seq* that are never
        yielded -- must carry a seq strictly greater than the entry before it.
        A duplicate or backward seq is refused with ``bad_data``, the same
        verdict :func:`~kiro_crew.crew_log.projection.advance` gives it, so an
        incremental read and a fold from the start agree on a damaged file
        instead of one refusing and the other silently skipping the record.
        The append-only writer cannot produce a non-advancing seq, so one in
        the file is external damage, not history. A forward GAP stays
        tolerated: inside one file it is a damaged line ``_iter_entries``
        skipped on purpose, and this check deliberately does not harden into
        a contiguity requirement.

        *strict_seq* is that refusal, and ``False`` is for the RENDERING
        callers only: a page shows history to a human, so refusing every
        intact line of a unit because one damaged line exists elsewhere in it
        would take the history away exactly when damage makes it most worth
        reading. A caller that FOLDS state must keep the default -- tolerating
        a non-advancing seq there is how two reads of the same bytes disagree,
        which is the defect this parameter's default closes.

        *resume* is a :class:`ResumePoint` this unit handed out on an earlier walk,
        and it is what makes a tail read cost the TAIL. Reaching *seq* otherwise
        means decoding every record below it, so a reader polling a growing log for
        its newest entry decodes the whole log on every poll. Given a point it can
        confirm, the walk opens the file AT that record and decodes only what follows.

        A point never narrows what this call answers. It is ignored outright when *seq*
        is below the point's own seq, because opening at the point would drop the
        records between the two and hand back a short answer to a caller that asked for
        them. So the same *seq* yields the same entries whether a point is passed or
        not, and the point only changes how much was read to produce them.

        What a resume gives up is exactly the prefix it does not read. The checks
        here -- the advancing-seq rule, each earlier segment's header, each segment's
        filename-against-first-entry claim, contiguity across the boundaries below
        the point -- apply only to the records this walk reads, so records below the
        point go unchecked. A resume is therefore honoured only when ``cuts`` still
        reads as it did (so the store itself has not moved a committed record) AND
        the record at ``offset`` still carries ``seq``; anything else, including a
        point this log never issued, falls back to the full walk. See
        :meth:`_resume_at` for each refusal.

        *cursor* is where this walk REPORTS its own resume point, for the caller to
        hand back next time. It is filled per yielded entry, so after the walk it
        names the last one -- and a walk that yielded nothing leaves it untouched,
        which is what lets a caller keep the point it already had.
        """
        walked = 0
        # Read ONCE, before any record, and stamped into every point this walk hands
        # out. A cut landing mid-walk then leaves the point carrying the count from
        # before it, which the next read compares unequal and falls back on -- the
        # error falls towards a full walk. Reading it afterwards would stamp the
        # count from after the cut, and the next read would resume over cut bytes.
        cuts = self.cuts() if (cursor is not None or resume is not None) else None
        # A point may start the walk only where it cannot hide an entry this call asked
        # for. Opening at the point yields its own record and everything after it, so
        # the answer is whole exactly when *seq* is at or above the point's seq. Asked
        # for anything lower, the records in between are owed, and a point that skipped
        # them would make this an optimisation that changes the answer -- which is the
        # one thing it must never be. :meth:`_resume_at` cannot see this: it is handed
        # the point and the bytes, and answers whether they agree, not what was asked.
        usable = resume if resume is not None and seq >= resume.seq else None
        for entry, segment, at in self._iter_segments_located(resume=usable, cuts=cuts):
            if entry.seq <= walked:
                if strict_seq:
                    raise CrewLogError(
                        f"entry {entry.seq} is at or below the previously read "
                        f"entry's seq {walked}; a duplicate or backward seq is "
                        "damage the append-only writer cannot produce",
                        code=CODE_BAD_DATA,
                        field="seq",
                    )
            else:
                walked = entry.seq
            if entry.seq < seq:
                continue
            if known is not None and entry.type not in known:
                if not entry.ignorable:
                    raise CrewLogError(
                        f"entry {entry.seq} has type {entry.type!r}, which this reader "
                        "does not know and which is not marked ignorable; "
                        "reconstruction stops here",
                        code=CODE_UNKNOWN_ENTRY_TYPE,
                        field="type",
                    )
                continue
            if cursor is not None:
                cursor.at = ResumePoint(segment=segment, offset=at, seq=entry.seq, cuts=cuts)
            yield entry

    def _durable_end(self, path: Path) -> int:
        """*path*'s size measured under the append lock: the end no append is still writing."""
        with _open_lock(_lock_path(self._kind, self._id)):
            try:
                return path.stat().st_size
            except FileNotFoundError:
                return 0

    def _iter_segments(self) -> Iterator[Entry]:
        """Every entry of every segment, oldest first, refusing bad provenance.

        A projection of :meth:`_iter_segments_located`, which owns the provenance
        refusals and the walk. Two walks over a unit's segments would be two answers
        waiting to disagree about which files are authoritative.
        """
        for entry, _segment, _at in self._iter_segments_located():
            yield entry

    def _resume_at(
        self,
        paths: "list[Path]",
        resume: "ResumePoint | None",
        newest_end: int | None,
        cuts: int | None,
    ) -> "tuple[int, int]":
        """Which segment index and byte offset a walk may start at, for *resume*.

        ``(0, 0)`` means "start at the top", and it is the answer to every question
        this cannot settle -- a resume point is an optimisation token, so a token that
        does not check out costs the full walk and never a wrong answer.

        The refusals, each for its own reason:

        * **No point, or a nonsense offset.** Offset zero is the header's own
          position, so it names no entry; a negative or non-integer one names nothing
          at all.
        * **The cut count moved, or either reading is unknown.** This is the only
          evidence there is about the prefix the resumed walk does NOT read: a cut is
          the one thing in the store that changes a committed record, so an unchanged
          count is what says those bytes still are what they were. ``None`` is not
          proven rather than zero (see :func:`read_cuts`), so a log with no counter
          takes the full walk -- it has nothing to vouch for its prefix with. The
          counter is a seqlock, so ``None`` also covers a reading taken while a cut is
          in flight: that one is odd, and admitting it would let a reader match the
          same odd value again over bytes the cut had meanwhile replaced.
        * **The named segment is gone.** Retention removes whole segments off the
          front, and a point into one of them is stale rather than wrong.
        * **The offset is at or past the durable end.** Bytes past it are an append
          still in flight (see :meth:`_durable_end`).
        * **The offset is not a record boundary.** Read there and the framing layer
          reports where the record it delivers actually began: an offset inside a
          record yields that record's remainder, which does not parse, and the first
          entry found then starts ABOVE the offset.
        * **The record there is not the expected seq.** The positive check, and the
          one that makes the whole thing safe to build on: it says the bytes at the
          offset are the bytes the caller thinks they are, not merely that something
          parses there. It also leaves the walk's advancing-seq rule anchored, since
          the resumed walk's first record is this known one.
        """
        if resume is None:
            return 0, 0
        if not isinstance(resume.offset, int) or isinstance(resume.offset, bool):
            return 0, 0
        if resume.offset <= 0:
            return 0, 0
        if resume.cuts is None or cuts is None or resume.cuts != cuts:
            return 0, 0
        index = next((at for at, path in enumerate(paths) if path.name == resume.segment), None)
        if index is None:
            return 0, 0
        end = newest_end if index == len(paths) - 1 else None
        if end is not None and resume.offset >= end:
            return 0, 0
        found = next(_iter_entries_located(paths[index], end, start=resume.offset), None)
        if found is None:
            return 0, 0
        entry, at = found
        if at != resume.offset or entry.seq != resume.seq:
            return 0, 0
        return index, resume.offset

    def _iter_segments_located(
        self,
        *,
        resume: "ResumePoint | None" = None,
        cuts: int | None = None,
    ) -> "Iterator[tuple[Entry, str, int]]":
        """Every entry of every segment with its segment name and byte offset.

        Oldest first, refusing bad provenance.

        Every segment repeats the header because retention may remove the first one.
        Each header must therefore identify this same crew log and schema before any
        entry from that independent file becomes authoritative. The filename's first
        seq is a second provenance claim and must agree with the physical first entry
        when that record is readable.

        Seq continuity remains a boundary-only check. Inside one file a missing seq
        means a damaged line, which ``_iter_entries`` skips on purpose -- one
        unreadable record must not make the rest of the file unreadable. Across two
        files a gap is a different thing: a half-finished copy or deleted middle
        segment is invisible unless the boundary is checked.

        *resume*, when :meth:`_resume_at` accepts it, starts the walk inside one
        segment and SKIPS every segment before it. The header of the resumed segment
        is still checked -- it is the file the walk reads -- but the earlier ones are
        not opened, and the boundary into the resumed segment has no previous entry
        to be compared against, so it is not checked either. That is the prefix the
        resume trades away, and :meth:`_resume_at` is where it is paid for.
        """
        expected = 0
        paths = segment_paths(self._kind, self._id)
        # The newest segment is the one a writer appends to, so its end is measured
        # under the append lock before anything is read. An append holds that lock
        # from its write through its fsync and any rollback, so the size seen under it
        # covers only lines the writer has made durable; bytes past it are an append
        # still in flight, and a reader that yields them can hand a fold an entry the
        # durable log never keeps. Older segments take no more appends.
        newest_end = self._durable_end(paths[-1]) if paths else None
        from_index, from_offset = self._resume_at(paths, resume, newest_end, cuts)
        for index, path in enumerate(paths):
            if index < from_index:
                continue
            raw_header = _read_header_line(path)
            parsed_header = None if not raw_header else _parses_to_object(raw_header)
            try:
                segment_header = parse_header(
                    parsed_header,
                    kind=self._kind,
                    unit_id=self._id,
                )
            except CrewLogError as exc:
                raise CrewLogError(
                    f"segment {path.name} has an invalid header: {exc}",
                    code=CODE_BAD_SEGMENT,
                    field=exc.field,
                ) from exc
            if segment_header.version != self._header.version:
                raise CrewLogError(
                    f"segment {path.name} has schema version {segment_header.version}, "
                    f"but this crew log uses {self._header.version}",
                    code=CODE_BAD_SEGMENT,
                    field="version",
                )

            declared_first = _segment_first_seq(path)
            actual_first = _read_first_entry_seq(path)
            if actual_first is not None and actual_first != declared_first:
                raise CrewLogError(
                    f"segment {path.name} declares first seq {declared_first} in its filename, "
                    f"but its first entry is seq {actual_first}",
                    code=CODE_BAD_SEGMENT,
                    field="seq",
                )

            at_boundary = bool(index) and expected > 0
            end = newest_end if index == len(paths) - 1 else None
            begin = from_offset if index == from_index else 0
            for entry, at in _iter_entries_located(path, end, start=begin):
                if at_boundary and entry.seq != expected:
                    raise CrewLogError(
                        f"segment {path.name} starts at seq {entry.seq}, but the "
                        f"previous segment ended at {expected - 1}: the log is not "
                        "contiguous across the boundary",
                        code=CODE_SEGMENT_GAP,
                        field="seq",
                    )
                at_boundary = False
                expected = entry.seq + 1
                yield entry, path.name, at

    def get(self, seq: int) -> Entry | None:
        """The entry at *seq*, or ``None``.

        Stops as soon as the stream passes *seq*: entries are written in seq
        order, so a miss costs the prefix, not the file.

        Reads across SEGMENTS, like :meth:`iter_from`. Reading only the newest file
        made a point read of a rotated entry answer ``None`` -- indistinguishable
        from "no such entry" -- so a caller resolving a seq it had just been handed
        would be told it does not exist. Retention is the one thing that legitimately
        removes an entry, and it deletes whole segments off the front; while a
        segment is still on disk its entries are still readable.
        """
        for entry in self._iter_segments():
            if entry.seq == seq:
                return entry
            if entry.seq > seq:
                return None
        return None

    def page(self, before: int | None = None, limit: int = DEFAULT_PAGE_LIMIT) -> Page:
        """Up to *limit* entries older than *before*, NEWEST first.

        ``before`` is exclusive, so feeding ``next_before`` straight back walks
        the history without repeating or skipping a line.
        """
        return self._page(before, limit, keep=lambda _entry: True)

    def thread_page(
        self, anchor: int, before: int | None = None, limit: int = DEFAULT_PAGE_LIMIT
    ) -> Page:
        """One thread's entries, NEWEST first, the anchor last.

        A thread is a GROUPING key, not a tree: members carry ``thread ==
        anchor`` and the anchor carries its own seq. The anchor is included, so
        the final page of a thread ends with the entry the group hangs off --
        which is what makes a thread readable bottom-up without a second call.
        """
        return self._page(
            before, limit, keep=lambda entry: entry.thread == anchor or entry.seq == anchor
        )

    def _page(self, before: int | None, limit: int, *, keep: Callable[[Entry], bool]) -> Page:
        bound = max(1, min(int(limit), MAX_PAGE_LIMIT))
        # One extra slot answers "is there an older entry" exactly, so the
        # cursor is None precisely when the caller has seen everything.
        window: deque[Entry] = deque(maxlen=bound + 1)
        # Across SEGMENTS, like `iter_from` and `get`. Reading only the newest file
        # made paging stop dead at a rotation boundary and report `next_before` as
        # None -- telling the caller it had seen the whole history when it had seen
        # one segment of it. Paging renders history for a human, and silently
        # truncating that is the one answer it must not give.
        for entry in self._iter_segments():
            if before is not None and entry.seq >= before:
                continue
            if keep(entry):
                window.append(entry)
        newest_first = list(reversed(window))
        has_more = len(newest_first) > bound
        entries = tuple(newest_first[:bound])
        return Page(entries=entries, next_before=entries[-1].seq if has_more and entries else None)

    def resolve(self, ref: Ref | dict[str, Any]) -> Resolution:
        """Follow *ref* and return the segment it cites.

        Four outcomes. ``ok`` carries the entries. ``gone`` means the cited crew log
        does not exist at all -- a normal answer, not an error, since the cited
        crew log may legitimately have been deleted and the citing entry stays honest
        about having pointed at it. ``pruned`` means the span reaches below the
        oldest surviving segment's first seq, so retention removed those lines.
        ``corrupt`` means the span lies inside a segment that still exists yet read
        short, which is damage.

        The last two are never reported as each other, and the classification is
        made from the segment names rather than from the short read: a reader told
        ``pruned`` stops looking, because retention removing old lines is a normal
        answer, while ``corrupt`` tells it the file it still has is not intact.
        Reporting damage as retention would turn a recoverable alarm into silence.
        Citing PAST the newest entry is ``corrupt`` as well, and deliberately not
        ``ok``: a ``Ref`` always names a definite span -- an absent ``to_seq``
        means the single line at ``from_seq``, so there is no open-ended spelling
        -- which makes a span reaching beyond the newest entry a claim about lines
        that are not in the file. ``ok`` means every cited seq was read back.

        This layer makes NO authorization claim, and takes no access callback. A
        half-built one would be worse than none: a check that defaults to allow
        makes the shortest call shape the insecure one, and a check with no
        permission model behind it only looks like a boundary. Every caller today
        is in-process gateway code that can already read the file. A ``forbidden``
        status arrives with the first caller that HAS a permission model -- the
        routes that mount this -- and it belongs there, where the caller identity
        it must be derived from actually exists.
        """
        pointer = _as_ref(ref)
        if pointer.unit == self._kind and pointer.id == self._id:
            target: CrewLog | None = self
        else:
            try:
                target = CrewLog.open(pointer.unit, pointer.id)
            except CrewLogError as exc:
                # Only "there is no such crew log" is `gone`. A crew log that EXISTS
                # but will not open -- a damaged header, an unreadable segment --
                # is damage, and answering `gone` for it tells the reader to stop
                # looking for a file that is right there and broken.
                if getattr(exc, "code", "") == CODE_NO_LEDGER:
                    return Resolution(status=STATUS_GONE, entries=())
                return Resolution(status=STATUS_CORRUPT, entries=())
        if target is None:
            return Resolution(status=STATUS_GONE, entries=())
        last = pointer.last_seq
        try:
            found = tuple(
                entry for entry in target.iter_from(pointer.from_seq) if entry.seq <= last
            )
        except CrewLogError:
            # A missing middle segment or a torn line RAISES out of the read. That
            # is the very condition this method promises to report, so it is
            # answered rather than propagated -- a caller resolving a citation
            # wants a verdict, and an exception here would make `corrupt`
            # unreachable for exactly the damage it names.
            return Resolution(status=STATUS_CORRUPT, entries=())
        # A SHORT answer has two possible causes and they are not the same fact.
        # Retention deletes whole segments off the front, so a span reaching below
        # the oldest survivor is `pruned` -- a normal answer. A span that lies
        # inside a segment which still exists, yet reads short, is DAMAGE: the
        # lines should be there. Reporting that as retention tells a reader to
        # stop looking for a file that is in fact corrupt.
        firsts = segment_first_seqs(target.kind, target.id)
        oldest = firsts[0] if firsts else 1
        if pointer.from_seq < oldest:
            # Only the surviving part of the span is expected, so coverage is
            # measured from the oldest survivor rather than from the citation's
            # own start. A hole INSIDE that surviving range is damage, not
            # retention, and gets `corrupt` on a span that also reaches below it.
            if not _covers_span(found, oldest, last):
                return Resolution(status=STATUS_CORRUPT, entries=found)
            return Resolution(status=STATUS_PRUNED, entries=found)
        # Seq is contiguous inside a file, so coverage is the test -- and the span
        # it is measured against comes from what the CITATION claims existed, never
        # from what is on disk now. `ok` means every cited seq was read back;
        # anything short of that is the citation failing, whatever shortened the
        # file.
        #
        # Measuring against the file's own tail instead is wrong in the one
        # direction that matters. A clean end-truncation -- whole lines removed, no
        # torn bytes, nothing for the read to raise on -- lowers the walked entries
        # and a reopened handle's `last_seq` together, so a tail-derived span
        # shrinks to exactly what survived and the verdict reads `ok` with the
        # cited lines missing. That is the worst available answer: a caller
        # resolving a citation is asking whether it can still be read, and `ok`
        # with fewer entries tells it yes while handing it a hole. A file that GREW
        # needs no tail-derived span either, because this walk stops at `last`, so
        # entries past the citation are never in `found` and cannot inflate the count.
        if not _covers_span(found, pointer.from_seq, last):
            # `corrupt` rather than a new status. The distinction a caller acts on is
            # "resolvable or not", and retention -- the one shortening that is normal
            # -- already has its own answer in the `pruned` branch above.
            return Resolution(status=STATUS_CORRUPT, entries=found)
        return Resolution(status=STATUS_OK, entries=found)
