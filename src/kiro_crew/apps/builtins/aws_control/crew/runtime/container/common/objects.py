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
import threading
import time
from collections.abc import Callable
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
    "is_precondition_failed",
    "PreconditionFailed",
    "classify_permanent",
    "read_bounded",
]

#: How much is read per chunk when a GET's ceiling is enforced. Small enough that the
#: overshoot before a refusal is bounded by this rather than by the object's size.
GET_CHUNK_BYTES: int = 1024 * 1024

#: The largest single underlying ``read`` a :class:`BoundedReader` issues, so the deadline
#: and the stop are re-checked at least this often DURING a transfer rather than only before
#: each caller-sized read. A caller asking for a megabyte on a link trickling at a few KiB/s
#: would otherwise block in one ``self._fh.read(1 MiB)`` for over a minute -- past the drain
#: window -- without the deadline/cancel checks (which run only between reads) ever getting a
#: turn. Capping the underlying read small turns that one long block into many short ones with
#: a check between each, so the window is honoured on a trickle, not just a stall.
_READ_SLICE_BYTES: int = 64 * 1024

#: How often the deadline and the stop are re-checked WHILE a single underlying slice read is
#: in flight. Capping the slice size (above) bounds a trickle only if the slice completes;
#: a link delivering a few hundred bytes/second keeps a 64 KiB ``self._fh.read`` alive for
#: much longer than the drain window with no check reachable inside it (the socket read
#: timeout never trips while bytes keep arriving). So the slice read runs on a worker thread
#: and the caller waits on it in steps of this length, re-asking the deadline/stop each step;
#: when one fires the read is abandoned mid-flight rather than waited out. Small enough that
#: the overshoot past the deadline is bounded by this, not by the object's size.
_READ_POLL_SECS: float = 0.05

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


class PreconditionFailed(RuntimeError):
    """A conditional write was rejected because the object was not in the expected state.

    Raised when a PUT carrying ``If-Match``/``If-None-Match`` meets a ``412`` (S3
    ``PreconditionFailed``): the key already changed since the caller read it, or already
    exists when the caller asked to create only. Never permanent -- the bucket answered and
    the credential worked -- so it is not a :class:`StoreUnusable`; it is the writer being
    told another writer got there first, which is a verdict to record, not a fault to retry
    blindly against the same answer.
    """


class StoreUnusable(RuntimeError):
    """The bucket cannot be used at all, and no retry will change that.

    Separate from an ordinary failed request because the two deserve opposite
    responses. A throttle or a dropped connection is retried and costs latency. A denied
    request, a bucket that does not exist, a credential that is not valid: the next
    attempt meets the same answer, so retrying turns "this is broken" into a log line
    nobody reads while the work it protects goes on unprotected.
    """


class UploadCut(RuntimeError):
    """An upload was stopped while it was still progressing. Never permanent.

    The base of the two reasons a body refuses to hand over its next chunk, because the
    caller does the SAME thing for both: record the object as this cycle's refusal and
    leave it for the next cycle. The bucket answered and the transport worked in each
    case, so neither is a :class:`StoreUnusable` and neither may be classified permanent
    -- doing that would end the process over a slow network or an ordinary shutdown.

    The two are separate names rather than one because they say different things to an
    operator reading the log: a window that was too short for this object, or a stop that
    arrived while it was in flight.
    """


class UploadDeadlineExceeded(UploadCut):
    """The whole request outlived the time its caller set aside for it.

    Raised from the BODY rather than by a check around the request, because the thing
    that needs bounding is the transmission itself. Socket timeouts bound one read or
    one connect; a connection that keeps delivering small chunks under that bound never
    trips either, so the sum across a large object is unbounded. The transport asks the
    body for the next chunk many times during a send, which makes that the one place a
    request can be cut off while it is progressing.

    Not a :class:`StoreUnusable`: the bucket answered and the transport worked. The
    object simply did not fit the window, so the caller records it as this cycle's
    refusal and the next cycle attempts it again.
    """


