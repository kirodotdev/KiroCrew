"""Folding one session's crew log on the projection kernel: the panel bundle
(:class:`SessionProjections`), the cold fold and its savepoint resume
(:func:`fold_session`), and the kernel units that drive the session folds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kiro_crew.crew_log.projection_catalogue import (
    KNOWN_TYPES,
    PROJECTION_NAMES,
    Projection,
    _Fold,
    logger,
)
from kiro_crew.crew_log.projection_kernel import Checkpoint, initial, projection_of, require_name
from kiro_crew.crew_log.projection_registry import _FOLDS
from kiro_crew.crew_log.projection_session_log import _log_identity, log_origin, open_session_log
from kiro_crew.crew_log.schema import Entry
from kiro_crew.crew_log.store import CrewLog, log_exception_text
from kiro_crew.projection import ProjectionRegistry, Savepoint, attribute_seq

if TYPE_CHECKING:
    # Type-only: the savepoint module imports the fold surface, so a runtime
    # import here would close the cycle its function-local imports avoid.
    from kiro_crew.crew_log.checkpoint import PrefixWitness


# --------------------------------------------------------------------------- #
# Reading a session's log
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SessionProjections:
    """Every projection for one session, all folded through the same seq.

    One pass over the file serves every requested fold, which is what makes pushing the whole
    side panel on each growth cost one read rather than one per fold.
    """

    session_id: str
    last_seq: int
    checkpoints: Mapping[str, Checkpoint] = field(default_factory=dict)
    #: The crew log file's creation identity when these checkpoints were folded,
    #: so a reuse (:func:`fold_session` ``since=``) can tell that the log it folds
    #: now is the SAME file. A log removed and recreated restarts its seqs, and if
    #: it grows past the cached seq before the next fold the seq guard alone
    #: passes -- stale state would then apply to a different file's bytes. ``None``
    #: when no log existed (the empty bundle) and never matches a real file.
    origin: str | None = None
    #: The seq every checkpoint in this bundle is PERSISTED through
    #: (:mod:`kiro_crew.crew_log.checkpoint`), which is not the seq it was folded
    #: through: a savepoint is allowed to lag, because resuming from an older one
    #: replays the tail and reaches the same value. Carried on the bundle so a
    #: caller reusing it across reads decides whether a write is owed from what it
    #: already holds, instead of reading the savepoint files to find out. 0 is
    #: "nothing on disk", which is what an unpersisted bundle must claim.
    saved_seq: int = 0

    #: A size fingerprint of the WHOLE segment set, captured beside ``origin``:
    #: the byte sum across every segment the walk would read, with the segment
    #: count folded in so a segment appearing or vanishing cannot cancel against
    #: another's growth. A reusable bundle may skip walking the log only while
    #: this still matches; ``None`` keeps bundles created before this field safe
    #: by forcing one validating walk before their next O(1) poll.
    size: int | None = None

    #: The newest modification time across the same segment set as ``size``. It
    #: detects an in-place same-size rewrite that size alone cannot distinguish;
    #: ``None`` keeps older bundles safe by forcing one validating walk.
    mtime_ns: int | None = None

    def projection(self, name: str) -> Projection:
        """One rendered projection, or raise ``bad_data`` for an unknown name."""
        return projection_of(self.checkpoints[require_name(name)])

    def rendered(self) -> dict[str, Projection]:
        """Every projection this bundle holds, rendered."""
        return {name: projection_of(cp) for name, cp in self.checkpoints.items()}


def empty_session(session_id: str, names: Iterable[str] = PROJECTION_NAMES) -> SessionProjections:
    """A bundle at seq 0 -- what a session with no crew log folds to.

    An absent log is not an error here. A session that ran with the emitter off
    has none, and its projections are the empty ones rather than a refusal, so a
    caller can render the panel without first asking whether the file exists.
    """
    return SessionProjections(
        session_id=session_id,
        last_seq=0,
        checkpoints={name: initial(name) for name in (require_name(n) for n in names)},
    )


def fold_session(
    session_id: str,
    names: Iterable[str] = PROJECTION_NAMES,
    *,
    since: SessionProjections | None = None,
    log: CrewLog | None = None,
) -> SessionProjections:
    """Every named projection for *session_id*, folded in one pass.

    *since* is a bundle from an earlier call and turns this into an incremental
    read: only the entries after its seq are consumed. It is discarded and the
    fold starts over in the two cases where continuing would be wrong -- the log
    is now SHORTER than the bundle (the unit was removed and recreated, so its
    seqs start again and the bundle describes different bytes), or the bundle is
    missing a name this call asks for.

    *log* is an already-open handle, so a caller that has just read
    ``last_seq`` folds against the same handle rather than opening the file
    twice.

    The on-disk savepoint (:mod:`kiro_crew.crew_log.checkpoint`) is not optional
    and has no switch: with no reusable *since* the fold resumes from what is
    beside the log instead of from seq 1, and the result is written back once it
    has moved far enough to earn a write. A read that DID reuse *since* serves from
    it but writes nothing, because it cannot vouch for the prefix that bundle was
    folded from -- so a hot incremental reader's savepoint is brought forward by the
    next read that folds the prefix itself rather than by every read. A flag would be
    a public surface with no production caller, and it is not needed to reach the
    from-scratch answer -- :func:`fold` and :func:`advance` ARE that answer, and a
    savepoint is never load-bearing, since every failure over there falls back to
    folding from seq 1.
    """
    wanted = tuple(require_name(name) for name in names)
    bundle, stable, handle, prefix = _fold_attempt(
        session_id, wanted, since=since, log=log, resume=True
    )
    if stable:
        return _persisted(bundle, handle=handle, prefix=prefix)
    # The file's identity changed WHILE it was being folded: the unit was removed
    # and recreated between the identity read and the pass, so the entries just
    # consumed may belong to a different file than the state they were folded onto.
    # One more attempt, from scratch -- no cached bundle, no savepoint, and a freshly
    # opened handle, since the one this call was given does not name the file it was
    # opened on.
    bundle, stable, handle, prefix = _fold_attempt(
        session_id, wanted, since=None, log=None, resume=False
    )
    if stable:
        return _persisted(bundle, handle=handle, prefix=prefix)
    # Twice in a row, so the unit is being recreated faster than it can be read.
    # The value is served, because the alternative is refusing to render a session
    # that exists, but its identity is reported as UNKNOWN: that is what stops a
    # caller from reusing it and stops it from being written to disk, both of which
    # compare against this field and neither of which accepts ``None``.
    logger.debug("crew log %s changed identity twice while folding it", session_id)
    return SessionProjections(
        session_id=session_id,
        last_seq=bundle.last_seq,
        checkpoints=bundle.checkpoints,
        origin=None,
        saved_seq=0,
    )


def _note_session_fold_origin(origin: str) -> None:
    """Stamp where this session pass got its starting state, for the metrics side.

    Function-local import and swallowed failure, which is the policy
    :mod:`kiro_crew.crew_log.eager` already follows for this same package: a
    telemetry package that will not load costs a fold its metric and nothing
    else. It also keeps ``kiro_crew.metrics`` out of this family's module-scope
    imports, so the projection facade does not have to re-export a name that is
    an implementation detail of one record site.

    STAMPS, never records: the mark is turned into a point by the eager fold
    worker after the pass, outside the per-cell locks. Off that worker's thread
    the stamp itself is a no-op, so a dashboard read pays one attribute lookup.
    No ``exc_info``: this module's frames bind open ``CrewLog`` handles.
    """
    try:
        from kiro_crew.metrics.crew_log_health import SCOPE_SESSION, note_fold_origin

        note_fold_origin(SCOPE_SESSION, origin)
    except Exception:  # pragma: no cover - a broken install loses the metric only
        logger.debug("crew log fold origin metric unavailable")


def _fold_attempt(
    session_id: str,
    wanted: Sequence[str],
    *,
    since: SessionProjections | None,
    log: CrewLog | None,
    resume: bool,
) -> tuple[SessionProjections, bool, CrewLog | None, PrefixWitness | None]:
    """One pass for :func:`fold_session`. ``(bundle, the file held still, handle, witness)``.

    The middle element is what makes the pass checkable. ``iter_from`` opens the
    log by NAME, so a unit removed and recreated mid-pass hands this function a
    different file's entries while it holds the first file's state -- and the seq
    numbers do not say so, because a recreated log starts its own again. So the
    identity is read before the pass and again after it, and a change makes the
    bundle untrustworthy rather than merely stale. The caller decides what to do
    about it; nothing is persisted from here, which is why the handle comes back
    too -- the caller writes the savepoint against the same handle rather than
    opening the file a second time.
    """
    handle = log if log is not None else open_session_log(session_id)
    if handle is None:
        return (empty_session(session_id, wanted), True, None, None)
    last_seq = handle.last_seq
    origin, size, mtime_ns = _log_identity(handle)
    # What the pass trusted about the file before reading it, so the same two things
    # can be asked again afterwards. The savepoint is held rather than a copy of its
    # digest: re-running the load is what re-checks it, and that keeps one routine
    # deciding whether a savepoint describes this file.
    resumed_from: SessionProjections | None = None
    prefix_seen: PrefixWitness | None = None
    # Whether this pass is standing on state an EARLIER call folded. That decides
    # whether it may write a savepoint at all, because a savepoint has to carry a
    # digest of the bytes its state came from. A pass that resumed from DISK carries
    # one transitively: the savepoint records the digest its own writer read before
    # folding, and ``resumed_prefix_still_verifies`` checks it again here, so the
    # custody survives the gap between the two calls. A cached bundle records no
    # digest -- there is no such field on it -- and the bytes below its seq were
    # consumed by a call that has already returned, so nothing this pass can read is
    # evidence about them. A digest read now would be honest about the file and wrong
    # about the state beside it, and the two would then agree with each other, so
    # every later resume would recompute those same bytes, match, and serve that state
    # for the life of the unit. So this pass takes no witness, and ``save`` writes
    # nothing without one. The savepoint is brought forward by the next read that
    # folds the prefix itself, which is a lag the module already allows for: resuming
    # from an older savepoint replays the tail and reaches the same value.
    reused_cached_state = False

    def held_still() -> bool:
        """Whether the file still matches everything this pass trusted about it.

        Three facts, and every one is read BEFORE the pass: the file's identity; on a
        read that resumed, the digest of the prefix the savepoint stood for; and, on a
        pass that will write a savepoint, the digest of the prefix it is about to
        consume. A pass is only trustworthy if none of them moved, so they are asked
        together here rather than at each return, where one would eventually be
        forgotten.

        The third is what lets the savepoint be written from a digest read before the
        pass instead of after it: proving that prefix held still is what makes the
        digest describe the bytes this fold actually consumed.
        """
        from kiro_crew.crew_log import checkpoint as savepoints

        if log_origin(handle) != origin:
            return False
        if prefix_seen is not None and not savepoints.prefix_unchanged(handle, prefix_seen):
            return False
        if resumed_from is None:
            return True
        return savepoints.resumed_prefix_still_verifies(handle, resumed_from)

    reusable = (
        since is not None
        and since.session_id == session_id
        # Same file: a recreated log gets a new inode, so a bundle folded from the
        # old one is refused even after the new file grows past its cached seq --
        # the case the seq guard below cannot catch on its own. ``origin is None``
        # (stat failed, or an old cached bundle predating this field) never
        # matches, so it falls back to the full rebuild.
        and origin is not None
        and since.origin == origin
        and since.last_seq <= last_seq
        and all(name in since.checkpoints for name in wanted)
    )
    if reusable and since is not None:
        base: dict[str, Checkpoint] = {name: since.checkpoints[name] for name in wanted}
        saved_seq = since.saved_seq
        reused_cached_state = True
        _note_session_fold_origin("warm")
    else:
        # No bundle in hand, so ask the disk before folding the file. A savepoint
        # covers the names it has and is silent about the rest, and each checkpoint
        # takes only the part of a chunk above its own seq, so a partial answer
        # costs the cold fold to the folds it did not cover rather than to all of
        # them.
        base = {name: initial(name) for name in wanted}
        saved_seq = 0
        # ``resume`` is false only on the retry a mid-pass identity change forces:
        # the savepoint on disk describes the file that just went away, so the
        # retry must not read it. It is private for that reason -- the one caller
        # that needs it is the retry, and a public switch would be a surface with
        # no production caller.
        resumed = _resume_from_disk(handle, wanted) if resume else None
        if resumed is not None:
            base.update(resumed.checkpoints)
            saved_seq = resumed.saved_seq
            resumed_from = resumed
            _note_session_fold_origin("savepoint")
        else:
            # Nothing warm and nothing on disk: this pass rebuilds from the first
            # record. The counter exists to make that share visible, because a
            # rising cold share is what a savepoint that is not being kept looks
            # like from the outside.
            _note_session_fold_origin("cold")
    from kiro_crew.crew_log import checkpoint as savepoints

    # The digest a savepoint is written with has to be read BEFORE the pass consumes
    # the file, and it is read here rather than at the write because a digest taken
    # afterwards can cover bytes the pass never saw. Only a fold that owes a write
    # pays for it, and only one that can vouch for the whole prefix may write at all --
    # see ``reused_cached_state``. Its boundary is the seq read before the pass, so a
    # pass that ends somewhere else -- the file grew under it, or the handle's own seq
    # was stale -- matches no fold in the bundle and writes nothing, which costs the
    # savepoint rather than the read: what this fold SERVES is unaffected either way.
    if not reused_cached_state and savepoints.write_is_earned(last_seq, saved_seq):
        prefix_seen = savepoints.prefix_witness(handle, last_seq)
    from_seq = min((cp.last_seq for cp in base.values()), default=0) + 1
    if from_seq > last_seq and (
        not reused_cached_state
        or (
            since is not None
            and since.size is not None
            and since.size == size
            and since.mtime_ns is not None
            and since.mtime_ns == mtime_ns
        )
    ):
        # No entries were read, but the bundle still describes the identity and the
        # prefix seen before this check. Recheck both so a recreation or interior
        # damage during the call retries cold instead of serving retired state.
        #
        # A pass standing on an in-memory cached bundle carries no prefix digest
        # (see ``reused_cached_state``), so identity alone is all ``held_still``
        # can recheck for it -- and identity survives an in-place rewrite that
        # regresses the tail seq back to the bundle's position. The segment-set
        # size and mtime fingerprint read beside the identity closes that gap:
        # they may skip the validating walk only while both still match what the
        # bundle recorded, and ``None`` (a bundle from before these fields) never
        # matches, which costs one validating walk before that bundle's next O(1)
        # poll. A pass that did NOT reuse a cached bundle is covered by the
        # digest machinery instead, so it keeps the fast return unconditionally.
        return (
            SessionProjections(
                session_id=session_id,
                last_seq=last_seq,
                checkpoints=base,
                origin=origin,
                saved_seq=saved_seq,
                size=size,
                mtime_ns=mtime_ns,
            ),
            held_still(),
            handle,
            prefix_seen,
        )
    grown = _drive_session(_session_registry(wanted), session_id, base, handle)
    reached = max((cp.last_seq for cp in grown.values()), default=last_seq)
    return (
        SessionProjections(
            session_id=session_id,
            last_seq=reached,
            checkpoints=grown,
            origin=origin,
            saved_seq=saved_seq,
            size=size,
            mtime_ns=mtime_ns,
        ),
        held_still(),
        handle,
        prefix_seen,
    )


# The savepoint module imports the fold surface it persists from the facade, which
# loads this module, so these two calls are function-local: a module-level import
# here would close that cycle.


def _resume_from_disk(handle: CrewLog, wanted: Sequence[str]) -> SessionProjections | None:
    """The savepoints for *wanted* beside *handle*'s log, or ``None``."""
    from kiro_crew.crew_log import checkpoint as savepoints

    return savepoints.load(handle, wanted)


