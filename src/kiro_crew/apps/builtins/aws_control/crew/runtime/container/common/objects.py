"""One spelling of "fetch an object, with a ceiling, and classify what came back".

Both processes talk to the same bucket for opposite reasons: the sidecar PUTs this
task's state, the front GETs the one transcript a turn continues. Each needs the same
three judgements, and they are here rather than in each process because the first
version kept a copy in both and the copies drifted immediately -- one classified a
missing BUCKET as a missing OBJECT and the other classified it as a failure, so the
same bucket typo produced a refused turn in one process and an empty conversation list
in the other. Neither copy was wrong about itself, which is why no test caught it.

The three judgements:

* **A ceiling that is enforced while reading**, not after. A single ``read()``
  materialises whatever the object happens to be, so a check afterwards is a check on
  memory already spent. Reading one chunk PAST the limit is what makes the refusal
  decidable, since stopping exactly at the limit cannot tell an object of that size
  from a larger one.
* **Absence**, which is the only answer that may be read as "there is nothing here".
  A denial is not absence. On a crew's first task the objects are genuinely not there
  yet, and that has to be distinguishable from a read that failed, because one of the
  two is allowed to continue and the other is not.
* **Permanence**, which decides whether a retry is worth anything. A throttle resolves
  itself; a denied request against a bucket that does not exist does not. Retrying the
  second one turns a broken backup into a log line while the task keeps taking turns
  nothing will save.
"""

from __future__ import annotations

import os
from typing import BinaryIO

__all__ = [
    "GET_CHUNK_BYTES",
    "ABSENT_CODES",
    "PERMANENT_CODES",
    "ObjectTooLarge",
    "StoreUnusable",
    "BoundedReader",
    "error_code",
    "is_absent",
    "classify_permanent",
    "read_bounded",
]

#: How much is read per chunk when a GET's ceiling is enforced. Small enough that the
#: overshoot before a refusal is bounded by this rather than by the object's size.
GET_CHUNK_BYTES: int = 1024 * 1024

#: Error codes S3 uses for "that key is not here". Anything else, ``AccessDenied``
#: included, is a FAILURE.
#:
#: ``NoSuchBucket`` is deliberately NOT here. A missing bucket is not an absent object:
#: read as one, it reports every object absent, so the restore side finds no authority
#: files, the backend boots with an empty slot table and flushes it over the real one,
#: and every later upload fails against the same name. It is a deployment fault and is
#: classified as one below.
ABSENT_CODES = frozenset({"NoSuchKey", "404", "NotFound"})

#: Error codes no retry resolves. A wrong bucket name, a role without the permission, a
#: credential that is not valid: the next attempt meets the same answer.
#:
#: Membership is the decision that a human must change something. Anything NOT here is
#: treated as transient and retried, which is the safe default: retrying a permanent
#: fault costs latency, while giving up on a transient one discards work.
PERMANENT_CODES = frozenset(
    {
        "AccessDenied",
        "AccountProblem",
        "AllAccessDisabled",
        "InvalidAccessKeyId",
        "InvalidBucketName",
        "NoSuchBucket",
        "PermanentRedirect",
        "SignatureDoesNotMatch",
        "UnauthorizedOperation",
    }
)


class ObjectTooLarge(RuntimeError):
    """The stored object is larger than the caller is willing to hold."""


class StoreUnusable(RuntimeError):
    """The bucket cannot be used at all, and no retry will change that.

    Separate from an ordinary failed request because the two deserve opposite
    responses. A throttle or a dropped connection is retried and costs latency. A denied
    request, a bucket that does not exist, a credential that is not valid: the next
    attempt meets the same answer, so retrying turns "this is broken" into a log line
    nobody reads while the work it protects goes on unprotected.
    """


