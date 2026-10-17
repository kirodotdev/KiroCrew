"""One backup cycle: what must be durable, and how each object is copied.

## What is in the set

Three kinds, and the set is a definition rather than a filter, so "was this file
backed up" has an answer that does not depend on what the directory happened to
hold:

1. **The two authority files**, ``session_map.json`` and ``open_slots.json``. They
   turn a slot id back into a conversation, so without them the transcripts are on
   disk and the conversation list is empty.
2. **Every live transcript**, the ``.jsonl`` files directly under the sessions
   directory. One per conversation this task has served.
3. **Every archived segment** under ``sessions/archive/``. Rotation moves the older
   part of a long conversation there and the container never reads it back -- the
   front's fetch may not list, and finding a segment requires listing -- so these are
   uploaded for the owner's control plane, which has credentials of its own. Leaving
   them behind would be silent loss of the older half of every long conversation.

Anything else under the sessions directory is not in the set. The front writes a
temporary file there while fetching and unlinks it itself, and a name that is
neither that nor a transcript is not a conversation.

## What order they go in, and why the order is a correctness rule

The transcripts go first and the authority files last, in their own phase. The
authority files are the INDEX: a replacement reads them to decide which conversations
exist, and the front then fetches each named transcript lazily. So an authority table
newer than the transcripts it names points at objects that are not in the bucket, and
the front reads an absent transcript as a conversation with no history -- a live
conversation served empty, with nothing raised anywhere. The opposite skew is
harmless: an authority table older than the transcripts names only slots whose bytes
are already there, and a transcript it does not name yet is unreferenced rather than
misread.

Which is why the authority files are OPENED first, before a single transcript is
listed, and sent from those descriptors at the end. Opening fixes the instant a file
describes, so the pair is one coherent snapshot of the index taken before the
enumeration it indexes. Reading them at send time instead let a slot table flushed
during the cycle name a transcript that cycle never listed.

That rests on the backend publishing both files the way it publishes a transcript, a
temporary file and a rename, which leaves an open descriptor addressing the whole
previous version. It does: ``session_map.json`` and ``open_slots.json`` are both written
through an atomic replace. A writer that truncated one in place instead would take the
snapshot property away without changing anything here, so it is pinned by a test rather
than left as an assumption.

The authority phase is SKIPPED when the transcript phase suffered a refusal a LATER
CYCLE COULD GET PAST -- a failed upload, an object the drain window could not fit, a
directory missing right now. Publishing it then would advance the index past bytes this
cycle failed to write; withholding it leaves the pair at the last cycle that completed,
which is older and coherent, and the next cycle publishes a pair the bucket supports.

It is NOT skipped for a refusal decided by an entry's SHAPE -- a symlink or a FIFO where
a transcript belongs, a linked archive root. Withholding is a WAIT, and every later cycle
meets that entry too, so the wait never ends: the index would freeze at the moment the
entry appeared while transcripts kept uploading past it, and the next replacement would
restore a conversation list predating every conversation served since. The cycle is still
incomplete and the entry is still named; see :class:`RefusedEntry` for the split and for
the bounded residue it accepts in exchange.

One residual remains in the pair itself. The two files are two PUTs into this cycle's
WRITER-UNIQUE ``gen/<id>/`` generation, and only the pointer's compare-and-swap -- written
after both land -- makes that generation live. So a failure BETWEEN the two PUTs leaves an
unpublished partial generation under ``gen/<id>/`` while the committed pointer is UNCHANGED,
still naming the last generation this writer fully committed. Nothing a restore reads has
moved: the half-written generation is never pointed at, and the failure raises rather than
passing quietly. The cost is one interval in which this cycle published nothing, which the
next cycle resolves by writing a fresh whole generation and flipping the pointer to it.

## How one object is copied

``open_snapshot`` opens the file ONCE and records the length that descriptor's file
had at that moment. The upload then sends exactly that many bytes from that
descriptor. Three properties follow, and they are the three constraints this design
has to hold at the same time:

* **Consistent** without a lock. The backend publishes a transcript with a temporary
  file and a rename, so it never writes into the bytes behind an open descriptor --
  it swaps the directory entry to a different inode. A descriptor opened before the
  swap keeps addressing a whole, finished version, and a file that is appended to
  instead is uploaded as the prefix that existed at open time, which is also a
  version that was really on disk.
* **Bounded** on disk. Nothing is copied first. A cycle spends one descriptor and one
  fixed transport buffer per object, so an oversized artifact cannot fill the
  filesystem the app is writing to.
* **Nothing dropped.** There is no size at which an object is skipped. An entry that
  genuinely cannot be uploaded is recorded and the cycle ends by RAISING
  :class:`BackupIncomplete` -- after uploading everything it could, so one bad entry
  does not cost every other conversation its backup.

## Every shape an entry can have, and what happens to it

| entry                                      | verdict                                |
| ------------------------------------------ | -------------------------------------- |
| regular file, one link                     | uploaded                               |
| regular file, several links                | uploaded: the descriptor still         |
|                                            | addresses real bytes, and this side     |
|                                            | only reads them                        |
| regular file that grew since it was opened  | uploaded to its length at open         |
| regular file that shrank since it was opened| uploaded short, and the declared length |
|                                            | makes the transport fail rather than    |
|                                            | pad; recorded, so the cycle raises      |
| zero bytes                                 | uploaded: an empty conversation is a    |
|                                            | conversation                            |
| above the reader's ceiling                 | uploaded, with a warning naming it:     |
|                                            | backed up, and the front will refuse to |
|                                            | restore it, so an operator hears it     |
|                                            | before a customer does                  |
| symlink                                    | recorded; the cycle raises              |
| reached through a symlinked directory      | recorded; the cycle raises, and a       |
|                                            | linked archive root is refused before    |
|                                            | anything under it is listed at all       |
| directory, FIFO or socket                  | recorded; the cycle raises              |
| gone between listing and opening           | counted as gone; the cycle continues,   |
|                                            | because a deleted conversation is not   |
|                                            | a backup failure                        |
| unchanged since its last upload            | not re-uploaded                         |

## What the fingerprint is for, and what it is not

Change detection is a COST decision, not a correctness one. The fingerprint is the
inode, the length and the modification time as they were at open, and an object is
re-uploaded whenever it differs from the one last uploaded successfully. It lives in
memory, so a restarted sidecar re-uploads everything once: paying for a full cycle is
the right way to be wrong here, and persisting the state would put a second authority
on disk to keep in agreement with the bucket.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import logging
import os
import stat
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Iterator

from ..common import Settings, keys
from ..common.config import (
    BACKUP_ATTEMPT_COST_SECS,
    BACKUP_PER_OBJECT_BUDGET_SECS,
    MAX_OBJECT_BYTES,
)

# One spelling of the transcript filename, rather than a second copy of the prefix rule
# here: the index names a conversation by its slot key and the file carries a
# ``dashboard_`` prefix plus a character substitution, so a local re-derivation would be a
# second definition of the same mapping and would drift from the one that names the file.
from ..front.transcript import transcript_stem
from . import generation
from .store import (
    ObjectStore,
    PreconditionFailed,
    StoreUnusable,
    UploadCancelled,
    UploadDeadlineExceeded,
)

log = logging.getLogger("smc.sidecar.backup")

__all__ = [
    "Fingerprint",
    "Snapshot",
    "BackupSet",
    "CycleResult",
    "BackupIncomplete",
    "SidecarSuperseded",
    "open_snapshot",
    "objects_to_back_up",
    "run_cycle",
    "new_incarnation",
    "startup_incarnation",
]

#: The task-incarnation minter, re-exported so the sidecar entrypoint reaches it through the
#: one module it already drives (``backup_mod``). It lives in ``keys`` with the other
#: id minting; the fence that consumes it lives here in :func:`_commit_generation`.
new_incarnation = keys.new_incarnation


def startup_incarnation(settings: Settings, store: ObjectStore) -> str:
    """This task's incarnation, derived from the committed pointer at startup.

    The incarnation orders tasks for the commit fence, and that ordering must NOT come from a
    host wall clock -- cross-host skew would let a replacement on a slower clock mint an older
    token and be refused as superseded forever, and one future-dated commit would poison the
    pointer for every later task. So the ordering is derived from SHARED STORAGE: read the
    committed pointer once at startup and mint the NEXT incarnation after the one it carries
    (:func:`keys.next_incarnation`), so this task's commits sort strictly after the committed
    generation's, independent of any clock. A missing or legacy pointer (``None``) carries no
    incarnation to follow, so this task starts at the base counter -- the same tolerance the
    restore applies, and the pointer's own compare-and-swap still settles a genuine race.

    An UNUSABLE pointer is NOT treated that way, and is NOT swallowed into a base token. A
    base token minted from an unreadable pointer is strictly LESS than whatever a predecessor
    actually committed, so :func:`_commit_generation`'s fence would then refuse every commit
    this task ever attempts -- for the task's whole life, with no in-process recovery -- while
    its blobs upload unreferenced and the replacement boots the predecessor's generation
    holding none of the turns this task served. A single transient GET (a throttle, a 503, a
    reset) is enough to reach here, because the sidecar's own read is ``BACKUP_MAX_ATTEMPTS``
    = 1 and a throttle is not a permanent code. So :class:`generation.PointerUnusable`
    propagates: the writer refuses to START on it -- the same posture ``restore_authority``
    already takes and the same posture this process takes for :class:`StoreUnusable` -- so the
    fault is reported loudly at boot rather than minting a token that silently fences the task
    forever. The read is retried once at the base attempt count; a transient answer that
    clears on the next task boot is exactly what the restart recovers.

    When the supervisor already CLAIMED ownership before restoration (:func:`generation.
    claim_ownership`), it exports the claimed token in :data:`generation.ENV_CLAIMED_INCARNATION`
    and this ADOPTS it rather than reading the pointer and bumping again. Re-deriving here would
    bump a SECOND time, leaving the committed pointer (which the supervisor set to the claimed
    token) naming a token no live task holds, so the writer's own first commit would be fenced
    against its own claim. Adopting the exported token is what makes the pre-restore claim and
    the writer one ownership rather than two.
    """
    claimed = os.environ.get(generation.ENV_CLAIMED_INCARNATION) or ""
    if claimed:
        return claimed
    pointer = generation.read_pointer(settings, store)
    previous = "" if pointer is None else pointer.incarnation
    return keys.next_incarnation(previous)


#: Flags for opening a file to be uploaded.
#:
#: ``O_NOFOLLOW`` refuses a symlink at the final component, so a link planted where a
#: transcript belongs is reported instead of followed to whatever it points at.
#: ``O_NONBLOCK`` is what keeps the open from hanging: opening a FIFO for reading blocks
#: until a writer arrives, and an entry planted as a FIFO would otherwise stall the cycle
#: indefinitely rather than be refused.
_OPEN_FLAGS: int = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)

#: Flags for opening one DIRECTORY component on the way down to a file.
#:
#: ``O_NOFOLLOW`` is what makes the descent safe. ``O_NOFOLLOW`` on the file alone
#: guards only the last name, so a link planted at ``sessions/archive`` -- a directory
#: the agent writes in -- is descended normally and every regular file behind it opens
#: and uploads. Walking down with this flag at each step means a link ANYWHERE in the
#: chain is refused instead, so the only files that reach the bucket are files reached
#: through real directories inside the data home.
_DIR_FLAGS: int = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)

#: How many unreached objects a stop or deadline gate names BY NAME before it stops looking
#: and records one count-free remainder marker instead. The gate is reached mid-stream over a
#: LAZY iterator whose tail is the archive tree, and retention-off lets that tree grow without
#: bound -- so draining the iterator to name every unreached object rebuilds the whole
#: inventory in a list at exactly the moment the cycle is trying to stop, the same
#: unbounded-retention hazard the streamed enumeration removes from the walk. The cycle's
#: completeness reads only whether ANYTHING was refused (see :attr:`CycleResult.complete`), so
#: the current item alone already makes it incomplete; the bounded sample gives an operator
#: names to act on without walking the tail. Sized to match the archive sample cap so a small
#: remainder is named whole and the tests that assert exact skipped-name sets are unaffected.
_UNREACHED_SAMPLE_CAP = 64

#: The count-free marker recorded once when the unreached remainder runs past the sample cap.
#: It is a refusal like any other -- it withholds the pair and makes the cycle incomplete --
#: but names no object, because naming the rest would mean draining the tail this marker
#: exists to avoid.
_UNREACHED_REMAINDER_NAME = "<remainder>"

#: How many archive-object fingerprints the lifetime *state* map keeps before the OLDEST are
#: evicted. The map exists so an unchanged object is not re-uploaded, and for a live transcript
#: (a bounded, flat set) it stays small. The ARCHIVE is different: rotation nests it and
#: retention-off never prunes it, so one entry per archived segment would grow the map without
#: bound for the process's whole life -- state tracking the very thing the streamed enumeration
#: was made to stop holding. Archive segments are IMMUTABLE, so the only cost of forgetting one
#: is re-uploading identical bytes once; that makes a bounded, evict-oldest cap safe where it
#: would not be for a mutable object. Sized well above any real cycle's live+recent-archive
#: working set, so an ordinary run never evicts and only a pathologically large archive does.
_ARCHIVE_STATE_CAP = 4096

#: How many bytes :func:`_digest_of` reads per ``pread`` while hashing a data object. The
#: digest needs every byte but not all at once, so the content is streamed through the hash
#: in chunks of this size rather than read whole into memory beside the copy the transport
#: already holds. 1 MiB is large enough that even a 64 MiB object is a handful of reads and
#: small enough that the transient buffer is negligible against the task's memory.
_DIGEST_CHUNK_BYTES = 1024 * 1024

#: How many entries the bounded per-object records keep BY NAME before keeping only the
#: running count. The ONE cap for every structure that samples the same object population:
#: :class:`CycleResult`'s uploaded/unchanged/gone/refused/unreachable records AND
#: :class:`_ArchiveWalkSink`'s archive-walk refusals, which fold into ``CycleResult.refused``.
#: Both are capped here so one refusal population is not truncated by two different numbers.
#: The archive puts one entry per segment through these every cycle, so an unbounded list
#: would hold the whole inventory for the log's sake alone. Sized well above any real cycle's
#: working set, so an ordinary run's lists are complete and the tests that assert exact
#: contents are unaffected.
_RESULT_SAMPLE_CAP = 4096

#: How many characters of a refusal's REASON are kept in the sample. A refusal carries an
#: exception string, and a store or transport error can be arbitrarily long (a stack-shaped
#: message, an echoed payload), so a capped COUNT of entries is not enough on its own -- one
#: entry can still be huge. The reason is truncated to this, with a marker, so a sampled
#: refusal costs a bounded number of bytes.
_REFUSAL_REASON_MAX_CHARS = 200

#: The most ``stem -> digest`` entries the committed transcript index may carry. The live
#: population SHOULD be the live-session count, but a slot that is repeatedly closed and
#: reopened leaves transcripts behind, so without a bound the index -- and the live set the
#: cycle retains to build it -- grows with the whole history of slots, not the live ones. A
#: cap turns that unbounded growth into a loud, counted refusal.
_TRANSCRIPT_INDEX_MAX_ENTRIES = 50_000

#: The most bytes the committed transcript index may serialize to. The restore reads the
#: index with ``limit=MAX_OBJECT_BYTES`` and an index past that is ``ObjectTooLarge`` -- a
#: bucket no boot can restore. So publication is refused well under that ceiling, and the
#: previous (readable) index stays committed, rather than publishing one no boot can read.
#: Half of ``MAX_OBJECT_BYTES`` leaves ample headroom for the reader's own framing.
_TRANSCRIPT_INDEX_MAX_BYTES = MAX_OBJECT_BYTES // 2

#: How many characters of a sampled IDENTIFIER -- an object key or a file name -- are kept.
#: The identifiers these samples retain are walk-built archive paths, and an agent is free to
#: write a transcript under an arbitrarily deep, arbitrarily long archive path; a cap on the
#: COUNT of sampled entries bounds how MANY are kept but not the length of each, so a
#: pathological tree could still exhaust the sidecar's memory through one deeply-nested path
#: string per entry. Every retention point that keeps a name or key truncates it to this with
#: a marker, the same way :func:`_bounded_identifier` and the reason cap bound the other
#: field -- so a sampled entry costs a bounded number of bytes in BOTH its fields, and
#: retained identifier length cannot grow the sidecar's memory without bound. The counts the
#: completeness and withhold decisions read are untouched; only the sampled strings are cut.
_IDENTIFIER_MAX_CHARS = 256


def _bounded_identifier(identifier: str) -> str:
    """Truncate a sampled object key or file name to :data:`_IDENTIFIER_MAX_CHARS`.

    The sample caps limit how many identifiers are retained; this limits how long each one
    is, because a walk-built archive path has no bound of its own. A marker makes a cut one
    visible in the log. Applied at every retention point that appends a name or key to a
    bounded sample, so no sampled field is left unbounded.
    """
    if len(identifier) <= _IDENTIFIER_MAX_CHARS:
        return identifier
    return identifier[:_IDENTIFIER_MAX_CHARS] + "… (truncated)"


def _bounded_reason(reason: str) -> str:
    """Truncate a sampled refusal/unreachable reason to :data:`_REFUSAL_REASON_MAX_CHARS`.

    A reason is an interpolated ``OSError`` string that carries a near-PATH_MAX filename, so
    like the identifier it has no bound of its own. The sample caps limit how many reasons
    are retained; this limits how long each one is. Applied at every retention point that
    appends a reason to a bounded sample -- the sink below as well as :class:`CycleResult` --
    so a reason is never the one field a bounded sample keeps whole.
    """
    if len(reason) <= _REFUSAL_REASON_MAX_CHARS:
        return reason
    return reason[:_REFUSAL_REASON_MAX_CHARS] + "… (truncated)"


class _ArchiveWalkSink:
    """The bounded record of what the archive walk could not enumerate.

    :func:`_archived_segments` streams file paths so the whole inventory is never held in a
    list at any level. Its REFUSALS -- a directory the walk met an error on, a linked
    subdirectory it dropped -- are the other per-archive collection, and are bounded here to
    a capped sample plus a true count rather than an unbounded list. ``refused`` withholds
    the authority pair; ``unreachable`` does not, split on the same permanence rule
    :class:`RefusedEntry` states.

    The counts are what the cycle's completeness and withhold decisions read (a refusal is
    present or it is not); the sampled names are for the log and the incompleteness report.
    """

    __slots__ = ("refused", "unreachable", "refused_count", "unreachable_count")

    def __init__(self) -> None:
        self.refused: list[tuple[str, str]] = []
        self.unreachable: list[tuple[str, str]] = []
        self.refused_count = 0
        self.unreachable_count = 0

    def reset(self) -> None:
        """Empty the record for a fresh walk.

        ``BackupSet.data`` is re-iterable and each iteration re-walks the archive, so the
        sink is reset at the start of each walk -- it then reflects the most recent walk,
        which in the one place that reads it (:func:`run_cycle`, after its single upload-phase
        consumption) is the only walk.
        """
        self.refused = []
        self.unreachable = []
        self.refused_count = 0
        self.unreachable_count = 0

    def refuse(self, entry: tuple[str, str]) -> None:
        self.refused_count += 1
        if len(self.refused) < _RESULT_SAMPLE_CAP:
            name, reason = entry
            self.refused.append((_bounded_identifier(name), _bounded_reason(reason)))

    def cannot_reach(self, entry: tuple[str, str]) -> None:
        self.unreachable_count += 1
        if len(self.unreachable) < _RESULT_SAMPLE_CAP:
            name, reason = entry
            self.unreachable.append((_bounded_identifier(name), _bounded_reason(reason)))


@dataclass(frozen=True)
class Fingerprint:
    """What an object looked like when it was last uploaded successfully."""

    inode: int
    size: int
    mtime_ns: int


class DurableState(dict):  # type: ignore[type-arg]
    """The lifetime "already uploaded" map, with an O(1) archive-population cap.

    It IS a ``dict`` -- every reader and writer treats it as ``dict[str, Fingerprint]`` --
    with one addition: a companion FIFO, :attr:`archive_order`, that :func:`_record_durable`
    keeps in step with the archive keys it inserts, so the oldest archive key is found and
    the archive population sized without rescanning the whole map on every insertion. That
    rescan would be O(n) per insert and O(n^2) across one large archive walk, the very
    unbounded cost the archive cap exists to prevent -- so the container that holds the cap
    must not reintroduce it. Only archive keys go in the FIFO; live and authority keys are
    the bounded flat set the map holds whole and never counts against the cap.

    A plain ``dict`` (a unit test that passes ``{}``) has no companion and :func:`_record_durable`
    falls back to a scan -- correct, just not O(1); the process constructs this.
    """

    def __init__(self) -> None:
        super().__init__()
        self.archive_order: deque[str] = deque()
        #: stem -> the content-addressed blob key currently recorded for that live
        #: transcript. A changing transcript takes a fresh key each cycle, so this is what
        #: lets :func:`_evict_prior_live_blob` drop the superseded version's key as the new
        #: one is recorded, bounding the live-blob population to one per live session rather
        #: than one per version ever uploaded.
        self.live_blobs: dict[str, str] = {}


@dataclass(frozen=True)
class Snapshot:
    """An open descriptor and the length its file had when it was opened.

    The pair IS the snapshot. Neither half is a snapshot alone: the descriptor without
    the length would upload however much had arrived by the time the transport got
    there, and the length without the descriptor would have to re-open the name, which
    is a second resolution of one path with a window in between.
    """

    fh: BinaryIO
    fingerprint: Fingerprint

    @property
    def size(self) -> int:
        return self.fingerprint.size

    def close(self) -> None:
        self.fh.close()


class RefusedEntry(RuntimeError):
    """This entry is not a file whose bytes can be uploaded.

    *permanent* says whether a LATER cycle could reach it. An entry's SHAPE is what makes
    a refusal permanent -- a symlink where a transcript belongs, a FIFO, a linked archive
    root -- and nothing the backup does changes it: every cycle meets the same answer
    until someone removes the name. A transient refusal is the ordinary case and the
    opposite: a directory missing right now, a descriptor that could not be opened, with
    bytes that may be perfectly live behind it.

    The cycle needs the difference because WITHHOLDING THE AUTHORITY PAIR IS A WAIT. It
    holds the index back one cycle so the next one can publish a pair the bucket's objects
    support, which is right when what it waits for will arrive. Against a permanent
    refusal the wait never ends: the pair is withheld on every later cycle too, the index
    freezes at the moment the entry appeared, and a replacement task restores a
    conversation list that predates every conversation served since -- while their
    transcripts keep uploading, unreferenced.

    So a permanent refusal still makes the cycle incomplete, and it does not withhold. The
    residue is stated rather than hidden: if a planted name happens to collide with a
    session the index does name, that one conversation's history is absent behind an index
    that advanced past it. That is bounded, it is named in the cycle's own report, and
    removing the file repairs it -- where the freeze is unbounded, silent, and repaired by
    nothing.
    """

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


def _descend(root: Path, parts: tuple[str, ...]) -> int:
    """Open the directory at *root* / *parts*, refusing a symlink at any component.

    Returns a descriptor the caller must close. ``ELOOP`` from any step means a
    directory in the chain is a link, which is refused rather than followed: a link out
    of the data home turns "back up this task's own state" into "upload whatever it
    points at", and the sessions tree is one the agent writes in.
    """
    fd = os.open(str(root), _DIR_FLAGS)
    try:
        for name in parts:
            nxt = os.open(name, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except OSError:
        os.close(fd)
        raise
    return fd


def _open_within(root: Path, path: Path) -> int:
    """Open *path* for reading, having walked to it from *root* one component at a time.

    *root* is the trust anchor -- the data home, which is the container's own mount and
    not a path the agent can replace. Every component below it is opened with the link
    refused, so the descriptor returned addresses a file inside the real data home and
    not one reached through a directory something swapped for a link.

    Opening the full path in one call cannot do this: ``O_NOFOLLOW`` applies to the last
    component only, and the kernel resolves the rest normally.

    A missing DIRECTORY on the way down is a refusal, while a missing leaf is left to the
    caller as ``FileNotFoundError``. The two are different events wearing one errno: the
    leaf is a conversation the owner deleted, and a directory is this task's whole state
    tree becoming unreachable -- an unmounted data home, a removed ``sessions/archive`` --
    with the transcripts still live behind it. Read as a deletion, that publishes an index
    for conversations the cycle never looked at.
    """
    rel = path.relative_to(root)
    parts = rel.parts
    try:
        fd = _descend(root, parts[:-1])
    except FileNotFoundError as exc:
        raise RefusedEntry(
            f"a directory on the way down to it is missing ({exc}); the file itself was "
            "listed moments ago, so its bytes are not known to be gone -- this is the data "
            "home or a directory inside it becoming unreachable, which must not be recorded "
            "as a conversation the owner deleted"
        ) from exc
    try:
        return os.open(parts[-1], _OPEN_FLAGS, dir_fd=fd)
    finally:
        os.close(fd)


@dataclass
class CycleResult:
    """What one cycle did, per object, for the log and for the tests."""

    #: The keys uploaded / found unchanged this cycle. Bounded: a capped SAMPLE of the keys
    #: plus a true count (``uploaded_count`` / ``unchanged_count``), because the archive
    #: contributes one entry per segment every cycle and retention-off lets the archive grow
    #: without bound -- an unbounded list here would hold the whole inventory the streamed
    #: enumeration exists to avoid holding. Nothing reads their IDENTITY to decide anything
    #: (the summary reads the counts; the withhold and completeness checks read ``refused`` /
    #: ``unreachable`` / ``gone_referenced``), so a sample serves the log while the count
    #: stays exact. Recorded through :meth:`record_uploaded` / :meth:`record_unchanged`.
    uploaded: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    uploaded_count: int = 0
    unchanged_count: int = 0
    gone: list[str] = field(default_factory=list)
    above_ceiling: list[str] = field(default_factory=list)
    #: Exact counts for ``gone`` / ``above_ceiling`` above and ``gone_undurable`` /
    #: ``gone_referenced`` below. Same reason as the other lists: the archive puts one entry
    #: per segment through each of these, so they are capped, count-tracked samples. Nothing
    #: reads their identity to decide anything -- the withhold decision reads
    #: ``gone_referenced_count`` and the summary reads the counts.
    gone_count: int = 0
    above_ceiling_count: int = 0
    refused: list[tuple[str, str]] = field(default_factory=list)
    #: Entries whose BYTES could not be reached at all: a name that is not a regular file,
    #: an archive root that is a link, a subtree that could not be enumerated. Held apart
    #: from ``refused`` because the two decide the authority pair differently, and only
    #: their CAUSE says which is which.
    #:
    #: A refusal means an object the pair can name is not in the bucket, so publishing the
    #: pair would point the front at bytes that are not there -- the pair is withheld. An
    #: unreachable entry is not an object the pair names: the backend never wrote it, so
    #: the index does not reference it and withholding the pair protects nothing. It only
    #: freezes it, and it freezes it FOREVER, because a planted name stays planted: every
    #: later cycle meets the same entry, the pair is never republished, and a replacement
    #: task restores an index from before the entry appeared while transcripts keep
    #: uploading past it.
    #:
    #: The cycle is incomplete either way, which is why this is a second list and not a
    #: log line: something in the set did not reach the bucket and the exit code has to
    #: say so.
    unreachable: list[tuple[str, str]] = field(default_factory=list)
    #: Exact counts for the two lists above. Like ``uploaded``/``unchanged``, the lists are a
    #: capped, length-bounded SAMPLE while these stay exact: on a pathological run every
    #: archived segment can fail its upload, so an entry per failure -- each carrying a full
    #: exception string -- would retain the whole inventory on the failure path, the same
    #: unbounded-retention hazard the streamed enumeration removes from the success path. The
    #: withhold and completeness checks read only whether these are NON-EMPTY (see
    #: :attr:`complete`), which the sample preserves, and the summary reads the count.
    refused_count: int = 0
    unreachable_count: int = 0
    #: Entries that vanished between the listing and their open with nothing in the bucket:
    #: candidates only, and not a verdict. Whether one matters depends on the captured
    #: index, which the authority phase reads -- so this list is informational and
    #: ``gone_referenced`` is what decides anything.
    gone_undurable: list[tuple[str, str]] = field(default_factory=list)
    gone_undurable_count: int = 0
    #: The ``gone_undurable`` entries the CAPTURED authority pair actually names. These
    #: decide the pair like a refusal rather than like an unreachable entry, and the reason
    #: is the premise the whole skew argument rests on: an index OLDER than its bytes is
    #: harmless only because every slot it names already has bytes in the bucket. The pair
    #: is captured BEFORE the enumeration, so it still names a conversation deleted during
    #: the cycle -- and when that conversation was created and deleted inside one interval,
    #: no earlier cycle uploaded it, so the committed index would name a slot the bucket has
    #: never held. The front then fetches an absent object and reads it as a conversation
    #: that never had history.
    #:
    #: Membership in the captured index is the whole test, not the disappearance. A
    #: conversation the index does not name is not something the pair can send a reader to,
    #: so racing its deletion stays the non-failure it has always been -- which is the
    #: routine case, since an owner deleting a conversation is ordinary use.
    #:
    #: Withholding here cannot freeze the index, which is what separates it from an
    #: unreachable entry: this verdict is a race WITHIN one cycle, not a shape on disk. A
    #: file that is genuinely deleted is not listed by the next cycle at all, so it cannot
    #: be gone again, and the pair publishes on that next cycle.
    gone_referenced: list[str] = field(default_factory=list)
    gone_referenced_count: int = 0
    #: Authority-phase keys this cycle did not publish, so they stay as the last complete
    #: cycle left them: the pair when a transcript in the same cycle was refused, and the
    #: generation pointer when the pair is whole but the pointer itself could not be sent
    #: on an interval cycle. On the final cycle an unsent pointer is a refusal, not this.
    withheld: list[str] = field(default_factory=list)
    #: Set when the commit was refused because a NEWER task incarnation is committed -- this
    #: task has been superseded by a replacement. Distinct from an ordinary refusal because
    #: the remedy is distinct: an ordinary refusal is retried next cycle, but a superseded
    #: task can NEVER commit again (the committed incarnation only moves further ahead), so
    #: retrying is the appearance of durability while its front keeps accepting turns no
    #: cycle will ever save. :func:`run_cycle` raises :class:`SidecarSuperseded` on it so the
    #: process ends non-zero and the supervisor tears the task down, exactly as it does for a
    #: bucket that cannot be written.
    superseded: bool = False

    def record_uploaded(self, key: str) -> None:
        """Count an uploaded key, keeping its name only while under the sample cap."""
        self.uploaded_count += 1
        if len(self.uploaded) < _RESULT_SAMPLE_CAP:
            self.uploaded.append(_bounded_identifier(key))

    def record_unchanged(self, key: str) -> None:
        """Count an unchanged key, keeping its name only while under the sample cap."""
        self.unchanged_count += 1
        if len(self.unchanged) < _RESULT_SAMPLE_CAP:
            self.unchanged.append(_bounded_identifier(key))

    @staticmethod
    def _bounded_reason(reason: str) -> str:
        return _bounded_reason(reason)

    def record_refused(self, name: str, reason: str) -> None:
        """Count a refusal, keeping a length-bounded sample while under the cap.

        A refusal withholds the authority pair and makes the cycle incomplete; the checks
        read whether ANY refusal is present, which the sample preserves, so a bounded sample
        plus the exact count carries every decision while a pathological run -- every archived
        segment failing its upload -- cannot retain the whole inventory here.
        """
        self.refused_count += 1
        if len(self.refused) < _RESULT_SAMPLE_CAP:
            self.refused.append((_bounded_identifier(name), self._bounded_reason(reason)))

    def record_unreachable(self, name: str, reason: str) -> None:
        """Count an unreachable entry, keeping a length-bounded sample while under the cap."""
        self.unreachable_count += 1
        if len(self.unreachable) < _RESULT_SAMPLE_CAP:
            self.unreachable.append((_bounded_identifier(name), self._bounded_reason(reason)))

    def record_gone(self, name: str) -> None:
        """Count a gone object, keeping its name only while under the sample cap."""
        self.gone_count += 1
        if len(self.gone) < _RESULT_SAMPLE_CAP:
            self.gone.append(_bounded_identifier(name))

    def record_above_ceiling(self, key: str) -> None:
        """Count an over-ceiling object, keeping its key only while under the sample cap."""
        self.above_ceiling_count += 1
        if len(self.above_ceiling) < _RESULT_SAMPLE_CAP:
            self.above_ceiling.append(_bounded_identifier(key))

    def record_gone_undurable(self, name: str, key: str) -> None:
        """Count a vanished-and-undurable candidate, keeping a sample while under the cap.

        A candidate only, not a verdict -- :meth:`record_gone_referenced` is what the withhold
        decision reads. Bounded like the rest because the archive can put one entry per segment
        through here on a pathological run.
        """
        self.gone_undurable_count += 1
        if len(self.gone_undurable) < _RESULT_SAMPLE_CAP:
            self.gone_undurable.append((_bounded_identifier(name), _bounded_identifier(key)))

    def record_gone_referenced(self, name: str) -> None:
        """Count a vanished object the captured index names, keeping a sample under the cap.

        This is the verdict the withhold decision reads -- via ``gone_referenced_count``, which
        stays exact so a run that overflowed the sample still withholds the pair.
        """
        self.gone_referenced_count += 1
        if len(self.gone_referenced) < _RESULT_SAMPLE_CAP:
            self.gone_referenced.append(_bounded_identifier(name))

    def fold_archive_refusals(self, sink: "_ArchiveWalkSink") -> None:
        """Merge a streamed archive walk's refusals in, preserving its exact counts.

        The sink already holds a capped sample plus a TRUE count; folding it must not shrink
        the count to the sample it carries. So the counts take the sink's true totals and the
        sample entries fill this result's own sample only up to its cap -- a walk that met more
        faults than either cap keeps a bounded sample and the exact number, never one entry per
        fault.
        """
        self.refused_count += sink.refused_count
        for name, reason in sink.refused:
            if len(self.refused) >= _RESULT_SAMPLE_CAP:
                break
            self.refused.append((_bounded_identifier(name), self._bounded_reason(reason)))
        self.unreachable_count += sink.unreachable_count
        for name, reason in sink.unreachable:
            if len(self.unreachable) >= _RESULT_SAMPLE_CAP:
                break
            self.unreachable.append((_bounded_identifier(name), self._bounded_reason(reason)))

    @property
    def complete(self) -> bool:
        # ``gone_referenced`` counts because it WITHHOLDS the pair: a cycle that did not
        # preserve the index has not done its job, and on the final cycle that verdict is
        # the exit code, which is the only way the loss is announced rather than silent.
        # ``gone_undurable`` does NOT count: it is a candidate list, and a candidate the
        # captured index never named cost the pair nothing. Read the COUNTS, not the sample
        # lists: a run that overflowed a sample cap still had those events and must still be
        # incomplete even if the sample were somehow shorter.
        return (
            self.refused_count == 0
            and self.unreachable_count == 0
            and self.gone_referenced_count == 0
        )

    def summary(self) -> str:
        # Every tally reads the true COUNT, not the sample length, so a run that overflowed a
        # sample cap reports how much it did rather than the cap.
        return (
            f"{self.uploaded_count} uploaded, {self.unchanged_count} unchanged, "
            f"{self.gone_count} gone ({self.gone_referenced_count} of them named by the "
            f"captured index and not in the bucket), "
            f"{self.refused_count} refused, "
            f"{self.unreachable_count} unreachable, "
            f"{len(self.withheld)} authority withheld"
        )


class BackupIncomplete(RuntimeError):
    """At least one object in the set could not be uploaded.

    Raised at the END of the cycle, with everything that could be uploaded already
    uploaded. The distinction matters: refusing the whole cycle on the first bad entry
    would cost every other conversation its backup, and dropping the bad entry with a
    log line would be the silent loss this design exists to prevent. So the cycle does
    all the work it can and then cannot be ignored.

    Both causes are named, because the remedies differ: a refused upload is retried by
    the next cycle, while an unreachable entry stays unreachable until someone removes
    the name -- and an operator reading only "could not be uploaded" would wait for a
    retry that can never succeed. A third cause needs neither remedy: an entry that
    vanished mid-cycle while the captured index still named it, with nothing in the bucket
    behind that name, is listed so the reason the pair was withheld is legible -- and the
    next cycle, which will not list that name at all, publishes the pair with no
    intervention.
    """

    def __init__(self, result: CycleResult) -> None:
        self.result = result
        # The sample lists are capped, so they name a bounded subset -- good for the human
        # DETAIL, wrong for the COUNT. The exact totals come from the true counters, which
        # a sample cap never truncates, so a run with 4,097 failures reports 4,097 and not
        # the 4,096 the sample would show.
        blocked = result.refused + result.unreachable
        blocked = blocked + [
            (name, "vanished mid-cycle while the captured index still named it")
            for name in result.gone_referenced
        ]
        detail = "; ".join(f"{name}: {why}" for name, why in blocked)
        total = result.refused_count + result.unreachable_count + result.gone_referenced_count
        super().__init__(
            f"{total} object(s) in the backup set did not reach the bucket "
            f"({result.refused_count} refused, {result.unreachable_count} unreachable, "
            f"{result.gone_referenced_count} vanished-but-indexed); a bounded sample "
            f"follows ({detail}). Everything else in this cycle was uploaded."
        )


class SidecarSuperseded(BackupIncomplete):
    """This task has been superseded by a replacement and can never commit again.

    A :class:`BackupIncomplete` whose cause is the INCARNATION FENCE: a newer task's
    incarnation is committed, so :func:`_commit_generation` refused this task's commit. It is
    its own type because the remedy is the opposite of an ordinary ``BackupIncomplete``. An
    ordinary incomplete cycle is retried at the next interval, and retrying eventually
    succeeds -- a throttle clears, a vanished name stops being listed. A superseded task's
    refusal NEVER clears: the committed incarnation only moves further ahead of this task's,
    so every future cycle is refused too. Retrying it is the appearance of durability while
    this task's front stays healthy and accepts turns no cycle will ever save -- the silent,
    unrecoverable loss the module docstring says must end the process. So the loop lets this
    leave, exactly as it lets :class:`StoreUnusable` leave, and the process exits non-zero so
    the supervisor tears the superseded task down.
    """


def open_snapshot(path: Path, *, root: Path) -> Snapshot | None:
    """Open *path* for upload, or ``None`` when it is not there any more.

    ``None`` means the LEAF was listed and then removed, which is a conversation the
    owner deleted rather than a backup failure. A directory on the way down being absent
    is not that -- the bytes behind it may be live -- so it raises like every other way
    this can fail: :class:`RefusedEntry`, because those are entries whose bytes cannot be
    shown to belong in the bucket or shown to be gone.

    Shape is decided on the DESCRIPTOR, never on the name: a check by name followed by
    an open by name is two resolutions of one path with a window in between. Opening
    first with the link refused and then reading ``fstat`` off the descriptor means the
    entry judged is exactly the entry that will be uploaded.

    *root* is the data home, and the open walks down to *path* from it one component at
    a time with each link refused, so an ancestor directory replaced by a link is a
    refusal here and not a file uploaded from outside the data home.
    """
    try:
        fd = _open_within(root, path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            # Both codes mean the same refusal. ``O_NOFOLLOW`` on a symlink reports
            # ELOOP for the final component and, combined with ``O_DIRECTORY``, ENOTDIR
            # for a directory component -- so the two are one case: something on the way
            # to this file is a link or is not the directory it is supposed to be.
            raise RefusedEntry(
                f"it, or a directory on the way down to it, is a symlink or is not a "
                f"directory ({exc}); a link where this task's own state belongs points "
                "at bytes it does not own",
                permanent=True,
            ) from exc
        if exc.errno == errno.ENXIO:
            # A socket named like a transcript. ``O_RDONLY | O_NONBLOCK`` on a UNIX-domain
            # socket fails with ENXIO before the descriptor exists, so the open never reaches
            # the ``S_ISREG`` check below that the module's table says refuses a socket
            # PERMANENTLY. Without this arm the socket is refused transiently and retried every
            # cycle, and a transcript-shaped refusal withholds the authority pair forever --
            # stranding every conversation served after the socket appeared. A socket holds no
            # transcript bytes and will not become a regular file, so the refusal is permanent.
            raise RefusedEntry(
                f"it is a socket, not a regular file ({exc}); a socket holds no transcript "
                "bytes to upload",
                permanent=True,
            ) from exc
        raise RefusedEntry(f"it could not be opened ({exc})") from exc
    try:
        st = os.fstat(fd)
    except OSError as exc:  # pragma: no cover - fstat on a fresh descriptor
        os.close(fd)
        raise RefusedEntry(f"its shape could not be read ({exc})") from exc
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise RefusedEntry(
            f"it is not a regular file (mode {st.st_mode:#o}); a directory, socket or "
            "FIFO holds no transcript bytes to upload",
            permanent=True,
        )
    return Snapshot(
        fh=os.fdopen(fd, "rb"),
        fingerprint=Fingerprint(inode=st.st_ino, size=st.st_size, mtime_ns=st.st_mtime_ns),
    )


def _live_transcripts(settings: Settings) -> tuple[list[Path], list[tuple[str, str]]]:
    """The ``.jsonl`` files directly under the sessions directory, and any root refusal.

    Returns ``(found, refused)``. A missing directory yields nothing and refuses nothing: a
    task that has served no turn has no sessions directory yet, and that is a first boot
    rather than a fault.

    The root is opened with every link refused BEFORE anything under it is listed, for the
    reason the archive root is: ``os.scandir`` resolves the path it is given, so a link
    planted at ``sessions/`` is followed and its target's files are listed as this task's
    transcripts. Each one is then refused on the way down -- :func:`_open_within` walks from
    the data home with ``O_NOFOLLOW`` and raises at the ``sessions`` component -- so the
    cycle uploads nothing while every refusal is a per-ENTRY one. Listing through the link
    and refusing afterwards is what turns a replaced root into an index that names
    conversations whose bytes are not in the bucket.

    A root refusal WITHHOLDS the authority pair, and unlike a per-entry refusal it does so
    whatever its cause. The residue :class:`RefusedEntry` accepts is one planted NAME among
    transcripts that are otherwise reaching the bucket, where freezing the index for all of
    them is the worse trade. A refused ROOT is the whole tree: nothing is reaching the
    bucket, so an index that advances describes a state the bucket does not hold at all,
    and the pointer staying at the last complete generation costs nothing that was going to
    be uploaded anyway.
    """
    root = settings.sessions_dir
    try:
        fd = _descend(settings.data_home, root.relative_to(settings.data_home).parts)
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [
            (
                root.name,
                f"the sessions directory, or a directory above it, could not be opened as "
                f"a real directory inside the data home ({exc}); nothing under it is "
                "listed, because a link there names files this task does not own, and the "
                "authority pair is withheld rather than published over a tree that was "
                "never enumerated",
            )
        ]
    # Listed THROUGH that descriptor, not by the name again. Checking a descended descriptor
    # and then re-resolving the root by name is two lookups of one directory: the root can be
    # swapped between them, so the listing walks the very tree the check refused while the
    # check reports it sound -- the same one-directory-throughout rule publication follows,
    # and the refusal is worth nothing without it.
    # The descriptor is held until the LAST ``DirEntry`` has been classified, not released
    # when the listing ends. ``os.scandir(fd)`` hands back entries whose ``dir_fd`` is this
    # very descriptor and whose ``path`` is the bare name, so an entry whose type ``readdir``
    # did not report -- ``DT_UNKNOWN``, which CPython documents for network filesystems and
    # the EFS-mounted data home is one -- answers ``is_dir`` by ``fstatat`` through it. Closed
    # first, that call is ``EBADF`` on exactly the filesystem this task runs on, and it raises
    # from inside ``objects_to_back_up``, which ``run_cycle`` calls ABOVE its own ``try`` --
    # so every cycle would die identically for the life of the task and nothing would ever be
    # uploaded. The close stays in a ``finally`` so it still happens on the early return and
    # on any raise.
    try:
        # The listing is bounded WHILE it is read, not after. ``sorted(scan)`` would first
        # materialise every ``DirEntry`` in the directory into one list -- and a slot closed
        # and reopened across a long task life leaves a transcript behind each time, so the
        # directory holds the whole history of slots, not the live ones. That full list is
        # built before any filtering or the entry cap downstream could bound it, so the
        # sidecar's memory grows with the directory and a pathological one OOM-kills it before
        # a single byte is uploaded. Instead the scan is STREAMED one entry at a time, only
        # transcript NAMES are retained (strings, not ``DirEntry`` objects), and retention
        # stops at :data:`_TRANSCRIPT_INDEX_MAX_ENTRIES`: past the cap the loop keeps draining
        # the iterator to COUNT the overflow but holds nothing more, so the retained set never
        # exceeds the cap regardless of how large the directory is.
        names: list[str] = []
        overflow = 0
        try:
            with os.scandir(fd) as scan:
                for entry in scan:
                    if not entry.name.endswith(keys.TRANSCRIPT_SUFFIX):
                        continue
                    # ``follow_symlinks=False`` so a link to a directory is not read as one
                    # file; the shape is decided again on the descriptor, and this only
                    # decides what to put in the list.
                    if entry.is_dir(follow_symlinks=False):
                        continue
                    if len(names) < _TRANSCRIPT_INDEX_MAX_ENTRIES:
                        names.append(entry.name)
                    else:
                        overflow += 1
        except FileNotFoundError:
            return [], []
        if overflow:
            # The live set already exceeds the index cap, so a published index would overflow
            # downstream anyway and the pair would be withheld there. Refusing HERE -- before
            # the full listing is retained and the paired upload stream is built over it --
            # bounds the sidecar's memory while giving the same loud, counted outcome: the
            # authority pair is withheld (so the previous, readable generation stays
            # committed) rather than letting the cycle grow unbounded to describe an index it
            # cannot publish.
            return [], [
                (
                    root.name,
                    f"the sessions directory holds more than the "
                    f"{_TRANSCRIPT_INDEX_MAX_ENTRIES}-entry transcript cap "
                    f"({_TRANSCRIPT_INDEX_MAX_ENTRIES + overflow}+ transcripts, {overflow} "
                    "over); the listing is bounded at the cap and the authority pair is "
                    "withheld rather than materialising the whole directory and publishing "
                    "an index a later backup rebuilds unbounded. A slot repeatedly closed "
                    "and reopened leaves transcripts behind.",
                )
            ]
        # The retained set is at most the cap, so sorting it is bounded.
        found = [root / name for name in sorted(names)]
        return found, []
    finally:
        os.close(fd)


def _is_shape_error(exc: OSError) -> bool:
    """Whether this failure is the PATH's shape rather than a condition that may pass.

    ``ELOOP`` and ``ENOTDIR`` say the name is a link or is not a directory, and nothing the
    backup does changes that -- every later cycle meets the identical error. That is exactly
    the permanence :class:`RefusedEntry` splits on, and it decides the authority pair: a
    withholding that can never end freezes the index instead of protecting it.

    Everything else is treated as passable, which is the safe direction for an unknown errno:
    it withholds the pair for one cycle rather than letting the index advance over a subtree
    that may be perfectly live behind a transient fault.
    """
    return exc.errno in (errno.ELOOP, errno.ENOTDIR)


def _archived_segments(
    settings: Settings,
    sink: _ArchiveWalkSink,
) -> Iterator[Path]:
    """Stream every file under the archive directory, at any depth, recording tree refusals.

    Yields each archived file path as the walk reaches it and NEVER accumulates them into a
    list at any level, so the whole archive inventory -- which retention-off accumulation
    lets grow without bound -- is never held in memory. The pairing and the upload phase pull
    one path at a time, so the sidecar cannot exhaust its allocation before the phase's
    deadline gate runs. Walked rather than globbed at one level because rotation is free to
    nest, and a segment missed here is the older half of a conversation lost at the next task
    replacement.

    The refusals the walk finds -- a directory it could not list, a linked subdirectory it
    dropped, or a refused root -- go into *sink*, bounded there to a capped sample plus a true
    count (see :class:`_ArchiveWalkSink`) rather than into an unbounded list of their own. The
    caller reads the sink AFTER the stream is drained: :func:`run_cycle` extends its result
    from it before the withhold check, so a walk refusal still withholds the authority pair
    and a shape refusal still leaves the pointer free to advance, exactly as when the lists
    were returned. ``refused`` a later cycle could get past; ``unreachable`` it could not --
    the same permanence split :class:`RefusedEntry` states.

    The chain down to the archive directory is opened first with every link refused. It
    has to be, because ``os.walk``'s ``followlinks=False`` governs directories it FINDS
    and not the root it is given: a link planted at ``sessions/archive`` is descended,
    and then every regular file behind it is a file with a key of its own and no reason
    to be in this bucket. When the chain is refused nothing under it is listed, and the
    refusal is recorded so the cycle ends loudly instead of quietly backing up less.
    """
    root = settings.archive_dir
    try:
        fd = _descend(settings.data_home, root.relative_to(settings.data_home).parts)
    except FileNotFoundError:
        return
    except OSError as exc:
        blocked = (
            root.name,
            f"the archive directory, or a directory above it, could not be opened "
            f"as a real directory inside the data home ({exc}); nothing under it is "
            "listed, because a link there points at files this task does not own",
        )
        # A refused ROOT withholds -- which is where it parts from the per-entry split in
        # :class:`RefusedEntry`. That split accepts one planted NAME behind an advancing index
        # because the freeze would cost every other conversation its updates. A root is not
        # one name: the whole subtree goes unenumerated, so an index published over it names
        # conversations whose segments are not in the bucket, and the replacement reads those
        # absent objects as conversations that never had history.
        #
        # But it withholds only for a cause a later cycle could get PAST. The permanence rule
        # is the same one :class:`RefusedEntry` states, and it has to be applied here too: a
        # link or a non-directory at this name is a SHAPE, so every later cycle meets the
        # identical error, the pair is withheld forever, and the index freezes at the moment
        # the name appeared while live transcripts keep uploading past it -- the unbounded
        # freeze that class says must never happen, reached through the guard meant to stop
        # the bounded loss. So a shape refusal is unreachable: the cycle still fails loudly
        # and names the entry, and the pointer is free to advance.
        (sink.cannot_reach if _is_shape_error(exc) else sink.refuse)(blocked)
        return
    # Every error the walk meets is COLLECTED rather than skipped. ``os.fwalk`` swallows an
    # OSError and continues when ``onerror`` is unset, so a directory this uid cannot open
    # contributed no segments, no refusal and no log: the cycle reported itself complete, the
    # pointer advanced over an index naming conversations whose older halves were never
    # uploaded, and the archive lives on an ephemeral disk -- so those segments were gone with
    # no record of which ones. Split on the same permanence rule as the root, into the sink.

    def collect(exc: OSError) -> None:
        where = getattr(exc, "filename", None) or root.name
        entry = (
            str(where),
            f"a directory under the archive could not be listed ({exc}); the segments under "
            "it are not in this cycle's set",
        )
        (sink.cannot_reach if _is_shape_error(exc) else sink.refuse)(entry)

    # Walked THROUGH the descended descriptor rather than from the name a second time, for the
    # reason :func:`_live_transcripts` is: a check on one lookup and a walk on another are two
    # directories the moment the root moves between them, so the refusal the check earns is
    # spent walking the tree it refused. ``fwalk`` starts at the inode the descent validated.
    # The descriptor is held until the walk is EXHAUSTED, across every path this generator
    # yields; the ``finally`` closes it when the generator is fully consumed or closed. The
    # upload phase always drains this iterator to the end -- naming any unreached tail on a
    # deadline or stop -- so the close is reached even when the cycle stops mid-stream.
    try:
        for parent, dirnames, filenames, dir_fd in os.fwalk(
            dir_fd=fd, follow_symlinks=False, onerror=collect
        ):
            dirnames.sort()
            base = root if parent == "." else root / parent
            # A LINKED subdirectory is the one drop the collector above cannot see.
            # ``fwalk`` with ``follow_symlinks=False`` does not descend it -- and does not
            # report it either: it opens the name, compares that descriptor's ``stat``
            # against the name's ``lstat``, and on a mismatch simply drops the entry
            # without calling ``onerror``. So its segments reach neither the stream nor
            # either refusal list, the cycle reports itself COMPLETE, the pointer advances
            # over an index naming conversations whose archived halves were never uploaded,
            # and the archive is on an ephemeral disk -- gone, with no record of which ones.
            # That is the same plant this function already answers loudly one component
            # higher, at the archive root, so going silent one level down is an
            # inconsistency in this defence rather than a case it decided to accept.
            # Named here, and removed from ``dirnames`` so the drop is this function's own
            # rather than a side effect of the walk. Permanent, like the root's shape
            # refusals: a link does not become a directory on the next cycle, so it is
            # UNREACHABLE and the pointer stays free to advance rather than the pair being
            # withheld forever.
            for name in list(dirnames):
                try:
                    linked = stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode)
                except OSError as exc:
                    dirnames.remove(name)
                    collect(exc)
                    continue
                if not linked:
                    continue
                dirnames.remove(name)
                sink.cannot_reach(
                    (
                        str(base / name),
                        "a directory under the archive is a symbolic link; nothing under it "
                        "is listed, because a link there names files this task does not own, "
                        "and its segments are not in this cycle's set",
                    )
                )
            for name in sorted(filenames):
                yield base / name
    finally:
        os.close(fd)


@dataclass(frozen=True)
class BackupSet:
    """What one cycle should upload, in two phases, and what it already could not reach.

    The two lists are separate because the authority files are POINTERS: they name the
    transcripts, so they are only true once those transcripts are in the bucket. Holding
    them in their own phase is what lets the cycle publish them last, and withhold them
    entirely when a transcript did not make it.

    The refusals belong here rather than being discovered later because some of them are
    decided while LISTING, not while opening: a linked archive directory means a whole
    subtree is not enumerated, and that has to reach the cycle as a refusal. A set that
    returned only items would report a short cycle as a complete one.

    ``refused`` and ``unreachable`` carry the LIVE and AUTHORITY refusals, complete when the
    set is built. The ARCHIVE refusals live on ``archive_sink`` instead, because the archive
    is streamed: its walk runs as ``data`` is consumed, so its refusals are known only once
    that stream is drained. :func:`run_cycle` reads the sink after the upload phase and
    before its withhold check, folding the archive refusals into the same split. Both are
    split exactly as :class:`RefusedEntry` splits them: the cycle withholds the authority
    pair for a refusal a later cycle can get past, and must not for one it cannot, because
    that withholding would never end.

    ``data`` is a lazy iterable rather than a built list: it lists the live transcripts (one
    flat directory, bounded) and STREAMS the archive tree one path at a time, so the upload
    phase's deadline gate is reached with the archive inventory never held in a list at any
    level and stops the cycle mid-stream instead of after the whole inventory is in memory.
    It is re-iterable -- each iteration re-lists the live paths and re-walks the archive
    afresh, resetting ``archive_sink`` -- so a caller that reads it more than once is safe.
    """

    data: Iterable[tuple[str | None, Path]]
    authority: list[tuple[str, Snapshot]]
    authority_gone: list[str]
    refused: list[tuple[str, str]]
    unreachable: list[tuple[str, str]]
    archive_sink: _ArchiveWalkSink
    #: The writer-unique id this cycle's pair is published under, which the pointer commits.
    #: Minted per cycle so two concurrent writers address distinct generations.
    generation_id: str

    def close_authority(self) -> None:
        """Release the authority descriptors, uploaded or not.

        The withheld path never uploads them, so closing cannot live at the upload site.
        """
        for _key, snapshot in self.authority:
            snapshot.close()


def objects_to_back_up(settings: Settings, *, generation_id: str | None = None) -> BackupSet:
    """The cycle's two phases: every transcript, then the authority files that name them.

    The authority files are OPENED FIRST, before a single transcript is listed, and
    uploaded from those descriptors at the end of the cycle. Opening is what fixes the
    instant they describe: a descriptor's bounded length is the file as it was at open
    time, so the pair is one coherent snapshot of the index taken BEFORE the enumeration
    it indexes. Reading them at upload time instead let a slot table flushed during the
    cycle name a transcript that cycle never listed -- an index pointing at bytes that
    are not in the bucket.

    They are uploaded last for the same reason they are opened first. An index newer than
    the transcripts it names sends the front to an absent object, and the front reads that
    as a conversation that never had history: a live conversation served empty, with
    nothing raised. An index OLDER than the transcripts is the harmless direction, because
    every slot it names already has its bytes there and a transcript it does not name yet
    is unreferenced rather than misread.

    A missing authority file is not a failure. On a first boot the backend has not written
    one yet, and there is no index to preserve. Such a cycle publishes the file it has and
    no completeness record, so the bucket keeps saying that no whole pair has been
    published -- which is what lets the next task boot instead of refusing.
    """
    refused: list[tuple[str, str]] = []
    unreachable: list[tuple[str, str]] = []
    authority: list[tuple[str, Snapshot]] = []
    gone: list[str] = []
    # A cycle publishes its pair into a WRITER-UNIQUE generation. Minted here when the caller
    # did not supply one, so two writers racing this window each address a distinct
    # ``gen/<id>/`` and neither can overwrite the other's pair -- the pointer's compare-and-swap
    # then settles which single generation is committed.
    if generation_id is None:
        generation_id = keys.new_generation_id()
    for name in keys.AUTHORITY_NAMES:
        path = settings.config_dir / name
        try:
            snapshot = open_snapshot(path, root=settings.data_home)
        except RefusedEntry as exc:
            log.error("backup: refusing %s -- %s", name, exc)
            (unreachable if exc.permanent else refused).append((name, str(exc)))
            continue
        if snapshot is None:
            log.info("backup: %s is not there yet; there is no index to preserve", name)
            gone.append(name)
            continue
        authority.append((keys.authority_generation_key(settings, generation_id, name), snapshot))
    # The open descriptors are OWNED here until :class:`BackupSet` takes them. Their only
    # close site is the ``finally: plan.close_authority()`` in :func:`run_cycle`, and
    # ``run_cycle`` calls this function ABOVE that ``try`` -- so a raise from the live
    # enumerator below escapes with the snapshots still open, and a task that raises once
    # per interval leaks two descriptors a cycle until ``EMFILE`` makes ``open_snapshot``
    # fail for a reason that looks nothing like the cause.
    #
    # The live transcripts are listed EAGERLY here: the sessions directory is one flat
    # level, so its listing is bounded by the live session count and holding it is not the
    # unbounded-inventory hazard. Listing it at plan time is also what the durability
    # contract needs -- a transcript listed now and unlinked before the upload opens it is
    # a deletion (gone), and a directory above it that vanishes is a refusal, and the
    # cycle can only tell those apart because the path was in the set BEFORE the open. The
    # ARCHIVE is the unbounded one -- rotation nests it and retention-off lets it grow -- so
    # it is STREAMED instead (see below), never held as a list.
    try:
        live, live_refused = _live_transcripts(settings)
        refused.extend(live_refused)
    except BaseException:
        for _key, snapshot in authority:
            snapshot.close()
        raise

    # The archive is enumerated LAZILY and its refusals collected into this bounded sink as
    # the walk runs. ``data`` streams the archive one path at a time (below), so the whole
    # archive inventory is never held in a list at any level -- the retention-off growth that
    # would otherwise exhaust the sidecar before the upload phase's deadline gate could bound
    # it. The sink's refusals are complete only once that stream is drained, so unlike the
    # live and authority refusals they are NOT in ``plan.refused``/``plan.unreachable`` at
    # build time: :func:`run_cycle` reads them from ``plan.archive_sink`` after the upload
    # phase and before its withhold check, where the whole-plan refusal split is decided.
    archive_sink = _ArchiveWalkSink()

    # ``data`` pairs each transcript with its key ON DEMAND rather than building the whole
    # list of pairs here. The live transcripts are listed above (a flat directory bounded by
    # its entries); the archive is streamed through :func:`_archived_segments`, which yields
    # each path as its walk reaches it and never accumulates them. Building a key/path tuple
    # for every archived object up front would be a second materialisation of the whole
    # inventory that the phase's deadline gate never gets to bound: the sidecar can exhaust
    # its allocation before a single deadline check runs, and the final cycle loses
    # everything since the prior one. Yielding the pairs lets the gate stop mid-stream.
    #
    # Re-iterable: each iteration re-lists the live paths from the same bounded list and
    # re-walks the archive afresh. Re-walking re-collects the archive refusals, so the sink
    # is RESET at the start of each iteration -- the sink then reflects the most recent walk,
    # which in the one place that reads it (:func:`run_cycle`, after its single consumption
    # via the upload phase) is the only walk. A caller that iterates for the paths alone and
    # ignores the sink is unaffected.
    def _pairs() -> Iterator[tuple[str | None, Path]]:
        # A live transcript carries its filename STEM, which is what the front derives a
        # fetch from and the transcript index maps to a blob digest. An archived segment
        # carries None: it is content-addressed and uploaded for durability, but the front
        # never fetches it (that would need a listing), so it earns no index entry.
        archive_sink.reset()  # a fresh walk re-collects its own refusals
        for path in live:
            yield path.name[: -len(keys.TRANSCRIPT_SUFFIX)], path
        for path in _archived_segments(settings, archive_sink):
            yield None, path

    class _DataPairs:
        """A re-iterable view over the cycle's ``(stem-or-None, path)`` data pairs.

        Live paths come from a bounded list and carry their stem; archive paths are streamed
        afresh each iteration and carry None, so nothing holds the whole archive inventory.
        """

        __slots__ = ()

        def __iter__(self) -> Iterator[tuple[str | None, Path]]:
            return _pairs()

    return BackupSet(
        data=_DataPairs(),
        authority=authority,
        authority_gone=gone,
        refused=refused,
        unreachable=unreachable,
        archive_sink=archive_sink,
        generation_id=generation_id,
    )


def run_cycle(
    settings: Settings,
    store: ObjectStore,
    *,
    state: dict[str, Fingerprint],
    deadline: float | None = None,
    incarnation: str | None = None,
    yield_when: Callable[[], bool] | None = None,
) -> CycleResult:
    """Upload everything in the set that has changed. Raise if anything was refused.

    *state* is read and written in place, so the caller keeps one map across cycles and
    an object unchanged since its last successful upload is not sent again.

    The authority phase runs only when the transcript phase reached everything it was
    asked for. A cycle that could not commit one transcript leaves the authority files
    as the last complete cycle wrote them, which is an older but coherent pair, rather
    than advancing the index past the bytes.

    A failure between the two authority PUTs leaves an unpublished partial generation under
    this cycle's writer-unique ``gen/<id>/`` prefix, while the committed pointer is UNCHANGED --
    it still names the last generation this writer fully committed, because only the pointer's
    compare-and-swap (written after both PUTs land) makes a generation live. Nothing a restore
    reads moved: the half-written generation is never pointed at. It is not silent either -- the
    failure is a refusal the cycle raises on, and the next cycle writes a fresh whole generation
    and flips the pointer to it. What it costs is one interval in which this cycle published
    nothing.

    *deadline* is a ``time.monotonic`` reading after which no further object is attempted.
    The final cycle passes one, because it runs inside a drain window and uploads
    sequentially: without a bound the window elapses mid-PUT and the process is SIGKILLed,
    which loses the object in flight and says nothing about the ones behind it. With one,
    every object the cycle could not reach is recorded as a refusal by name, the cycle is
    incomplete, and the process exits non-zero on a report an operator can act on. The
    ordinary interval cycles pass none: they have a next interval.

    The transcript phase stops EARLY enough to leave the index its own room. Both phases
    are bounded by the same deadline, but a data phase allowed to spend all of it would
    reach the end with nothing left for the authority pair, and the authority PUTs would
    then run past the window and be killed mid-request -- publishing one file and not the
    other, which is the torn index the two-phase order exists to avoid.
    """
    result = CycleResult()
    # The incarnation is defaulted BELOW, after the single pointer read, so a direct caller
    # that supplies none continues the committed task rather than re-reading the pointer here
    # (a second read would consume a concurrent-writer ETag advance the CAS test relies on).
    publish = True
    committed_index: dict[str, str] = {}
    try:
        committed = generation.read_pointer(settings, store, deadline=deadline, cancel=yield_when)
        if incarnation is None:
            # A direct caller (a test, or anything not the sidecar entrypoint) that supplies
            # no incarnation CONTINUES the committed task rather than minting a fresh racing
            # one: the default is the committed pointer's own token, so a second cycle against
            # a bucket this task already committed carries the SAME token and is not fenced
            # out as a concurrent N+1. With no committed pointer, or one with no incarnation,
            # there is nothing to continue, so a base token is minted. The sidecar entrypoint
            # does NOT rely on this -- it mints once at startup via :func:`startup_incarnation`
            # and passes the same token every cycle, which is what lets the commit refuse a
            # predecessor a replacement has superseded.
            incarnation = (
                keys.new_incarnation()
                if committed is None or not committed.incarnation
                else committed.incarnation
            )
        if committed is not None:
            # The committed generation's transcript index, read once here and handed to the
            # upload phase: it is what tells a transcript that VANISHED before the phase
            # could open it apart from one that was never published -- the first is still
            # fetchable from its committed blob, the second is a loss the captured-index
            # check then weighs. An absent index (generation 0, or a first cycle before any
            # transcript) reads as empty, which is the correct "nothing committed yet".
            committed_index = generation.read_transcript_index(
                settings, store, committed.generation, deadline=deadline, cancel=yield_when
            )
    except (generation.PointerUnusable, generation.TranscriptIndexUnusable) as exc:
        log.error(
            "backup: %s The authority pair is NOT published this cycle, because committing a "
            "new generation without knowing which one is currently committed can overwrite "
            "the generation a replacement would boot from. Transcripts still upload.",
            exc,
        )
        committed = None
        publish = False
        if incarnation is None:
            # The pointer was unusable, so there is no committed token to continue: mint a
            # base one. publish is false this cycle anyway, but transcripts still upload and
            # the token must not be None where the upload phase reads it.
            incarnation = keys.new_incarnation()
    plan = objects_to_back_up(settings)
    for _name, _reason in plan.refused:
        result.record_refused(_name, _reason)
    for _name, _reason in plan.unreachable:
        result.record_unreachable(_name, _reason)
    for _name in plan.authority_gone:
        # Through the recorder, not a bare ``extend``: ``record_gone`` is what keeps
        # ``gone_count`` exact, so a missing ``session_map.json`` / ``open_slots.json`` is
        # counted in the cycle line rather than landing in the sample list while the count
        # still reads zero.
        result.record_gone(_name)
    if plan.authority and not _record_is_due(plan):
        # SOME of the pair, not all of it, which is not the same as none of it. With no whole
        # pair there is no generation to commit, so the pointer stays where it is -- and a
        # restore that finds no pointer at all reads the bucket as empty. Uploading the one
        # file it has would therefore put it in a generation
        # nothing can reach, while the cycle reported itself complete and exited zero: an
        # index silently absent rather than an index preserved. The two files have
        # independent writers, so a one-file window is ordinary timing skew and not an
        # extreme state -- which is exactly why it must not be the quiet path.
        #
        # None of the pair stays a non-failure, as above: on a first boot there is no index
        # to preserve and nothing to say. Some of it is refused, which withholds the file
        # that IS there and makes the cycle incomplete, so the next cycle -- once both
        # writers have flushed -- publishes a whole pair into a generation a reader can
        # reach, and the exit code names the wait instead of hiding it.
        absent = [name for name in keys.AUTHORITY_NAMES if name in set(plan.authority_gone)]
        missing = absent or ["an authority file"]
        for name in missing:
            result.record_refused(
                name,
                "the authority pair is incomplete this cycle, so there is no generation to "
                "publish the rest of it into",
            )
    if not publish:
        for key, _snapshot in plan.authority:
            result.withheld.append(key)
        if deadline is not None:
            # The final cycle. Withholding alone would exit zero and report a lossless
            # stop, while the pointer still names the older generation and the pair this
            # drain flush produced is never published -- so the replacement boots an index
            # without the conversations served since the last interval publish, whose
            # transcripts are in the bucket and unreferenced. An interval cycle keeps the
            # plain withholding above, because its next cycle re-reads the pointer.
            for key, _snapshot in plan.authority:
                result.record_refused(
                    key, "the committed generation could not be read, so the pair is unpublished"
                )
    try:
        new_index = _upload_phase(
            plan.data,
            settings=settings,
            store=store,
            state=state,
            result=result,
            committed_index=committed_index,
            deadline=_reserve_for_authority(
                deadline,
                # Reserve for the authority phase only when it will RUN. When the pair is
                # withheld this cycle (the block above records every authority key as
                # withheld or refused and publishes none), reserving a PUT-worth of window
                # per authority object would carve time off the data phase for uploads that
                # never happen -- shrinking what the transcripts get for no gain. Derive the
                # count from the phase that will run: zero when it is withheld, else the pair
                # plus the transcript index plus the pointer when a record is due.
                len(plan.authority) + (2 if _record_is_due(plan) else 0) if publish else 0,
            ),
            yield_when=yield_when,
        )
        # The archive is streamed, so its refusals are known only now that the upload phase
        # has drained the stream (or drained its tail to name what it did not reach). Fold
        # them into the result HERE -- after the phase, before the withhold check below --
        # so a directory the walk could not list or a linked subtree it dropped withholds
        # the pair, and a shape refusal leaves the pointer free to advance. The sink is
        # bounded (a capped sample plus a true count), and the checks below key on whether
        # there were ANY refusals, not their identity, so the bound changes no decision.
        # Folded so the result's own COUNTS take the sink's true counts (not the sample
        # length) while the sample entries fill the result's sample only up to its cap.
        result.fold_archive_refusals(plan.archive_sink)
        # The lifetime *state* map is kept from growing with the archive AT INSERTION time now
        # (see :func:`_record_durable`), not swept here: a single cycle can enumerate an
        # arbitrarily large archive, so trimming only after the phase would let the map hold
        # that whole cycle's archive keys transiently -- the peak the bound must forbid. The
        # per-insertion eviction holds the archive population at the cap at every moment.
        # Keyed on ``refused`` ALONE, never on the cycle being incomplete. A refusal means
        # an object the pair CAN name did not reach the bucket, so publishing the pair
        # would send the front to bytes that are not there. An unreachable entry is a name
        # the backend never wrote, so the pair does not reference it -- and because a shape
        # refusal stays put, withholding on one would withhold the pair on every later
        # cycle too: the index frozen permanently while transcripts keep uploading past
        # it, and a replacement task restoring the pair from before the entry appeared.
        if result.unreachable_count and not result.refused_count:
            log.warning(
                "backup: %d entries in the backup set could not be reached, so this cycle "
                "is incomplete -- the authority pair IS still published, because none of "
                "them is an object the pair can name: %s",
                result.unreachable_count,
                ", ".join(name for name, _why in result.unreachable),
            )
        _referenced_by_captured_index(plan, result, settings=settings)
        # Seed the generation's transcript index from the committed index BEFORE comparing or
        # committing it. The upload phase builds *new_index* from only the transcripts THIS
        # cycle enumerated locally, which on a replacement task is a subset -- the front
        # fetches a conversation's transcript lazily, on the turn that continues it, so a
        # replacement's first cycles have most conversations' bytes still only in the bucket
        # and not on local disk. Publishing new_index alone would drop every such stem from
        # the committed generation, and the front reading the new index would miss it and
        # serve an empty history -- the silent-loss
        # hazard this subsystem exists to prevent. So the committed index's mappings for
        # stems the CAPTURED authority pair still names are carried forward (their blobs are
        # immutable and still durable), then this cycle's local digests overlay them so a
        # conversation that gained turns takes its new blob. A stem the pair does not name is
        # dropped: that conversation was deleted, so forgetting its blob is correct.
        merged_index = _merge_transcript_index(plan, committed_index, new_index)
        # Before publishing the index, bound it: the live population SHOULD be the live-session
        # count, but repeatedly closing and reopening a slot leaves transcripts behind, so the
        # merged index can grow with the whole history of slots. An index past the entry cap,
        # or one that serializes past the byte cap the restore can read, is NOT published --
        # the authority pair is withheld (so the previous, readable generation stays committed)
        # and the overflow is recorded as a counted refusal rather than shipping an index a
        # later backup rebuilds unbounded and a restore cannot read at all.
        if publish:
            index_body_len = len(json.dumps(merged_index, sort_keys=True).encode("utf-8"))
            if (
                len(merged_index) > _TRANSCRIPT_INDEX_MAX_ENTRIES
                or index_body_len > _TRANSCRIPT_INDEX_MAX_BYTES
            ):
                index_key = keys.transcript_index_key(
                    settings,
                    committed.generation if committed is not None else plan.generation_id,
                )
                over_entries = max(0, len(merged_index) - _TRANSCRIPT_INDEX_MAX_ENTRIES)
                result.record_refused(
                    index_key,
                    f"the transcript index is {len(merged_index)} entries / {index_body_len} "
                    f"bytes, over the {_TRANSCRIPT_INDEX_MAX_ENTRIES}-entry / "
                    f"{_TRANSCRIPT_INDEX_MAX_BYTES}-byte cap ({over_entries} entries over); "
                    "the pair is withheld rather than publishing an unreadable index",
                )
                for key, _snapshot in plan.authority:
                    if key not in result.withheld:
                        result.withheld.append(key)
                log.error(
                    "backup: the transcript index would be %d entries / %d bytes, past the "
                    "%d-entry / %d-byte cap, so the authority pair is NOT published this "
                    "cycle. A slot repeatedly closed and reopened leaves transcripts behind; "
                    "the committed generation stays at the last readable index.",
                    len(merged_index),
                    index_body_len,
                    _TRANSCRIPT_INDEX_MAX_ENTRIES,
                    _TRANSCRIPT_INDEX_MAX_BYTES,
                )
                publish = False
        if result.refused_count or result.gone_referenced_count:
            for key, _snapshot in plan.authority:
                if key not in result.withheld:
                    result.withheld.append(key)
            log.error(
                "backup: %d object(s) refused and %d vanished while the captured index "
                "still named them, so the authority files are NOT published this cycle -- "
                "the pair in the bucket stays at the last complete cycle rather than "
                "naming transcripts that are not there: %s",
                result.refused_count,
                result.gone_referenced_count,
                ", ".join(result.gone_referenced) or "-",
            )
        elif publish:
            if _pair_unchanged(
                plan,
                settings=settings,
                state=state,
                committed=committed,
                committed_index=committed_index,
                new_index=merged_index,
            ):
                for key, _snapshot in plan.authority:
                    result.record_unchanged(
                        keys.authority_generation_key(
                            settings,
                            committed.generation,  # type: ignore[union-attr]
                            key.rsplit("/", 1)[-1],
                        )
                    )
            else:
                _commit_authority(
                    plan.authority,
                    settings=settings,
                    store=store,
                    state=state,
                    result=result,
                    deadline=deadline,
                    yield_when=yield_when,
                )
                # The transcript index is committed as part of the generation, BEFORE the
                # pointer: the pointer is the last object, so a crash after the index lands
                # but before the pointer leaves the index in an unreferenced generation and
                # the previous one still committed -- the same ordering that makes the pair
                # safe. The front reads this index to resolve a stem to its blob, so it must
                # be present under the generation the pointer will name.
                _commit_transcript_index(
                    merged_index,
                    settings=settings,
                    store=store,
                    state=state,
                    result=result,
                    generation_id=plan.generation_id,
                    deadline=deadline,
                    cancel=yield_when,
                )
                _commit_generation(
                    plan,
                    settings=settings,
                    store=store,
                    state=state,
                    result=result,
                    generation_id=plan.generation_id,
                    committed=committed,
                    incarnation=incarnation,
                    deadline=deadline,
                )
                _evict_superseded_generation(
                    settings=settings,
                    state=state,
                    superseded=committed,
                    committed_now=plan.generation_id,
                    incarnation=incarnation,
                )
                # The commit landed, so the generation the MERGED index describes is now the
                # committed one -- its stems are the authoritative live-transcript set. Retire
                # every live-blob mapping and blob key in *state* for a stem it does not name:
                # a session created, backed up, then deleted leaves ballast that
                # ``_evict_prior_live_blob`` (which only ever REPLACES a changing stem's key)
                # never retires. Pruned against the merged index, NOT this cycle's local
                # ``new_index`` -- the merged set carries forward a replacement task's
                # bucket-only stems, so pruning against it drops only genuinely-departed
                # sessions and never a live conversation fetched lazily later.
                _prune_departed_live_blobs(state, set(merged_index))
    finally:
        plan.close_authority()
    log.info("backup: cycle complete -- %s", result.summary())
    if result.superseded:
        # A newer task's incarnation is committed, so this task's commit was fenced and
        # cannot ever land -- a distinct cause from an ordinary incomplete cycle, raised so
        # the loop ends the process rather than retrying a commit every future cycle refuses.
        raise SidecarSuperseded(result)
    if not result.complete:
        raise BackupIncomplete(result)
    return result


def _record_is_due(plan: BackupSet) -> bool:
    """Whether this cycle can commit a generation at all.

    True only when the plan holds EVERY authority file. A cycle that found one of them
    missing locally has no whole pair to publish, and a generation containing one file
    would be a committed generation the restore boots from while the backend flushes its
    own empty view of the other. Such a cycle leaves the pointer alone, so the bucket
    keeps saying that the last committed generation is the one before it.
    """
    published = {key.rsplit("/", 1)[-1] for key, _snapshot in plan.authority}
    return published == set(keys.AUTHORITY_NAMES)


def _evict_superseded_generation(
    *,
    settings: Settings,
    state: dict[str, Fingerprint],
    superseded: generation.Pointer | None,
    committed_now: str,
    incarnation: str,
) -> None:
    """Drop the just-superseded generation's control-plane keys from the lifetime *state*.

    Each committing cycle mints a FRESH generation id and records its two authority keys and
    its transcript-index key in *state* so an unchanged pair/index is not re-PUT. Those keys
    are per-generation, so without this the one ``DurableState`` the sidecar holds for its
    whole life gains three permanent entries every cycle (~2880/day at a 60s interval) --
    the superseded generation's entries, which ``_pair_unchanged`` only ever reads for the
    CURRENTLY committed generation, so they are dead the moment the pointer moves off them.
    The data objects are NOT evicted here, and are bounded elsewhere: an archive segment's
    path key is held under the archive FIFO cap (:func:`_record_durable`), and a live
    transcript's content-addressed blob key is bounded to ONE per stem by
    :func:`_evict_prior_live_blob`, which drops a stem's superseded key as its new one is
    recorded. This function only drops the superseded GENERATION's three control-plane keys.

    Guarded two ways. It runs only when THIS cycle's pointer actually landed -- the pointer
    key in *state* now carries this generation's body fingerprint -- so a rejected or
    withheld commit (where the old generation is still the committed one) evicts nothing. And
    it never touches the generation just committed, only the one it replaced.
    """
    if superseded is None or superseded.generation == committed_now:
        return
    pointer_key = keys.authority_pointer_key(settings)
    committed_body = generation.pointer_body(committed_now, incarnation)
    committed_fp = Fingerprint(
        inode=0,
        size=len(committed_body),
        mtime_ns=int.from_bytes(hashlib.sha256(committed_body).digest()[:8], "big"),
    )
    if state.get(pointer_key) != committed_fp:
        # This cycle's pointer did not land, so the superseded generation is still the
        # committed one and its entries must stay for the next cycle's _pair_unchanged.
        return
    for name in keys.AUTHORITY_NAMES:
        state.pop(keys.authority_generation_key(settings, superseded.generation, name), None)
    state.pop(keys.transcript_index_key(settings, superseded.generation), None)


def _merge_transcript_index(
    plan: BackupSet, committed_index: dict[str, str], new_index: dict[str, str]
) -> dict[str, str]:
    """The generation's transcript index: committed mappings the pair still names, then local.

    *new_index* holds only the stems THIS cycle enumerated on local disk. On a replacement
    task that is a subset of the conversations the committed authority pair names -- the
    front fetches a transcript lazily, on the turn that continues it, so a conversation this
    task has not served yet has its bytes only in the bucket. Publishing *new_index* alone
    would drop every such stem from the committed generation and the front, reading the new
    index, would miss it and serve an empty history.

    So the committed index is carried forward for every stem the CAPTURED authority pair
    still names (its blob is immutable and still in the bucket), and *new_index* overlays
    it so a conversation that gained turns takes its fresh blob. A stem the pair does not
    name is NOT carried: that conversation was deleted, and forgetting its blob is right.
    When the captured pair cannot be read, nothing is carried -- the local view is the only
    one that can be trusted, which is the conservative direction.
    """
    named = _slots_named_by(plan.authority)
    merged: dict[str, str] = {}
    if named is not None:
        for slot in named:
            stem = transcript_stem(slot)
            if stem and stem in committed_index:
                merged[stem] = committed_index[stem]
    merged.update(new_index)
    return merged


def _pair_unchanged(
    plan: BackupSet,
    *,
    settings: Settings,
    state: dict[str, Fingerprint],
    committed: generation.Pointer | None,
    committed_index: dict[str, str],
    new_index: dict[str, str],
) -> bool:
    """Whether the committed generation already holds exactly this cycle's pair AND index.

    Without this the protocol republishes on every interval: each cycle mints a fresh
    generation id, so the pair's keys differ from the ones last uploaded and every cycle
    looks like a change. The comparison is therefore made against the COMMITTED generation's
    keys, which is where the last published bytes actually went, via the fingerprints this
    process recorded for them.

    The TRANSCRIPT INDEX is compared too, and it is why a cycle can be a real change even
    when the pair's own bytes did not move: a conversation that gained turns keeps the same
    slot, so ``session_map.json`` / ``open_slots.json`` are byte-identical, but the
    transcript's blob digest changed -- so the committed index names a DIFFERENT blob than
    its current one. Republishing is then required, or the committed generation would point
    the front at the stale blob. Equal index AND equal pair is the only true "nothing to
    publish".

    False whenever anything is unknown -- no pointer, a name the commitment does not
    cover, a fingerprint this process never recorded -- because republishing a pair that
    was already there costs one cycle's bandwidth, while skipping one that was not costs
    the index.
    """
    if committed is None or not _record_is_due(plan):
        return False
    if new_index != committed_index:
        return False
    for key, snapshot in plan.authority:
        name = key.rsplit("/", 1)[-1]
        if name not in committed.authority:
            return False
        if (
            state.get(keys.authority_generation_key(settings, committed.generation, name))
            != snapshot.fingerprint
        ):
            return False
    return True


def _slots_named_by(authority: list[tuple[str, Snapshot]]) -> set[str] | None:
    """The slot ids the CAPTURED authority pair names, or ``None`` if that cannot be read.

    Read with :func:`os.pread` off the snapshot's own descriptor, so the offset the upload
    reads from is not moved and the bytes are the ones that will be published -- asking the
    path again would be a second resolution of one name with a window in between, which is
    the thing every other read here avoids.

    ``session_map.json`` names a conversation by its KEY, and ``open_slots.json`` by a
    member of its ``keys`` list. Both shapes are the ones the restore side validates and the
    backend's own loaders accept; anything else in the file is ignored here, because this
    answers only "could the published pair send a reader to this name".

    ``None`` means the question could not be answered -- bytes that do not decode or do not
    parse. The caller must treat that as "it might name anything", never as "it names
    nothing": an unreadable index is exactly when a wrong guess is least recoverable.
    """
    named: set[str] = set()
    for key, snapshot in authority:
        name = key.rsplit("/", 1)[-1]
        try:
            raw = os.pread(snapshot.fh.fileno(), snapshot.size, 0)
            parsed = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - answered as "unknown", never as "empty"
            log.error(
                "backup: the captured %s could not be read to see which conversations it "
                "names (%s), so this cycle cannot tell whether a vanished transcript is "
                "one of them",
                name,
                exc,
            )
            return None
        if not isinstance(parsed, dict):
            log.error(
                "backup: the captured %s is a JSON %s rather than an object, so which "
                "conversations it names cannot be established",
                name,
                type(parsed).__name__,
            )
            return None
        if name == "open_slots.json":
            listed = parsed.get("keys")
            if isinstance(listed, list):
                named.update(member for member in listed if isinstance(member, str))
        else:
            named.update(str(entry) for entry in parsed)
    return named


def _referenced_by_captured_index(
    plan: BackupSet, result: CycleResult, *, settings: Settings
) -> None:
    """Record which vanished-and-undurable entries the captured pair would send a reader to.

    Records directly into *result* through :meth:`CycleResult.record_gone_referenced`, so the
    verdict is a capped sample plus an exact ``gone_referenced_count`` -- the count is what the
    withhold decision reads, and it stays exact where the sample is truncated.

    An unreadable index answers with EVERY candidate rather than none: the pair is withheld,
    the cycle is incomplete, and the next cycle republishes -- where guessing "names nothing"
    would commit an index this cycle could not read against bytes it knows are absent. Because
    ``gone_undurable`` is itself a capped sample, the count of that candidate set (exact) is
    what drives the unreadable-index verdict, not the retained names: every undurable candidate
    is referenced, so the exact count carries the decision even past the sample.
    """
    if result.gone_undurable_count == 0:
        return
    named = _slots_named_by(plan.authority)
    if named is None:
        # Unreadable index: every undurable candidate is referenced. Record the sample we have
        # for the log, then set the count to the exact candidate total so the withhold decision
        # is not fooled by a truncated sample.
        for name, _key in result.gone_undurable:
            result.record_gone_referenced(name)
        result.gone_referenced_count = result.gone_undurable_count
        return
    # The two sides live in DIFFERENT namespaces, and comparing them directly is how this
    # check silently matched nothing: the index names a conversation by its SLOT KEY
    # (``cust-8831``) while its transcript is ``dashboard_cust-8831.jsonl``. So the slot keys
    # are mapped FORWARD through the front's own ``transcript_stem`` -- the function that
    # decides the real filename -- rather than the prefix being stripped off the filename
    # here. Stripping by hand would also miss the character substitution that function does,
    # so a key holding an unsafe character would map to a name this comparison never made.
    named_files = {
        f"{stem}{keys.TRANSCRIPT_SUFFIX}"
        for stem in (transcript_stem(slot) for slot in named)
        if stem
    }
    for name, _key in result.gone_undurable:
        if name in named_files:
            result.record_gone_referenced(name)


def _commit_transcript_index(
    index: dict[str, str],
    *,
    settings: Settings,
    store: ObjectStore,
    state: dict[str, Fingerprint],
    result: CycleResult,
    generation_id: str,
    deadline: float | None = None,
    cancel: Callable[[], bool] | None = None,
) -> None:
    """Publish this generation's ``stem -> blob-digest`` index, before the pointer commits it.

    Written into ``gen/<id>/transcript_index.json``, under the SAME writer-unique id the
    pair goes under, so it is part of one immutable generation the pointer either names
    whole or does not name at all. The front reads it to turn a slot stem into the blob it
    fetches, so it must be in the bucket before the pointer that commits the generation --
    which is why it is published here, in the authority phase, ahead of
    :func:`_commit_generation`.

    Recorded in *result* like any other object. A refusal here keeps the pointer where it
    is -- :func:`_commit_generation` returns on any refusal -- so a cut index leaves the
    previous generation committed and whole rather than naming a generation whose index
    never landed. Written ``If-None-Match: *``: the id is fresh this cycle, so nothing holds
    the key, and a defeated precondition would mean a collision that cannot occur.

    An EMPTY index is still published, so the committed generation always carries one and a
    reader never has to tell "no transcripts yet" apart from "index lost": absent means
    generation 0, present-and-empty means a committed generation with no live transcripts.
    """
    key = keys.transcript_index_key(settings, generation_id)
    body = json.dumps(index, sort_keys=True).encode("utf-8")
    fingerprint = Fingerprint(
        inode=0,
        size=len(body),
        mtime_ns=int.from_bytes(hashlib.sha256(body).digest()[:8], "big"),
    )
    if state.get(key) == fingerprint:
        result.record_unchanged(key)
        return
    if deadline is not None and not _time_for_one_more(deadline, BACKUP_ATTEMPT_COST_SECS):
        log.error(
            "backup: the drain window cannot fit the transcript index, so generation %s is "
            "NOT committed and this cycle is refused. The generation the pointer still names "
            "is whole, but it is the older one and no later cycle follows this.",
            generation_id,
        )
        result.record_refused(key, "the drain window could not fit the transcript index")
        return
    try:
        # The index is NOT a few hundred bytes: it can hold up to 50,000 stems or
        # MAX_OBJECT_BYTES // 2 of JSON (the caps at the top of this module), so on a slow
        # link its upload can outlast the drain window and be SIGKILLed mid-write, losing
        # the newest history with nothing to recover it. So it is budgeted and cancellable
        # exactly like the pair-index PUT in the same phase: one attempt's worth of time
        # when a deadline is set, and the cycle's yield check so it stops at the boundary
        # rather than running past it.
        store.put(
            key,
            io.BytesIO(body),
            len(body),
            if_none_match="*",
            budget=None if deadline is None else BACKUP_ATTEMPT_COST_SECS,
            cancel=cancel,
        )
    except StoreUnusable:
        raise
    except PreconditionFailed:
        # The id is minted fresh this cycle, so a key already there is not a legitimate
        # state -- it would mean two cycles minted the same id, which the random suffix
        # makes vanishingly unlikely. Treated as a refusal (not success) because, unlike a
        # content-addressed blob, this key is NOT its content's digest: an object already
        # there might be a DIFFERENT index, so adopting it would commit a mismatched map.
        state.pop(key, None)
        log.error(
            "backup: the transcript index key for generation %s already exists, which should "
            "be impossible for a freshly minted id; refusing rather than committing a "
            "possibly mismatched index.",
            generation_id,
        )
        result.record_refused(key, "the transcript index key already existed; commit refused")
        return
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        state.pop(key, None)
        log.error("backup: the transcript index could not be published (%s)", exc)
        result.record_refused(key, f"the transcript index could not be published ({exc})")
        return
    state[key] = fingerprint
    result.record_uploaded(key)


def _commit_generation(
    plan: BackupSet,
    *,
    settings: Settings,
    store: ObjectStore,
    state: dict[str, Fingerprint],
    result: CycleResult,
    generation_id: str,
    committed: "generation.Pointer | None" = None,
    incarnation: str = "",
    deadline: float | None = None,
) -> None:
    """Publish the pointer naming *generation_id* as the committed generation. The LAST step.

    Last is the property the restore depends on, not a preference. A cycle interrupted
    anywhere in the authority phase therefore leaves the pointer naming the PREVIOUS
    generation, whose objects are all still there and were never rewritten -- so the
    replacement boots from a coherent older pair instead of a torn newer one. Committing
    first would invert that: the pointer would name a generation the crash never finished
    writing, and the restore would refuse a bucket whose previous generation was fine.

    The commit is fenced two ways, and both are required. The pointer PUT is a COMPARE-AND-
    SWAP on the ETag this cycle read, which rejects a commit whose read went stale. On top of
    that the pointer carries this task's INCARNATION token, and the commit refuses to publish
    when the committed pointer's incarnation is NEWER than this one's. The CAS alone cannot
    catch the overlap the two fences together close: a predecessor task whose cycle BEGAN
    AFTER its replacement committed reads the replacement's pointer, so it holds a FRESH ETag
    and its ``If-Match`` would succeed -- rolling the committed generation back to the
    predecessor's older, local-only state. The incarnation is minted once per task at process
    start and is strictly greater for a later-started task, so a predecessor whose incarnation
    is older than what is committed is refused: a superseded task cannot publish.

    Skipped, not failed, when the pair was not whole or anything in this cycle was
    refused: a generation is committed only when it contains one.

    Sent once per published generation through the same *state* map the objects use, so an
    idle cycle that re-published nothing does not re-PUT the pointer. The fingerprint is
    derived from the pointer's own bytes, because it is the one object with no file behind
    it, and it is written only after the PUT returns, so a failed commit is retried.

    A pointer that cannot be written is recorded in ``withheld`` on an interval cycle, and
    that cycle stays complete: the previous generation is still committed and still whole,
    so nothing is lost -- this cycle's newer pair simply is not adopted yet, and the next
    cycle commits it.

    On the FINAL cycle the same failure is a refusal instead, because the recovery above
    is a later cycle and the final cycle has none. Left withheld it would exit zero and
    report a lossless stop while the replacement adopts the generation the pointer still
    names -- the older index, without the conversations this cycle wrote. ``deadline`` is
    what tells the two apart: only the final cycle sets one.
    """
    if not _record_is_due(plan) or result.refused or result.withheld:
        return
    key = keys.authority_pointer_key(settings)
    # The INCARNATION FENCE. A predecessor whose cycle began after its replacement committed
    # holds a fresh pointer ETag, so the compare-and-swap below would admit its stale commit;
    # refusing when the committed incarnation SUPERSEDES this task's is what stops a superseded
    # task from rolling the committed generation back to its older local state.
    # :func:`keys.incarnation_supersedes` is true when the committed counter is strictly
    # greater OR -- the case a bare counter comparison missed -- the counters are equal but the
    # tokens differ: two tasks that both started while the pointer carried counter N both mint
    # N+1 (the read-plus-one is not atomic), so a task that finds a DIFFERENT N+1 already
    # committed cannot prove it is the later one and steps aside rather than overwrite it. A
    # task re-committing its OWN token across cycles sees the identical token and is NOT
    # superseded, so its ordinary cycles are never refused. Ordered BEFORE the
    # fingerprint/unchanged check so a superseded task refuses rather than reporting a no-op
    # success. A committed pointer written before this field carries counter 0, which never
    # supersedes, so a bucket from an earlier writer stays committable.
    if committed is not None and keys.incarnation_supersedes(committed.incarnation, incarnation):
        if committed.is_claim:
            # The newer incarnation that fences this task is a PROVISIONAL CLAIM, not a real
            # commit: a replacement bumped the pointer BEFORE restoration and has not yet
            # committed anything (it may never -- its backend can fail readiness and abort,
            # releasing the claim). Treating that as a permanent supersession is the outage
            # Opus named: a healthy predecessor serving turns at its 60s interval would read
            # the claim, raise SidecarSuperseded and be torn down LONG before the failed
            # replacement's abort path (gated on a 180s readiness timeout) releases the claim
            # -- killing the only live task and losing its turns since the last commit. So a
            # claim-fenced commit is refused as RETRYABLE (``superseded`` stays unset): the
            # predecessor keeps serving and re-attempts next interval, and it steps aside only
            # once a replacement makes a REAL, unmarked commit carrying a newer incarnation --
            # which is proof the replacement is live, not merely claiming.
            log.warning(
                "backup: the committed pointer carries a newer incarnation that is a "
                "PROVISIONAL CLAIM (a replacement that bumped the pointer before restoration "
                "and has not committed). This task's commit of generation %s is refused this "
                "cycle but RETRIED next interval rather than ending the process -- a claim "
                "that never becomes a real commit (its claimant aborts) must not fence a live "
                "predecessor, so the predecessor keeps serving until a real commit supersedes "
                "it.",
                generation_id,
            )
            result.record_refused(
                key, "a newer incarnation is claimed but not yet committed; retried next cycle"
            )
            return
        log.error(
            "backup: the committed generation carries a newer task incarnation than this "
            "task's, so this task has been superseded by a replacement and its commit of "
            "generation %s is refused rather than rolling the committed generation back to "
            "this task's older state.",
            generation_id,
        )
        result.record_refused(
            key, "a newer task incarnation is committed; superseded commit refused"
        )
        # Distinct from an ordinary refusal: this task is SUPERSEDED and can never commit
        # again, so run_cycle raises SidecarSuperseded (not a plain BackupIncomplete) and the
        # process ends rather than retrying a commit the fence will refuse every future cycle.
        result.superseded = True
        return
    body = generation.pointer_body(generation_id, incarnation)
    # The fingerprint distinguishes one committed generation from the next so a pointer
    # naming a NEW generation is re-PUT rather than skipped as unchanged. The pointer is the
    # one object with no file behind it, so the fingerprint is derived from its own bytes:
    # ``mtime_ns`` carries a stable digest of the whole body -- the generation id -- which
    # differs whenever it does, and every body is the same length so ``size`` alone could
    # not tell two commitments apart.
    fingerprint = Fingerprint(
        inode=0,
        size=len(body),
        mtime_ns=int.from_bytes(hashlib.sha256(body).digest()[:8], "big"),
    )
    if state.get(key) == fingerprint:
        result.record_unchanged(key)
        return
    final = deadline is not None
    if deadline is not None and not _time_for_one_more(deadline, BACKUP_ATTEMPT_COST_SECS):
        log.error(
            "backup: the drain window cannot fit the generation pointer, so generation %s "
            "is NOT committed and this cycle is refused. The generation the pointer still "
            "names is whole, but it is the older one and no later cycle follows this.",
            generation_id,
        )
        result.record_refused(key, "the drain window could not fit the generation pointer")
        return
    # The commit is a COMPARE-AND-SWAP against the pointer's state AS THIS CYCLE READ IT, so a
    # second sidecar writing this prefix in the task-replacement window cannot clobber the
    # generation this one publishes. The validator is the ETag captured by ``read_pointer`` at
    # the START of the cycle -- NOT a fresh HEAD here -- for two reasons the review named: a
    # HEAD at commit time is unbudgeted work the final cycle's drain window could be SIGKILLed
    # inside, and reading the validator at cycle start widens the CAS window to cover the
    # authority writes this commit blesses, not just the pointer PUT. An existing pointer commits
    # ``If-Match`` its captured ETag; no pointer yet commits ``If-None-Match: *``. A writer
    # that advanced the pointer since this read changes the ETag, so this PUT is rejected
    # rather than overwriting -- the stale commit steps aside.
    #
    # FAIL CLOSED on a missing validator: a pointer that was present but whose ETag the store
    # could not supply has NO precondition available, and committing unconditionally would
    # defeat the guard against a concurrent writer. So the commit is refused rather than run
    # blind -- the generation the pointer still names is whole, and a later cycle re-reads a
    # validator and commits.
    if_match: str | None = None
    if_none_match: str | None = None
    if committed is None:
        if_none_match = "*"
    elif committed.etag is not None:
        if_match = committed.etag
    else:
        log.error(
            "backup: the committed generation pointer carried no ETag validator, so "
            "generation %s cannot be committed under a compare-and-swap and this cycle is "
            "refused rather than overwriting a concurrent writer's generation blind.",
            generation_id,
        )
        result.record_refused(key, "the generation pointer had no CAS validator; commit refused")
        return
    try:
        # Neither bounded nor cancellable, and both for the same reason: the body is a
        # few hundred bytes the transport reads in ONE call, so a predicate asked between
        # chunks has no second chunk to refuse and a deadline enforced by the body is
        # never re-consulted. What bounds this request is the client's own
        # ``connect_timeout`` and ``read_timeout`` at one attempt, which is a real bound
        # precisely because there is no transmission here to outgrow them.
        store.put(key, io.BytesIO(body), len(body), if_match=if_match, if_none_match=if_none_match)
    except StoreUnusable:
        raise
    except PreconditionFailed:
        # Another writer committed a newer generation since this cycle read the pointer, so
        # this commit is stale and is rejected rather than overwriting it. The cached belief
        # is dropped so a later cycle re-reads the now-current pointer and rebuilds on it, and
        # this cycle is recorded incomplete: it did not publish the pair it opened, and the
        # pointer names another writer's generation, not this one's.
        state.pop(key, None)
        log.warning(
            "backup: the generation pointer moved under this cycle before it could commit "
            "generation %s, so a concurrent writer's generation stands and this commit is "
            "rejected rather than clobbering it.",
            generation_id,
        )
        result.record_refused(
            key,
            "a concurrent writer committed a newer generation; this stale commit was rejected",
        )
        return
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        # The PUT's response was lost, so whether the pointer landed is UNKNOWN -- it may
        # hold the new generation, the old one, or nothing coherent. Any cached belief that
        # this key already holds *fingerprint* is therefore untrustworthy: left in place
        # it would make the early ``state.get(key) == fingerprint`` return above skip the
        # re-PUT on every later cycle and report the unpublished commit as an unchanged,
        # complete stop -- the generation frozen behind a belief a failed write installed.
        # Dropping it forces the next cycle to attempt the commit again.
        state.pop(key, None)
        if final:
            log.error(
                "backup: the generation pointer could not be published (%s), so generation "
                "%s is not committed and this cycle is refused. No later cycle follows this "
                "one, so the replacement would silently adopt the older generation.",
                exc,
                generation_id,
            )
            result.record_refused(key, f"the generation pointer could not be published ({exc})")
            return
        log.error(
            "backup: the generation pointer could not be published (%s), so generation %s "
            "is not committed. The generation it still names is whole, so nothing is lost; "
            "this cycle's pair is simply not adopted yet.",
            exc,
            generation_id,
        )
        result.withheld.append(key)
        return
    state[key] = fingerprint
    result.record_uploaded(key)


def _commit_authority(
    items: list[tuple[str, Snapshot]],
    *,
    settings: Settings,
    store: ObjectStore,
    state: dict[str, Fingerprint],
    result: CycleResult,
    deadline: float | None = None,
    yield_when: Callable[[], bool] | None = None,
) -> None:
    """Upload the already-open authority snapshots, recording each verdict in *result*.

    Separate from the transcript phase because these descriptors are opened by the plan,
    before the enumeration, and are closed by the caller whether or not this runs. Bounded
    by the same deadline: an index PUT that cannot finish inside the window would be killed
    mid-request, and publishing one of the pair without the other is the torn index the
    phase order exists to prevent.

    *yield_when* bounds this phase on an interval cycle the way the deadline bounds it on
    the final one. These uploads are as capable of outliving a stop as a transcript's, and
    a refusal here keeps the pointer where it is -- :func:`_commit_generation` returns on
    any refusal -- so a cut index leaves the previous generation committed and whole rather
    than half-replaced.
    """
    for index, (key, snapshot) in enumerate(items):
        if deadline is not None and not _time_for_one_more(deadline, BACKUP_ATTEMPT_COST_SECS):
            unreached = [k.rsplit("/", 1)[-1] for k, _s in items[index:]]
            log.error(
                "backup: the drain window cannot fit another upload, so %d authority "
                "file(s) are NOT published: %s. The pair in the bucket stays at the last "
                "complete cycle rather than being left half new.",
                len(unreached),
                ", ".join(unreached),
            )
            result.withheld.extend(k for k, _s in items[index:])
            for name in unreached:
                result.record_refused(
                    name, "not attempted: the drain window could not fit another upload"
                )
            return
        _commit_one(
            key,
            snapshot,
            name=key.rsplit("/", 1)[-1],
            settings=settings,
            store=store,
            state=state,
            result=result,
            # This phase's gate reserves one attempt per index object, so that is what
            # its PUT is bounded by -- the reservation and the bound are one number here
            # too, and an index PUT cannot eat the window the rest of the pair needs.
            budget=None if deadline is None else BACKUP_ATTEMPT_COST_SECS,
            cancel=yield_when,
        )


def _time_for_one_more(deadline: float, budget: float) -> bool:
    """Whether one more upload of *budget* seconds still fits before *deadline*.

    Measured against a budget rather than against any remaining time at all: starting a
    PUT with two seconds left buys nothing, because the kill lands mid-request and the
    object is lost anyway while the ones behind it go unmentioned.

    The two phases ask for different budgets, and the difference is the point. A transcript
    asks for a whole PUT including its retries, because it is the thing the window is for.
    An authority file asks for one attempt, because it is uploading inside a slice the data
    phase already set aside for it -- if the index needs retries the cycle is failing
    anyway, and this check stops it before it runs past the window rather than after.
    """
    return deadline - time.monotonic() >= budget


def _reserve_for_authority(deadline: float | None, count: int) -> float | None:
    """Pull *deadline* in by what the index needs, so the data phase leaves it room.

    Without this the transcripts can spend the whole window and the authority PUTs start
    with nothing left: they run past it and are killed mid-request, which publishes one
    file and not the other. That torn pair is the state the two-phase order exists to
    avoid, so the reservation is part of the ordering rather than a tuning choice.

    One attempt per file, not a whole retry budget per file. The budgets are what the drain
    window has to cover, and reserving the worst case for two small JSON files and a pointer
    would consume most of ``SIDECAR_DRAIN_SECS`` before a single transcript moved. The index phase's
    own deadline check is what covers a retry eating into the rest.
    """
    if deadline is None:
        return None
    return deadline - count * BACKUP_ATTEMPT_COST_SECS


def _evict_prior_live_blob(state: dict[str, "Fingerprint"], stem: str, new_key: str) -> None:
    """Drop *stem*'s prior live-transcript blob key from *state* when a new one replaces it.

    A live transcript is content-addressed, so a transcript that gained turns takes a NEW
    key every cycle. Without this the superseded version's key would stay in the lifetime
    *state* map for the process's whole life -- one entry per version per changing session,
    unbounded growth with no in-process recovery. Recording the new key here, and popping the
    stem's prior key when it differs, bounds the live-blob population to the live SESSION
    count (one key per stem). The prior blob OBJECT in the bucket is left alone -- it is
    immutable and the committed index does not name it, so retention prunes it; what is
    bounded is this process's memory, which is what the finding is about.

    The map lives on :class:`DurableState`; a plain ``dict`` (a unit test) has no companion,
    so it cannot track and does not evict -- the process always uses :class:`DurableState`.
    """
    live_blobs = getattr(state, "live_blobs", None)
    if live_blobs is None:
        return
    prior = live_blobs.get(stem)
    if prior is not None and prior != new_key:
        state.pop(prior, None)
    live_blobs[stem] = new_key


def _prune_departed_live_blobs(state: dict[str, "Fingerprint"], live_stems: set[str]) -> None:
    """Drop every live-transcript stem that *live_stems* does not name from the lifetime maps.

    :func:`_evict_prior_live_blob` bounds a CHANGING transcript to one blob per stem, but it
    never fires for a stem that simply STOPS appearing: a session that is created, backed up,
    then deleted leaves its ``live_blobs`` mapping and its content-addressed blob key in
    *state* with nothing to replace them, so the two maps grow one dead entry per
    create/delete cycle for the process's whole life -- unbounded, recovered only by a
    process restart. This retires those entries, dropping both a departed stem's
    ``live_blobs`` mapping and the blob key it named from *state*, so the live-transcript
    population is bounded by the live SESSION count at every committing cycle, not by the
    number of sessions that ever existed.

    *live_stems* is the set of stems the generation this cycle committed names -- the keys of
    the MERGED transcript index, NOT this cycle's local ``new_index``. The distinction is the
    safety of the prune: ``new_index`` holds only the stems this cycle enumerated on local
    disk, a strict subset on a replacement task whose other conversations are fetched lazily
    and are present only in the bucket, so pruning against it would retire a live
    conversation's mapping and (via ``_gone_stem_is_durable``, which reads this ballast)
    later misjudge its vanished local file as undurable. The merged index carries those
    bucket-only stems forward, so it is the authoritative live set and pruning against it
    retires only genuinely-departed stems. Called ONLY after a commit lands (the generation
    the merged index describes is now the committed one); a cycle that refused or withheld
    the pair changes no committed set, so it must not prune.

    The map lives on :class:`DurableState`; a plain ``dict`` (a unit test) has no companion,
    so there is nothing to prune and nothing to read it -- the process always uses
    :class:`DurableState`.
    """
    live_blobs = getattr(state, "live_blobs", None)
    if live_blobs is None:
        return
    for stem in [s for s in live_blobs if s not in live_stems]:
        departed_key = live_blobs.pop(stem)
        state.pop(departed_key, None)


def _record_durable(
    settings: Settings,
    state: dict[str, "Fingerprint"],
    key: str,
    fingerprint: "Fingerprint",
    *,
    is_archive: bool = False,
) -> None:
    """Record *key* as durable in *state*, capping the ARCHIVE population AT THIS INSERTION.

    The lifetime *state* map exists so an unchanged object is not re-uploaded. Live
    transcripts and the authority pair are a bounded, flat set the map is meant to hold
    whole, and they are never counted against the cap or evicted. The ARCHIVE is different:
    rotation nests it and retention-off never prunes it, so without a bound one entry per
    archived segment -- and each entry's KEY STRING, an unbounded-length identifier -- would
    grow the map for the process's whole life, the very unbounded retention the streamed
    enumeration was built to stop, reappearing in *state*.

    *is_archive* is supplied by the CALLER, because under content-addressing a key does not
    carry where its bytes came from: both a live transcript and an archived segment are
    ``data/blob/<digest>``, so the key cannot be read to tell them apart. The upload phase
    knows -- an archived segment is the pair whose stem is ``None`` -- and says so here. A
    prefix check against the archive directory cannot work, because a content-addressed key
    carries the digest of the bytes, not the directory they came from.

    The cap is enforced HERE, as each archive key is inserted, not once at the end of a
    cycle. A single cycle can enumerate an arbitrarily large archive, so a post-cycle sweep
    would let the map hold that whole cycle's archive keys transiently before trimming them
    -- the peak the bound is supposed to forbid. Evicting the oldest archive key on the
    insertion that would exceed the cap holds the archive population at
    :data:`_ARCHIVE_STATE_CAP` at EVERY moment, so neither the count nor the retained key
    identity ever runs past it, mid-cycle or after.

    The archive-key ORDER is tracked in a companion FIFO on :class:`DurableState` so the
    oldest is found and the population sized in O(1), not by rescanning the whole map on
    every insertion. The FIFO is also the ONLY record of which keys are archive, now that
    the FIFO is the sole record of which keys are archive, because the content-addressed key
    shape does not say: a key goes in the FIFO exactly when a caller inserts it with
    *is_archive*, and eviction pops from the FIFO and removes that key from *state*. A
    plain ``dict`` handed in (a unit test that passes ``{}``) has no companion FIFO, so it
    cannot cap -- the process always uses :class:`DurableState`, which can. An archived
    segment is immutable, so a dropped entry costs one re-upload of identical bytes the next
    time it is enumerated and nothing else.
    """
    already_present = key in state
    state[key] = fingerprint
    # A non-archive object -- a live transcript blob, an authority file, the pointer, the
    # index -- is held whole and never triggers an eviction; re-recording a key already
    # present changes no archive count.
    if already_present or not is_archive:
        return
    order = getattr(state, "archive_order", None)
    if order is None:
        # Plain dict (unit tests only): no companion FIFO to cap with. The process uses
        # DurableState, which does. A content-addressed key cannot be re-identified as
        # archive by its shape, so there is no scan fallback -- the FIFO is the record.
        return
    # O(1) path: DurableState tracks archive-key insertion order and count in a FIFO.
    order.append(key)
    if len(order) > _ARCHIVE_STATE_CAP:
        oldest = order.popleft()
        state.pop(oldest, None)


def _name_unreached_remainder(
    current: Path, rest: Iterator[tuple[str | None, Path]], reason: str
) -> list[tuple[str, str]]:
    """Name the not-attempted *current* object plus a BOUNDED sample of what follows it.

    Called when a stop or a deadline gate fires mid-stream. *rest* is the tail of a lazy
    iterator whose end is the archive tree, and retention-off lets that tree grow without
    bound. Draining it to name every unreached object would rebuild the whole inventory in a
    list at the one moment the cycle is trying to STOP -- the same unbounded-retention hazard
    the streamed enumeration removes from the walk. So this pulls at most
    ``_UNREACHED_SAMPLE_CAP`` names and then stops, recording one count-free remainder marker
    when the tail runs past the cap rather than walking it to its end.

    Every entry returned is a refusal, so the cycle is incomplete whether the remainder was
    named whole or summarised -- the current object alone already carries that. A small
    remainder (every real cycle's) fits under the cap and is named in full; only a
    pathological tree is summarised, which is exactly the case that must not be materialised.
    """
    named: list[tuple[str, str]] = [(current.name, reason)]
    for _key, path in rest:
        if len(named) >= _UNREACHED_SAMPLE_CAP:
            # Do not touch *rest* again: advancing it once more is one step further into the
            # tail this cap exists to leave unwalked. The marker stands for "and the rest",
            # count-free because a count is a drain.
            named.append((_UNREACHED_REMAINDER_NAME, reason))
            break
        named.append((path.name, reason))
    return named


def _upload_phase(
    items: Iterable[tuple[str | None, Path]],
    *,
    settings: Settings,
    store: ObjectStore,
    state: dict[str, Fingerprint],
    result: CycleResult,
    committed_index: dict[str, str],
    deadline: float | None = None,
    yield_when: Callable[[], bool] | None = None,
) -> dict[str, str]:
    """Upload one phase of the set, recording every entry's verdict in *result*.

    Returns the ``stem -> blob-digest`` map for the LIVE transcripts this phase reached --
    the transcript index the committing cycle publishes so the front, reading it, can fetch
    a slot's blob by content without a listing. An archived segment is uploaded as a blob
    too (durability) but earns no index entry: the front never fetches one.

    Each data object is CONTENT-ADDRESSED: its bytes are digested and it is written at
    ``data/blob/<digest>`` with ``If-None-Match: *``, so two writers overlapping in the
    task-replacement window never contend for one mutable key -- differing bytes take
    different keys and identical bytes converge on one, a re-PUT of which the store answers
    with a defeated precondition that is success, not a fault. This is what closes the stale
    -overwrite hazard the old mutable ``data/sessions/<stem>`` key carried.

    Stops attempting objects once there is not enough of *deadline* left for one PUT's
    whole retry budget, and records every object from there on as refused BY NAME. Trying
    one more and being killed in the middle of it would lose that object and leave the rest
    unmentioned; stopping first costs the same objects and says which they are.

    *committed_index* is the transcript index of the currently committed generation, read
    once by the caller. It is what settles the durability of a transcript that VANISHED
    before this phase could open it: a gone stem is still durable when the committed index
    names a digest for it and that blob is recorded durable, and undurable otherwise.

    *yield_when* lets an ordinary interval cycle stand down the moment a stop arrives. Its
    uploads predate the backend's flush, so everything it has left is something the FINAL
    cycle will send anyway -- continuing only spends the drain window that cycle needs.
    """
    new_index: dict[str, str] = {}
    # ``items`` is a lazy iterable (see :class:`BackupSet`): the pairs are produced on
    # demand, so this gate is reached before the whole inventory is built and stops the
    # cycle mid-stream rather than after it is all in memory. On a stop or a deadline the
    # current pair is not attempted, so it and the REST OF THE ITERATOR are the unreached
    # remainder -- named by :func:`_name_unreached_remainder`, which records the current
    # object and a BOUNDED sample of what follows rather than draining the tail (the archive
    # tree, unbounded under retention-off) back into a list at the moment the cycle is
    # stopping.
    it = iter(items)
    for stem, path in it:
        if yield_when is not None and yield_when():
            unreached = _name_unreached_remainder(
                path,
                it,
                "not attempted: the stop arrived and the final cycle takes these",
            )
            log.info(
                "backup: the stop arrived mid-cycle, so this object and the objects after it "
                "are left to the final cycle rather than spending its drain window here; "
                "naming up to %d of them: %s",
                _UNREACHED_SAMPLE_CAP,
                ", ".join(name for name, _why in unreached),
            )
            for _name, _reason in unreached:
                result.record_refused(_name, _reason)
            # The stem->digest entries this phase already recorded stand: their blobs are in
            # the bucket. A partial index is still coherent -- it names only blobs that
            # landed -- but a cycle with any refusal withholds the pair anyway, so the caller
            # never commits this partial index. Returned so a completed phase's is committed.
            return new_index
        try:
            snapshot = open_snapshot(path, root=settings.data_home)
        except RefusedEntry as exc:
            # Split on the refusal's own permanence, not on the phase it surfaced in: a
            # missing directory here may be a data home that comes back, while a FIFO at
            # this name will still be a FIFO next cycle. Only the first is worth holding
            # the index back for; see :class:`RefusedEntry`.
            log.error("backup: refusing %s -- %s", path.name, exc)
            record = result.record_unreachable if exc.permanent else result.record_refused
            record(path.name, str(exc))
            continue
        if snapshot is None:
            # Gone before this phase could open it. Not a verdict yet: whether it matters
            # depends on the CAPTURED index, which the authority phase holds, so the
            # authority phase decides referencing. Durability is the half settled here, and
            # under content-addressing it is settled against the COMMITTED transcript index:
            # a stem whose committed digest names a blob this process recorded durable still
            # resolves for a reader, so it is gone-but-durable; a stem with no committed
            # digest, or one whose blob is not durable, is gone-undurable. An archived
            # segment (no stem) is never named by the pair, so its disappearance is a plain
            # gone with no undurable verdict to reach.
            log.info("backup: %s is gone; nothing to upload for it", path.name)
            if stem is not None and not _gone_stem_is_durable(
                settings, stem, committed_index, state
            ):
                result.record_gone_undurable(path.name, path.name)
            result.record_gone(path.name)
            continue
        try:
            # BOTH an archive segment and a live transcript are hashed: the live transcript
            # for its content-addressed blob key, the archive segment to make its path key
            # writer-unique (see below). Hashing is bounded by the per-object budget before
            # the first chunk and between chunks, so a large tree cannot spend the final
            # cycle's window here before the PUT gate runs.
            try:
                digest = _digest_of(snapshot, deadline=deadline)
            except _DigestDeadlineExceeded:
                # The window ran out while hashing. This object and the rest of the
                # iterator are the unreached remainder -- named bounded, not drained --
                # exactly as the pre-PUT gate below does, so a hash overrun and a PUT
                # overrun end the cycle identically.
                unreached = _name_unreached_remainder(
                    path,
                    it,
                    "not attempted: the drain window could not fit hashing another object",
                )
                log.error(
                    "backup: the drain window has %.1fs left, less than the %.0fs hashing "
                    "one object can take, so this object and the objects after it are NOT "
                    "attempted; naming up to %d of them: %s",
                    max(0.0, deadline - time.monotonic()) if deadline is not None else 0.0,
                    BACKUP_PER_OBJECT_BUDGET_SECS,
                    _UNREACHED_SAMPLE_CAP,
                    ", ".join(name for name, _why in unreached),
                )
                for _name, _reason in unreached:
                    result.record_refused(_name, _reason)
                return new_index
            if stem is None:
                # ARCHIVE SEGMENT. Keyed by its RELATIVE PATH with its content DIGEST
                # appended: the owner's control plane finds a conversation's archived rows by
                # PREFIX-listing their ``sessions/archive/<conv>/`` path, so the path prefix
                # is kept intact (a bare ``data/blob/<digest>`` key would strand them), while
                # the appended digest makes the key writer-unique. Two overlapping tasks that
                # rotate the same stem in the same second on isolated filesystems derive the
                # identical PATH -- their per-filesystem collision counters cannot see each
                # other -- so a bare path key PUT unconditionally would silently drop one
                # distinct segment; the digest suffix sends their differing bytes to
                # different keys so neither is lost, and identical bytes converge on one key.
                # A torn mid-write snapshot and the whole segment likewise differ in content,
                # so the key is immutable and written create-only (the whole segment is never
                # overwritten by the torn one, and vice versa). No index entry is earned --
                # the front never fetches archive segments.
                key = keys.archive_segment_key(settings, path, digest)
            else:
                # LIVE TRANSCRIPT. Content-addressed, so two overlapping writers of different
                # bytes never collide on one mutable key, and the committed index maps the
                # stem to this digest for the front to fetch.
                key = keys.blob_key(settings, digest)
                # Record the mapping regardless of whether the blob needs a PUT: an unchanged
                # transcript's blob is already durable, but the committing cycle still
                # publishes a fresh index that must name it, or the front would miss it.
                new_index[stem] = digest
                # NOTE: the prior blob key for this stem is evicted from *state* only AFTER
                # the new key is recorded durable (the _already_durable short-circuit below,
                # or after _commit_one lands) -- NEVER here, before the PUT. Evicting first
                # would pop the prior (genuinely durable, committed-index-named) key out of
                # *state* and then, if the new PUT is refused or the deadline gate skips it,
                # leave neither key recorded: a conversation the owner deletes next cycle
                # would read as gone-undurable though its committed blob is still in the
                # bucket, withholding the pair and reporting a lossless cycle lossy.
            # BEFORE the deadline gate, because an object the bucket already holds needs no
            # PUT and the gate exists to stop PUTs. Checked after the gate, a stop arriving
            # late in an interval cycle refuses every remaining object without opening one,
            # and a refusal withholds the authority pair -- so a drain with nothing to
            # upload would report itself lossy and exit non-zero while the index phase
            # still had most of the window. A content-addressed blob recorded durable in
            # *state* is exactly these bytes under exactly this key, so it needs no re-PUT.
            if _already_durable(key, snapshot, state):
                result.record_unchanged(key)
                # The new key is durable (already in the bucket), so now it is safe to drop
                # this stem's PRIOR blob key from *state* -- bounding the live population to
                # one blob per stem without ever leaving a committed blob unrecorded.
                if stem is not None:
                    _evict_prior_live_blob(state, stem, key)
                continue
            if deadline is not None and not _time_for_one_more(
                deadline, BACKUP_PER_OBJECT_BUDGET_SECS
            ):
                # This pair is not attempted, so it and the rest of the iterator are the
                # unreached remainder. :func:`_name_unreached_remainder` records this object
                # and a bounded sample of what follows rather than draining the tail -- the
                # archive tree is unbounded under retention-off, and rebuilding it into a list
                # to name it is the hazard the streamed enumeration removes from the walk.
                unreached = _name_unreached_remainder(
                    path,
                    it,
                    "not attempted: the drain window could not fit another upload",
                )
                log.error(
                    "backup: the drain window has %.1fs left, less than the %.0fs one "
                    "upload can take, so this object and the objects after it are NOT "
                    "attempted; naming up to %d of them: %s",
                    max(0.0, deadline - time.monotonic()),
                    BACKUP_PER_OBJECT_BUDGET_SECS,
                    _UNREACHED_SAMPLE_CAP,
                    ", ".join(name for name, _why in unreached),
                )
                for _name, _reason in unreached:
                    result.record_refused(_name, _reason)
                return new_index
            _commit_one(
                key,
                snapshot,
                name=path.name,
                settings=settings,
                store=store,
                state=state,
                result=result,
                # BOTH kinds are now IMMUTABLE at their key, so each is created not
                # overwritten (``If-None-Match: *``). A live transcript blob's key IS its
                # digest; an archive segment's key is its path with its digest appended, so
                # a key that already exists holds byte-identical bytes either way. A re-PUT
                # of the same content meets a defeated precondition -- which for an immutable
                # object is success (the bytes are present), recorded durable rather than a
                # fault -- so two writers of identical bytes converge and never collide, and
                # a torn mid-write snapshot takes a DIFFERENT key from the whole segment
                # rather than overwriting it. The authority pair does not pass this: those
                # keys live under the cycle's own fresh generation id and the pointer's
                # compare-and-swap is their fence.
                create_only=True,
                # An archived segment (no stem) counts against the archive cap in state; a
                # live transcript blob does not. The key does not say which, so the phase
                # that knows passes it.
                is_archive=stem is None,
                # The same number the gate just reserved, so the PUT cannot outlive what
                # was set aside for it. None on an interval cycle: there is no window to
                # protect and a next cycle to finish the object, so cutting a slow upload
                # there would abandon one that was on its way.
                budget=None if deadline is None else BACKUP_PER_OBJECT_BUDGET_SECS,
                # The stop reaches INSIDE the upload, not just between them. The check at
                # the top of this loop only runs between objects, so a stop landing during
                # a PUT that has no budget is not observed until that PUT ends on its own
                # -- and this cycle must return before the final one may begin, whose
                # deadline is measured from when the stop was observed rather than from
                # then. Every second spent finishing this object is a second taken from
                # uploading the turns the backend flushed on its way out.
                cancel=yield_when,
            )
            # Evict this stem's PRIOR blob key only now the NEW key is recorded durable --
            # _commit_one puts a landed key in *state* (and a refused / cut / deadline-missed
            # upload does NOT). A failed PUT therefore leaves the prior key in place, so a
            # conversation deleted next cycle is still correctly judged durable against its
            # committed blob. Live transcripts only: an archive segment has no stem and is
            # bounded by its own FIFO cap, not this per-stem replacement.
            if stem is not None and key in state:
                _evict_prior_live_blob(state, stem, key)
        finally:
            snapshot.close()
    return new_index


class _DigestDeadlineExceeded(Exception):
    """The final cycle's drain window ran out while hashing an object for its content key.

    Raised by :func:`_digest_of` so :func:`_upload_phase` ends the cycle the same way the
    pre-PUT deadline gate does -- naming this object and a bounded remainder as unreached --
    rather than hashing a large tree past the window and being SIGKILLed with nothing said.
    """


def _digest_of(snapshot: Snapshot, *, deadline: float | None = None) -> str:
    """The sha256 hex digest of *snapshot*'s bounded bytes, read without moving its offset.

    Read through :func:`os.pread` off the snapshot's own descriptor, exactly as
    :func:`_slots_named_by` reads the authority pair: the offset the upload streams from is
    not disturbed, and the bytes digested are the same bounded length the PUT will send, so
    the key names precisely the content that goes to the bucket. Read in bounded chunks
    rather than whole, so a large transcript is not held in memory twice (once here, once by
    the transport) -- the digest needs the bytes but not all of them at one instant.

    *deadline* bounds the hashing on the FINAL cycle, which runs inside the drain window:
    the per-object budget is checked before the first chunk AND between chunks, so a single
    large object -- or a long run of them -- cannot spend the window inside this read before
    the upload gate downstream ever runs. On an interval cycle (*deadline* None) there is no
    window to protect and a next cycle to finish, so the hash runs unbounded. The check is
    the SAME ``_time_for_one_more`` against ``BACKUP_PER_OBJECT_BUDGET_SECS`` the PUT gate
    uses, so a budget that fits the hash fits the upload it precedes.
    """
    if deadline is not None and not _time_for_one_more(deadline, BACKUP_PER_OBJECT_BUDGET_SECS):
        raise _DigestDeadlineExceeded
    hasher = hashlib.sha256()
    remaining = snapshot.size
    offset = 0
    while remaining > 0:
        if deadline is not None and not _time_for_one_more(deadline, BACKUP_PER_OBJECT_BUDGET_SECS):
            raise _DigestDeadlineExceeded
        chunk = os.pread(snapshot.fh.fileno(), min(remaining, _DIGEST_CHUNK_BYTES), offset)
        if not chunk:
            break
        hasher.update(chunk)
        offset += len(chunk)
        remaining -= len(chunk)
    return hasher.hexdigest()


def _gone_stem_is_durable(
    settings: Settings,
    stem: str,
    committed_index: dict[str, str],
    state: dict[str, Fingerprint],
) -> bool:
    """Whether a transcript that vanished is still fetchable from the committed generation.

    True when the committed transcript index names a blob digest for *stem* AND this process
    recorded that blob durable -- so a reader following the committed index still resolves
    the conversation's history even though the local file is gone. False when the stem has no
    committed digest (nothing was ever published for it) or the named blob is not recorded
    durable, which is the gone-undurable case the captured-index check then weighs against
    what the pair names.
    """
    digest = committed_index.get(stem)
    if digest is None:
        return False
    return keys.blob_key(settings, digest) in state


def _already_durable(key: str, snapshot: Snapshot, state: dict[str, Fingerprint]) -> bool:
    """Whether the bucket already holds exactly these bytes under *key*.

    One definition, because two callers ask it for different reasons and must not disagree:
    :func:`_commit_one` asks so it does not re-send an object, and :func:`_upload_phase` asks
    BEFORE its deadline gate so an object needing no PUT is never recorded as refused.
    """
    return state.get(key) == snapshot.fingerprint


def _commit_one(
    key: str,
    snapshot: Snapshot,
    *,
    name: str,
    settings: Settings,
    store: ObjectStore,
    state: dict[str, Fingerprint],
    result: CycleResult,
    create_only: bool = False,
    is_archive: bool = False,
    budget: float | None = None,
    cancel: Callable[[], bool] | None = None,
) -> None:
    """Send ONE open snapshot, or record why it was not sent. Never closes the descriptor.

    The caller owns the descriptor, because the two phases acquire it at different times:
    the transcript phase opens one per object as it goes, and the authority pair was opened
    by the plan before anything was enumerated.

    *create_only* makes the PUT ``If-None-Match: *`` -- it commits only if nothing holds the
    key yet. A content-addressed blob is immutable, so a key that already exists holds
    exactly these bytes (the key IS their digest); a defeated precondition is therefore
    success, not a fault, and is recorded durable rather than refused. The authority pair
    does not pass it: those keys live under the cycle's own fresh generation id, so nothing
    is there to collide with, and the pointer's own compare-and-swap is the fence that
    settles concurrent commits.

    *budget* is the seconds the caller's deadline gate set aside for this object, handed to
    the store so the PUT is bounded by the same number the gate reserved. Without it the
    gate reserves a window the transmission is free to overrun, which is the one way an
    object is lost rather than reported: the drain SIGKILLs the process in the middle of a
    PUT, so that object and every object after it go with no record of which.

    *cancel* is what an interval cycle hands over instead. It has no window to reserve, so
    it has no budget to pass; the predicate is how its upload is still ended the moment the
    stop arrives, rather than after a transmission that has no bound at all. Neither is a
    permanent failure, so a cut upload is recorded as this cycle's refusal and re-attempted.
    """
    if _already_durable(key, snapshot, state):
        result.record_unchanged(key)
        return
    if snapshot.size > MAX_OBJECT_BYTES:
        # Uploaded anyway: skipping it is the data loss this design exists to
        # prevent. The warning is the point -- the front refuses to restore an
        # object this large, so the pair is honest but incomplete for this one
        # conversation, and an operator has to hear that from the writer rather
        # than from a customer's failed turn.
        log.warning(
            "backup: %s is %d B, above the %d B ceiling the restore side will "
            "read. It is uploaded, and a turn continuing this conversation on a "
            "replaced task will be refused rather than served an empty history.",
            name,
            snapshot.size,
            MAX_OBJECT_BYTES,
        )
        result.record_above_ceiling(key)
    try:
        store.put(
            key,
            snapshot.fh,
            snapshot.size,
            if_none_match="*" if create_only else None,
            budget=budget,
            cancel=cancel,
        )
    except PreconditionFailed:
        # Only reachable with *create_only*: the key already exists. For a content-addressed
        # blob that means its bytes -- which ARE this key's digest -- are already in the
        # bucket, put there by this task or by a concurrent writer of identical content. The
        # object is durable, so it is recorded durable rather than refused, and the index
        # that will name it is correct. There is nothing to re-send: an immutable blob at a
        # given key is byte-identical to any other write of that key.
        log.info("backup: blob for %s is already in the bucket; recorded durable", name)
        _record_durable(settings, state, key, snapshot.fingerprint, is_archive=is_archive)
        result.record_unchanged(key)
        return
    except StoreUnusable:
        # Not recorded as this object's refusal and not retried: the bucket
        # itself cannot be written, so every remaining object in this cycle and
        # every later cycle meets the same answer. It leaves here whole so the
        # process can end on it.
        raise
    except UploadCancelled as exc:
        # The interval cycle's counterpart to the window refusal below, and the reason it
        # is worth recording rather than silently dropping: this object is left to the
        # final cycle DELIBERATELY, and the pair is withheld so the index never names a
        # transcript whose upload was cut. What was given up is one object the final cycle
        # sends anyway; what was bought is the window it sends everything else in.
        log.info("backup: PUT for %s was cut by the stop -- %s", name, exc)
        result.record_refused(name, f"the upload was cut by the stop ({exc})")
        return
    except UploadDeadlineExceeded as exc:
        # A refusal like any other, and deliberately not permanent: the bucket answered
        # and the object is simply larger than this window at the rate the connection is
        # managing. Recording it is the whole gain over being killed mid-PUT -- the pair
        # is withheld, the cycle exits non-zero, and the name of what is missing is in
        # the log instead of nowhere.
        log.error("backup: PUT for %s did not fit its window -- %s", name, exc)
        result.record_refused(name, f"the upload did not fit its window ({exc})")
        return
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        log.error("backup: PUT failed for %s -- %s", name, exc)
        result.record_refused(name, f"the upload failed ({exc})")
        return
    _record_durable(settings, state, key, snapshot.fingerprint, is_archive=is_archive)
    result.record_uploaded(key)