def _persisted(
    bundle: SessionProjections, *, handle: CrewLog | None, prefix: PrefixWitness | None
) -> SessionProjections:
    """*bundle*, with its savepoint on disk brought forward if a write is owed.

    Whether a write is owed is the savepoint module's decision, not this one's: how
    far a fold must have moved to earn one is a property of the files, and stating
    it here as well would give two places an answer that has to agree. A session
    with no log has nothing to write beside.

    *prefix* is the digest the pass read before consuming the file and rechecked
    after it, and it is what the savepoint is written with -- that write must not read
    the file again, or it could certify bytes no fold saw.
    """
    if handle is None:
        return bundle
    from kiro_crew.crew_log import checkpoint as savepoints

    return savepoints.save(handle, bundle, prefix=prefix)


# --------------------------------------------------------------------------- #
# The folds as projection-kernel units
# --------------------------------------------------------------------------- #


class _SessionFold:
    """One crew-log fold as a :mod:`kiro_crew.projection` unit.

    The kernel drives a pure ``apply`` and decides "something changed" by comparing
    the returned object's IDENTITY with the one it handed over. These folds are
    written the other way round -- ``step`` edits the dict it is given and returns
    nothing -- so this wrapper supplies the difference rather than the folds being
    rewritten: an entry the fold cannot be moved by returns the state UNTOUCHED, and
    any other entry is stepped onto a copy.

    Keeping ``step`` as it is, byte for byte, is the point. The folds are where this
    module's meaning lives, and a rewrite of every mutation site into a functional
    form would put a hundred chances to change a number between the old behaviour and
    the new one, in the one place that must not move.
    """

    def __init__(self, fold: _Fold) -> None:
        self._fold = fold
        self.key = fold.name
        self.state_version = fold.state_version

    def init(self) -> dict[str, Any]:
        return self._fold.start()

    def apply(self, state: dict[str, Any], entry: Entry) -> dict[str, Any]:
        if not self._fold.touched_by(entry):
            return state
        grown = self._fold.copied(state)
        self._fold.step(grown, entry)
        return grown

    def view(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._fold.render(state)


class _HeldCheckpoints:
    """The checkpoints a read already holds, offered as the kernel's savepoint source.

    ``prime_checkpointed`` installs each unit's state at its watermark and then folds
    only the tail past the lowest of them, which is exactly what a read resuming from
    a cached bundle or from disk wants -- so both arrive through this one seam rather
    than through a second seeding path the tail fold would have to agree with.

    The identity is not checked here and no equality is compared, because the caller
    has already decided these checkpoints describe the log it is about to fold: a
    cached bundle passes the reuse test in :func:`_fold_attempt`, and a savepoint read
    from disk passed every admission condition in
    :mod:`kiro_crew.crew_log.checkpoint`. Re-deciding it on weaker evidence is how the
    two answers would get the chance to disagree.

    A name this holds nothing for answers ``None``, which drops the kernel's floor to
    the start and refolds the whole log -- the cold fold, which reaches the same value.
    """

    def __init__(self, held: Mapping[str, Checkpoint]) -> None:
        self._held = held

    def load(
        self,
        store: str,
        key: str,
        *,
        state_version: int,
        identity: Mapping[str, Any],
        admit: Any = None,
    ) -> Savepoint | None:
        checkpoint = self._held.get(key)
        if checkpoint is None:
            return None
        return Savepoint(
            key=key,
            state_version=state_version,
            watermark=checkpoint.last_seq,
            state=checkpoint.state,
            identity=identity,
        )

    def save(self, store: str, savepoint: Savepoint) -> bool:
        """Never written to. Persisting is :mod:`kiro_crew.crew_log.checkpoint`'s."""
        return False

    def discard(self, store: str, key: str) -> None:
        return None


def _session_registry(names: Sequence[str]) -> ProjectionRegistry:
    """A registry holding exactly *names*, for one read.

    Per READ, not one shared instance, and that is a behaviour requirement rather
    than a preference. A registry folds every unit registered in it, so a shared one
    would make ``fold_session(("status",))`` fold all six session folds and write all
    six savepoints -- and it would compute the persisted floor across folds the caller
    never asked for. What this module caches between reads is the bundle a caller
    hands back as ``since=``, so the registry's own cells have nothing to carry.
    """
    registry = ProjectionRegistry(seq_of=attribute_seq)
    for name in names:
        registry.register(_SessionFold(_FOLDS[name]))
    return registry


def _drive_session(
    registry: ProjectionRegistry,
    session_id: str,
    base: Mapping[str, Checkpoint],
    handle: CrewLog,
) -> dict[str, Checkpoint]:
    """*base* carried forward over every entry of *handle* above it.

    One pass over the file for every fold, and NOTHING is materialized: the kernel
    takes the tail as a stream and folds each entry through every unit as it arrives,
    so a cold fold of a long log holds one entry at a time. Each unit drops an entry
    at or below its own watermark inside the kernel's own fold step -- the same step a
    live event takes -- which is what lets one pass serve folds sitting at different
    seqs: a resumed bundle can hold ``status`` further along than ``tools``.

    A fold that RAISES over resumed state retires the state instead of the read. The
    shape checks a savepoint passes read a state's top level, so a value malformed
    below it -- a tool row holding a number where a list belongs -- is admitted and
    fails in the fold, and nothing above catches that: the routes answer only to
    ``CrewLogError``, so the read 500s and does so on every later read, because the
    file that caused it is still there. Any exception therefore discards the savepoints
    and folds the whole log from empty, which is the module's standing answer to doubt.
    A cold fold that raises is NOT swallowed -- that is a fold defect on real entries,
    and serving a value past it would hide it.
    """
    try:
        registry.prime_checkpointed(session_id, _HeldCheckpoints(base), {}, _tail_reader(handle))
        return _cells_as_checkpoints(registry, session_id)
    except Exception:
        log_exception_text(
            logger,
            logging.DEBUG,
            "crew log %s could not fold its resumed state; discarding and folding cold",
            session_id,
        )

    from kiro_crew.crew_log import checkpoint as savepoints

    wanted = tuple(base)
    savepoints.discard(handle, wanted)
    # A FRESH registry: the one above holds cells the failed pass half-folded, and
    # priming over them would carry that half into the cold answer. An empty source
    # drops the kernel's floor to the start, which is the cold fold.
    cold = _session_registry(wanted)
    cold.prime_checkpointed(session_id, _HeldCheckpoints({}), {}, _tail_reader(handle))
    return _cells_as_checkpoints(cold, session_id)


def _tail_reader(handle: CrewLog) -> Callable[[int], Iterable[Entry]]:
    """The kernel's tail source for *handle*: the entries strictly above a watermark."""

    def tail_from(watermark: int) -> Iterable[Entry]:
        # The kernel's empty watermark is -1 and a checkpoint's is 0; both mean
        # nothing consumed, and the log's own first seq is 1.
        return handle.iter_from(max(watermark, 0) + 1, known=KNOWN_TYPES)

    return tail_from


def _cells_as_checkpoints(registry: ProjectionRegistry, session_id: str) -> dict[str, Checkpoint]:
    """Every registered fold's cell as the checkpoint this module's callers carry."""
    return {
        name: Checkpoint(name=name, last_seq=max(watermark, 0), state=state)
        for name, (state, watermark) in registry.cells(session_id).items()
    }
