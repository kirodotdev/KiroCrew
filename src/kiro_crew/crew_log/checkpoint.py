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
each is checked before a file is used -- the three below, plus the one that makes
"cannot change" checkable rather than assumed: ``prefix_sha`` is a digest of the
consumed prefix's raw record bytes, recomputed on resume, because a line damaged
AFTER it was folded is skipped by a cold fold while a savepoint keeps the value it
contributed, and none of the three below reads the prefix at all.

* the file describes a DIFFERENT log -- a unit removed and recreated under the
  same id restarts its seqs, and once the new file has grown past the stored seq a
  seq check alone would pass. ``origin`` is the log's creation identity and is
  compared first.
* the log LOST its front -- retention deletes whole segments off the oldest end,
  so a cold fold folds a window while the savepoint still counts entries the file
  does not hold. The two answers differ, and the savepoint's is the one no
  reader can reproduce. ``first_seq`` is the oldest surviving segment's first seq,
  and a change to it retires the file.
* the log is SHORTER than the savepoint -- most of its causes are caught above,
  but it is checked on its own because a fold resumed past the end of a file is
  the one state no later read recovers from.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final, NamedTuple

from kiro_crew.atomic_write import atomic_write
from kiro_crew.crew_log.errors import CrewLogError
from kiro_crew.crew_log.lease import LEASE_FILE
from kiro_crew.crew_log.lease import acquire as acquire_lease
from kiro_crew.crew_log.lease import release as release_lease
from kiro_crew.crew_log.projection import (
    Checkpoint,
    SessionProjections,
    log_origin,
    require_name,
)
from kiro_crew.crew_log.store import (
    CrewLog,
    crew_log_dir,
    log_exception_text,
    segment_first_seqs,
    segment_paths,
)
from kiro_crew.platform_compat import restrict_dir_to_owner

logger = logging.getLogger(__name__)

#: Directory inside a unit's store directory that holds its fold savepoints.
CHECKPOINT_DIR: Final[str] = "projections"

#: The payload shape this build writes and is willing to read. A file carrying
#: anything else was written by a build this one does not understand, and the
#: answer is the cold fold rather than a guess at which fields still mean what
#: they did.
#:
#: THE RULE: any change to what a fold's ``start`` or ``step`` STORES bumps this,
#: including one that keeps the same keys. Shape is all this number and
#: ``_state_matches_fold`` can check, so a counting fix that leaves the keys alone
#: resumes the old build's state onto the new logic -- and the long sessions this
#: module speeds up are the ones that then serve pre-fix numbers for the life of
#: the unit. Bumping retires every savepoint to a cold fold, which costs one
#: refold each and is the only in-product way to retire them, since the tree is
#: fenced from the agent. ``test_changing_what_a_fold_stores_forces_the_savepoint_
#: version_to_move`` pins each fold's stored state, so forgetting the bump fails
#: CI rather than shipping.
CHECKPOINT_VERSION: Final[int] = 3

#: Largest savepoint file this module reads or writes. Every fold's state is
#: already bounded by construction (``crew-log-projection.md`` section 2), so this
#: is a BACKSTOP on those bounds rather than the bound itself: a fold that grew
#: unbounded state loses its savepoint here instead of writing an unbounded file
#: on every read. Exceeding it costs performance and nothing else.
MAX_CHECKPOINT_BYTES: Final[int] = 2 * 1024 * 1024

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
    wanted = tuple(require_name(name) for name in names)
    if not wanted:
        return None
    identity = _identity(handle)
    if identity is None:
        return None
    origin, first_seq = identity
    resumed: dict[str, Checkpoint] = {}
    prefix_digests: dict[int, tuple[str, int]] = {}
    for name in wanted:
        loaded = _load_one(
            handle,
            name,
            origin=origin,
            first_seq=first_seq,
            prefix_digests=prefix_digests,
        )
        if loaded is not None:
            resumed[name] = loaded
    if not resumed:
        return None
    reached = max(cp.last_seq for cp in resumed.values())
    # The floor, not the ceiling: a name with no file is persisted through nothing,
    # so the bundle is only as saved as its least-saved fold and the next write is
    # owed for the whole set.
    saved = min(cp.last_seq for cp in resumed.values()) if len(resumed) == len(wanted) else 0
    return SessionProjections(
        session_id=handle.id,
        last_seq=reached,
        checkpoints=resumed,
        origin=origin,
        saved_seq=saved,
    )


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


