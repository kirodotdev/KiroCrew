"""Fold savepoints on disk -- where a read resumes instead of replaying the file.

A projection is a fold over one session's crew log, and folding costs one step per
entry with no upper bound on the total: the projection route folds from seq 1 on
every read, so what it costs to show a session's status grows with how long that
session has run. The push avoids that with an in-memory bundle, but that cache
dies with the process and holds a bounded number of sessions, so a restart and an
eviction each pay for the whole file again. This module is the savepoint RFC NFR-1
asks for -- each fold's state written beside the log it came from, resumed on the
next read.

It is DISPOSABLE by construction, and that is the property to keep while changing
it. Every failure here -- no file, a truncated one, a payload this build does not
understand, a store the file does not describe -- is answered by folding from
seq 1, which reaches the same value at more cost. So :func:`load` and :func:`save`
never raise: a savepoint that cannot be trusted is not an error a reader should
see, it is one read at the cost of a cold fold. Nothing a caller is served
depends on a savepoint existing or being current.

Layout, one file per fold inside the unit's own directory (RFC section 3)::

    <store dir>/projections/<fold>.json

One file per fold rather than one file for all five, so a payload this build
cannot read costs that fold its savepoint instead of costing all of them, and so a
caller asking for one projection writes one file. The name is a fold name that
passed :func:`~kiro_crew.crew_log.projection.require_name`, which is how a file
name here can never be anything but one of the five words this package declares.

The store directory is already fenced -- hidden from a sandboxed process and
refused to the agent's own file tools, stated per LEAF at ``crew-log`` -- so a
savepoint inherits that protection by living there and needs no fence entry of its
own. It holds what the folds retain (a model name, a tool name, a stop reason, a
cwd), which is the same class of fact as the entries it was folded from, and it
goes with the unit because removal deletes the whole directory.

WHAT MAKES A SAVEPOINT SAFE TO RESUME. An append-only prefix never invalidates
one: the entries a checkpoint consumed cannot change, so folding the entries after
it reaches what a cold fold reaches, which is the equality
``crew-log-projection.md`` states and the tests pin. FOUR things break that, and
each is checked before a file is used.

The checking is the projection kernel's (:mod:`kiro_crew.projection.checkpoint`),
and this module supplies the two things the kernel cannot know. The IDENTITY BLOCK
holds the facts that must match verbatim, which is what the first two below are; the
WITNESS holds the evidence a live check needs, which is what the last two read. The
kernel compares the block, refuses a payload from another fold or another state
shape, and hands the witness to :func:`prefix_admit`.

* the file describes a DIFFERENT log -- a unit removed and recreated under the
  same id restarts its seqs, and once the new file has grown past the stored seq a
  seq check alone would pass. ``origin`` is the log's creation identity. Identity
  block, beside ``unit``, which catches a payload copied from another unit.
* the log LOST its front -- retention deletes whole segments off the oldest end,
  so a cold fold folds a window while the savepoint still counts entries the file
  does not hold. The two answers differ, and the savepoint's is the one no
  reader can reproduce. ``first_seq`` is the oldest surviving segment's first seq,
  and a change to it retires the file. Identity block.
* the prefix CHANGED after it was folded -- a line damaged afterwards is skipped by
  a cold fold while a savepoint keeps the value that line contributed, and no
  equality above reads the prefix at all. Witness, in one of two spellings, because
  a reader cannot name either value before opening the file that states it.

  ``prefix_cuts`` is the cheap one and the one a current log gets: the unit's cut
  count (:meth:`~kiro_crew.crew_log.store.CrewLog.cuts`), read when the savepoint was
  written. A cut is the only thing this store does that can change a committed
  record, and the count rises durably before each one, so the same count again means
  the records the state was folded from are the same bytes -- settled by a file a few
  bytes long whatever the log's size, on the write side as well as the read side.

  ``prefix_sha`` is a digest of the consumed prefix's raw record bytes, recomputed on
  resume from ``prefix_records``. It is what a log with no cut counter is settled by,
  and it charges a walk of the whole consumed prefix to each write and each resume --
  unless the witness also carries ``prefix_extent`` (:mod:`~kiro_crew.crew_log.prefix_extent`)
  and that still holds, which settles it in a few stats and one bounded read.

  Either spelling may also carry ``resume``, the byte position of the record at its
  seq, which the tail read after a resume seeks to. Both keys are additive.
* the log is SHORTER than the savepoint -- most of its causes are caught above,
  but it is checked on its own because a fold resumed past the end of a file is
  the one state no later read recovers from. Witness, against the live ``last_seq``.

A savepoint written before the witness existed carries none, and :func:`prefix_admit`
refuses an empty one: it is a payload nothing can check, which this module holds to
be worse than no payload at all. It is DISCARDED and cold-folded, never migrated --
and because the file name is unchanged, the next write replaces it rather than
leaving it on disk for a collector that does not exist.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, NamedTuple, TypeVar

from kiro_crew.crew_log.errors import CrewLogError
from kiro_crew.crew_log.lease import LEASE_FILE
from kiro_crew.crew_log.lease import acquire as acquire_lease
from kiro_crew.crew_log.lease import release as release_lease
from kiro_crew.crew_log.prefix_extent import (
    PrefixExtent,
    extent_at,
    extent_from,
    extent_holds,
    extent_mapping,
    resume_mapping,
    resume_point_from,
)
from kiro_crew.crew_log.projection import (
    Checkpoint,
    SessionProjections,
    fold_state_version,
    log_origin,
    require_name,
)
from kiro_crew.crew_log.projection_session_log import _log_identity
from kiro_crew.crew_log.store import (
    CrewLog,
    ResumePoint,
    crew_log_dir,
    log_exception_text,
    segment_first_seqs,
    segment_paths,
)
from kiro_crew.platform_compat import restrict_dir_to_owner
from kiro_crew.projection import (
    MAX_PAYLOAD_BYTES,
    Admit,
    DirectoryCheckpointStore,
    Savepoint,
)

logger = logging.getLogger(__name__)


def _count_savepoint_write(*, kind: str, written: bool, elapsed_ms: float) -> None:
    """Count one savepoint write attempt, beside the write. Never raises.

    Called only from a pass that actually offered a payload to the savepoint store, so
    a call that found no write earned, no witness, a removed unit or a contended lease
    contributes nothing: those are refusals, and counting them as failures would read
    as a failing disk.

    Called AFTER the unit's lease is released, never inside it. The first
    ``get_recorder()`` of a process builds the SDK synchronously, and doing that under
    a held lease would lengthen the window in which a removal of that unit is refused
    -- a telemetry call must not change how long anything else has to wait.

    Both callers are on the fold and read path -- a savepoint is written by whoever
    just folded, roughly once per 256 entries -- never on a crew log write thread,
    which is why a ``time.monotonic()`` pair around the write is affordable here and is
    not affordable there.

    IMPORTED at call time, deferred rather than taken at module scope because this
    module sits under the gateway's boot path and the metrics facade reads config. A
    plain import, NOT the ``sys.modules`` lookup its sibling in ``crew_log.bus`` uses,
    and the difference is the whole point of each: a lookup records nothing until
    something else has built the recorder, which would silently drop the first savepoint
    of a process whose recorder is not up yet. Being off the write thread, this site can
    afford the import that closes that window; the bus site cannot, and says so.
    """
    try:
        from kiro_crew.metrics.crew_log_health import savepoint_write

        savepoint_write(kind=kind, written=written, elapsed_ms=elapsed_ms)
    except Exception:  # pragma: no cover - telemetry must never reach a savepoint
        pass


#: Directory inside a unit's store directory that holds its fold savepoints.
CHECKPOINT_DIR: Final[str] = "projections"

#: Directory inside a unit's store directory that holds SLOT fold savepoints.
#:
#: A LEAF OF ITS OWN beside :data:`CHECKPOINT_DIR`, not a file inside it. The kernel
#: store derives a file name from the fold name alone, and a slot-keyed fold can carry
#: the same name as a session-keyed one (``tools`` is read both ways), so one directory
#: would have the two overwrite each other -- each then refused by the other's identity
#: block, which is correct and leaves neither fold a savepoint it ever gets to use.
#:
#: Inside the NEWEST unit's directory, which is the only unit a continuation lets grow
#: and the one whose bytes the witness is a digest of. Two consequences, both wanted: the
#: file inherits the ``crew-log`` fence and is removed with the unit, and a slot that
#: gains a unit (a session reset) finds no savepoint at its new newest unit and folds
#: cold once -- exactly what the warm cell already does for a changed unit list.
SLOT_CHECKPOINT_DIR: Final[str] = "slot-projections"

#: Largest savepoint file this package reads or writes, and the number the kernel
#: store ENFORCES (:data:`~kiro_crew.projection.MAX_PAYLOAD_BYTES`). Every fold's
#: state is already bounded by construction (``crew-log-projection.md`` section 2),
#: so this is a BACKSTOP on those bounds rather than the bound itself: a fold that
#: grew unbounded state loses its savepoint instead of writing an unbounded file on
#: every read. Exceeding it costs performance and nothing else. Re-exported under
#: this name because the session-tree store measures its own payloads against the
#: same number.
MAX_CHECKPOINT_BYTES: Final[int] = MAX_PAYLOAD_BYTES

#: Entries a bundle must have advanced past its savepoint before another one is
#: written. A savepoint is allowed to LAG -- resuming from an older one replays the
#: tail and reaches the same value -- so a write is spent only when it saves a
#: meaningful replay. Without this the push would rewrite five files every time a
#: session grew by one entry, which is the cost this module exists to remove
#: rather than to relocate. It also means a short session leaves no file behind:
#: folding it from the start is already cheap.
MIN_ADVANCE_ENTRIES: Final[int] = 256


def checkpoint_dir(kind: str, unit_id: str) -> Path:
    """The directory holding one unit's fold savepoints. Does not create it."""
    return crew_log_dir(kind, unit_id) / CHECKPOINT_DIR


