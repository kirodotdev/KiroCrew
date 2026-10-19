"""Snapshots and what the bucket already holds: fingerprints, the state map, descriptor reads.

A cycle skips an object whose bytes the bucket already holds, and the only memory of that is
the lifetime ``state`` map this process keeps: :class:`Fingerprint` says what an object looked
like when it was last uploaded, :class:`DurableState` is the map with its archive cap, and
:class:`Snapshot` pairs an open descriptor with the fingerprint it was opened at. The two reads
that go through a snapshot's own descriptor live here as well: the content digest that keys an
upload, and the slot names a captured authority file carries.

The drain-window gate every phase asks before it starts one more object,
:func:`_time_for_one_more`, lives here because this is the lowest module both the facade and
``backup_commit`` import. It reads ``time`` through :mod:`container.sidecar.backup` at call
time, so a test that patches ``backup.time`` reaches the gate.

Part of :mod:`container.sidecar.backup`, which re-exports every name here and is the import
path callers use. New durability bookkeeping goes here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import deque
from dataclasses import dataclass
from typing import BinaryIO

from ..common import Settings, keys
from ..common.config import BACKUP_PER_OBJECT_BUDGET_SECS

log = logging.getLogger("smc.sidecar.backup")


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
    # Read through the facade at call time, so a patch of ``backup.time`` reaches here.
    from . import backup

    return deadline - backup.time.monotonic() >= budget


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
