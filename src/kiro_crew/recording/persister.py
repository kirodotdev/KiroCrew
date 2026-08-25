"""Debounced live-transcript persistence.

Writes ``transcript_local.json`` on a debounce so that an abrupt
termination loses at most a few seconds of text.  All file I/O is
offloaded to the subprocess executor so nothing blocking touches the
asyncio event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Optional

from kiro_crew.executors import subprocess_executor

logger = logging.getLogger(__name__)

# Debounce interval — persist at most this often, so an abrupt kill loses
# at most ~this many seconds of transcript.
_DEBOUNCE_SECS = 3.0


@dataclass
class TranscriptSegment:
    """A single transcript segment."""

    text: str
    is_final: bool
    timestamp: float = field(default_factory=time.time)


class LiveTranscriptPersister:
    """Accumulates transcript segments and persists them on a debounce.

    The transcript is stored as a JSON array of segment objects::

        [
            {"text": "Hello world", "is_final": true, "timestamp": 1719849600.0},
            {"text": "partial hypo", "is_final": false, "timestamp": 1719849601.5}
        ]

    Final segments are appended permanently.  The latest partial replaces
    any previous partial at the end of the list.

    Parameters
    ----------
    path:
        Filesystem path for the JSON file.
    debounce_secs:
        Minimum interval between disk writes.
    """

    def __init__(self, path: Path, *, debounce_secs: float = _DEBOUNCE_SECS) -> None:
        self._path = path
        self._debounce_secs = debounce_secs
        self._segments: list[TranscriptSegment] = []
        self._dirty = False
        self._debounce_task: Optional[asyncio.Task[None]] = None
        self._last_persist: float = 0.0

    @property
    def path(self) -> Path:
        """Path to the transcript file."""
        return self._path

    @property
    def segments(self) -> list[TranscriptSegment]:
        """Current accumulated segments (read-only view)."""
        return list(self._segments)

    async def add_segment(self, text: str, *, is_final: bool = False) -> None:
        """Add a transcript segment and schedule persistence.

        Final segments are appended.  A partial replaces the last partial
        (if any) at the end of the list.
        """
        segment = TranscriptSegment(text=text, is_final=is_final)

        if is_final:
            # Remove any trailing partial before appending the final
            if self._segments and not self._segments[-1].is_final:
                self._segments[-1] = segment
            else:
                self._segments.append(segment)
        else:
            # Replace or append the trailing partial
            if self._segments and not self._segments[-1].is_final:
                self._segments[-1] = segment
            else:
                self._segments.append(segment)

        self._dirty = True
        self._schedule_persist()

    def _schedule_persist(self) -> None:
        """Schedule a debounced persist if one is not already pending."""
        if self._debounce_task is not None and not self._debounce_task.done():
            return  # Already scheduled
        try:
            loop = asyncio.get_running_loop()
            self._debounce_task = loop.create_task(self._debounced_persist())
        except RuntimeError:
            # No running event loop — skip scheduling (test scenario)
            pass

    async def _debounced_persist(self) -> None:
        """Wait for the debounce interval, then persist."""
        elapsed = time.monotonic() - self._last_persist
        remaining = self._debounce_secs - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)
        await self._do_persist()

    async def _do_persist(self) -> None:
        """Write the transcript to disk via the executor."""
        if not self._dirty:
            return

        # Serialize to JSON before clearing dirty — if the write fails
        # the dirty flag remains unset, but segments are still in memory.
        data = [
            {
                "text": seg.text,
                "is_final": seg.is_final,
                "timestamp": seg.timestamp,
            }
            for seg in self._segments
        ]
        content = json.dumps(data, ensure_ascii=False, indent=None)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            subprocess_executor(),
            partial(self._write_sync, content),
        )
        # Clear dirty AFTER the write completes so a concurrent flush
        # that races with the debounce task cannot skip a write that
        # was cancelled mid-executor.
        self._dirty = False
        self._last_persist = time.monotonic()

    def _write_sync(self, content: str) -> None:
        """Synchronous file write — runs on the executor."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp file and rename for atomicity
            tmp_path = self._path.with_suffix(".tmp")
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.replace(self._path)
        except Exception:
            logger.warning("Failed to persist live transcript to %s", self._path, exc_info=True)

    async def flush(self) -> None:
        """Force an immediate persist, bypassing the debounce.  Idempotent."""
        # Cancel any pending debounce
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
            try:
                await self._debounce_task
            except asyncio.CancelledError:
                pass
            self._debounce_task = None

        if self._dirty:
            await self._do_persist()