def checkpoint_path(kind: str, unit_id: str, name: str) -> Path:
    """The savepoint file for one fold of one unit. Does not create it."""
    return checkpoint_dir(kind, unit_id) / f"{require_name(name)}.json"


def slot_checkpoint_dir(kind: str, unit_id: str) -> Path:
    """The directory holding the SLOT fold savepoints kept at one unit. Does not create it."""
    return crew_log_dir(kind, unit_id) / SLOT_CHECKPOINT_DIR


def slot_checkpoint_path(kind: str, unit_id: str, name: str) -> Path:
    """The savepoint file for one slot fold kept at one unit. Does not create it."""
    return slot_checkpoint_dir(kind, unit_id) / f"{require_name(name)}.json"


class _UnitStore(DirectoryCheckpointStore):
    """The kernel's savepoint store, laid out FLAT inside one unit's directory.

    The kernel names a file ``<root>/<store>/<key>.json`` because one root serves
    many stores. Here the root already IS one unit's directory, so the store segment
    would add a level naming the unit twice -- and the layout this package documents,
    ``<store dir>/projections/<fold>.json``, is what :func:`checkpoint_path` answers
    and what a reader looking for a fold's savepoint expects.

    Keeping the path is also what makes a savepoint from before the kernel DISCARDED
    rather than orphaned: it sits at exactly this name, so it is read, refused by the
    kernel's own envelope check, and overwritten by the next write. A new path would
    leave it on disk forever, since nothing else collects it.
    """

    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self._directory = Path(directory)

    def path_for(self, store: str, key: str) -> Path | None:
        if not self._is_safe(key):
            return None
        return self._directory / f"{key}.json"


def _identity_block(handle: CrewLog, origin: str, first_seq: int) -> dict[str, Any]:
    """The facts a savepoint must MATCH to describe this log, compared verbatim.

    All three are known before the fold and fixed afterwards, which is what makes
    them equality rather than an ``admit`` condition. ``unit`` catches a payload that
    moved or was copied from another unit's directory, ``origin`` a unit removed and
    recreated under the same id, and ``first_seq`` a retention trim that took whole
    segments off the front.
    """
    return {"unit": handle.id, "origin": origin, "first_seq": first_seq}


def prefix_admit(handle: CrewLog, first_seq: int) -> Admit:
    """The conditions that read the LIVE log, judged against a savepoint's witness.

    Public and client-neutral on purpose. Every value it reads is on the ``CrewLog``
    handle or in the witness the writer stored, so a second client over a crew log
    evaluates the same condition by calling this rather than by writing its own --
    two spellings of "are these still the bytes that state came from" would
    eventually disagree, and the one that said yes too often serves a value no cold
    fold reproduces for the life of the store.

    An EMPTY witness is refused, and that is what retires a payload written before
    the witness existed: it carries no evidence about the bytes its state came from,
    and this module's whole posture is that a savepoint it cannot check is worse than
    none.

    *first_seq* is the oldest surviving entry's seq, which bounds how few raw records
    a prefix through the witness's seq can possibly hold.

    TWO WITNESS SPELLINGS, and a payload carries exactly one. ``prefix_cuts`` is the
    unit's cut count and settles the question in a few bytes; ``prefix_sha`` settles it
    by hashing the consumed prefix, and is what a log with no cut counter has. The
    counter is taken only when the digest keys are ABSENT, so a payload carrying both
    is judged by the digest rather than downgraded to the cheaper half of itself.

    The digest is memoized per record count, because the folds of one read share a
    boundary and hashing it once per fold would walk the same bytes five times.
    """
    digests: dict[int, tuple[str, int]] = {}
    extents: "dict[PrefixExtent, bool]" = {}

    def admit(identity: Mapping[str, Any], witness: Mapping[str, Any]) -> bool:
        seq = witness.get("seq")
        sha = witness.get("prefix_sha")
        records = witness.get("prefix_records")
        cuts = witness.get("prefix_cuts")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            return False
        if seq > handle.last_seq:
            # The short-store answer: the log does not reach the savepoint, so
            # resuming would fold nothing and serve state for entries the file no
            # longer has. Most of its causes are equality above; it is checked on its
            # own because a fold resumed past the end of a file is the one state no
            # later read recovers from.
            logger.debug(
                "crew log savepoint is at seq %d past the log's %d; folding cold",
                seq,
                handle.last_seq,
            )
            return False
        extent = extent_from(witness.get("prefix_extent"))
        if extent is not None and extent not in extents:
            # Growth is allowed only beside the cut count, which vouches for the store's
            # own rewrites; on its own an extent proves the files UNTOUCHED, nothing less.
            extents[extent] = extent_holds(handle, extent, grown_ok=cuts is not None)
        if sha is None and records is None and cuts is not None:
            # The count covers the store's own rewrites; the extent, when the writer
            # stored one, covers the files being replaced or rewritten in place by
            # anything else. A payload from before the extent is judged by the count
            # alone, as it always was.
            return _cuts_admit(handle, cuts) and (extent is None or extents[extent])
        if (
            not isinstance(sha, str)
            or len(sha) != 64
            or any(char not in "0123456789abcdef" for char in sha)
            or not isinstance(records, int)
            or isinstance(records, bool)
            # A LOWER bound, not equality: the count is raw records, and a blank or
            # unparseable interior line is a record the fold did not count as an
            # entry, so the two can legitimately differ upward. Equality would force
            # the entry span onto the digest and leave the trailing consumed records
            # uncovered. A count too LARGE cannot pass either -- the walk then hashes
            # fewer records than claimed and the comparison below refuses it.
            or records < max(0, seq - first_seq + 1)
        ):
            return False
        if extent is not None and extents[extent] and handle.cuts() is None:
            # Settled without the walk: same file, prefix still in place, its end bytes
            # unchanged, and no cut counter has appeared (one would mean a cut ran, and
            # then only the digest can say what it touched).
            return True
        prefix = digests.get(records)
        if prefix is None:
            prefix = handle.raw_prefix_digest(records)
            digests[records] = prefix
        digest, records_hashed = prefix
        if digest != sha or records_hashed != records:
            logger.debug("crew log savepoint has a changed prefix; folding cold")
            return False
        return True

    return admit


def _cuts_admit(handle: CrewLog, stored: Any) -> bool:
    """Whether *handle*'s cut count still reads as *stored*, so its prefix is intact.

    The counter rises durably BEFORE each cut, and a cut is the only thing this store
    does that can change a record already committed. So two equal readings bracket a
    window in which no committed record changed, which is the whole of what the digest
    proves and all a resume needs.

    The live reading being ``None`` refuses. An absent or unreadable counter is NOT
    PROVEN rather than zero: a counter lost after a cut would otherwise compare equal
    across it, which is the one reading this evidence exists to prevent. A log with no
    counter writes a digest witness instead, so refusing here costs nothing it had.
    """
    if not isinstance(stored, int) or isinstance(stored, bool) or stored < 0:
        return False
    live = handle.cuts()
    if live is None:
        logger.debug("crew log savepoint cites a cut count the unit no longer states; folding cold")
        return False
    if live != stored:
        logger.debug("crew log savepoint was written %d cuts ago; folding cold", live - stored)
        return False
    return True


