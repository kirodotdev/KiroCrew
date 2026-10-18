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
import json
import logging
import os
import stat
import time
from pathlib import Path
from typing import Callable, Iterable, Iterator

from ..common import Settings, keys
from ..common.config import (  # noqa: F401
    BACKUP_ATTEMPT_COST_SECS,
    BACKUP_PER_OBJECT_BUDGET_SECS,
    MAX_OBJECT_BYTES,
)
from ..front.transcript import transcript_stem  # noqa: F401
from . import generation

# The cycle's owners. This module keeps the code that reads the names the tests patch here
# -- the enumeration, the data phase and ``run_cycle`` -- and re-exports the rest from them;
# an owner that needs a patched name reads it through this module at call time.
# ``backup_record`` holds the plan and the result, ``backup_durable`` the snapshots and what
# the bucket already holds, ``backup_commit`` the authority phase and its fence.
from .backup_commit import (  # noqa: F401
    _commit_authority,
    _commit_generation,
    _commit_transcript_index,
    _evict_superseded_generation,
    _merge_transcript_index,
    _pair_unchanged,
    _record_is_due,
    _referenced_by_captured_index,
    _reserve_for_authority,
    new_incarnation,
    startup_incarnation,
)
from .backup_durable import (  # noqa: F401
    _ARCHIVE_STATE_CAP,
    _DIGEST_CHUNK_BYTES,
    DurableState,
    Fingerprint,
    Snapshot,
    _already_durable,
    _digest_of,
    _DigestDeadlineExceeded,
    _evict_prior_live_blob,
    _gone_stem_is_durable,
    _prune_departed_live_blobs,
    _record_durable,
    _slots_named_by,
    _time_for_one_more,
)
from .backup_record import (  # noqa: F401
    _IDENTIFIER_MAX_CHARS,
    _REFUSAL_REASON_MAX_CHARS,
    _RESULT_SAMPLE_CAP,
    _UNREACHED_REMAINDER_NAME,
    _UNREACHED_SAMPLE_CAP,
    BackupSet,
    CycleResult,
    _ArchiveWalkSink,
    _bounded_identifier,
    _bounded_reason,
    _name_unreached_remainder,
)
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
