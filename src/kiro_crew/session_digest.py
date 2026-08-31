"""Lazy per-session detail: first message, real turn count, image count.

Called once when a row expands in the storage inventory UI.  Reads files one
capped record at a time so neither a multi-GB session nor a single crafted
newline-free line loads into memory.  Degrades to empty/zero on any malformed,
over-cap, or unreadable file rather than raising.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from kiro_crew.config.paths import data_home, kiro_sessions_dir
from kiro_crew.history import ARCHIVE_DIR_NAME, ARCHIVE_SEGMENT_DELIMITER, SESSIONS_DIR_NAME

logger = logging.getLogger(__name__)

# Longest record this module will materialise, in BYTES (terminator excluded: the
# threshold applies to the record without its newline, so a cap-length record plus
# its terminator is accepted and the peak read is cap+1). Both trees read here are
# agent-writable, so `for line in handle` would hand one crafted newline-free line
# a single allocation the size of the whole file.
#
# The bound is in bytes, not characters, and the files are opened binary for that
# reason: a character cap is not a memory bound, because one astral code point is
# four bytes of `str` under PEP 393, so a 256 MiB CHARACTER cap admits a gibibyte
# of resident text. Decoding cannot widen a record past its byte count -- UTF-8
# spends at least as many bytes per code point as CPython's widest string
# representation -- so capping the read at N bytes caps the decoded `str` at N
# bytes too, and the transient peak is a small constant multiple of this value,
# flat in file size.
#
# The cap CANNOT be session_storage's ``_MANIFEST_RECORD_CAP`` (8 MiB): a manifest
# record is a header or one session's file list, while these files carry whole
# conversation turns. A legitimate record's ceiling is ~90 MB -- image_artifacts'
# ``MAX_IMAGE_BYTES_PER_MESSAGE`` (64 MiB) base64-expands to ~85 MB, plus text --
# and the largest observed on a live install (30,351 kiro-cli logs, 27 GB) is
# 77,920,032 bytes, itself load-bearing: it carries an image content item, and the
# largest ``Prompt`` record (turns and first_message) is 56,203,168 bytes. A cap
# below those would silently undercount the biggest sessions, so 128 MiB is the
# smallest round value that clears the derived ceiling with margin. This constant
# is the dial if a legitimate record ever grows past it.
_RECORD_CAP = 128 * 1024 * 1024


def _bounded_lines(handle: IO[bytes], path: Path) -> Iterator[str]:
    """Yield *handle*'s lines decoded, skipping any record over ``_RECORD_CAP`` bytes.

    *handle* is BINARY so the cap can be a memory bound rather than a code-point
    count (see ``_RECORD_CAP``). Decoding per record is equivalent to decoding the
    whole file, because ``\\n`` is never part of a UTF-8 multi-byte sequence -- lead
    and continuation bytes are all >= 0x80 -- so no sequence can straddle a record
    boundary and be replaced differently than it would have been.

    Record boundaries are ``readline``'s, which is what the ``for line in handle``
    iteration this replaces used: a record containing an exotic line boundary
    (``\\u2028``, ``\\x1c``, ...) stays one record and still parses. Using
    ``str.splitlines`` here -- the shape
    :func:`kiro_crew.session_storage._manifest_records` needs, because its reader
    replaced a whole-file ``splitlines`` -- would split such a record in two and
    lose the turn it describes. Binary reads narrow this in one direction only:
    a lone ``\\r`` no longer ends a record, since universal-newline translation is
    gone. Neither writer of these files emits one, and a planted file that does is
    read as one over-cap record and skipped, which is the safe direction. A
    ``\\r\\n`` terminator survives, because every caller strips the record first --
    it only shifts the boundary by one byte, since the ``\\r`` counts toward the
    read and a ``\\r\\n``-terminated record is therefore refused one byte sooner
    than an ``\\n``-terminated one. Immaterial at this cap; stated so a future
    reader does not read the threshold as byte-exact on a CRLF file.

    An over-cap record is SKIPPED and its tail drained without being kept, so a
    hostile file costs time proportional to its length and memory proportional to
    the cap. Skipping is the right posture for these three callers and the wrong
    one for the manifest reader: nothing here feeds a rewrite, the digest is
    read-only per-row detail that already skips a malformed line and keeps going
    (see the module docstring), so an over-cap record degrades exactly one row's
    counts. ``restore()`` in session_storage rewrites the manifest from what it
    parsed, which is why an over-cap record there aborts the whole read instead.
    """
    oversized = 0
    while True:
        # Reading cap+1 makes the verdict unambiguous: a returned chunk shorter
        # than that either ended on a newline or hit EOF, so it is a whole record.
        raw = handle.readline(_RECORD_CAP + 1)
        if not raw:
            break
        if len(raw) > _RECORD_CAP and not raw.endswith(b"\n"):
            oversized += 1
            while True:
                tail = handle.readline(_RECORD_CAP + 1)
                if not tail or tail.endswith(b"\n"):
                    break
            continue
        yield raw.decode("utf-8", errors="replace")
    if oversized:
        # %r: *path* names a file in an agent-writable tree, and the archive
        # segments are discovered with iterdir(), so a planted name can embed a
        # newline. The repr keeps one log record from forging others.
        logger.debug(
            "digest: skipped %d record(s) over %d bytes in %r", oversized, _RECORD_CAP, path
        )


@dataclass(frozen=True)
class SessionDigest:
    """Lazy detail for one session row."""

    first_message: str
    turns: int
    images: int


def digest(uid: str, stems: tuple[str, ...], sid: str) -> SessionDigest:
    """Compute the lazy detail for a single session.

    *uid* is the opaque session identifier (for logging only).
    *stems* are the transcript filename stems (canonical + any legacy).
    *sid* is the kiro-cli session id (UUID).

    Reads the Kiro Crew transcript(s) for first_message and turns, then
    the kiro-cli event log for images (and supplemental turns if the
    transcript is absent).

    Never raises on I/O or parse errors: degrades to ""/0 and logs at debug.
    """
    first_message = ""
    turns = 0
    images = 0

    # --- Kiro Crew transcripts: first_message + turns ---
    sessions_dir = data_home() / SESSIONS_DIR_NAME
    archive_dir = sessions_dir / ARCHIVE_DIR_NAME

    # Collect all transcript files for this session, ordered oldest first:
    # archive segments (sorted by name = sorted by timestamp), then the live file.
    transcript_files: list[Path] = []

    for stem in stems:
        # Archive segments: <stem>__<timestamp>[-N].jsonl
        if archive_dir.is_dir():
            try:
                prefix = stem + ARCHIVE_SEGMENT_DELIMITER
                segments = sorted(
                    p
                    for p in archive_dir.iterdir()
                    if p.name.startswith(prefix) and p.suffix == ".jsonl"
                )
                transcript_files.extend(segments)
            except OSError:
                logger.debug("digest: cannot list archive dir for %s", uid)

        # Live transcript
        live = sessions_dir / f"{stem}.jsonl"
        if live.is_file():
            transcript_files.append(live)

    for path in transcript_files:
        fm, tc = _scan_transcript(path)
        if not first_message and fm:
            first_message = fm
        turns += tc

    # --- kiro-cli event log: images (and fallback turns) ---
    cli_dir = kiro_sessions_dir()
    cli_jsonl = cli_dir / f"{sid}.jsonl"
    if cli_jsonl.is_file():
        cli_turns, cli_images = _scan_cli_log(cli_jsonl)
        images = cli_images
        # If transcripts were empty/missing, fall back to cli turn count
        if turns == 0:
            turns = cli_turns
        # If no first_message from transcript, try cli log
        if not first_message:
            first_message = _first_message_from_cli(cli_jsonl)

    return SessionDigest(
        first_message=first_message,
        turns=turns,
        images=images,
    )


def _scan_transcript(path: Path) -> tuple[str, int]:
    """Stream a transcript file and extract (first_user_message, user_turn_count).

    Skips metadata/archive header lines, and any record over ``_RECORD_CAP``
    bytes (see :func:`_bounded_lines`, which also owns the decode). Never raises.
    """
    first_msg = ""
    turn_count = 0

    try:
        with open(path, "rb") as f:
            for line in _bounded_lines(f, path):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    # Malformed line: skip, don't lose the rest
                    logger.debug("digest: skipping malformed line in %r", path)
                    continue

                if not isinstance(record, dict):
                    continue

                # Skip metadata and archive header lines
                if record.get("_type") in ("metadata", "archive"):
                    continue

                if record.get("role") == "user":
                    content = record.get("content")
                    if isinstance(content, str):
                        turn_count += 1
                        if not first_msg and content.strip():
                            first_msg = _collapse_whitespace(content, 280)
    except OSError:
        logger.debug("digest: cannot read transcript %r", path)

    return first_msg, turn_count


def _scan_cli_log(path: Path) -> tuple[int, int]:
    """Stream a kiro-cli event log and count (user_turns, images).

    User turns are lines with ``kind == "Prompt"``.
    Images are content items with ``kind == "image"`` in any record.
    Records over ``_RECORD_CAP`` bytes are skipped (see :func:`_bounded_lines`).
    Never raises.
    """
    turns = 0
    images = 0

    try:
        with open(path, "rb") as f:
            for line in _bounded_lines(f, path):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue

                if not isinstance(record, dict):
                    continue

                kind = record.get("kind")
                if kind == "Prompt":
                    turns += 1

                # Count images in any record's content array
                data = record.get("data")
                if isinstance(data, dict):
                    content = data.get("content")
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("kind") == "image":
                                images += 1
    except OSError:
        logger.debug("digest: cannot read cli log %r", path)

    return turns, images


def _first_message_from_cli(path: Path) -> str:
    """Extract the first user message text from a kiro-cli event log.

    Returns the text of the first Prompt record's first text content item.
    Records over ``_RECORD_CAP`` bytes are skipped (see :func:`_bounded_lines`).
    Never raises.
    """
    try:
        with open(path, "rb") as f:
            for line in _bounded_lines(f, path):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue

                if not isinstance(record, dict):
                    continue

                if record.get("kind") != "Prompt":
                    continue

                data = record.get("data")
                if not isinstance(data, dict):
                    continue

                content = data.get("content")
                if not isinstance(content, list):
                    continue

                for item in content:
                    if isinstance(item, dict) and item.get("kind") == "text":
                        text = item.get("data")
                        if isinstance(text, str) and text.strip():
                            return _collapse_whitespace(text, 280)
                # Prompt found but no text content
                return ""
    except OSError:
        logger.debug("digest: cannot read cli log %r for first_message", path)

    return ""


def _collapse_whitespace(text: str, max_len: int) -> str:
    """Collapse all whitespace runs to single spaces and truncate."""
    collapsed = " ".join(text.split())
    if len(collapsed) > max_len:
        return collapsed[:max_len]
    return collapsed