def witness_mapping(prefix: "PrefixWitness | CutWitness") -> dict[str, Any]:
    """*prefix* as the opaque mapping a savepoint stores beside its identity.

    The keys :func:`prefix_admit` reads, written in one place so a second client cannot
    store a witness under names the shared predicate does not look up -- which would
    read as "carries no evidence" and cost that client every savepoint it ever wrote,
    silently. One spelling per witness, never both: a cut count is evidence on its own,
    and a payload carrying a digest beside it would be judged by the digest and pay for
    it on every resume.
    """
    if isinstance(prefix, CutWitness):
        mapping: dict[str, Any] = {"seq": prefix.seq, "prefix_cuts": prefix.cuts}
    else:
        mapping = {"seq": prefix.seq, "prefix_sha": prefix.sha, "prefix_records": prefix.records}
    if prefix.extent is not None:
        mapping["prefix_extent"] = extent_mapping(prefix.extent)
    # ADDITIVE keys only. A build that predates them reads the same evidence it always
    # read and ignores the rest, and a payload written before them lacks them and costs
    # its resume the walk it always cost -- so neither direction needs a migration.
    resume = prefix.resume
    if resume is not None and resume.seq == prefix.seq:
        mapping["resume"] = resume_mapping(resume)
    return mapping


def load(handle: CrewLog, names: Iterable[str]) -> SessionProjections | None:
    """The savepoints for *names* as a bundle to resume from, or ``None``.

    A name whose file is absent, unreadable or not describing this log is left OUT
    of the returned bundle rather than refusing the whole read: the fold surface
    advances each checkpoint over only the entries above its own seq, so a bundle
    mixing a resumed fold with one starting at zero costs the cold fold to that
    one fold. ``None`` when nothing could be resumed, which is the same answer as
    an empty bundle and saves the caller a pass over it.

    Never raises. The bundle's ``saved_seq`` is the seq every requested fold is
    persisted through, so a caller can tell whether a write is owed without
    reading these files again; it is 0 whenever a requested name had no file.
    """
    return _load(handle, names)[0]


def _load(
    handle: CrewLog, names: Iterable[str]
) -> "tuple[SessionProjections | None, dict[str, Mapping[str, Any]]]":
    """:func:`load`, plus the stored witness each resumed fold was admitted on.

    The second element is what :func:`load_resumable` needs: which files still make
    every resume hash the consumed prefix (worth rewriting once), and the byte position
    each one stored for the tail read.
    """
    wanted = tuple(require_name(name) for name in names)
    if not wanted:
        return None, {}
    identity = _identity(handle)
    if identity is None:
        return None, {}
    origin, first_seq = identity
    try:
        store = _UnitStore(checkpoint_dir(handle.kind, handle.id))
    except Exception:  # pragma: no cover - a path refusal from the store's checks
        log_exception_text(
            logger, logging.DEBUG, "crew log savepoint path refused for %s", handle.id
        )
        return None, {}
    block = _identity_block(handle, origin, first_seq)
    admit = prefix_admit(handle, first_seq)
    resumed: dict[str, Checkpoint] = {}
    witnesses: dict[str, Mapping[str, Any]] = {}
    for name in wanted:
        loaded = _resume_one(store, handle, name, block, admit)
        if loaded is not None:
            resumed[name], witnesses[name] = loaded
    if not resumed:
        return None, {}
    reached = max(cp.last_seq for cp in resumed.values())
    # The floor, not the ceiling: a name with no file is persisted through nothing,
    # so the bundle is only as saved as its least-saved fold and the next write is
    # owed for the whole set.
    saved = min(cp.last_seq for cp in resumed.values()) if len(resumed) == len(wanted) else 0
    bundle = SessionProjections(
        session_id=handle.id,
        last_seq=reached,
        checkpoints=resumed,
        origin=origin,
        saved_seq=saved,
    )
    return bundle, witnesses


class DiskResume(NamedTuple):
    """What :func:`load_resumable` read: the bundle, and how its tail read may start.

    ``points`` maps a checkpoint seq to the byte position its savepoint stored for the
    record at that seq, for the tail read to seek to. ``owes_point`` is true when some
    resumed file stored none, so the next pass should write one even though it has not
    advanced far enough to earn a write on entry count alone -- without that, a session
    resumed from a file written before positions existed decodes its whole log on every
    restart until it grows by :data:`MIN_ADVANCE_ENTRIES`.
    """

    bundle: "SessionProjections | None"
    points: "dict[int, ResumePoint]"
    owes_point: bool


def load_resumable(handle: CrewLog, names: Iterable[str]) -> DiskResume:
    """:func:`load`, the stored tail positions, and a once-only upgrade of old witnesses.

    THE UPGRADE. A savepoint admitted by a full prefix DIGEST makes every later resume
    hash that prefix again, and a quiet session never advances far enough to earn the
    rewrite that would fix it. So such a file is rewritten here, once, with cheaper
    evidence for the same state and seq:

    * a CUT COUNT where the unit states one. Sound for the reason :func:`cut_witness`
      is: the count is read BEFORE the load and again after it, and the counter rises
      before every cut, so two equal settled readings bracket a window in which no
      committed record changed -- and the digest the load just matched was computed
      inside it, so the state was folded from exactly the bytes that count stands for.
    * a :class:`PrefixExtent` where it does not, read at the file's stored position. The
      digest the load just matched covers the same bytes because the segment-set
      fingerprint is the same before the load and after the extent is read; a file that
      moved in between is not upgraded.

    Never raises, and the bundle is :func:`load`'s answer whatever the rewrite does: the
    files are an optimisation, and a failed write leaves the old witness in place.
    """
    before = handle.cuts()
    files_before = _log_identity(handle)[1:]
    bundle, witnesses = _load(handle, names)
    if bundle is None:
        return DiskResume(None, {}, False)
    points: dict[int, ResumePoint] = {}
    owes_point = False
    upgrades: dict[str, PrefixWitness | CutWitness] = {}
    settled = before is not None and handle.cuts() == before
    for name, witness in witnesses.items():
        checkpoint = bundle.checkpoints[name]
        point = resume_point_from(witness.get("resume"))
        if point is not None and point.seq == checkpoint.last_seq:
            points.setdefault(point.seq, point)
        else:
            point = None
            owes_point = True
        sha = witness.get("prefix_sha")
        records = witness.get("prefix_records")
        if not isinstance(sha, str) or not isinstance(records, int):
            # Already a cut witness: nothing to upgrade here. A missing position is owed
            # by the next pass that walks (``owes_point``).
            continue
        if settled and before is not None:
            upgrades[name] = with_extent(
                handle, CutWitness(seq=checkpoint.last_seq, cuts=before, resume=point)
            )
        elif (
            before is None
            and point is not None
            and not extent_holds(handle, extent_from(witness.get("prefix_extent")))
        ):
            # No extent, or one the files have moved past (an append): the digest just
            # admitted these bytes, so the extent is re-taken once here and the next
            # resume of an unchanged log is O(1) again.
            extended = with_extent(
                handle,
                PrefixWitness(seq=checkpoint.last_seq, records=records, sha=sha, resume=point),
            )
            if extended.extent is not None:
                upgrades[name] = extended
    # The upgrade is written only if the files did not move from before the load (whose
    # digest or count admitted these files) to after every extent above was read, so an
    # extent always describes the bytes the admitting check saw.
    if upgrades and None not in files_before and _log_identity(handle)[1:] == files_before:
        _rewitness(handle, bundle, upgrades, cuts=before)
    return DiskResume(bundle, points, owes_point)


def _rewitness(
    handle: CrewLog,
    bundle: SessionProjections,
    upgrades: "Mapping[str, PrefixWitness | CutWitness]",
    *,
    cuts: "int | None",
) -> None:
    """Rewrite the named savepoints with the given witnesses, through the unit's lease.

    The state, the seq and the identity block are the ones the load just admitted; only
    the witness changes. The identity and the cut count are re-read here, so a unit
    recreated or cut between the load and this write writes nothing.
    """
    identity = _identity(handle)
    if identity is None or identity[0] != bundle.origin:
        return
    origin, first_seq = identity
    lease = _hold(handle)
    if lease is None:
        return
    try:
        directory = _ensure_dir(handle)
        if directory is None:
            return
        if handle.cuts() != cuts:
            return
        store = _UnitStore(directory)
        block = _identity_block(handle, origin, first_seq)
        for name, witness in upgrades.items():
            checkpoint = bundle.checkpoints.get(name)
            if checkpoint is None or checkpoint.last_seq != witness.seq:
                continue
            _save_one(
                store,
                checkpoint,
                unit=handle.id,
                identity=block,
                witness=witness_mapping(witness),
            )
        _discard_if_unit_gone(handle, directory)
    except Exception:  # pragma: no cover - the kernel store swallows its own errors
        log_exception_text(
            logger, logging.DEBUG, "crew log savepoint for %s not re-witnessed", handle.id
        )
    finally:
        release_lease(lease)


