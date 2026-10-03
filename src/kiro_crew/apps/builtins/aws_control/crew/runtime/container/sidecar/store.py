"""The bucket, as the two narrowest operations the durability pair needs.

``put`` and ``get``, and nothing else. There is no ``list`` and no ``delete``, for
the same reason the front's reader has neither: a capability that exists is a
capability a later edit can reach for, and both of those change what the pair
means. A ``list`` on the restore side turns "bring back this task's own authority
files" into "enumerate the bucket", and a ``delete`` puts retention -- deciding
that a customer's history may go -- inside the process whose job is to keep it.

## Why ``put`` takes a descriptor and a size

Both halves of the consistency property live in that signature. The caller opens
the file ONCE and hands over the open descriptor, so the bytes uploaded are the
bytes that descriptor addresses, whatever later happens to the name. And the size
is the caller's, measured on the same descriptor at the same moment, so the upload
sends exactly the length that was true then.

Together they make a coherent copy without a lock and without a staged duplicate:

* The backend publishes a transcript with a temporary file and a rename, so it
  never writes into the bytes behind an open descriptor -- it swaps the directory
  entry to a different inode. A descriptor opened before the swap therefore keeps
  addressing a whole, finished version.
* A file that is instead appended to grows behind the descriptor. Sending exactly
  the recorded length uploads the prefix that existed at open time, which is a
  version that was really on disk. The next cycle sees a newer size and sends the
  longer one.

Neither case copies the file first, so there is nothing to bound: the upload spends
one descriptor and one fixed buffer, not a second copy of the data.

## What is NOT here

Absence, permanence and the read ceiling. Those are in ``common/objects.py``, shared
with the front's reader, because the first version kept a copy in each process and the
copies disagreed about a missing bucket within one revision. This module is the two
requests and nothing else.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import BinaryIO, Protocol, runtime_checkable

from ..common.config import BACKUP_MAX_ATTEMPTS, BACKUP_REQUEST_TIMEOUT_SECS
from ..common.objects import (
    GET_CHUNK_BYTES,
    BoundedReader,
    ObjectTooLarge,
    PreconditionFailed,
    StoreUnusable,
    UploadCancelled,
    UploadCut,
    UploadDeadlineExceeded,
    classify_permanent,
    is_absent,
    is_precondition_failed,
    read_bounded,
)

__all__ = [
    "ObjectStore",
    "ObjectAbsent",
    "ObjectTooLarge",
    "PreconditionFailed",
    "StoreUnusable",
    "UploadCut",
    "UploadCancelled",
    "UploadDeadlineExceeded",
    "S3ObjectStore",
    "BoundedReader",
    "read_bounded",
    "classify_permanent",
    "GET_CHUNK_BYTES",
]


class ObjectAbsent(Exception):
    """The key is not in the bucket.

    Distinct from a failure to read it. On a task's first boot the authority objects
    are genuinely not there yet, and that has to be told apart from a denial: reading
    a denial as absence is how a task boots with an empty slot table and then
    overwrites the real one.
    """


@runtime_checkable
class ObjectStore(Protocol):
    """Put one object from a descriptor; get one object by key.

    ``budget`` is optional on both sides of the protocol: a caller with a window to
    protect passes the seconds it set aside for this object, and a caller with none
    passes nothing. An implementation that does not transmit anything may ignore it.

    ``cancel`` is likewise optional: a caller that can be told to stop mid-upload passes
    a predicate the transmission asks between chunks. The two are complementary -- a
    caller with a window bounds the upload by time, a caller without one bounds it by the
    stop -- so an implementation that transmits should honour whichever it is given.
    """

    def put(
        self,
        key: str,
        body: BinaryIO,
        size: int,
        *,
        budget: float | None = None,
        cancel: Callable[[], bool] | None = None,
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> str | None: ...

    def get(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> bytes: ...

    def get_with_etag(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> tuple[bytes, str | None]: ...


class S3ObjectStore:
    """The real store: ``PutObject`` and ``GetObject``, one key at a time.

    boto3 is imported and the client built lazily, so this module is importable, and
    every test runnable, with no AWS present.
    """

    def __init__(self, bucket: str, *, client=None) -> None:
        self._bucket = bucket
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            import boto3  # local import: keep the package importable without AWS
            from botocore.config import Config

            # Bounded on purpose. The final cycle runs inside the supervisor's drain
            # window, so a request that waits on boto3's minutes-long defaults would be
            # SIGKILLed mid-upload -- the window would exist and the cycle would still
            # not finish. These numbers are what that window is sized against.
            #
            # ``total_max_attempts``, not ``max_attempts``: the latter is a count of
            # RETRIES, which botocore normalises by adding one for the initial request, so
            # the client would make one more attempt than the budget derived from the same
            # constant sets aside. This key is the total, so the constant means what the
            # budget reads it as.
            self._client = boto3.client(
                "s3",
                config=Config(
                    connect_timeout=BACKUP_REQUEST_TIMEOUT_SECS,
                    read_timeout=BACKUP_REQUEST_TIMEOUT_SECS,
                    retries={
                        "total_max_attempts": BACKUP_MAX_ATTEMPTS,
                        "mode": "standard",
                    },
                ),
            )
        return self._client

    def put(
        self,
        key: str,
        body: BinaryIO,
        size: int,
        *,
        budget: float | None = None,
        cancel: Callable[[], bool] | None = None,
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> str | None:
        """Replace the object at *key* with *size* bytes read from *body*.

        Returns the ETag the write LEFT on the object, taken from the ``PutObject`` response
        itself -- the one request that is known to have committed these exact bytes. A caller
        that needs a compare-and-swap validator for a LATER conditional write against this same
        write (the ownership claim, whose abort path must revert only while its own write is
        still the committed one) takes it from here rather than from a follow-up GET: a GET
        after the PUT can fail or time out on a degraded link even though the PUT already
        committed, and it can also read a NEWER writer's ETag if one committed in the window --
        either way binding the validator to something other than this write. The response ETag
        has neither hazard. ``None`` when the response carried no ETag (then the caller must
        treat the validator as unavailable and not attempt a conditional revert).

        ``ContentLength`` is passed explicitly and the body is wrapped, so the length
        declared to S3 and the length actually sent are the same number and both are
        the one the caller measured. Without the wrapper a file that grew during the
        upload would send more bytes than the header promised; without the header the
        transport would buffer to find the length, which is the staged copy this
        design exists to avoid.

        *budget* is seconds this whole REQUEST may take, and the body is given the part of it
        that the body can enforce. The client's ``connect_timeout`` and ``read_timeout`` bound
        a single connect and a single read; a connection delivering small chunks under the read
        timeout trips neither however long the object takes, so without this the only thing
        that ends a slow upload is the drain window's SIGKILL -- which arrives in the middle of
        a PUT, losing that object and saying nothing about the ones after it. With it the
        upload is cut while it is still progressing and the caller records exactly what did
        not go.

        The body's share is the budget less one request timeout, because after the body is
        drained the transport waits for the RESPONSE and never asks the body again. Handing the
        body the whole budget would let the request spend that wait on top of it, so the gate
        would reserve one number and the PUT would spend it plus a timeout -- the overrun the
        reservation exists to make impossible.

        *cancel* is what bounds the upload a caller deliberately gave no budget. Asked
        between chunks by the same body, so a stop arriving mid-transmission ends the
        request instead of being noticed after it: the caller whose next act is to hand a
        shorter window to a more important cycle cannot wait out an upload that has no
        window at all.

        A permanent failure -- a denial, a bucket that is not there -- is raised as
        :class:`StoreUnusable` rather than as itself, so the caller is not handed a
        fault its retry can never resolve. Neither cut is permanent: the bucket answered
        and the transport worked, so each leaves as its own :class:`UploadCut` and the
        next cycle attempts the object again.
        """
        reader = BoundedReader(
            body,
            size,
            deadline=(
                None
                if budget is None
                else time.monotonic() + max(0.0, budget - BACKUP_REQUEST_TIMEOUT_SECS)
            ),
            cancel=cancel,
        )
        # Conditional headers make the write a compare-and-swap: ``If-Match`` commits only
        # if the object still carries the ETag the caller read, and ``If-None-Match: *``
        # commits only if the object does not exist yet. S3 answers a defeated precondition
        # with 412, which is surfaced as :class:`PreconditionFailed` -- a concurrent writer
        # got there first, a verdict for the caller, not a retryable transport fault.
        extra: dict[str, str] = {}
        if if_match is not None:
            extra["IfMatch"] = if_match
        if if_none_match is not None:
            extra["IfNoneMatch"] = if_none_match
        try:
            resp = self._ensure_client().put_object(
                Bucket=self._bucket,
                Key=key,
                Body=reader,
                ContentLength=size,
                **extra,
            )
        except UploadCut:
            raise
        except Exception as exc:  # noqa: BLE001 - classified, never swallowed
            # A defeated precondition is a lost race, not a broken transport: raised as its
            # own class before the cut/permanent classification so the commit path can record
            # it and step aside rather than retry into the same answer.
            if is_precondition_failed(exc):
                raise PreconditionFailed(
                    f"PutObject on {key} was rejected by its precondition: another writer "
                    "changed the object since it was read"
                ) from exc
            # Asked of the reader rather than of the exception, because the transport
            # owns what surfaces from a body that raised mid-send: botocore may re-raise
            # it, wrap it in a connection error, or convert it into a retry that then
            # fails on its own. The reader's own state is the fact, so a cut upload is
            # reported as the cut it was whatever shape the failure arrived in -- and
            # never classified permanent, which would end the process over a slow network
            # or an ordinary shutdown.
            if reader.cancelled():
                raise UploadCancelled(
                    f"PutObject on {key} was cut by a stop while it was in flight"
                ) from exc
            if reader.expired():
                raise UploadDeadlineExceeded(
                    f"PutObject on {key} did not finish inside its {budget:.0f}s budget"
                ) from exc
            classify_permanent(exc, bucket=self._bucket, key=key, verb="PutObject on")
            raise
        etag = resp.get("ETag")
        return etag if isinstance(etag, str) else None

    def get(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        """Fetch one object, bounded twice, or raise :class:`ObjectAbsent`."""
        raw, _etag = self.get_with_etag(key, limit=limit, deadline=deadline, cancel=cancel)
        return raw

    def get_with_etag(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> tuple[bytes, str | None]:
        """Fetch one object AND its ETag in ONE GET, or raise :class:`ObjectAbsent`.

        The ETag rides the same ``get_object`` response as the bytes, so a caller that needs
        both -- the generation pointer read, whose ETag is the commit's compare-and-swap
        validator -- gets a coherent (bytes, validator) pair from a single request. A separate
        HEAD after the GET is worse two ways: a concurrent write can land between them so the
        validator describes a pointer this read never saw (a stale ``If-Match`` then passes),
        and it is an extra request the final cycle's drain window is not budgeted for.

        ``ContentLength`` is a CLAIM by the source, so it is checked and then not trusted: an
        object that declares itself too large is refused before a byte is read, and the
        streaming read enforces the same ceiling on what actually arrives. A header is not a
        bound.

        *deadline* is a ``time.monotonic`` reading after which the streaming read raises
        :class:`UploadDeadlineExceeded` instead of waiting on another chunk. The final cycle's
        two preflight GETs -- the pointer and its transcript index -- run inside the
        supervisor's drain window, and the socket's 5s ``read_timeout`` a chunk-trickling
        connection never trips cannot bound them: a multi-megabyte index on a merely degraded
        link would otherwise outlive the window and be SIGKILLed mid-read, so the final commit
        never happens and the flushed turns are lost. Wrapping the body in a deadline-bearing
        :class:`BoundedReader` cuts the read the moment the window is spent -- a verdict the
        caller records, not a kill.

        *cancel* is the predicate the body asks between chunks; once it answers True the read
        raises :class:`UploadCancelled`. It is what bounds an INTERVAL cycle's preflight GETs,
        which have no deadline (they have a next interval) but must still be cut the instant a
        stop arrives: a multi-megabyte transcript index trickling in at a few KiB/s during an
        interval read would otherwise keep reading across the SIGTERM and eat the drain window
        the final cycle needs, and the socket's read_timeout never trips on a connection that
        delivers small chunks. With the stop threaded here the interval read ends at the stop
        and the final cycle begins inside the window. An interval cycle passes the stop and no
        deadline; the final cycle passes the deadline and no stop.
        """
        try:
            resp = self._ensure_client().get_object(Bucket=self._bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - classified, never swallowed
            if is_absent(exc):
                raise ObjectAbsent(key) from exc
            classify_permanent(exc, bucket=self._bucket, key=key, verb="GetObject on")
            raise
        declared = resp.get("ContentLength")
        if isinstance(declared, int) and declared > limit:
            raise ObjectTooLarge(
                f"the stored object {key} declares {declared} bytes, above the "
                f"{limit}-byte ceiling, and was not read."
            )
        etag = resp.get("ETag")
        # ``limit + 1`` as the reader's own ceiling: the deadline/abort gate rides on
        # :class:`BoundedReader`, but its byte limit would SILENTLY truncate at *limit*
        # whereas :func:`read_bounded` must still see the overflow byte to raise
        # :class:`ObjectTooLarge`. One extra byte lets the real ceiling check stay with
        # ``read_bounded`` while the reader contributes only the deadline/cancel cut.
        body = (
            resp["Body"]
            if deadline is None and cancel is None
            else BoundedReader(resp["Body"], limit + 1, deadline=deadline, cancel=cancel)
        )
        raw = read_bounded(body, key, limit=limit, what="object")
        return raw, (etag if isinstance(etag, str) else None)