class UploadCancelled(UploadCut):
    """A stop arrived while this upload was in flight, so the body cut it.

    The deadline above cannot cover this. An interval cycle is deliberately given no
    window -- it has a next cycle to finish a slow object, so bounding it would refuse a
    large transcript forever -- and that is exactly the upload a SIGTERM can land in the
    middle of. Its cycle must return before the final one can begin, and the final cycle's
    deadline is measured from when the stop was OBSERVED, so every second an in-flight
    interval PUT keeps running is a second taken from the window that uploads the turns
    the backend just flushed. Cutting it costs an object the final cycle was going to send
    anyway; not cutting it costs the turns that only the final cycle has.

    Checked in the body for the same reason the deadline is: between objects is too coarse
    when one object's transmission is the thing that outlives the signal.
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

    *deadline* is an optional ``time.monotonic`` reading after which a read raises
    :class:`UploadDeadlineExceeded` instead of returning bytes. It bounds the WHOLE
    transmission, so a rewind does not refresh it: the caller set that much time aside
    for getting this object into the bucket, and the retries are part of getting it
    there. A reader built without one is an unbounded body, which is what an interval
    cycle wants -- it has no window to overrun and a next cycle to correct it, so cutting
    a slow-but-progressing upload there abandons an object that was on its way instead of
    saving anything.

    *cancel* is the other half of that, and the reason an unbounded body is still safe. It
    is a predicate asked before each chunk; once it answers True the read raises
    :class:`UploadCancelled`. An interval cycle passes the stop flag, so the upload it has
    no reason to bound by TIME is still cut the moment there is a reason to stop it at all
    -- which is what lets the cycle return and the final cycle begin inside the window the
    supervisor is counting. A rewind does not clear it either: the predicate is re-asked on
    every read rather than latched, so a transport retrying the body meets the same stop.

    The two are complementary by design and not both set in practice: the final cycle has
    the deadline and no stop to honour (the flag is what made it final), an interval cycle
    has the stop and no window.
    """

    def __init__(
        self,
        fh: BinaryIO,
        limit: int,
        *,
        deadline: float | None = None,
        clock: Callable[[], float] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> None:
        self._fh = fh
        self._limit = max(0, limit)
        self._remaining = self._limit
        self._deadline = deadline
        self._cancel = cancel
        # Resolved here rather than as a default argument, which would bind
        # ``time.monotonic`` once at class definition and leave the clock unsubstitutable.
        self._clock = clock if clock is not None else time.monotonic
        try:
            self._start = fh.tell()
            self._seekable = fh.seekable()
        except (AttributeError, OSError):
            # A stream that cannot report its position cannot be rewound either. Reading
            # still works, so the caller keeps a usable body rather than an error.
            self._start = 0
            self._seekable = False

    def expired(self) -> bool:
        """Whether the deadline has passed. Always False when there is no deadline."""
        return self._deadline is not None and self._clock() >= self._deadline

    def cancelled(self) -> bool:
        """Whether the stop has arrived. Always False when there is no predicate."""
        return self._cancel is not None and self._cancel()

    def read(self, amt: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        want = self._remaining if amt is None or amt < 0 else min(amt, self._remaining)
        # The read is served in slices of at most ``_READ_SLICE_BYTES`` so the deadline and
        # the stop are re-checked DURING the transfer, not merely before the caller's whole
        # request. The checks stay BEFORE each underlying read, so the refusal does not first
        # wait on one more chunk from a connection that is already too slow -- the next chunk
        # is exactly what the shutdown cannot afford to wait for. But a single
        # ``self._fh.read(slice)`` on a link that trickles below the socket read timeout blocks
        # for the whole slice with no between-slice check reachable inside it, so each slice is
        # itself read interruptibly: it runs on a worker thread the caller waits on in short
        # steps, abandoning it the moment the deadline or the stop fires rather than waiting
        # the trickle out. (A refused slice's thread is reaped by the socket read timeout; the
        # process is shutting down, so a briefly-orphaned read costs nothing.)
        parts: list[bytes] = []
        got = 0
        while got < want:
            self._raise_if_done(sent=got)
            slice_want = min(_READ_SLICE_BYTES, want - got)
            chunk = self._read_slice_interruptibly(slice_want, sent=got)
            if not chunk:
                # The underlying stream ended before ``want`` was satisfied: return what
                # arrived, exactly as a single short read would.
                break
            self._remaining -= len(chunk)
            got += len(chunk)
            parts.append(chunk)
        return b"".join(parts)

    def _raise_if_done(self, *, sent: int) -> None:
        """Raise the right end-of-window error when the deadline or the stop has fired."""
        if self.expired():
            raise UploadDeadlineExceeded(
                f"the body passed its deadline with {self._remaining} of {self._limit} B unsent"
            )
        if self.cancelled():
            raise UploadCancelled(
                f"the body was cut by a stop with {self._remaining} of {self._limit} B unsent"
            )

    def _read_slice_interruptibly(self, slice_want: int, *, sent: int) -> bytes:
        """Read one slice, interruptible by the deadline/stop while it is still in flight.

        With neither a deadline nor a stop there is nothing to interrupt, so the read is
        issued directly -- the untimed callers (an ordinary GET with no shutdown pressure)
        keep the plain blocking path and pay no thread. When a budget IS present the read runs
        on a worker thread and this waits on it in ``_READ_POLL_SECS`` steps, re-asking the
        deadline/stop each step; a fired budget abandons the in-flight read and raises rather
        than waiting for the trickle to deliver the slice.
        """
        if self._deadline is None and self._cancel is None:
            return self._fh.read(slice_want)

        result: list[bytes] = []
        error: list[BaseException] = []

        def _run() -> None:
            try:
                result.append(self._fh.read(slice_want))
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
                error.append(exc)

        worker = threading.Thread(target=_run, name="bounded-reader-slice", daemon=True)
        worker.start()
        while True:
            worker.join(_READ_POLL_SECS)
            if not worker.is_alive():
                break
            # The slice is still arriving: cut it the instant the window closes rather than
            # waiting the trickle out. The orphaned read ends on its own socket read timeout.
            self._raise_if_done(sent=sent)
        if error:
            raise error[0]
        return result[0] if result else b""

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


def is_precondition_failed(exc: Exception) -> bool:
    """True when *exc* is a ``412`` -- a conditional write another writer's state defeated."""
    if error_code(exc) == "PreconditionFailed":
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        meta = response.get("ResponseMetadata")
        if isinstance(meta, dict) and meta.get("HTTPStatusCode") == 412:
            return True
    return False


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