def discard(handle: CrewLog, names: Iterable[str]) -> None:
    """Remove the savepoints for *names*, so the next read does not trip on them again.

    For a payload that passed every admission condition and then could not be FOLDED:
    the shape checks read a state's top level, so a malformed value nested below it is
    admitted and raises inside the fold instead. Refusing it on the way in would take a
    per-fold walk of every nested container, which is a schema this package does not
    otherwise keep; discarding the file on the way out costs one cold fold and needs
    no such walk.

    Never raises. Failing to remove a file leaves the next read to fold cold again,
    which is the same answer at the same cost.
    """
    try:
        store = _UnitStore(checkpoint_dir(handle.kind, handle.id))
    except Exception:  # pragma: no cover - a path refusal from the store's checks
        log_exception_text(
            logger, logging.DEBUG, "crew log savepoint path refused for %s", handle.id
        )
        return
    for name in names:
        try:
            store.discard(handle.id, require_name(name))
        except Exception:  # pragma: no cover - the kernel store swallows its own errors
            log_exception_text(logger, logging.DEBUG, "crew log savepoint for %s not removed", name)


def resumed_prefix_still_verifies(handle: CrewLog, resumed: SessionProjections) -> bool:
    """Whether the savepoints *resumed* was loaded from still describe *handle*'s log.

    :func:`load` checks each savepoint's prefix digest BEFORE the caller folds the
    entries above it, so damage landing during that pass would otherwise leave the
    resumed state carrying a record that the file does not yield -- the state and
    the file would disagree about a prefix nobody looks at again. Asking :func:`load`
    again is what answers it, rather than a second digest routine that would have to
    agree with the first one: a changed prefix makes the same check reject that name,
    so the name drops out of the bundle or its seq moves, and either is a mismatch
    here. Growth above the savepoint is not a mismatch, since the prefix ends at the
    seq the savepoint names.

    The cost is one more pass over the consumed bytes, and it falls only on a read
    that actually resumed.
    """
    again = load(handle, tuple(resumed.checkpoints))
    if again is None:
        return False
    return {name: cp.last_seq for name, cp in again.checkpoints.items()} == {
        name: cp.last_seq for name, cp in resumed.checkpoints.items()
    }


class PrefixWitness(NamedTuple):
    """A digest of the raw records through *seq*, read at one known moment.

    :func:`save` persists this value instead of a digest of its own, so what a
    savepoint certifies are the bytes the fold that produced its state read.
    """

    seq: int
    records: int
    sha: str
    #: Where the digested prefix ENDS on disk, so a resume can settle it without
    #: hashing it again (``prefix_extent.extent_holds``). ``None`` costs that resume the digest.
    extent: "PrefixExtent | None" = None
    #: As :attr:`CutWitness.resume`. On a log with no cut counter the store does not
    #: seek to it today (it has no evidence for the bytes below it); it is stored so a
    #: build that can vouch for that prefix another way has the position to hand.
    resume: "ResumePoint | None" = None


class CutWitness(NamedTuple):
    """The unit's cut count when the records through *seq* were folded.

    What :class:`PrefixWitness` proves, proved by a COUNT instead. A cut is the only
    mutation this store performs on a committed record and the count rises durably
    before each one, so the same count again means those records are the same bytes.

    It costs a few bytes to read where the digest costs a walk of the whole consumed
    prefix -- and it costs them on the WRITE side too, which is where a savepoint's own
    share of the saving is: a digest witness has to resolve a record boundary by
    decoding every record up to it, every time one is written.
    """

    seq: int
    cuts: int
    #: The byte position of the record numbered *seq*, from the pass that folded it, so
    #: the tail read after a resume opens the file there instead of decoding every
    #: record below it. Checked by the store before use (``CrewLog.iter_from``), so a
    #: stale or forged one costs a full walk and never a wrong answer.
    resume: "ResumePoint | None" = None
    #: As :attr:`PrefixWitness.extent`: what a resume checks besides the count, so a
    #: segment replaced or rewritten in place by something other than the store is
    #: refused rather than resumed over.
    extent: "PrefixExtent | None" = None


def cut_witness(handle: CrewLog, seq: int) -> "CutWitness | None":
    """*handle*'s cut count as the witness for a fold that reached *seq*, or ``None``.

    ``None`` when the unit states no readable count, which is every log created before
    the counter existed. Such a log is settled by :func:`prefix_witness` instead, so the
    caller's fallback is a digest rather than no savepoint.

    Read BEFORE the pass that consumes the file, for the reason :func:`prefix_witness`
    gives -- but with one less thing to go wrong. A digest read before a pass certifies
    bytes a concurrent cut may change during it, and every later resume recomputes the
    same changed bytes and matches. A count cannot: a cut during the pass RAISES it, so
    the pre-pass reading differs from the live one and the write is refused.
    """
    live = handle.cuts()
    return None if live is None else CutWitness(seq=seq, cuts=live)


def cuts_unchanged(handle: CrewLog, witness: CutWitness) -> bool:
    """Whether *handle*'s cut count still reads as *witness* recorded it.

    :func:`prefix_unchanged` for the counter: asked after a pass about a witness read
    before it, so a cut that landed in between refuses the write rather than recording a
    savepoint whose own resume could never admit it.
    """
    return _cuts_admit(handle, witness.cuts)


def write_is_earned(last_seq: int, saved_seq: int) -> bool:
    """Whether a fold reaching *last_seq* owes a write against *saved_seq*.

    :func:`save` asks this itself and stays the authority for it. It is exposed
    because the witness that call needs has to be read BEFORE the fold's pass, and a
    pass that will not write a savepoint should not pay for one. Asking here is what
    avoids a second copy of the threshold.
    """
    return last_seq - saved_seq >= MIN_ADVANCE_ENTRIES


_W = TypeVar("_W", bound="PrefixWitness | CutWitness")


def prefix_witness(handle: CrewLog, seq: int) -> PrefixWitness | None:
    """The digest of *handle*'s raw records through *seq*, or ``None``.

    Read this BEFORE a fold consumes the file and ask :func:`prefix_unchanged` again
    after the pass, because a digest read only afterwards can certify bytes the pass
    never saw. A consumed record that changes in between is hashed together with
    state folded from its earlier value, and because every later resume recomputes
    the digest from those same changed bytes, the comparison passes and the state is
    served for the life of the unit while disagreeing with a cold fold. A digest is
    evidence about a prefix only when it was read from the bytes the state was.

    ``None`` when the boundary does not resolve or the walk falls short of it, which
    costs a savepoint rather than recording one nothing can check.
    """
    records = handle.raw_records_through(seq)
    if records is None:
        return None
    sha, hashed = handle.raw_prefix_digest(records)
    if hashed != records:
        return None
    return with_extent(handle, PrefixWitness(seq=seq, records=records, sha=sha))


def with_extent(handle: CrewLog, witness: "_W") -> "_W":
    """*witness* with a :class:`PrefixExtent` attached, or unchanged if none can be had.

    Taken from the byte position the witness carries (``resume``), in a few stats and one
    bounded read -- no walk of the prefix. A witness with no position gets no extent, which
    costs its later resumes the check they had before: the cut count alone, or the digest.
    """
    if witness.extent is not None or witness.resume is None or witness.resume.seq != witness.seq:
        return witness
    extent = extent_at(handle, witness.resume)
    if extent is None:
        return witness
    # ``_replace`` keeps the witness's own type, which a NamedTuple union cannot say.
    return witness._replace(extent=extent)  # type: ignore[return-value]


def prefix_unchanged(handle: CrewLog, witness: PrefixWitness) -> bool:
    """Whether *handle*'s first ``witness.records`` records still hash to *witness*.

    Growth above them is not a change: the walk stops at the count the witness
    names, which is the same reason a savepoint's own prefix ends at its seq.
    """
    sha, hashed = handle.raw_prefix_digest(witness.records)
    return hashed == witness.records and sha == witness.sha


