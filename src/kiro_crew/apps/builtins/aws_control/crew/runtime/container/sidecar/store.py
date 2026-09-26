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

from typing import BinaryIO, Protocol, runtime_checkable

from ..common.config import BACKUP_MAX_ATTEMPTS, BACKUP_REQUEST_TIMEOUT_SECS
from ..common.objects import (
    GET_CHUNK_BYTES,
    BoundedReader,
    ObjectTooLarge,
    StoreUnusable,
    classify_permanent,
    is_absent,
    read_bounded,
)

__all__ = [
    "ObjectStore",
    "ObjectAbsent",
    "ObjectTooLarge",
    "StoreUnusable",
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
    """Put one object from a descriptor; get one object by key."""

    def put(self, key: str, body: BinaryIO, size: int) -> None: ...

    def get(self, key: str, *, limit: int) -> bytes: ...


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

    def put(self, key: str, body: BinaryIO, size: int) -> None:
        """Replace the object at *key* with *size* bytes read from *body*.

        ``ContentLength`` is passed explicitly and the body is wrapped, so the length
        declared to S3 and the length actually sent are the same number and both are
        the one the caller measured. Without the wrapper a file that grew during the
        upload would send more bytes than the header promised; without the header the
        transport would buffer to find the length, which is the staged copy this
        design exists to avoid.

        A permanent failure -- a denial, a bucket that is not there -- is raised as
        :class:`StoreUnusable` rather than as itself, so the caller is not handed a
        fault its retry can never resolve.
        """
        try:
            self._ensure_client().put_object(
                Bucket=self._bucket,
                Key=key,
                Body=BoundedReader(body, size),
                ContentLength=size,
            )
        except Exception as exc:  # noqa: BLE001 - classified, never swallowed
            classify_permanent(exc, bucket=self._bucket, key=key, verb="PutObject on")
            raise

    def get(self, key: str, *, limit: int) -> bytes:
        """Fetch one object, bounded twice, or raise :class:`ObjectAbsent`.

        ``ContentLength`` is a CLAIM by the source, so it is checked and then not
        trusted: an object that declares itself too large is refused before a byte is
        read, and the streaming read enforces the same ceiling on what actually
        arrives. A header is not a bound.
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
        return read_bounded(resp["Body"], key, limit=limit, what="object")