def write_is_earned(last_seq: int, saved_seq: int) -> bool:
    """Whether a fold reaching *last_seq* owes a write against *saved_seq*.

    :func:`save` asks this itself and stays the authority for it. It is exposed
    because the witness that call needs has to be read BEFORE the fold's pass, and a
    pass that will not write a savepoint should not pay for one. Asking here is what
    avoids a second copy of the threshold.
    """
    return last_seq - saved_seq >= MIN_ADVANCE_ENTRIES


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
    return PrefixWitness(seq=seq, records=records, sha=sha)


def prefix_unchanged(handle: CrewLog, witness: PrefixWitness) -> bool:
    """Whether *handle*'s first ``witness.records`` records still hash to *witness*.

    Growth above them is not a change: the walk stops at the count the witness
    names, which is the same reason a savepoint's own prefix ends at its seq.
    """
    sha, hashed = handle.raw_prefix_digest(witness.records)
    return hashed == witness.records and sha == witness.sha


def save(
    handle: CrewLog, bundle: SessionProjections, *, prefix: PrefixWitness | None
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
    if bundle.last_seq - bundle.saved_seq < MIN_ADVANCE_ENTRIES:
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
    try:
        directory = _ensure_dir(handle)
        if directory is None:
            return bundle
        written = 0
        for checkpoint in bundle.checkpoints.values():
            if checkpoint.last_seq != prefix.seq:
                # The witness certifies ONE boundary. A fold sitting at another has
                # no evidence here, and re-reading the file for it is exactly what
                # this call must not do, so its write waits for a pass whose witness
                # covers it. The partial-write check below then leaves the bundle
                # claiming nothing, which costs a cold fold rather than a digest that
                # may describe bytes nothing folded.
                continue
            if _save_one(
                directory,
                checkpoint,
                unit=handle.id,
                origin=origin,
                first_seq=first_seq,
                prefix_sha=prefix.sha,
                prefix_records=prefix.records,
            ):
                written += 1
        if _discard_if_unit_gone(handle, directory):
            return bundle
    finally:
        release_lease(lease)
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
# One file
# --------------------------------------------------------------------------- #


def _load_one(
    handle: CrewLog,
    name: str,
    *,
    origin: str,
    first_seq: int,
    prefix_digests: dict[int, tuple[str, int]],
) -> Checkpoint | None:
    """One fold's savepoint, or ``None`` for every reason not to resume from it."""
    try:
        path = checkpoint_path(handle.kind, handle.id, name)
        size = path.stat().st_size
    except OSError:
        # Absent is the ordinary case: no session has a savepoint until one is
        # written, and a cold fold is the answer.
        return None
    except Exception:  # pragma: no cover - a path refusal from the store's checks
        log_exception_text(logger, logging.DEBUG, "crew log savepoint path refused for %s", name)
        return None
    if size > MAX_CHECKPOINT_BYTES:
        logger.debug("crew log savepoint %s is over the size cap; folding cold", path)
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # A file that cannot be read or parsed is not a savepoint. Each of these is
        # the cold fold, which is why none is reported as an error.
        #
        # ``RecursionError`` has to be named because it is a ``RuntimeError`` rather
        # than a ``ValueError``: a payload nested past the interpreter's stack limit
        # raises it out of ``json.loads`` at a few tens of KB, far under the size cap
        # checked above, so the cap does not stand in for this guard. It is also the
        # only unusable payload that survives being read -- the file stays on disk --
        # so an escape costs the session every later fold rather than one cold fold.
        log_exception_text(
            logger, logging.DEBUG, "crew log savepoint %s unusable; folding cold", path
        )
        return None
    return _checkpoint_from(
        raw,
        name=name,
        origin=origin,
        first_seq=first_seq,
        handle=handle,
        prefix_digests=prefix_digests,
    )


def _checkpoint_from(
    raw: Any,
    *,
    name: str,
    origin: str,
    first_seq: int,
    handle: CrewLog,
    prefix_digests: dict[int, tuple[str, int]],
) -> Checkpoint | None:
    """*raw* as a checkpoint for *name*, or ``None`` when it does not describe this log."""
    if not isinstance(raw, Mapping):
        return None
    if raw.get("v") != CHECKPOINT_VERSION:
        return None
    if raw.get("fold") != name or raw.get("unit") != handle.id:
        # The file's own statement of what it is disagrees with where it was found:
        # a directory copied from another unit, or a file renamed. Neither is a
        # savepoint of this fold of this log.
        return None
    if raw.get("origin") != origin or raw.get("first_seq") != first_seq:
        return None
    seq = raw.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        return None
    if seq > handle.last_seq:
        # The short-store fallback: the log does not reach the savepoint, so
        # resuming would fold nothing and serve state for entries the file no
        # longer has.
        logger.debug(
            "crew log savepoint for %s is at seq %d past the log's %d; folding cold",
            name,
            seq,
            handle.last_seq,
        )
        return None
    prefix_sha = raw.get("prefix_sha")
    prefix_records = raw.get("prefix_records")
    if (
        not isinstance(prefix_sha, str)
        or len(prefix_sha) != 64
        or any(char not in "0123456789abcdef" for char in prefix_sha)
        or not isinstance(prefix_records, int)
        or isinstance(prefix_records, bool)
        # A LOWER bound, not equality: the count is raw records, and a blank or
        # unparseable interior line is a record the fold did not count as an entry,
        # so the two can legitimately differ upward. Equality would force the entry
        # span onto the digest and leave the trailing consumed records uncovered. A
        # count too LARGE cannot pass either -- the walk then hashes fewer records
        # than claimed and the comparison below refuses it.
        or prefix_records < max(0, seq - first_seq + 1)
    ):
        return None
    prefix = prefix_digests.get(prefix_records)
    if prefix is None:
        prefix = handle.raw_prefix_digest(prefix_records)
        prefix_digests[prefix_records] = prefix
    digest, records_hashed = prefix
    if digest != prefix_sha or records_hashed != prefix_records:
        logger.debug("crew log savepoint for %s has a changed prefix; folding cold", name)
        return None
    try:
        return Checkpoint.from_dict({"name": name, "last_seq": seq, "state": raw.get("state")})
    except Exception:
        # The fold surface's own validation refused the payload. Same answer as
        # every other unusable file.
        log_exception_text(
            logger, logging.DEBUG, "crew log savepoint for %s refused by the fold surface", name
        )
        return None


def _save_one(
    directory: Path,
    checkpoint: Checkpoint,
    *,
    unit: str,
    origin: str,
    first_seq: int,
    prefix_sha: str,
    prefix_records: int,
) -> bool:
    """Write one fold's savepoint. ``True`` when it reached the file."""
    payload = {
        "v": CHECKPOINT_VERSION,
        "unit": unit,
        "origin": origin,
        "first_seq": first_seq,
        "fold": checkpoint.name,
        "seq": checkpoint.last_seq,
        "prefix_sha": prefix_sha,
        "prefix_records": prefix_records,
        "state": checkpoint.state,
    }
    try:
        # ASCII-ONLY, and the encode is inside this guard. A crew log's own JSON
        # admits a lone surrogate, so a fold can retain one in a label -- and with
        # ``ensure_ascii=False`` that character survives into the payload and makes
        # the UTF-8 encode raise ``UnicodeEncodeError``, out of a function that
        # promises never to raise and into a read the caller asked to serve.
        # Escaping every non-ASCII character makes the bytes unrepresentable-proof
        # and round-trips the surrogate back through ``json.loads``; it is also what
        # the store's own serializer does. The encode stays inside the guard anyway,
        # because ``UnicodeEncodeError`` IS a ``ValueError`` and the contract should
        # not depend on one argument staying as it is.
        blob = json.dumps(payload, separators=(",", ":"))
        encoded = blob.encode("utf-8")
    except (TypeError, ValueError):
        # A fold whose state cannot be serialized is a defect in that fold, and not
        # a reason to fail the read it was folded for.
        logger.debug("crew log fold %s has unwritable state", checkpoint.name, exc_info=True)
        return False
    if len(encoded) > MAX_CHECKPOINT_BYTES:
        logger.debug(
            "crew log fold %s state is %d bytes, over the savepoint cap; not written",
            checkpoint.name,
            len(encoded),
        )
        return False
    try:
        # No fsync. A savepoint a crash leaves unpersisted is an older savepoint or
        # no savepoint, and both are answered by folding further -- so a flush per
        # write would buy nothing the cold fold does not give for free. The rename
        # is still atomic, which is what keeps a reader from seeing half a payload.
        atomic_write(directory / f"{checkpoint.name}.json", blob, fsync=False, newline="")
    except OSError:
        logger.debug("crew log savepoint for %s not written", checkpoint.name, exc_info=True)
        return False
    return True


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


def _ensure_dir(handle: CrewLog) -> Path | None:
    """*handle*'s savepoint directory, created owner-only, or ``None``.

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
        directory = checkpoint_dir(handle.kind, handle.id)
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