def fold_witness(handle: CrewLog, seq: int) -> "CutWitness | PrefixWitness | None":
    """The cheapest evidence *handle*'s log can give for a fold that reaches *seq*.

    The cut count where the unit states one (:func:`cut_witness`), the digest otherwise
    (:func:`prefix_witness`). Read BEFORE the pass, and asked again after it with
    :func:`witness_unchanged`.

    The count is a few bytes to read and to check again on resume. The digest decodes
    every record up to *seq* to find its boundary, hashes them, and makes every later
    resume hash them again -- so a session savepoint written with one costs each read
    after a restart a walk of the whole consumed prefix, which is what the count exists
    to remove.
    """
    held = cut_witness(handle, seq)
    if held is not None:
        return held
    return prefix_witness(handle, seq)


def witness_unchanged(handle: CrewLog, witness: "CutWitness | PrefixWitness") -> bool:
    """Whether *witness*, read before a pass, still describes *handle*'s log after it."""
    if isinstance(witness, CutWitness):
        return cuts_unchanged(handle, witness)
    return prefix_unchanged(handle, witness)


def save(
    handle: CrewLog,
    bundle: SessionProjections,
    *,
    prefix: "PrefixWitness | CutWitness | None",
    owes_point: bool = False,
) -> SessionProjections:
    """*bundle*, with its savepoints on disk brought forward when a write is owed.

    A write is owed once the bundle has advanced :data:`MIN_ADVANCE_ENTRIES` past
    what is already persisted (``bundle.saved_seq``). The returned bundle carries
    the seq that is now on disk, so a caller reusing it across reads keeps
    deciding without reading the files again.

    *prefix* is the digest of the records the fold consumed, read by the caller
    before its pass and rechecked after it. It is persisted as given rather than
    re-derived here, because a digest read at THIS point can cover bytes the fold
    never saw: a consumed record that changed in between would be hashed against
    state folded from its earlier value, and every later resume would recompute the
    same changed bytes, match, and serve that state instead of folding cold. ``None``
    -- no witness, or one whose prefix moved during the pass -- writes nothing.

    Never raises, and never reports a write it did not make: the bundle comes back
    unchanged unless every fold in it reached its file.

    **Through the unit's lease, non-sole.** These files are derived data and any
    writer's version is a valid savepoint of the same append-only bytes, so
    nothing here needs ownership to be correct against another READER. Removal is
    the different case: it takes the lease ``sole``, which ``acquire`` refuses
    while any other hold exists, so holding a shared one across the create, the
    write and the final check is what stops a removal from starting in the middle
    of them -- and a removal already in progress refuses THIS call instead, which
    is the answer that leaves the removal whole. Contention is therefore a reason
    to skip, never to wait or retry: a savepoint is an optimization, and the read
    it was folded for is already served.
    """
    if not bundle.checkpoints:
        return bundle
    # ``owes_point`` (:class:`DiskResume`): the files on disk store no tail position, and
    # this pass has one to give them. Owed only with a position in hand, so a pass that
    # read nothing cannot rewrite five files for no gain.
    owed = owes_point and prefix is not None and prefix.resume is not None
    if bundle.last_seq - bundle.saved_seq < MIN_ADVANCE_ENTRIES and not owed:
        return bundle
    if prefix is not None and bundle.size is not None:
        # The files must be exactly as they were BEFORE the pass (the bundle's segment
        # set fingerprint), or the extent taken below would describe bytes the pass never
        # read: a record rewritten between the pass and this write would be pinned as if
        # it were the one folded. A file that moved at all -- even by an append -- costs
        # this write, and the next pass writes instead.
        _origin, live_size, live_mtime = _log_identity(handle)
        if (live_size, live_mtime) != (bundle.size, bundle.mtime_ns):
            return bundle
    if prefix is not None:
        # The position the pass reached lets this witness carry an extent, so the next
        # resume is checked in a few stats rather than by the count alone or a digest.
        prefix = with_extent(handle, prefix)
        if prefix.extent is not None and bundle.size is not None:
            # Asked again AFTER the extent was read, so a write landing between the
            # check above and that read cannot be pinned beside the earlier digest.
            if _log_identity(handle)[1:] != (bundle.size, bundle.mtime_ns):
                return bundle
    if prefix is None:
        # Either the caller never read a witness, or the prefix moved while it was
        # folding. Both mean nothing here can say which bytes produced this state,
        # and a savepoint that cannot say so is the one thing worse than none.
        return bundle
    identity = _identity(handle)
    if identity is None:
        # Also the "unit is gone" answer, and the reason there is no separate check
        # for that above: establishing the identity stats the newest segment, so a
        # removed unit fails here -- BEFORE the lease, which would otherwise create
        # a lease file inside a directory that removal has already emptied.
        return bundle
    origin, first_seq = identity
    if bundle.origin != origin:
        # The bundle was folded from a different file than the one on disk now, or
        # from one whose identity could not be established. Persisting it would
        # write a savepoint every later read has to reject.
        return bundle
    lease = _hold(handle)
    if lease is None:
        return bundle
    #: (folds offered, folds written, how long the offer took in ms). Zeroed so every
    #: exit from the block below -- including one that never reaches the write loop --
    #: leaves the counter below a value to read.
    counted: "tuple[int, int, float]" = (0, 0, 0.0)
    try:
        directory = _ensure_dir(handle)
        if directory is None:
            return bundle
        store = _UnitStore(directory)
        block = _identity_block(handle, origin, first_seq)
        # The digest the caller read before its pass, stored as the savepoint's
        # witness: it is the evidence a later read re-checks against the live file,
        # and it is the one thing equality cannot hold, since a reader cannot name a
        # record count before opening the file that states it.
        witness = witness_mapping(prefix)
        written = 0
        attempted = 0
        started = time.monotonic()
        try:
            for checkpoint in bundle.checkpoints.values():
                if checkpoint.last_seq != prefix.seq:
                    # The witness certifies ONE boundary. A fold sitting at another has
                    # no evidence here, and re-reading the file for it is exactly what
                    # this call must not do, so its write waits for a pass whose witness
                    # covers it. The partial-write check below then leaves the bundle
                    # claiming nothing, which costs a cold fold rather than a digest that
                    # may describe bytes nothing folded.
                    continue
                attempted += 1
                if _save_one(store, checkpoint, unit=handle.id, identity=block, witness=witness):
                    written += 1
        finally:
            # The metric's two numbers, read where they are known and counted after the
            # lease is released. In a ``finally`` so a raise on the way through is
            # counted as a failure rather than going unrecorded -- the store does not
            # raise today, and a counter that assumed so would go quiet on the day it
            # did.
            counted = (attempted, written, (time.monotonic() - started) * 1000.0)
        if _discard_if_unit_gone(handle, directory):
            return bundle
    finally:
        release_lease(lease)
        # One point per PASS, not per fold: the folds are offered as a set and the
        # question is whether the disk took them. ``written == attempted``, NOT
        # ``== len(bundle.checkpoints)``: a fold skipped above sat at another boundary
        # and was never offered, so holding the pass to the whole bundle would report a
        # refusal as a failing disk. Whether the BUNDLE may claim a savepoint is a
        # stricter question and is answered below, where it belongs. A pass that offered
        # nothing is counted as nothing.
        if counted[0]:
            _count_savepoint_write(
                kind="session",
                written=counted[1] == counted[0],
                elapsed_ms=counted[2],
            )
    if written != len(bundle.checkpoints):
        # A partial write leaves a correct set of files -- each names its own fold
        # and seq -- but the bundle must not claim a savepoint the folds that failed
        # do not have, or the next read would skip the write they are still owed.
        return bundle
    # ``replace`` rather than a field-by-field rebuild: this function's only edit
    # is ``saved_seq``, and naming the other fields here would silently drop any
    # field it does not know about -- the caller's size and mtime stamps are what
    # let its next no-growth poll skip the validating walk, and losing them here
    # would charge one full-file read per savepoint write for nothing.
    return dataclasses.replace(
        bundle, saved_seq=min(cp.last_seq for cp in bundle.checkpoints.values())
    )


