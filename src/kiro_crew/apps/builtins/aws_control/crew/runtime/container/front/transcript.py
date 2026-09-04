"""Putting ONE conversation's transcript on disk, at the turn that needs it.

Boot no longer downloads every transcript the crew has ever had (see
``EPHEMERAL-CONTRACT.md``): a task starts with the two authority files and no
conversations at all. This module is the other half of that change. Before a
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
  the backend would create a fresh transcript holding only this turn, and the
  sidecar replaces whole objects (``container/backup/store.py`` ``put``), so the
  next backup cycle would overwrite the customer's entire history in S3. The
  failure is :class:`TranscriptUnavailable`, surfaced with its own error code.
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
  container (``container/CONTRACT.md``), so every conversation here is a
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
import logging
import os
import re
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..backup.layout import full_key, sessions_prefix
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

# Namespace of a transcript inside the backup layout. The two namespaces
# (``data/`` and ``config/``) and the ``<backup_prefix>/<crew_name>/`` join are
# defined by ``container/backup/layout.py``, which belongs to the backup track and
# is not ours to import: the container contract keeps track internals private, and
# that module is under active change.
#
# So the rule is mirrored the way that module derives it (relative to the data
# home, not as a hardcoded ``data/sessions/``) and pinned against its public
# functions by ``test_key_agrees_with_the_backup_layout``. A drift is then a red
# test in this track's suite rather than a fetch that silently misses every
# object, which is the failure mode a second hardcoded copy would produce.


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
    exists for dies at the first list, and the sidecar must remain the only
    writer, so neither operation is reachable from the turn path even by
    mistake. ``get`` returns the whole object, or raises: either
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
        resp = self._ensure_client().get_object(Bucket=self._bucket, Key=key)
        return resp["Body"].read()


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
    """The full S3 key of a transcript, matching what the sidecar writes.

    Both halves come from the sidecar's own layout module and are deliberately
    NOT re-derived here. This code is the READER of a key the sidecar WRITES, so
    a second copy of either the prefix or the sessions namespace would be free to
    drift, and drift here is invisible: the fetch would simply miss, and a
    customer whose history was not found is indistinguishable from a new one.

    That is not hypothetical. The first live deployment doubled the crew name in
    every key (``crews/<crew>/<crew>/``) because two places decided one prefix,
    and it survived twelve green gates because writer and reader agreed with each
    other while both disagreed with the contract.
    """
    return full_key(settings, f"{sessions_prefix(settings)}{stem}.jsonl")


def local_transcript_path(settings: Settings, stem: str) -> Path | None:
    """Where the transcript belongs on disk, or None if the stem escapes it.

    A slot id is untrusted input. The fold in :func:`normalize_slot_key` already
    turns ``/`` into ``_``, so traversal is not reachable, but the containment is
    asserted rather than assumed: the cost is one comparison and the failure it
    guards is a write outside the data home.
    """
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

    Two properties, both load-bearing:

    * **Atomic.** The bytes land in a temp file in the same directory, are
      fsynced, and then appear at the target under one name. A crash cannot
      leave a truncated transcript for the backend to append to and the sidecar
      to upload.
    * **Never an overwrite.** ``os.link`` refuses an existing target, so the
      do-not-overwrite rule holds even against a file that appeared in the
      window since the caller looked. An existing target is not an error here:
      whoever created it has the newer copy, which is exactly what the rule
      protects.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".smc-fetch-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, str(path))
        except FileExistsError:
            logger.info(
                "transcript fetch: sid=%s appeared while fetching; keeping the "
                "copy on disk, which is the newer one",
                _sid_of(path.name),
            )
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _fetch(reader: TranscriptReader, key: str) -> bytes | None:
    """Blocking read of one key. None means absent. Runs off the event loop.

    The one place absence is decided. ``AccessDenied`` is NOT absence: reading a
    denial as "new conversation" is the silent route to serving an empty history
    and then overwriting the real one at the next backup cycle.

    Residual risk, named because it cannot be settled from here: S3 answers a
    missing key with 403 rather than 404 when the caller lacks ``s3:ListBucket``
    for it, and the task role's grant carries an ``s3:prefix`` condition
    (``deploy/templates/crew.yaml`` ``ListOwnPrefixOnly``) whose effect on that
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

    if path.exists():
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