class BoundedReader:
    """A read-only view of *fh* that stops after *limit* bytes, and can be rewound.

    The bound belongs to the CALLER rather than to the transport because it is a
    property of the snapshot: *limit* is the length the descriptor's file had when it
    was opened, so stopping there is what makes an appended file's upload a coherent
    prefix instead of a race with its writer. Letting the transport read to end-of-file
    would send however much had arrived by the time it got there, which is a length
    nothing observed.

    Rewinding is offered because the transport needs it and the bound survives it. A
    retryable S3 failure makes botocore seek the body back to its start before sending
    again, so a body that refuses to seek turns every transient error into a lost object
    -- and on the final cycle there is no next interval to correct it. Seeking here moves
    the DESCRIPTOR back to where this view began and restores the remaining budget, so a
    retry re-sends exactly the same prefix rather than a longer one the file has grown
    into. Offsets are relative to the view, so nothing outside ``[0, limit]`` is
    reachable through it.
    """

    def __init__(self, fh: BinaryIO, limit: int) -> None:
        self._fh = fh
        self._limit = max(0, limit)
        self._remaining = self._limit
        try:
            self._start = fh.tell()
            self._seekable = fh.seekable()
        except (AttributeError, OSError):
            # A stream that cannot report its position cannot be rewound either. Reading
            # still works, so the caller keeps a usable body rather than an error.
            self._start = 0
            self._seekable = False

    def read(self, amt: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        want = self._remaining if amt is None or amt < 0 else min(amt, self._remaining)
        chunk = self._fh.read(want)
        self._remaining -= len(chunk)
        return chunk

    def seekable(self) -> bool:
        return self._seekable

    def tell(self) -> int:
        """How far into the bounded view the next read starts."""
        return self._limit - self._remaining

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        """Move within the view, in the view's own coordinates.

        Refuses on a stream that cannot seek rather than moving the descriptor by a route
        the bound does not cover, and clamps to the view so a transport cannot reach a
        byte the snapshot did not include.
        """
        if not self._seekable:
            raise OSError("this body cannot be rewound")
        if whence == os.SEEK_SET:
            target = offset
        elif whence == os.SEEK_CUR:
            target = self.tell() + offset
        elif whence == os.SEEK_END:
            target = self._limit + offset
        else:
            raise ValueError(f"unsupported whence: {whence}")
        target = min(max(0, target), self._limit)
        self._fh.seek(self._start + target)
        self._remaining = self._limit - target
        return target


def error_code(exc: Exception) -> str:
    """The S3 error code of a botocore ``ClientError``, or ``""``.

    Read off the response dict rather than the exception type so a stub client in a test
    can produce a genuine absence without botocore installed.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if isinstance(code, str):
                return code
        meta = response.get("ResponseMetadata")
        if isinstance(meta, dict) and meta.get("HTTPStatusCode") == 404:
            return "404"
    return ""


def is_absent(exc: Exception) -> bool:
    """True when *exc* says the key is not there, as opposed to not readable."""
    return error_code(exc) in ABSENT_CODES


def classify_permanent(exc: Exception, *, bucket: str, key: str, verb: str) -> None:
    """Re-raise *exc* as :class:`StoreUnusable` when no retry can resolve it.

    Returns without doing anything for every other error, which leaves the caller its
    normal handling. The classification is on the error CODE rather than on how many
    times the request has failed: a count cannot tell a throttle from a denial, and
    treating N failures as permanent would give up during a long outage.
    """
    code = error_code(exc)
    if code in PERMANENT_CODES:
        raise StoreUnusable(
            f"{verb} s3://{bucket}/{key} failed with {code}, which no retry resolves. "
            "The task's state cannot be made durable until this is corrected, so it is "
            "reported rather than retried against the same answer."
        ) from exc


def read_bounded(body, key: str, *, limit: int, what: str = "object") -> bytes:
    """Read at most *limit* bytes from a streaming body, refusing more.

    *what* names the thing in the refusal message, because the two callers hold the
    bytes for different reasons -- a transcript for the length of a turn, an authority
    file to be validated and written -- and an operator reading the message needs to
    know which one was refused.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = body.read(GET_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ObjectTooLarge(
                f"the stored {what} {key} exceeds {limit} bytes and was not read. It is "
                "held in memory, so an object this size is refused rather than loaded."
            )
        chunks.append(chunk)
    return b"".join(chunks)