# --------------------------------------------------------------------------- #
# A slot's fold -- one cell folded across the units a slot ran under
# --------------------------------------------------------------------------- #
#
# A SLOT fold concatenates several units into one stream, where a session fold reads one
# file. Everything above still applies -- the same kernel store, the same identity
# equality, the same witness handed to :func:`prefix_admit` -- and only two things are
# added, both forced by the concatenation.
#
# THE IDENTITY BLOCK CARRIES THE WHOLE UNIT VECTOR. A slot fold's state was folded over
# every unit the slot ran under, so the facts that must match verbatim are the slot key,
# the unit list in fold order, and every EARLIER unit's whole mark -- its origin, its
# height and its byte fingerprint, as the fold surface recorded them. That is the same
# comparison ``projection._continuable`` makes before carrying a warm cell forward, so a
# savepoint is admitted on exactly the shapes a warm cell is: an added or removed unit, an
# earlier unit that grew, an earlier unit rewritten in place, and a unit recreated under
# the same id each refuse the file. The NEWEST unit is held to its origin and its witness
# instead, because it is the one unit a continuation exists to let grow.
#
# TWO POSITIONS, NOT ONE. The kernel orders a slot stream by an ORDINAL -- a unit's base
# plus an entry's seq -- while the checkpoint the fold surface's callers carry reports the
# newest unit's OWN seq. So ``watermark`` is the ordinal the cell resumes at and the
# witness's ``seq`` is the unit seq the tail stream resumes from, and the two are tied by
# the newest unit's ordinal base. That base is reproducible on resume precisely BECAUSE
# every earlier unit's mark is in the identity block: a vector that compares equal yields
# the same base, so the equality is what makes the ordinal meaningful across processes.
# The tie is checked on both sides -- nothing is written or resumed whose two numbers
# disagree -- because an ordinal off by a unit's height would resume a cell past entries
# the tail then folds again.


#: Key the slot payload's ``state`` rides under, holding the fold state as a JSON STRING
#: rather than as the object itself.
#:
#: THE ORDER OF A STATE'S KEYS IS PART OF THE STATE. The kernel store serializes with
#: ``sort_keys=True``, which is right for an envelope a person greps and wrong for these
#: folds: ``ledger``'s artifact map and ``radar``'s ``last_update`` are ordered by
#: INSERTION and age out ``next(iter(...))``, so a state that came back alphabetical
#: evicts whichever key sorts first instead of the oldest one. On a slot whose artifact
#: keys are not already in alphabetical order that drops the NEWEST pointer on the first
#: read after a restart, and the record a reader is served then disagrees with a cold
#: fold for the life of the unit.
#:
#: Encoding the state here keeps the order the fold wrote it in, and keeps it without
#: changing what the kernel does for its other clients -- whose folds would each need
#: their own audit before that sort could be called safe to drop. The cost is the escape
#: characters a JSON string inside JSON carries.
#:
#: A payload written before this wrapper decodes to a state with none of the fold's own
#: keys, which :func:`~kiro_crew.crew_log.projection.Checkpoint.from_dict` refuses, so it
#: is retired to a cold fold rather than misread.
_SLOT_STATE_JSON: Final[str] = "state_json"


class SlotSavepoint(NamedTuple):
    """One slot fold's resumable position, as :func:`load_slot` answers it."""

    name: str
    state: Any
    #: The kernel ORDINAL the cell stands at -- the newest unit's base plus
    #: :attr:`reached`. What ``prime_checkpointed`` installs as the cell's watermark.
    watermark: int
    #: The newest unit's OWN seq. What the tail stream resumes from, and what the
    #: witness certifies a prefix digest through.
    #:
    #: The digest itself is deliberately NOT handed back. It would look like a free
    #: substitute for the one a resuming caller has to read before its own pass -- it was
    #: just verified against these bytes, after all -- but it ends at THIS seq, and that
    #: caller is about to fold the tail above it. A cell carrying it vouches for bytes it
    #: never read, which costs a wrong value on the next continuation and a savepoint
    #: :func:`prefix_admit` refuses on the next restart.
    reached: int
    #: The byte position of the newest unit's record at :attr:`reached`, as the writer's
    #: pass found it, for the tail read to seek to. ``None`` for a file that stored none.
    resume: "ResumePoint | None" = None
    #: Whether the file's witness can be checked again without a full prefix walk -- a
    #: cut count, or a digest carrying a :class:`PrefixExtent`. A caller resuming from a
    #: file where this or :attr:`resume` is missing rewrites it once with what it has.
    cheap: bool = False


def slot_identity(
    *,
    slot: str,
    units: Sequence[str],
    marks: Sequence[Any],
    unit: str,
    origin: str,
    first_seq: int,
) -> dict[str, Any]:
    """The facts a slot savepoint must MATCH to describe this fold, compared verbatim.

    Public and client-neutral for the reason :func:`prefix_admit` is: the write and the
    resume both build this, and two spellings of "is this the same fold over the same
    units" would eventually disagree -- with the lenient one serving a value no cold fold
    reproduces.

    *marks* is every EARLIER unit's whole mark, JSON-shaped by the caller. Lists rather
    than objects, so a field a later build adds cannot make an older payload's comparison
    pass by absence: a shape change here retires every savepoint written before it, which
    is this module's standing answer to a payload it cannot be sure of.
    """
    return {
        "slot": slot,
        "units": [str(one) for one in units],
        "marks": [list(mark) for mark in marks],
        "unit": unit,
        "origin": origin,
        "first_seq": first_seq,
    }


def load_slot(
    handle: CrewLog,
    name: str,
    *,
    slot: str,
    units: Sequence[str],
    marks: Sequence[Any],
    ordinal_base: int,
) -> SlotSavepoint | None:
    """The savepoint for one slot fold, or ``None`` for every reason not to resume.

    *handle* is the NEWEST unit's log. *marks* is every earlier unit's whole mark and
    *ordinal_base* is the newest unit's ordinal base, which the caller derives from those
    same marks -- so a block that compares equal is also a block that reproduces this
    number.

    Never raises, and every refusal is the same answer as an absent file: fold cold,
    which reaches the same value at more cost.
    """
    require_name(name)
    identity = _identity(handle)
    if identity is None:
        return None
    origin, first_seq = identity
    block = slot_identity(
        slot=slot,
        units=units,
        marks=marks,
        unit=handle.id,
        origin=origin,
        first_seq=first_seq,
    )
    try:
        store = _UnitStore(slot_checkpoint_dir(handle.kind, handle.id))
    except Exception:  # pragma: no cover - a path refusal from the store's checks
        log_exception_text(
            logger, logging.DEBUG, "crew log slot savepoint path refused for %s", handle.id
        )
        return None
    try:
        savepoint = store.load(
            handle.id,
            name,
            state_version=fold_state_version(name),
            identity=block,
            admit=prefix_admit(handle, first_seq),
        )
    except RecursionError:
        # A payload nested past the interpreter's stack limit raises straight through
        # the kernel's guard, as it does for a session fold -- and this function
        # promises never to raise.
        log_exception_text(
            logger, logging.DEBUG, "crew log slot savepoint for %s unusable; folding cold", name
        )
        return None
    except Exception:  # pragma: no cover - a path refusal from the store's checks
        log_exception_text(logger, logging.DEBUG, "crew log slot savepoint refused for %s", name)
        return None
    if savepoint is None:
        return None
    reached = savepoint.witness.get("seq")
    if not isinstance(reached, int) or isinstance(reached, bool) or reached <= 0:
        return None
    if savepoint.watermark != ordinal_base + reached:
        # The payload disagrees with ITSELF about where it stands: the ordinal the cell
        # would resume at has to be the newest unit's base plus the seq the witness
        # certifies. Nothing here can say which of the two is right, so it is not a
        # savepoint of this fold.
        logger.debug("crew log slot savepoint %s/%s disagrees about its own position", slot, name)
        return None
    state = _decoded_slot_state(savepoint.state, name)
    if state is None:
        return None
    try:
        # The fold surface's own validation, against the UNIT seq -- the number a
        # checkpoint's contract names -- rather than the ordinal.
        Checkpoint.from_dict({"name": name, "last_seq": reached, "state": state})
    except Exception:
        log_exception_text(
            logger,
            logging.DEBUG,
            "crew log slot savepoint for %s refused by the fold surface",
            name,
        )
        return None
    point = resume_point_from(savepoint.witness.get("resume"))
    return SlotSavepoint(
        name=name,
        state=state,
        watermark=savepoint.watermark,
        reached=reached,
        resume=point if point is not None and point.seq == reached else None,
        cheap=("prefix_cuts" in savepoint.witness and "prefix_sha" not in savepoint.witness)
        or extent_from(savepoint.witness.get("prefix_extent")) is not None,
    )


def _decoded_slot_state(payload: Any, name: str) -> "dict[str, Any] | None":
    """The fold state inside a slot payload, with the key order its writer used.

    ``None`` for anything that is not this module's own wrapper around a JSON object --
    a payload from before the wrapper, a truncated string, a value that decodes to a
    list. Every one is the same answer as an absent file.
    """
    if not isinstance(payload, dict):
        return None
    encoded = payload.get(_SLOT_STATE_JSON)
    if not isinstance(encoded, str):
        logger.debug("crew log slot savepoint for %s carries no encoded state", name)
        return None
    try:
        state = json.loads(encoded)
    except ValueError:
        logger.debug("crew log slot savepoint for %s has an unreadable state", name)
        return None
    return state if isinstance(state, dict) else None


