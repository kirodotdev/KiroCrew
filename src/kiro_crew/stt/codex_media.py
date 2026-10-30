"""Paced WebRTC microphone input; imported only for the optional Codex provider."""

from __future__ import annotations

import asyncio
import fractions
from typing import Any

SAMPLE_RATE = 16000
FRAME_SAMPLES = SAMPLE_RATE // 50
MAX_BUFFER_BYTES = SAMPLE_RATE * 2 * 90
DRAIN_TIMEOUT_SECS = MAX_BUFFER_BYTES / (SAMPLE_RATE * 2) + 5


class PcmInput:
    """Keep RTP running with silence and retain bounded pre-ready microphone audio."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.samples = 0
        self.started_at: float | None = None
        self.drained = asyncio.Event()
        self.drained.set()

    def append(self, pcm: bytes) -> None:
        if len(pcm) % 2:
            raise ValueError("PCM16 requires whole samples")
        if len(self.buffer) + len(pcm) > MAX_BUFFER_BYTES:
            raise ValueError("Codex microphone backlog exceeded its limit")
        self.buffer.extend(pcm)
        if pcm:
            self.drained.clear()

    async def read(self) -> tuple[bytes, int]:
        loop = asyncio.get_running_loop()
        if self.started_at is None:
            self.started_at = loop.time()
        await asyncio.sleep(max(0, self.started_at + self.samples / SAMPLE_RATE - loop.time()))
        size = FRAME_SAMPLES * 2
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        pts = self.samples
        self.samples += FRAME_SAMPLES
        if not self.buffer:
            self.drained.set()
        return data.ljust(size, b"\0"), pts

    async def drain(self) -> None:
        await asyncio.wait_for(self.drained.wait(), DRAIN_TIMEOUT_SECS)


def create_peer() -> tuple[Any, Any]:
    """Load the optional media stack only when opening an actual voice session."""
    import av
    from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection

    class InputTrack(AudioStreamTrack):
        def __init__(self) -> None:
            super().__init__()
            self.source = PcmInput()

        def append(self, pcm: bytes) -> None:
            self.source.append(pcm)

        async def drain(self) -> None:
            await self.source.drain()

        async def recv(self) -> Any:
            data, pts = await self.source.read()
            frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
            frame.planes[0].update(data)
            frame.sample_rate = SAMPLE_RATE
            frame.pts = pts
            frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
            return frame

    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    track = InputTrack()
    pc.addTrack(track)
    return pc, track


def remote_answer(sdp: str) -> Any:
    """The native SDP value, kept behind the mockable media boundary."""
    from aiortc import RTCSessionDescription

    return RTCSessionDescription(sdp=sdp, type="answer")
