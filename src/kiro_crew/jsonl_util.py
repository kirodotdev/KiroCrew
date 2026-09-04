"""Shared bounding helpers for append-only JSONL logs — on write and on read.

Several long-lived JSONL logs (the MCP stub's fallback audit log, the
subagents' slow-command log, per-member activity logs) grow one appended
record at a time from writers that must never stall an event loop. Each
needs the same bound: once the live file reaches its cap, rename it to a
single ``.1`` generation — O(1), no whole-file read — so total disk stays
at roughly twice the cap while one generation of history is kept.

:func:`rotate_jsonl_at` owns that rotation step. Each call site keeps its
own append, record shape, size cap, and error contract, because those
differ per log; what they share is exactly the rotate-by-rename.

:func:`bounded_records` and :func:`strict_records` own the matching bound on
the READ side. A file's total size being rotated does not bound one RECORD:
``for line in handle`` asks for bytes up to the next newline, so a single
crafted newline-free line is one allocation the size of the whole file, and
every tree these readers touch is agent-writable. The two functions differ
only in the posture an over-cap record takes, which is a per-call-site
judgement and the reason both exist:

* :func:`bounded_records` SKIPS the record. Correct where the read is
  read-only and degradable — the caller already tolerates a malformed
  record, and dropping one costs a count, not durable state.
* :func:`strict_records` ABORTS the read. Correct where the parsed output
  feeds a rewrite or any other durable decision, because a silently
  skipped record there loses or duplicates data.

The two postures differ in exactly one more thing than the over-cap branch,
and that is the point rather than an accident. A skip reader only has to be
good enough to count with, so it decodes with ``errors="replace"``. A strict
reader's caller PERSISTS what it read, which that is not good enough for: a
replacement character would be written back in place of the original bytes.
The strict readers therefore make one guarantee — a record they yield is
exactly the record on disk — and raise :class:`UnreadableRecord` for every
other outcome.

Record BOUNDARIES are shared, and are the universal-newline set: ``\\n``,
``\\r\\n`` and a bare ``\\r`` all end a record, matching the text-mode reads
these callers were converted from. Splitting only on ``\\n`` would glue a
CR-delimited pair into one line that parses as neither record, which costs a
skip reader a count but costs a strict caller its correctness — a dedupe probe
stops seeing a prior entry.

Each has an undecoded twin — :func:`bounded_raw_records` and
:func:`strict_raw_records` — yielding ``bytes``. The bounded twin has one
caller, which already iterated a binary handle and prefilters on bytes before
parsing. The strict twin's caller is the snapshot notification merge, which
copies records into another file verbatim: it needs one guarantee the other
readers' callers do not, namely that the bytes are valid for the DESTINATION
file and not merely a faithful copy of the source. That is why it takes the raw
twin and validates the encoding itself rather than taking
:func:`strict_records`: a decode whose OUTPUT is what gets written back is the
non-byte-exact round trip that site is fixing. Its write-side contract is
stated on :func:`kiro_crew.snapshot._merge_notifications`.

:func:`kiro_crew.session_storage._manifest_records` is a third reader and
deliberately stays separate: it needs ``str.splitlines`` boundaries, since
it replaced a whole-file ``splitlines`` and a manifest split on any unicode
line boundary must keep parsing as it always did. The two readers here split
on the universal-newline set only, which is what the handle iteration they
replace
used.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import IO

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

# Either record terminator, so one scan finds whichever comes first. See
# _boundary_end for why two bytes.find() calls are not equivalent.
_BOUNDARY_RE = re.compile(rb"[\r\n]")

# Longest record these readers will materialise, in BYTES (terminator excluded:
# the threshold applies to the record without its newline, so a cap-length record
# plus its terminator is accepted). Peak held is a carried tail plus one read and
# is flat in file size; MEASURED at 3.03x this value, which is parity with the
# reader in session_digest that this generalises. An earlier comment here said
# "roughly TWICE" and that was never true of either -- see _frames.
#
# The bound is in bytes, not characters, and callers open their file binary for
# that reason: a character cap is not a memory bound, because one astral code
# point is four bytes of `str` under PEP 393, so a 128 MiB CHARACTER cap admits
# half a gibibyte of resident text. Reading N bytes bounds the decoded `str` at a
# small constant multiple of N -- NOT at N exactly, because `errors="replace"`
# turns each undecodable byte into a U+FFFD costing 2 or 4 bytes of `str`
# depending on the widest code point in the record. That is still a bound flat in
# FILE size, which is the property the cap exists for, and it is why the peak is
# a constant multiple of this value rather than equal to it.
#
# One cap serves every caller, and it is sized for the LARGEST record shape any
# of them reads: a session transcript record, which carries a whole conversation
# turn. Its legitimate ceiling is ~90 MB -- image_artifacts'
# ``MAX_IMAGE_BYTES_PER_MESSAGE`` (64 MiB) base64-expands to ~85 MB, plus text --
# and the largest observed on a live install (30,351 kiro-cli logs, 27 GB) is
# 77,920,032 bytes, itself load-bearing: it carries an image content item, and the
# largest ``Prompt`` record (turns and first_message) is 56,203,168 bytes. 128 MiB
# is the smallest round value that clears that ceiling with margin.
#
# The other shapes read through here -- token-usage rows, telemetry export cycles,
# notification records, member activity entries (~150 bytes), stub fallback rows,
# cost samples -- are orders of magnitude smaller, so this cap never undercounts
# them either. A per-format cap would bound each tighter, but the memory that
# matters is the peak of one record, and a cap set below a format's real ceiling
# buys nothing while risking a silent undercount of legitimate data. This is why
# the cap is NOT session_storage's ``_MANIFEST_RECORD_CAP`` (8 MiB): a manifest
# record is a header or one session's file list, and 8 MiB here would silently
# truncate the biggest real sessions.
RECORD_CAP = 128 * 1024 * 1024


class UnreadableRecord(Exception):
    """A record cannot be delivered INTACT, so the file cannot be read completely.

    Raised only by the strict readers. The abort posture exists because its
    callers write based on what they parsed, so a record they cannot reproduce
    faithfully is not something to paper over -- every way of failing to
    deliver one intact has to reach the caller, not just an over-cap one.
    Callers map this to their own fail-closed posture and must not treat it
    as "no more records".
    """


class OversizedRecord(UnreadableRecord):
    """A record exceeded the cap, so it was never materialised."""


class UndecodableRecord(UnreadableRecord):
    """A record is not valid UTF-8, so decoding it would alter its bytes.

    The skip readers decode with ``errors="replace"``, which is right for a
    caller that only counts or displays: one mangled character costs it a
    record's contribution. It is wrong for a caller that writes what it read,
    because the replacement is what gets persisted -- ``compact_cost_log``
    would ``os.replace`` the log with U+FFFD substituted for the original
    bytes, and a dedupe key built from a replaced record no longer matches the
    record it came from.
    """


def _body_len(piece: bytes) -> int:
    """Length of *piece* without its record terminator.

    The cap bounds a record's CONTENT, so the terminator must not count against
    it -- otherwise a record whose body is exactly at the cap is refused for
    carrying the delimiter that makes it a record at all, and whether it is
    refused depends on which terminator it happens to use.
    """
    if piece.endswith(b"\r\n"):
        return len(piece) - 2
    if piece.endswith((b"\n", b"\r")):
        return len(piece) - 1
    return len(piece)


def _boundary_end(buf: bytes, start: int = 0) -> int | None:
    """Index just past the first record terminator at or after *start*, else None.

    Terminators are the universal-newline set -- ``\\n``, ``\\r\\n`` and a bare
    ``\\r`` -- because that is what the TEXT-mode reads these callers were
    converted from treated as a record end.

    *start* lets the caller walk one buffer without re-slicing it. Searching
    from an offset rather than trimming the front is what keeps a chunk holding
    many ``\\r``-delimited records linear: trimming copies the whole remaining
    tail per record, which is quadratic in the record count and is itself a
    cheap denial-of-service on an agent-writable file.

    One regex scan finds whichever terminator comes first. Two separate
    ``bytes.find`` calls would NOT do: a file delimited only by ``\\r`` has no
    ``\\n`` until its very end, so searching for ``\\n`` walks the whole
    remaining buffer on every record and reintroduces the same quadratic cost
    the offset was added to remove. Measured before and after on a
    40,000-record file: 0.086s of superlinear growth against 0.012s flat.

    A ``\\r`` that is the LAST byte is deliberately not treated as a terminator:
    it may be the first half of a ``\\r\\n`` whose ``\\n`` is in the next read,
    and splitting there would invent a boundary and then leave a stray ``\\n``
    looking like an empty record. The caller waits for more bytes instead, and
    at EOF yields the remainder whole -- so a file genuinely ending in a bare
    ``\\r`` still produces exactly one final record.
    """
    match = _BOUNDARY_RE.search(buf, start)
    if match is None:
        return None
    at = match.start()
    if buf[at : at + 1] != b"\r":
        return at + 1
    if at + 1 == len(buf):
        return None  # may be the \r of a \r\n not read yet
    return at + 2 if buf[at + 1 : at + 2] == b"\n" else at + 1


class _Oversized:
    """Marker for one record whose body exceeded the cap.

    A distinct type rather than ``None`` so that a policy layer cannot confuse
    "no record" with "a record I refused to materialise".
    """

    __slots__ = ()


_OVERSIZED = _Oversized()


def _frames(handle: IO[bytes], cap: int) -> Iterator[bytes | _Oversized]:
    """Frame *handle* into complete records, or :data:`_OVERSIZED` for one too long.

    FRAMING ONLY, and that separation is the point. This function owns the
    buffer, the record boundaries, and the cap as a memory bound; it decides
    nothing about what an over-cap record MEANS. The policy layers above it
    decide skip-versus-abort and never see a buffer, so a framing subtlety
    cannot turn into a policy bug.

    That split is a direct response to three review findings on this pull
    request, which looked like three bugs and were one entanglement -- framing
    state and policy state being read off the same raw buffer. A trailing
    ``\\r`` must be HELD rather than split (its ``\\n`` may be in the next
    read), and every one of the three was that held byte interacting with a
    policy decision: counted as body, so an at-cap record was refused; carried
    into a read that completed it, so an over-cap record was admitted; and
    erased when dropping, so the next record's terminator ended the drop and a
    valid record vanished. None of those is expressible here, because the only
    things that leave this function are a whole record and a marker.

    Two properties the callers depend on:

    - A record's body is measured when the record is COMPLETE, not per read. A
      record ending inside one read may have started in an earlier one, so a
      bounded ``readline`` bounds a read, not a record.
    - An over-cap record is dropped in full, including its terminator, so the
      next thing yielded always starts on a real boundary. Otherwise a hostile
      line's tail could forge a record that framing had just reported as
      dropped.

    Peak memory is MEASURED, not reasoned about, because an earlier version of
    this docstring claimed "roughly twice the cap" and that was simply wrong --
    of this reader and of the one it generalises. With ``tracemalloc`` at a 4 MiB
    cap: main's ``session_digest._bounded_lines`` peaks at 3.03x the cap, and so
    does this reader. It reached 4.03x while each read asked for a full ``cap``
    regardless of what the carried tail already held, which added a whole cap on
    top of a buffer that could already be that large; reads are now bounded by
    the remainder instead. The floor is a carried tail plus one read, and it is
    flat in file size, which is the property that actually matters.

    There is deliberately no ``drain`` switch. A caller that aborts simply
    stops consuming this generator, and generators are lazy, so the work of
    discarding a multi-GB tail is never done for it. That is the same guarantee
    the old explicit flag gave, with no flag to pass or get wrong.
    """
    buf = b""
    # True while discarding the remainder of an over-cap record, whose own
    # terminator is what ends the discard.
    dropping = False
    while True:
        # Read only what this record has left, not a full cap every time. A
        # carried tail plus a full-cap read is what made the peak 4x the cap
        # instead of the 3x main pays; bounding by the remainder restores parity.
        # cap + 2 is the FLOOR, not a tidy constant: a legal at-cap record ending
        # CRLF is cap + 2 bytes, so a buffer that could only ever hold cap + 1
        # could not assemble one and would refuse it -- the round-7 bug exactly.
        # The max(2, ...) keeps a read able to see a whole CRLF once the buffer is
        # already full, so a \r there is still resolvable rather than deadlocked.
        chunk = handle.readline(max(2, cap + 2 - len(buf)))
        if not chunk:
            break
        buf += chunk
        # Walk by offset and slice the remainder ONCE at the end of the chunk:
        # re-slicing per record copies the whole tail each time, which is
        # quadratic in the number of records a single read holds.
        start = 0
        while (idx := _boundary_end(buf, start)) is not None:
            piece, start = buf[start:idx], idx
            if dropping:
                dropping = False  # this piece's terminator ended the dropped record
                continue
            if _body_len(piece) > cap:
                # Already terminated, so there is nothing left to discard and
                # `dropping` must stay clear -- the next piece is a real record.
                yield _OVERSIZED
                continue
            yield piece
        buf = buf[start:]
        # A trailing \r is a pending TERMINATOR half, not body: it was left
        # unsplit above in case its \n is in the next read, so counting it would
        # make an exactly-at-cap CRLF record look over-cap.
        pending_cr = buf.endswith(b"\r")
        if len(buf) - pending_cr > cap:
            if not dropping:
                yield _OVERSIZED
                dropping = True
            # KEEP a pending \r. It is the dropped record's own terminator, and
            # dropping it would leave the NEXT record's terminator to end the
            # discard -- silently consuming a valid record.
            buf = b"\r" if pending_cr else b""
    if buf and not dropping:
        # A crash mid-append leaves a final record with no terminator. Its BODY
        # is within the cap because the per-read check above reported and dropped
        # any longer tail; the piece itself may be one byte longer, when the file
        # ends on a bare \r that is a terminator here rather than a pending half.
        yield buf


def _decode(raw: bytes) -> str:
    """Decode one accepted record.

    Per-record decoding is equivalent to decoding the whole file, because
    ``\\n`` is never part of a UTF-8 multi-byte sequence — lead and
    continuation bytes are all >= 0x80 — so no sequence can straddle a
    record boundary and be replaced differently than it would have been.
    """
    return raw.decode("utf-8", errors="replace")


def bounded_raw_records(
    handle: IO[bytes], path: Path, *, cap: int = RECORD_CAP, label: str = "read"
) -> Iterator[bytes]:
    """Yield *handle*'s records as raw bytes, SKIPPING any over *cap* bytes.

    The undecoded form of :func:`bounded_records`, for a caller that already
    iterated a binary handle and prefilters on bytes before parsing. Such a
    caller gains the bound with no other change; routing it through the
    decoding variant instead would make it decode every record to run a
    filter that rejects most of them.

    The skip posture. Use where the read is read-only and degradable: the
    caller already skips a record it cannot parse, so an over-cap record
    costs that one record's contribution and nothing durable. Where the
    output instead feeds a rewrite or a durable decision, use
    :func:`strict_records`.

    *handle* is BINARY so the cap is a memory bound rather than a code-point
    count (see :data:`RECORD_CAP`). Record boundaries are the universal-newline
    set -- ``\\n``, ``\\r\\n`` and a bare ``\\r`` -- which is exactly what the
    text-mode ``for line in handle`` iteration this replaces split on, so
    moving to a binary handle changes where records begin and end not at all
    (see :func:`_boundary_end`). A record containing an exotic boundary
    ``str.splitlines`` would honour (``\\u2028``, ``\\x1c``, ...) stays ONE
    record and still parses, matching the iteration rather than
    ``splitlines`` -- which is the difference from
    :func:`kiro_crew.session_storage._manifest_records`, whose caller needs the
    splitlines shape.

    An over-cap record is dropped in full by the framing layer, including its
    terminator, so a hostile file costs time proportional to its length and
    memory proportional to the cap. *label* names the caller in the one debug
    line emitted when anything was skipped; that line runs only if the generator
    is driven to exhaustion, and a caller that stops early forfeits it
    knowingly.
    """
    oversized = 0
    for frame in _frames(handle, cap):
        if isinstance(frame, _Oversized):
            oversized += 1
            continue
        yield frame
    if oversized:
        # %r: *path* names a file in an agent-writable tree, and several
        # callers discover their inputs with iterdir()/glob(), so a planted
        # name can embed a newline. The repr keeps one log record from
        # forging others.
        logger.debug("%s: skipped %d record(s) over %d bytes in %r", label, oversized, cap, path)


def bounded_records(
    handle: IO[bytes], path: Path, *, cap: int = RECORD_CAP, label: str = "read"
) -> Iterator[str]:
    """Yield *handle*'s records decoded, SKIPPING any over *cap* bytes.

    :func:`bounded_raw_records` plus :func:`_decode`, and the form almost
    every caller wants: it replaces a text-mode ``for line in handle``
    without touching the loop body. See that function for the posture,
    boundary and cap properties they share.
    """
    for raw in bounded_raw_records(handle, path, cap=cap, label=label):
        yield _decode(raw)


def strict_raw_records(handle: IO[bytes], path: Path, *, cap: int = RECORD_CAP) -> Iterator[bytes]:
    """Yield *handle*'s records as raw bytes, raising on any it cannot deliver intact.

    The abort posture, undecoded. Use where the record must survive
    byte-for-byte: a reader that copies records into another file cannot go
    through a lossy ``errors="replace"`` decode and re-encode, which would
    rewrite an undecodable byte as U+FFFD.

    Raises :class:`OversizedRecord` past *cap*, which is
    :class:`UnreadableRecord` -- catch that, so a future refusal reason cannot
    leak past this caller. Peak held is measured at 3.03x the cap -- a carried
    tail plus one read, flat in file size -- and an over-cap record's tail is
    NOT walked: raising abandons the framing generator, and because generators
    are lazy the discard it would have done next simply never runs. So the
    caller does not pay a multi-GB traversal for a read it is giving up on --
    the cost the cap exists to deny.
    """
    for frame in _frames(handle, cap):
        if isinstance(frame, _Oversized):
            raise OversizedRecord(f"record over {cap} bytes in {path!r}")
        yield frame


def strict_records(handle: IO[bytes], path: Path, *, cap: int = RECORD_CAP) -> Iterator[str]:
    """Yield *handle*'s records decoded STRICTLY, raising on any it cannot deliver intact.

    The abort posture. Use where the parsed output feeds a rewrite or another
    durable decision: skipping a record there would silently drop it from
    what gets written back, or hide it from a dedupe probe so a duplicate
    is appended.

    Unlike :func:`bounded_records` this decodes with ``errors="strict"`` and
    turns a failure into :class:`UndecodableRecord`, because here the
    replacement character is what the caller PERSISTS rather than a display
    artefact. With :class:`OversizedRecord` that adds up to one guarantee
    worth stating plainly: a record this generator yields is exactly the
    record on disk, and every other outcome stops the read. Catch
    :class:`UnreadableRecord` to cover both.

    See :func:`strict_raw_records` for the no-drain rationale.
    """
    for raw in strict_raw_records(handle, path, cap=cap):
        try:
            yield raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UndecodableRecord(f"record is not valid UTF-8 in {path!r}") from exc


def rotate_jsonl_at(path: Path, max_bytes: int) -> None:
    """Rotate ``path`` aside to ``<name>.1`` once it reaches ``max_bytes``.

    Call immediately before appending a record. Keeps ONE previous
    generation, replacing any older one, so total disk use stays bounded at
    about twice the cap. The live file can overshoot the cap by the few
    records written between a size check and the next rotation; callers
    accept that slack in exchange for never blocking.

    Rotation is guarded by a NON-BLOCKING try-lock on a sibling
    ``<name>.lock`` file so that two writers hitting the cap together
    cannot both rotate (the second would replace ``.1`` with the first's
    fresh live file, discarding a generation). A loser skips rotating — it
    never waits, so no caller can stall its event loop — and the next
    writer rotates. Every current caller is (or must be treated as) a
    multi-process writer, so the lock is unconditional; the cost to a
    single writer is one fd and one non-blocking syscall.

    Best-effort by contract: NEVER raises. Any failure — the lock file
    unopenable (fd exhaustion, read-only or ACL-restricted dir), a
    fresh-boot missing log, a Windows sharing violation rejecting the
    rename, an unusable path value — degrades to not rotating, so the
    caller's append still runs. Fd/disk exhaustion is a leading cause of
    the very incidents these logs diagnose, so a rotation failure must
    never cost the record; only a failure of the caller's own append may.
    """
    try:
        lock_fd = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            locked = platform_compat.try_acquire_lock(lock_fd, exclusive=True)
            try:
                if locked and path.stat().st_size >= max_bytes:
                    os.replace(path, path.with_name(path.name + ".1"))
            finally:
                if locked:
                    platform_compat.release_lock(lock_fd)
        finally:
            os.close(lock_fd)
    except (OSError, ValueError):
        pass