def slot_resume_still_verifies(
    handle: CrewLog,
    name: str,
    *,
    slot: str,
    units: Sequence[str],
    marks: Sequence[Any],
    ordinal_base: int,
    resumed: SlotSavepoint,
) -> bool:
    """Whether the savepoint *resumed* came from still describes *handle*'s log.

    :func:`load_slot` checks the savepoint's prefix BEFORE its caller folds the tail
    above it, and the window between those two things is not empty. The store's
    supersede repair replaces orphan chunk records at seqs the savepoint already
    consumed; a tail that starts above them never reads the replacement, and a digest
    the caller captured after the repair matches the repaired file, so every check on
    that side passes while the state came from the bytes the repair removed.

    Asking :func:`load_slot` AGAIN is what answers it, rather than a second digest
    routine that would have to agree with the first one: the repair changes the prefix
    the savepoint's own witness certifies, so the same admission refuses it, and the
    answer comes back ``None`` or at another position. Either is a mismatch here. Growth
    above the savepoint is not, since its witness ends at the seq it names.

    The cost is one more pass over the records the savepoint covers, and it falls only
    on a read that actually resumed. This is :func:`resumed_prefix_still_verifies` for
    the slot half, and it exists separately only because the identity it re-compares is
    the unit vector rather than one log's.
    """
    again = load_slot(handle, name, slot=slot, units=units, marks=marks, ordinal_base=ordinal_base)
    if again is None:
        return False
    return (again.watermark, again.reached) == (resumed.watermark, resumed.reached)


def save_slot(
    handle: CrewLog,
    name: str,
    *,
    slot: str,
    units: Sequence[str],
    marks: Sequence[Any],
    ordinal_base: int,
    state: Any,
    watermark: int,
    prefix: "PrefixWitness | CutWitness",
    expect_origin: str,
    expect_files: "tuple[int | None, int | None] | None" = None,
) -> bool:
    """Write one slot fold's savepoint. ``True`` when it reached the file.

    Whether a write is OWED is the caller's to ask (:func:`write_is_earned`), because the
    position it measures is the kernel ordinal and only the caller holds it. What this
    function owes in return is that nothing is written whose state it cannot tie to
    bytes: *prefix* is the evidence the caller read BEFORE its pass and re-checked after
    it -- a cut count (:class:`CutWitness`) on a unit that states one, a digest
    (:class:`PrefixWitness`) on one that does not -- *watermark* must be
    ``ordinal_base + prefix.seq``, and *expect_origin* is the identity the pass folded
    under.

    *expect_origin* is not redundant with the block built below. A unit removed and
    recreated between the pass and this call gives a FRESH origin here, and writing the
    state under it would record a savepoint claiming the new file for state folded from
    the old one -- a payload every later read would accept and no cold fold would
    reproduce.

    Never raises. Through the unit's NON-SOLE lease, for the reasons :func:`save` states:
    a removal takes it ``sole`` and refuses this instead, which leaves the removal whole,
    and contention is a reason to skip rather than to wait.
    """
    require_name(name)
    if prefix.seq <= 0 or watermark != ordinal_base + prefix.seq:
        # Either the newest unit folded nothing, or the witness certifies one boundary
        # and the state stands at another. Both leave nothing here able to say which
        # bytes produced this state, and a savepoint that cannot say so is the one thing
        # worse than none.
        return False
    identity = _identity(handle)
    if identity is None:
        # Also the "unit is gone" answer, and why it comes BEFORE the lease: establishing
        # the identity stats the newest segment, so a removed unit fails here rather than
        # creating a lease file inside a directory removal has already emptied.
        return False
    origin, first_seq = identity
    if origin != expect_origin:
        return False
    if isinstance(prefix, PrefixWitness) and prefix.records < max(0, prefix.seq - first_seq + 1):
        # The witness cannot cover its own seq span: a prefix through ``seq`` holds at
        # least one raw record per surviving entry, so a count below that describes a
        # SHORTER stretch of the file than the state was folded from. It is the same
        # inequality :func:`prefix_admit` applies on the way in, asked here so a payload
        # that could only ever be refused is never written -- and so a caller that
        # carried a digest from a lower boundary than its own pass is told no at the
        # write rather than on the restart after it.
        #
        # A cut witness has no span to fall short of: it says the records this unit holds
        # are the ones they were, whichever records those are, so retention taking the
        # front off is caught by ``first_seq`` in the identity block rather than here.
        logger.debug(
            "crew log slot savepoint %s/%s has a witness of %d records for seq %d; not written",
            slot,
            name,
            prefix.records,
            prefix.seq,
        )
        return False
    # So the next resume is settled by a few stats and one bounded read: for a unit with
    # no cut counter instead of a walk of every record, and for one with a counter as a
    # check on the files themselves (``prefix_extent.extent_holds``).
    #
    # Only when the segment-set fingerprint after the extent read is still
    # *expect_files*, the one the caller sampled BEFORE the pass that the witness vouches
    # for. Otherwise the files moved somewhere between the witness and this read, the
    # extent could pin bytes the witness never covered, and the payload is written
    # without one (its next resume pays the check it always paid).
    extended = with_extent(handle, prefix)
    if (
        extended.extent is not None
        and expect_files is not None
        and None not in expect_files
        and _log_identity(handle)[1:] == expect_files
    ):
        prefix = extended
    block = slot_identity(
        slot=slot,
        units=units,
        marks=marks,
        unit=handle.id,
        origin=origin,
        first_seq=first_seq,
    )
    lease = _hold(handle)
    if lease is None:
        return False
    written = False
    #: Whether the store was actually asked to write. Set only around that call, so
    #: every earlier exit from the block below records nothing.
    offered = False
    elapsed_ms = 0.0
    try:
        directory = _ensure_dir(handle, SLOT_CHECKPOINT_DIR)
        if directory is None:
            return False
        store = _UnitStore(directory)
        try:
            # Encoded HERE rather than handed over as an object, so the kernel's
            # ``sort_keys`` cannot reorder a state whose key order is part of its
            # meaning. See :data:`_SLOT_STATE_JSON`.
            encoded = json.dumps(state, ensure_ascii=False)
        except (TypeError, ValueError):
            # A fold state that will not serialize loses its savepoint and nothing
            # else, which is the kernel store's own answer to the same thing.
            log_exception_text(
                logger, logging.DEBUG, "crew log slot fold %s has an unserializable state", name
            )
            return False
        started = time.monotonic()
        try:
            written = store.save(
                handle.id,
                Savepoint(
                    key=name,
                    state_version=fold_state_version(name),
                    watermark=watermark,
                    state={_SLOT_STATE_JSON: encoded},
                    identity=block,
                    witness=witness_mapping(prefix),
                ),
            )
        finally:
            # In a ``finally`` for the reason :func:`save` states: a raise through the
            # store is a failed write and must be counted as one. ``written`` is still
            # False here on that path, since the assignment never completed.
            offered = True
            elapsed_ms = (time.monotonic() - started) * 1000.0
        if _discard_if_unit_gone(handle, directory):
            return False
    finally:
        release_lease(lease)
        # After the lease, for the reason :func:`_count_savepoint_write` states. Exactly
        # one point per call that reached the store, and none from a call that returned
        # before it: an unserializable state, a missing directory and a contended lease
        # are refusals, not failed writes.
        if offered:
            _count_savepoint_write(kind="slot", written=bool(written), elapsed_ms=elapsed_ms)
    return written


def discard_slot(handle: CrewLog, names: Iterable[str]) -> None:
    """Remove the slot savepoints for *names*, so the next read does not trip on them.

    For a payload that passed every admission condition and then could not be FOLDED --
    the case :func:`discard` exists for, with the same reasoning and the same promise
    never to raise.
    """
    try:
        store = _UnitStore(slot_checkpoint_dir(handle.kind, handle.id))
    except Exception:  # pragma: no cover - a path refusal from the store's checks
        log_exception_text(
            logger, logging.DEBUG, "crew log slot savepoint path refused for %s", handle.id
        )
        return
    for name in names:
        try:
            store.discard(handle.id, require_name(name))
        except Exception:  # pragma: no cover - the kernel store swallows its own errors
            log_exception_text(
                logger, logging.DEBUG, "crew log slot savepoint for %s not removed", name
            )


# --------------------------------------------------------------------------- #
# One file
# --------------------------------------------------------------------------- #


