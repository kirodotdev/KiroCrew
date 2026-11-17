"""The metadata line of one transcript: how it is parsed, and one uncached read of it.

A transcript's first JSONL line is its metadata record. :func:`parse_metadata_line` is the
one rule for turning that line's bytes into a record and a ``METADATA_LINE_*`` state; the
conversation store's cached reader (``history_projection_reads``) and
:func:`read_metadata_state` both use it.

:func:`read_metadata_state` exists for a process that needs one session's record and
nothing else of the store. A stdio MCP server names the agents its session may start in a
tool description, which takes the session's execution record
(``execution_context.read_session_execution``), and it writes that description before it
has loaded the code that runs its tools. So this module imports nothing of the store: no
caches, no locks, no writers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger("kiro_crew.history")

#: What :meth:`TranscriptReadProjection.metadata_line_state` answers about a
#: transcript's first line. ``get_metadata_status`` folds the last two into
#: ``readable=False``, which suits readers. Writers need the split: a
#: ``transient`` failure (open or UTF-8 decode failed after retries) clears on its
#: own, so the write is deferred, but a ``corrupt`` line never does, and a writer
#: deferring on it would never persist another row.
METADATA_LINE_READABLE = "readable"
METADATA_LINE_TRANSIENT = "transient"
METADATA_LINE_CORRUPT = "corrupt"

# Metadata reads retry briefly before reporting "no metadata": on Windows a
# just-written session file can be transiently unopenable while an indexer or AV
# scanner holds it (ERROR_SHARING_VIOLATION -> PermissionError). Those holds are
# measured in milliseconds, and the caller that matters here (the open-tab
# restore) treats an empty result as "session never existed" and drops the tab.
METADATA_READ_ATTEMPTS = 3
METADATA_READ_RETRY_SECS = 0.02


def parse_metadata_line(raw_first: bytes) -> tuple[dict[str, Any], str]:
    """The metadata record a transcript's first line holds, and that line's state.

    An empty line and a line that is JSON but not a metadata object both read as
    ``({}, METADATA_LINE_READABLE)``. Bytes that are not UTF-8 or not JSON are
    ``METADATA_LINE_CORRUPT``: every line writer encodes UTF-8 and replaces the file
    atomically, so such bytes are damage (a torn or foreign write), never a write in
    flight, and no retry reads them. A damaged line cannot establish whether this was a
    member session, so identity-sensitive readers refuse on that state.
    """
    try:
        first = raw_first.decode("utf-8").strip()
    except UnicodeDecodeError:
        return {}, METADATA_LINE_CORRUPT
    if not first:
        return {}, METADATA_LINE_READABLE
    try:
        data = json.loads(first)
    except json.JSONDecodeError:
        return {}, METADATA_LINE_CORRUPT
    if isinstance(data, dict) and data.get("_type") == "metadata":
        return data, METADATA_LINE_READABLE
    return {}, METADATA_LINE_READABLE


def read_metadata_state(directory: Path, stems: Sequence[str]) -> tuple[dict[str, Any], str]:
    """Read one session's metadata line once, uncached, with the store's bounded retries.

    *stems* are the filename stems the session's transcript can occupy under *directory*,
    canonical first (``history_files.transcript_stems``). The first one that exists is
    read, which is the file ``ConversationLog._path`` resolves to. No transcript reads as
    ``({}, METADATA_LINE_READABLE)``, as it does through the store. A file that cannot be
    opened after :data:`METADATA_READ_ATTEMPTS` tries is ``METADATA_LINE_TRANSIENT``.
    Between tries it pauses only off the event loop.
    """
    candidates = (directory / f"{stem}.jsonl" for stem in stems)
    path = next((candidate for candidate in candidates if candidate.exists()), None)
    if path is None:
        return {}, METADATA_LINE_READABLE
    for attempt in range(METADATA_READ_ATTEMPTS):
        try:
            # Bytes, decoded by the parser: a first line that is not UTF-8 is a
            # damaged line no retry will ever read, not a read in flight.
            with open(path, "rb") as handle:
                raw_first = handle.readline()
        except OSError:
            if attempt + 1 < METADATA_READ_ATTEMPTS:
                _pause_between_attempts()
                continue
            logger.warning(
                "history: could not read a session's metadata after %d attempts; "
                "reporting no metadata",
                METADATA_READ_ATTEMPTS,
                exc_info=True,
            )
            return {}, METADATA_LINE_TRANSIENT
        return parse_metadata_line(raw_first)
    return {}, METADATA_LINE_READABLE


def _pause_between_attempts() -> None:
    """Sleep between read attempts only when off the event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        time.sleep(METADATA_READ_RETRY_SECS)
