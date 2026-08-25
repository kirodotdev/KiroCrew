"""WAV file writer for recording sessions.

Writes 16 kHz signed 16-bit little-endian monaural PCM audio to a WAV file.
All I/O is offloaded to the subprocess executor so no blocking call touches
the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
import wave
from functools import partial
from pathlib import Path

from kiro_crew.executors import subprocess_executor

logger = logging.getLogger(__name__)

# Audio format constants matching the browser capture pipeline.
SAMPLE_RATE_HZ = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # 16-bit signed LE


class WavWriter:
    """Async WAV writer that offloads blocking I/O to a thread pool.

    Usage::

        writer = WavWriter(Path("/tmp/audio.wav"))
        await writer.open()
        await writer.write(pcm_bytes)
        await writer.close()
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._wf: wave.Wave_write | None = None
        self._closed = False
        self._total_frames = 0

    @property
    def path(self) -> Path:
        """Path to the WAV file being written."""
        return self._path

    @property
    def total_frames(self) -> int:
        """Number of audio frames written so far."""
        return self._total_frames

    @property
    def duration_secs(self) -> float:
        """Duration of audio written, in seconds."""
        if SAMPLE_RATE_HZ == 0:
            return 0.0
        return self._total_frames / SAMPLE_RATE_HZ

    async def open(self) -> None:
        """Open the WAV file for writing.  Creates parent directories."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(subprocess_executor(), self._open_sync)

    def _open_sync(self) -> None:
        """Synchronous open — runs on the executor."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._wf = wave.open(str(self._path), "wb")
        self._wf.setnchannels(CHANNELS)
        self._wf.setsampwidth(SAMPLE_WIDTH_BYTES)
        self._wf.setframerate(SAMPLE_RATE_HZ)

    async def write(self, pcm_data: bytes) -> None:
        """Write raw PCM data to the WAV file.

        Offloaded to the subprocess executor so it never blocks the loop.
        No-op if the writer is closed or the data is empty.
        """
        if self._closed or not pcm_data:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(subprocess_executor(), partial(self._write_sync, pcm_data))

    def _write_sync(self, pcm_data: bytes) -> None:
        """Synchronous write — runs on the executor."""
        if self._wf is None or self._closed:
            return
        # Each frame is SAMPLE_WIDTH_BYTES * CHANNELS bytes
        frame_size = SAMPLE_WIDTH_BYTES * CHANNELS
        num_frames = len(pcm_data) // frame_size
        if num_frames > 0:
            self._wf.writeframes(pcm_data[: num_frames * frame_size])
            self._total_frames += num_frames

    async def close(self) -> None:
        """Close the WAV file, finalizing the header.  Idempotent."""
        if self._closed:
            return
        self._closed = True
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(subprocess_executor(), self._close_sync)

    def _close_sync(self) -> None:
        """Synchronous close — runs on the executor."""
        if self._wf is not None:
            try:
                self._wf.close()
            except Exception:
                logger.debug("Error closing WAV file", exc_info=True)
            self._wf = None