def _save_one(
    store: _UnitStore,
    checkpoint: Checkpoint,
    *,
    unit: str,
    identity: Mapping[str, Any],
    witness: Mapping[str, Any],
) -> bool:
    """Write one fold's savepoint. ``True`` when it reached the file.

    The kernel store owns the serialization, the size cap and the atomic rename, and
    it never raises: a fold whose state cannot be serialized, or whose state is over
    the cap, loses its savepoint and nothing else -- which is not a reason to fail the
    read it was folded for.
    """
    return store.save(
        unit,
        Savepoint(
            key=checkpoint.name,
            state_version=fold_state_version(checkpoint.name),
            watermark=checkpoint.last_seq,
            state=checkpoint.state,
            identity=identity,
            witness=witness,
        ),
    )


def _resume_one(
    store: _UnitStore,
    handle: CrewLog,
    name: str,
    block: Mapping[str, Any],
    admit: Admit,
) -> "tuple[Checkpoint, Mapping[str, Any]] | None":
    """One fold's savepoint and the witness it was admitted on, or ``None`` to fold cold.

    The kernel decides the envelope, the fold name, the state shape's version and the
    identity; :func:`prefix_admit` decides the live-log conditions. What is left here is
    the crew log's own two: the payload must not disagree with itself about the seq it
    stands at, and the state must satisfy the fold surface's validation.
    """
    try:
        savepoint = store.load(
            handle.id,
            name,
            state_version=fold_state_version(name),
            identity=block,
            admit=admit,
        )
    except RecursionError:
        # ``RecursionError`` is a ``RuntimeError`` rather than a ``ValueError``, so a
        # payload nested past the interpreter's stack limit raises straight through the
        # kernel's own guard, at a few tens of KB and far under the size cap. It is
        # also the only unusable payload that survives being read -- the file stays on
        # disk -- so an escape costs the session every later fold rather than one cold
        # fold, and this function promises never to raise.
        log_exception_text(
            logger, logging.DEBUG, "crew log savepoint for %s unusable; folding cold", name
        )
        return None
    except Exception:  # pragma: no cover - a path refusal from the store's checks
        log_exception_text(logger, logging.DEBUG, "crew log savepoint refused for %s", name)
        return None
    if savepoint is None:
        # Absent is the ordinary case: no session has a savepoint until one is
        # written, and a cold fold is the answer.
        return None
    if savepoint.witness.get("seq") != savepoint.watermark:
        # The witness certifies ONE boundary, and the state resumes at another. Nothing
        # here can say which is right, so the file is not a savepoint of this fold.
        logger.debug("crew log savepoint for %s disagrees about its own seq", name)
        return None
    try:
        checkpoint = Checkpoint.from_dict(
            {"name": name, "last_seq": savepoint.watermark, "state": savepoint.state}
        )
        return checkpoint, savepoint.witness
    except Exception:
        # The fold surface's own validation refused the payload. Same answer as
        # every other unusable file.
        log_exception_text(
            logger, logging.DEBUG, "crew log savepoint for %s refused by the fold surface", name
        )
        return None


# --------------------------------------------------------------------------- #
# The unit
# --------------------------------------------------------------------------- #


def _hold(handle: CrewLog) -> str | None:
    """A NON-SOLE lease on *handle*'s unit, or ``None`` when it cannot be had.

    ``None`` covers both refusals with one answer, because the caller's response is
    the same: write nothing. A removal holding the lease ``sole`` refuses this, and
    so does a lease file that cannot be opened at all.
    """
    try:
        return acquire_lease(
            crew_log_dir(handle.kind, handle.id) / LEASE_FILE,
            kind=handle.kind,
            unit_id=handle.id,
        )
    except (CrewLogError, OSError):
        log_exception_text(
            logger, logging.DEBUG, "crew log savepoint for %s not owned; skipping", handle.id
        )
        return None
    except Exception:  # pragma: no cover - a path refusal from the store's checks
        log_exception_text(
            logger, logging.DEBUG, "crew log savepoint lease refused for %s", handle.id
        )
        return None


def _history_present(handle: CrewLog) -> bool:
    """Whether *handle*'s unit still has a segment.

    ``False`` means the unit is being removed or already is. An error reading the
    directory is reported as PRESENT: it is not evidence of removal, and the
    savepoint is checked against the log on every read anyway.
    """
    try:
        return bool(segment_paths(handle.kind, handle.id))
    except OSError:
        return True
    except Exception:
        return True


def _identity(handle: CrewLog) -> tuple[str, int] | None:
    """``(origin, first_seq)`` for *handle*'s log, or ``None`` when unknown.

    ``None`` is "cannot establish which bytes these are". It never matches a stored
    identity and is never written into one, so a stat failure or a header without a
    creation stamp costs a savepoint rather than producing one nothing can check.
    """
    origin = log_origin(handle)
    if origin is None:
        return None
    try:
        fronts = segment_first_seqs(handle.kind, handle.id)
    except Exception:
        return None
    if not fronts:
        return None
    return (origin, fronts[0])


def _ensure_dir(handle: CrewLog, leaf: str = CHECKPOINT_DIR) -> Path | None:
    """*handle*'s savepoint directory, created owner-only, or ``None``.

    *leaf* selects which of the unit's two savepoint directories is meant --
    :data:`CHECKPOINT_DIR` for its own session folds, :data:`SLOT_CHECKPOINT_DIR` for the
    slot folds kept at it. A parameter rather than two near-identical functions, so the
    mode, the owner restriction and the mid-write-removal reasoning below have one home.

    ``parents=False``: this function never creates a unit directory, only the
    savepoint directory inside one that already exists. It is NOT on its own a
    guarantee that a reader cannot resurrect a removed unit, because
    ``atomic_write`` creates its target's parents itself -- the guarantee comes from
    the three together: the caller holds the unit's lease so a removal cannot run
    underneath, it checks for a segment before and after writing, and
    :func:`_discard_if_unit_gone` tears down a directory the write did recreate.

    The mode is set at creation rather than after it, so the directory is never
    briefly wider than its contents allow; ``restrict_dir_to_owner`` then covers
    Windows, where the POSIX mode is a documented no-op. Both are best-effort for
    the reason the store's own directory creation is: a filesystem that refuses the
    mode change must not break the feature, and the sandbox mask and the file-tool
    fence over ``crew-log`` still stand.
    """
    try:
        directory = crew_log_dir(handle.kind, handle.id) / leaf
        directory.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        log_exception_text(
            logger, logging.DEBUG, "crew log savepoint directory unavailable for %s", handle.id
        )
        return None
    except Exception:  # pragma: no cover - a path refusal from the store's checks
        log_exception_text(
            logger, logging.DEBUG, "crew log savepoint directory refused for %s", handle.id
        )
        return None
    try:
        restrict_dir_to_owner(directory)
    except OSError:
        log_exception_text(
            logger, logging.DEBUG, "crew log savepoint directory not restricted: %s", directory
        )
    return directory


def _discard_if_unit_gone(handle: CrewLog, directory: Path) -> bool:
    """Remove what was just written when the unit's history is gone. ``True`` if it was.

    The caller holds the unit's lease, so a removal cannot delete the segments
    between its check and this one. This covers what the lease cannot: a removal
    that finished BEFORE the lease was taken, and any writer that unlinks segments
    without taking it. No segment means the unit is gone, so what this call wrote
    is deleted again rather than left behind for a session that is gone.

    The unit directory goes too when it comes away empty. ``atomic_write`` creates
    its target's parents, so a write that landed after a removal had emptied the
    tree rebuilt the unit directory as well, and nothing later collects an empty
    one -- the retention sweep reads a unit's own entries to decide, and a unit with
    no segments has none. Best-effort by nature: the ``rmdir`` fails while any
    other file is in there, which for a unit that still has its lease file is the
    common case, and a removal in progress collects it instead.
    """
    if _history_present(handle):
        return False
    logger.debug("crew log unit %s went away mid-write; discarding its savepoints", handle.id)
    for child in _files_in(directory):
        try:
            child.unlink()
        except OSError:
            log_exception_text(logger, logging.DEBUG, "crew log savepoint %s not removed", child)
    for victim in (directory, directory.parent):
        try:
            victim.rmdir()
        except OSError:
            log_exception_text(
                logger, logging.DEBUG, "crew log savepoint directory %s kept", victim
            )
            break
    return True


def _files_in(directory: Path) -> list[Path]:
    """Every regular file directly in *directory*, or an empty list."""
    try:
        return [child for child in directory.iterdir() if child.is_file()]
    except OSError:
        return []
