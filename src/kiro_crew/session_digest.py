"""Lazy per-session detail: first message, real turn count, image count.

Called once when a row expands in the storage inventory UI.  Streams files
line-by-line so multi-GB sessions never load into memory.  Degrades to
empty/zero on any malformed or unreadable file rather than raising.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.config.paths import data_home, kiro_sessions_dir
from kiro_crew.history import ARCHIVE_DIR_NAME, ARCHIVE_SEGMENT_DELIMITER, SESSIONS_DIR_NAME

logger = logging.getLogger(__name__)


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

    Skips metadata/archive header lines. Never raises.
    """
    first_msg = ""
    turn_count = 0

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    # Malformed line: skip, don't lose the rest
                    logger.debug("digest: skipping malformed line in %s", path)
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
        logger.debug("digest: cannot read transcript %s", path)

    return first_msg, turn_count


def _scan_cli_log(path: Path) -> tuple[int, int]:
    """Stream a kiro-cli event log and count (user_turns, images).

    User turns are lines with ``kind == "Prompt"``.
    Images are content items with ``kind == "image"`` in any record.
    Never raises.
    """
    turns = 0
    images = 0

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
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
        logger.debug("digest: cannot read cli log %s", path)

    return turns, images


def _first_message_from_cli(path: Path) -> str:
    """Extract the first user message text from a kiro-cli event log.

    Returns the text of the first Prompt record's first text content item.
    Never raises.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
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
        logger.debug("digest: cannot read cli log %s for first_message", path)

    return ""


def _collapse_whitespace(text: str, max_len: int) -> str:
    """Collapse all whitespace runs to single spaces and truncate."""
    collapsed = " ".join(text.split())
    if len(collapsed) > max_len:
        return collapsed[:max_len]
    return collapsed
