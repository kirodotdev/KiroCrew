"""Putting ONE conversation's transcript on disk, at the turn that needs it.

Boot downloads no transcripts at all: a task starts with the two authority files
and no conversations. This module is the other half of that change. Before a
turn reaches the backend, the one transcript that turn continues is fetched.

The property the pair achieves, stated exactly: *a task only ever holds the
conversations it itself served, and loses them when it exits.*

Each rule below is a way this goes wrong silently, so each is enforced by
construction rather than by review.

* **One object, never a list.** The reader this module talks to has no ``list``
  and no ``put`` (:class:`TranscriptReader`), so listing the prefix is not a
  thing this code path can do. A single list would undo the whole change at the
  first turn.
* **Absent is NORMAL.** A new conversation has no transcript yet, and its first
  turn must proceed.
* **Already on disk means this task already served it.** Do not re-fetch and do
  not overwrite: the local copy is newer than S3 by up to one backup interval,
  so overwriting rolls a customer's conversation backwards. The write uses
  ``os.link`` onto the target, which FAILS if the target exists, so
  "never overwrite" is a filesystem guarantee and not a check that a later edit
  can quietly drop.
* **A fetch that fails FAILS THE TURN.** A customer whose conversation appears
  forgotten is worse than an error, and the damage is worse than it first looks:
  the backend would create a fresh transcript holding only this turn, and once a
  durability writer exists it will replace whole objects (that is the write model
  the extracted backup used and the one its replacement must keep), so the next
  backup cycle would overwrite the customer's entire history in S3. Failing the
  turn now keeps that hazard closed before the writer lands. The failure is
  :class:`TranscriptUnavailable`, surfaced with its own error code.
* **Never log a transcript's contents.** The sid and the byte count only.

**The filename, which is the part that was found the hard way.** The object is
``<thread>_<slot>.jsonl``, not ``<slot>.jsonl``, and for this deployment the
thread is always ``dashboard``. Verified in the Kiro Crew wheel this image ships
(``vendor/kirocrew-0.6.0-py3-none-any.whl``):

* ``dashboard/openai_compat.py:257`` takes the turn's ``id`` as the slot id and
  ``:296`` resolves it through ``state.get_or_create_slot``, which normalizes
  with ``dashboard/state.py:_normalize_slot_key``.
* That function's docstring states the invariant a restart depends on:
  ``_safe_key(_history_key_for(key)) == f"dashboard_{key}"``. So the transcript
  stem is the slot key with a ``dashboard_`` prefix, and the file is
  ``<sessions_dir>/dashboard_<slot>.jsonl``.
* ``control/observe.py resolve_open_slots`` documents the same mapping from the
  other side, plus the verbatim form (``weixin_...``) that belongs to a
  channel-born conversation. No channel credentials are supplied to this
  container (``docs/system-specs/modules/aws-control.md``), so every conversation here is a
  dashboard slot and the thread is not ambiguous.

``_normalize_slot_key`` is mirrored rather than imported: the wheel is a vendored
artifact the front process does not import from, and the mapping is three lines.
:func:`transcript_stem` is pinned against the values in those two sources by
``tests/test_front_transcript_fetch.py``.

Archived segments (``sessions/archive/**``) are deliberately NOT fetched: finding
them requires listing, which is the one thing this path may not do. Rotation
keeps the recent window in the live transcript, so a turn continues from the live
object; older segments stay in S3 and remain readable by the owner's control
plane, which has credentials of its own.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
import stat
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..common import Settings
from .slotlock import SlotSerializer

logger = logging.getLogger("smc.front.transcript")

__all__ = [
    "TranscriptReader",
    "S3TranscriptReader",
    "TranscriptUnavailable",
    "TranscriptAbsent",
    "FetchOutcome",
    "transcript_stem",
    "object_key",
    "local_transcript_path",
    "ensure_local_transcript",
    "prepared_turn",
]

# The transport prefix every dashboard conversation's transcript carries. See the
# module docstring for where this is established.
THREAD_PREFIX = "dashboard"

# Mirrors ``dashboard/state.py:_ascii_slot_key`` then the filename fold in
# ``history.py:_safe_key`` (``re.ASCII`` pins ``\w`` to ``[a-zA-Z0-9_]``). Order
# matters: the non-printable pass produces ``-``, which the filename fold keeps.
_NON_PRINTABLE_RE = re.compile(r"[^\x20-\x7e]")
_FILENAME_UNSAFE_RE = re.compile(r"[^\w\-.]", flags=re.ASCII)

# Namespace and key derivation for a transcript object in the S3 bucket. These moved HERE
# from the backup layout module when the backup subsystem was extracted from this PR (the
# durability design is tracked separately). The front's on-demand transcript fetch is
# runtime, not backup, but it READS a key an eventual sidecar will WRITE, so the two must
# derive the same key. Until the durability feature lands there is no writer, so this fetch
# reads an empty bucket -- a capability the container does not yet have, not a regression.
# When backup returns, the writer and this reader must share ONE derivation; the first live
# deployment doubled the crew name in every key because two places each decided the prefix,
# and it survived twelve green gates because writer and reader agreed with each other while
# both disagreed with the contract. The lesson stands regardless of which module owns it.
_TRANSCRIPT_DATA_NS = "data/"


def _object_prefix(settings: Settings) -> str:
    parts = [p for p in (settings.backup_prefix.strip("/"), settings.crew_name.strip("/")) if p]
    return ("/".join(parts) + "/") if parts else ""


def _sessions_prefix(settings: Settings) -> str:
    """Rel-key prefix under which conversation transcripts live."""
    try:
        tail = settings.sessions_dir.relative_to(settings.data_home).as_posix()
    except ValueError:
        tail = settings.sessions_dir.name
    return _TRANSCRIPT_DATA_NS + tail + "/"


def _full_key(settings: Settings, rel_key: str) -> str:
    return _object_prefix(settings) + rel_key


class TranscriptUnavailable(RuntimeError):
    """The transcript could not be obtained, so the turn must not be served.

    Distinct from absence. Absence means the conversation is new; this means we
    do not know whether it is, and answering anyway would present a returning
    customer with an empty conversation and then overwrite their history in S3
    at the next backup cycle.
    """

    code = "transcript_unavailable"


class TranscriptAbsent(Exception):
    """The key is not in the bucket, which for a new conversation is normal.

    A reader may raise this to say so explicitly. It does not have to: a
    botocore-shaped ``NoSuchKey``/404 error is classified the same way by
    :func:`_fetch`, so the real boto3 client needs no wrapper of its own.
    """


# Error codes S3 uses for "that key is not here". Anything else, ``AccessDenied``
# included, is a FAILURE: absence and denial are different answers and only one
# of them may let the turn proceed.
_ABSENT_CODES = frozenset({"NoSuchKey", "404", "NotFound"})


@runtime_checkable
class TranscriptReader(Protocol):
    """Read ONE object by key. Deliberately the whole surface.

    There is no ``list`` and no ``put``. The isolation property this change
    exists for dies at the first list, and the turn path must never be a writer,
    so neither operation is reachable from it even by mistake. ``get`` returns
    the whole object, or raises: either
    :class:`TranscriptAbsent`, or whatever the client raised, which
    :func:`_fetch` classifies.
    """

    def get(self, key: str) -> bytes: ...


class S3TranscriptReader:
    """The real reader: ``GetObject``, one key, read-only.

    boto3 is imported and the client built lazily so the front process is
    importable, and every existing test runnable, with no AWS present.

    It deliberately does NOT classify its own errors. Absence policy lives in
    one place (:func:`_fetch`) so the fake used in tests and the real client are
    judged by the same rule, rather than the rule being exercised only through
    boto3-shaped exceptions the tests would have to imitate here.
    """

    def __init__(self, bucket: str, *, client=None) -> None:
        self._bucket = bucket
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            import boto3  # local import: keep the front process AWS-free to import

            self._client = boto3.client("s3")
        return self._client

    def get(self, key: str) -> bytes:
        """Fetch one object, bounded twice.

        ``ContentLength`` is a CLAIM by the source, so it is checked and then not
        trusted: an object that declares itself too large is refused before a byte is
        read, and the streaming read enforces the same ceiling on what actually arrives.
        A header is not a bound, which is the same reason the request-body limit on the
        turn route does not trust ``Content-Length`` either.
        """
        resp = self._ensure_client().get_object(Bucket=self._bucket, Key=key)
        declared = resp.get("ContentLength")
        if isinstance(declared, int) and declared > MAX_TRANSCRIPT_BYTES:
            raise TranscriptTooLarge(
                f"the stored transcript {key} declares {declared} bytes, above the "
                f"{MAX_TRANSCRIPT_BYTES}-byte ceiling, and was not read."
            )
        return _read_bounded(resp["Body"], key)


#: Ceiling on a transcript restored from the bucket.
#:
#: The bytes come from outside the container and are held IN MEMORY for the length of a
#: turn, so without a ceiling one object decides how much memory this process uses. 64
#: MiB is far above any real transcript (a conversation is JSONL text) and far below the
#: task's memory, so it separates "a big conversation" from "an object that should not be
#: read at all" without needing to know which conversations exist.
MAX_TRANSCRIPT_BYTES: int = 64 * 1024 * 1024

#: How much is read per chunk while enforcing that ceiling. Small enough that the
#: overshoot before the refusal is bounded by this rather than by the object's size.
_READ_CHUNK_BYTES: int = 1024 * 1024


class TranscriptTooLarge(TranscriptUnavailable):
    """The stored object is larger than a transcript is allowed to be."""


def _read_bounded(body, key: str, *, limit: int = MAX_TRANSCRIPT_BYTES) -> bytes:
    """Read at most *limit* bytes from an S3 body, refusing an object that exceeds it.

    Streamed in chunks rather than ``body.read()``: a single read materialises whatever
    the object happens to be, so the ceiling would be enforced after the memory was
    already committed. Reading one chunk PAST the limit is what makes the refusal
    decidable -- stopping exactly at the limit cannot tell a file of exactly that size
    from a larger one.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = body.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise TranscriptTooLarge(
                f"the stored transcript {key} exceeds {limit} bytes and was not read. It "
                "is held in memory for the length of a turn, so an object this size is "
                "refused rather than loaded."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _error_code(exc: Exception) -> str:
    """The S3 error code of a botocore ClientError, or ``""``.

    Read off the response dict rather than the exception type so a stub client in
    a test can produce a genuine absence without botocore installed.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if isinstance(code, str):
                return code
        status = response.get("ResponseMetadata")
        if isinstance(status, dict) and status.get("HTTPStatusCode") == 404:
            return "404"
    return ""


def _sid_of(key: str) -> str:
    """The sid a key names, for a log line. Never the key's contents."""
    tail = key.rsplit("/", 1)[-1]
    return tail[: -len(".jsonl")] if tail.endswith(".jsonl") else tail


# --- naming ---------------------------------------------------------------


def is_fetchable_slot_id(slot_id: str) -> bool:
    """True when this id is worth spending an S3 GET on.

    The front proxies a turn; the BACKEND decides whether an id is legal. That
    ordering leaves a hole, because the fetch happens first: an id the backend
    goes on to reject can still name a real conversation once folded, so the
    task ends up holding a conversation it never served, which is precisely the
    property this change exists to establish. ``id="dashboard:cust-1"`` is the
    demonstrated case. The backend's own grammar
    (``kiro_crew.session_storage._UNIT_ID_RE``, ``[A-Za-z0-9_][A-Za-z0-9._-]*``)
    has no colon in it, yet the fold turns that string into ``dashboard_cust-1``
    and downloads someone's transcript.

    The test is deliberately a SHAPE test and not a copy of that grammar, which
    lives in a dependency this process does not import. Every character the
    backend accepts is a character the sanitizer leaves alone, so "the sanitizer
    changed nothing" admits every legal id and excludes the folded spellings.
    Being wrong in the permissive direction is the only safe way to be wrong
    here: skipping a fetch for an id the backend ACCEPTS would serve an empty
    history, and the sidecar's whole-object put would then overwrite that
    customer's real history in S3. Skipping one it REJECTS costs nothing, since
    a refused turn never reaches the session store.
    """
    body = slot_id
    while body.startswith(THREAD_PREFIX + "_"):
        body = body[len(THREAD_PREFIX) + 1 :]
    if not body:
        return False
    return _FILENAME_UNSAFE_RE.sub("_", _NON_PRINTABLE_RE.sub("-", body)) == body


def normalize_slot_key(slot_id: str) -> str:
    """Fold a turn's ``id`` exactly as the backend folds it into a slot key.

    Mirrors ``dashboard/state.py:_normalize_slot_key``. The prefix stripping is
    the part that matters here: ``id="dashboard_cust-1"`` reaches the SAME slot,
    and therefore the same transcript, as ``id="cust-1"``, so a fetch that
    skipped this step would miss the object for one of the two spellings and
    hand that caller an empty conversation.
    """
    if slot_id.startswith(THREAD_PREFIX + ":"):
        slot_id = slot_id[len(THREAD_PREFIX) + 1 :]
    while slot_id.startswith(THREAD_PREFIX + "_"):
        slot_id = slot_id[len(THREAD_PREFIX) + 1 :]
    return _FILENAME_UNSAFE_RE.sub("_", _NON_PRINTABLE_RE.sub("-", slot_id))


def transcript_stem(slot_id: str) -> str:
    """The transcript's filename stem, or ``""`` when the id names nothing.

    ``"cust-8831"`` -> ``"dashboard_cust-8831"``, whose file is
    ``dashboard_cust-8831.jsonl``. NOT ``cust-8831.jsonl``: see the module
    docstring for the two independent sources that establish the prefix.
    """
    normalized = normalize_slot_key(slot_id)
    if not normalized:
        return ""
    return f"{THREAD_PREFIX}_{normalized}"


def object_key(settings: Settings, stem: str) -> str:
    """The full S3 key of a transcript, matching what a sidecar would write.

    Derived from THIS module's own key helpers now that backup is extracted. When the
    durability feature returns, its writer and this reader must derive the same key from a
    single shared definition, because drift here is invisible: the fetch would simply miss,
    and a customer whose history was not found is indistinguishable from a new one.

    That is not hypothetical. The first live deployment doubled the crew name in every key
    (``crews/<crew>/<crew>/``) because two places each decided one prefix, and it survived
    twelve green gates because writer and reader agreed with each other while both disagreed
    with the contract.
    """
    return _full_key(settings, f"{_sessions_prefix(settings)}{stem}.jsonl")


#: Longest single filename this build will construct for a transcript. 255 is the POSIX
#: ``NAME_MAX`` on ext4/xfs and the limit NTFS applies per component too, so it is the
#: bound that actually exists rather than one chosen for neatness. Measured on the build
#: host with ``getconf NAME_MAX /``.
_MAX_STEM_CHARS = 255


def local_transcript_path(settings: Settings, stem: str) -> Path | None:
    """Where the transcript belongs on disk, or None if the stem escapes it.

    A slot id is untrusted input. The fold in :func:`normalize_slot_key` already
    turns ``/`` into ``_``, so traversal is not reachable, but the containment is
    asserted rather than assumed: the cost is one comparison and the failure it
    guards is a write outside the data home.
    """
    # Length is checked HERE, beside the containment check, because this function is the
    # one place that decides whether a stem maps to a usable path -- and it does so with
    # string operations only. A stem longer than the filesystem allows therefore passes
    # this function and raises ``OSError(ENAMETOOLONG)`` at the caller's first ``exists()``
    # instead, which reads as "the disk is broken" rather than "that id is not valid".
    #
    # ``_MAX_STEM_CHARS`` is derived from the measured ``NAME_MAX`` rather than a number
    # quoted from the backend: no length limit was found in the backend's own persistence
    # code, so the real bound is what the filesystem will accept for the ``.jsonl`` name.
    if len(stem) + len(".jsonl") > _MAX_STEM_CHARS:
        return None
    candidate = settings.sessions_dir / f"{stem}.jsonl"
    root = os.path.normpath(str(settings.sessions_dir))
    resolved = os.path.normpath(str(candidate))
    if not resolved.startswith(root + os.sep):
        return None
    return candidate


# --- the fetch ------------------------------------------------------------


@dataclass(frozen=True)
class FetchOutcome:
    """What the fetch did. Returned for logging and tests, never to the customer.

    ``action`` is one of:

    * ``"no_slot"``  the turn names no conversation, so there is nothing to fetch
    * ``"no_store"`` no bucket is configured, so there is nothing to fetch from
    * ``"not_a_slot_id"`` the backend will refuse this id, so fetching would put
      a conversation this task never serves on its disk
    * ``"present"``  already on disk: this task served it, keep the newer copy
    * ``"fetched"``  restored from S3
    * ``"absent"``   not in S3: a new conversation, which is normal
    """

    action: str
    stem: str = ""
    bytes_written: int = 0


def _write_without_clobbering(path: Path, data: bytes) -> None:
    """Create ``path`` with ``data``, failing if it already exists.

    Three properties, all load-bearing:

    * **Atomic.** The bytes land in a temp file in the same directory, are
      fsynced, and then appear at the target under one name. A crash cannot
      leave a truncated transcript for the backend to append to and the sidecar
      to upload.
    * **Never an overwrite.** ``os.link`` refuses an existing target, so the
      do-not-overwrite rule holds even against a file that appeared in the
      window since the caller looked. An existing target is not an error here:
      whoever created it has the newer copy, which is exactly what the rule
      protects. It also refuses a symlink at the target without following it, so
      a link planted there cannot receive a customer's conversation.
    * **Into a real directory.** The sessions directory is checked by ``lstat``
      before it is created or written into, because ``mkdir(exist_ok=True)``
      succeeds on a symlink to a directory and every write then lands wherever
      that link points.
    """
    parent = path.parent
    if parent.is_symlink():
        raise TranscriptUnavailable(
            f"the sessions directory is a symlink: {parent}. Writing a transcript "
            "through it would put a customer's conversation outside the data home. "
            "Refusing the turn."
        )
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # A regular file where the sessions directory belongs fails EVERY turn, so it is
        # answered as a refusal naming the path rather than as whatever ``mkdir`` raises.
        # The supervisor makes the same call about the directories it creates at boot.
        raise TranscriptUnavailable(
            f"the sessions directory could not be created at {parent} ({exc})."
        ) from exc
    fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=".smc-fetch-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, str(path))
        except FileExistsError:
            # Something arrived at this path while the fetch was in flight, so the file
            # the turn will use is NOT the one just written and has had none of this
            # module's checks applied to it. Keeping the copy on disk is still right --
            # whoever created it has the newer history -- but only if it is a
            # transcript at all, so the entry that won is validated the same way the
            # pre-fetch check validates one, through the same helper rather than a
            # second check that could drift from it. A shape that fails raises, which
            # fails this turn.
            _probe_local_entry(path)
            logger.info(
                "transcript fetch: sid=%s appeared while fetching; keeping the "
                "copy on disk, which is the newer one",
                _sid_of(path.name),
            )
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


#: Flags for a shape probe on a local transcript entry.
#:
#: ``O_NOFOLLOW`` refuses a symlink at the final component, and ``O_NONBLOCK`` is what
#: keeps a FIFO from turning the probe into a hang: opening a pipe for reading blocks
#: until a writer arrives, and an entry planted as a FIFO would otherwise stall the
#: turn indefinitely rather than be refused.
_PROBE_FLAGS: int = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _probe_local_entry(path: Path) -> bool:
    """Is this slot's transcript already a real file on disk?

    ``True`` it is a regular file with one link, ``False`` nothing is there. Any other
    shape RAISES :class:`TranscriptUnavailable` naming the path, because the turn must
    not proceed on an entry the backend is about to open and append to.

    Shape is decided on the DESCRIPTOR, never on the name. ``Path.exists()`` follows a
    symlink, answers true for a directory, a FIFO or a socket, and is a separate
    operation from the open that follows it -- so a check by name plus an open by name
    is two resolutions of one path with a window in between, which is the gap every
    finding in this series has lived in. Mirrors ``hooks.safe_read_file_bytes_nolink``:
    open first with the link refused, then ``fstat`` the descriptor, so the entry
    validated is exactly the entry that was opened. The helper cannot be imported here
    -- ``kiro_crew`` is not importable inside the image -- so the shape is mirrored,
    the way ``supervisor/bundle.py`` mirrors the agents-dir resolver.

    ``st_nlink > 1`` is refused for a reason the file type does not cover: a hard link
    is a regular file, and the backend appends to this path, so a second name for the
    same inode makes that append write into whatever else holds it.

    Nothing here reads the file. This process only decides whether the backend may,
    and the refusals are the shapes an operator has to know about.
    """
    try:
        fd = os.open(str(path), _PROBE_FLAGS)
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise TranscriptUnavailable(
                f"the transcript path is a symlink, not a file: {path}. The backend "
                "appends to it, so a link there would redirect a conversation's history "
                "somewhere else. Refusing the turn rather than writing through it."
            ) from exc
        raise TranscriptUnavailable(
            f"the transcript path could not be opened to check its shape: {path} ({exc})."
        ) from exc
    try:
        st = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(st.st_mode):
        raise TranscriptUnavailable(
            f"the transcript path is not a regular file: {path} (mode {st.st_mode:#o}). "
            "A directory, socket or FIFO there is not a conversation this task can "
            "serve, so the turn is refused rather than handed to the backend."
        )
    if st.st_nlink > 1:
        raise TranscriptUnavailable(
            f"the transcript file has {st.st_nlink} links: {path}. The backend appends "
            "to it, so another name for the same inode would receive a customer's "
            "conversation. Refusing the turn."
        )
    return True


def _fetch(reader: TranscriptReader, key: str) -> bytes | None:
    """Blocking read of one key. None means absent. Runs off the event loop.

    The one place absence is decided. ``AccessDenied`` is NOT absence: reading a
    denial as "new conversation" is the silent route to serving an empty history
    and then overwriting the real one at the next backup cycle.

    Residual risk, named because it cannot be settled from here: S3 answers a
    missing key with 403 rather than 404 when the caller lacks ``s3:ListBucket``
    for it, and the task role's grant carries an ``s3:prefix`` condition
    (the deploy track's template narrows it to the task's own prefix) whose effect on that
    choice needs a real bucket to establish. If it does answer 403, a brand new
    conversation's FIRST turn fails closed here instead of starting fresh. That
    is the fail direction the contract asks for, and the fix belongs to the
    deploy track, not to a looser rule here.
    """
    try:
        return reader.get(key)
    except TranscriptAbsent:
        return None
    except Exception as exc:  # noqa: BLE001 - classified, never swallowed
        if _error_code(exc) in _ABSENT_CODES:
            return None
        raise TranscriptUnavailable(f"GetObject failed for sid {_sid_of(key)}") from exc


async def ensure_local_transcript(
    settings: Settings,
    slot_id: str,
    *,
    reader: TranscriptReader | None,
) -> FetchOutcome:
    """Make sure this slot's transcript is on disk. Call inside the slot lock.

    Raises :class:`TranscriptUnavailable` when the object exists as far as we
    know but could not be read. Every other outcome lets the turn proceed.
    """
    stem = transcript_stem(slot_id)
    if not stem:
        return FetchOutcome("no_slot")

    if not is_fetchable_slot_id(slot_id):
        # The backend will refuse this id. Folding it would still name a real
        # conversation, so fetching first would put someone else's history on
        # this task's disk for a turn that is about to be rejected. Let the
        # backend do the refusing; a refused turn writes nothing, so there is no
        # empty-history hazard in declining to fetch here.
        logger.info("transcript fetch: id is not a slot id; not fetching")
        return FetchOutcome("not_a_slot_id", stem)

    path = local_transcript_path(settings, stem)
    if path is None:
        # Unreachable through the backend's own id validation, and fail-closed
        # rather than dropped: we cannot promise the conversation's history.
        raise TranscriptUnavailable(f"slot id does not map into the sessions dir: {stem!r}")

    if _probe_local_entry(path):
        # This task already served this conversation. The local copy leads S3 by
        # up to one backup interval, so re-fetching could only lose turns.
        logger.debug("transcript fetch: sid=%s already on disk; not re-fetching", stem)
        return FetchOutcome("present", stem)

    if reader is None:
        # No bucket configured. Nothing was ever uploaded, so nothing is missing:
        # this is a crew running without durability, not a failure to restore.
        # The warning is emitted once at startup, not once per turn.
        return FetchOutcome("no_store", stem)

    key = object_key(settings, stem)
    # boto3 is blocking. A multi-megabyte GET on the event loop would stall every
    # OTHER conversation's turn, so it runs in a thread.
    data = await asyncio.to_thread(_fetch, reader, key)
    if data is None:
        logger.info("transcript fetch: sid=%s not in S3; treating as a new conversation", stem)
        return FetchOutcome("absent", stem)

    # The write is offloaded for exactly the reason the fetch above is: this is the
    # same multi-megabyte payload, and `_write_without_clobbering` fsyncs it, which
    # is the slowest thing a filesystem does. Leaving it inline would have made the
    # thread on the previous line pointless -- the loop would stall on the write it
    # was just spared on the read.
    await asyncio.to_thread(_write_without_clobbering, path, data)
    logger.info("transcript fetch: sid=%s restored %d B", stem, len(data))
    return FetchOutcome("fetched", stem, len(data))


@asynccontextmanager
async def prepared_turn(
    serializer: SlotSerializer,
    settings: Settings,
    slot_id: str,
    reader: TranscriptReader | None,
):
    """Hold the slot for this turn, with its transcript present.

    The two transports differ in WHERE they enter this scope, not in what it
    does: the non-streamed turn enters it in the request handler, and the
    streamed turn enters it inside its generator, because an SSE response must
    keep the slot for the life of the stream. Both enter the same object, so the
    fetch happens inside the per-slot lock on both paths and the ordering exists
    in one place. Writing the fetch into each transport instead would leave two
    copies of "before the body reaches the backend" to keep in agreement.
    Both the lock and the fetch are keyed on the CANONICAL slot, not the string
    the caller sent. ``cust-1``, ``dashboard_cust-1`` and ``dashboard:cust-1`` all
    name one conversation and resolve to one transcript, so locking on the raw id
    would hand two spellings two different locks and let them run at once, on the
    same file, while the serializer reported one turn per slot.
    """
    canonical = normalize_slot_key(slot_id)
    async with serializer.for_slot(canonical or slot_id):
        await ensure_local_transcript(settings, slot_id, reader=reader)
        yield
