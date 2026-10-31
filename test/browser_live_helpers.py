"""Fakes the live-browser tests share: a CDP relay, a clock, a frame sink, a JPEG.

``FakeRelay`` records every ``send`` and answers from a table; ``attach`` hands
back a relay-owned session id and keeps the event callback so a test can push a
``Page.screencastFrame`` through it. ``FakeClock`` is the one clock the stream
reads: ``sleep`` advances it instead of waiting, so pacing, the ladder and the
watchdog run with no real time passing (Determinism contract D2/D3).
"""

from __future__ import annotations

import base64
import struct
from typing import Any

from kiro_crew.browser_harness import screencast as sc

SESSION = "relay-sess-1"
CHROME_SESSION = "chrome-frame-sess"


def make_jpeg(width: int, height: int, payload: int = 512) -> bytes:
    """A JPEG-shaped byte string: SOI, one SOF0 segment with the size, filler, EOI."""
    sof = struct.pack(">BBHBHHB", 0xFF, 0xC0, 11, 8, height, width, 1)
    return b"\xff\xd8" + sof + b"\x00" * payload + b"\xff\xd9"


def frame_event(jpeg: bytes, *, device_w: int = 1280, device_h: int = 800) -> dict[str, Any]:
    return {
        "method": "Page.screencastFrame",
        "params": {
            "data": base64.b64encode(jpeg).decode("ascii"),
            "sessionId": CHROME_SESSION,
            "metadata": {"deviceWidth": device_w, "deviceHeight": device_h},
        },
    }


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)


class FakeRelay:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.on_event: Any = None
        self.attached: list[str] = []
        self.detached: list[str] = []
        self.answers: dict[str, Any] = {}
        self.fail: set[str] = set()

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, dict(params or {}), session_id))
        if method in self.fail:
            raise RuntimeError(f"{method}: boom")
        answer = self.answers.get(method, {})
        return answer() if callable(answer) else dict(answer)

    async def attach(self, target_id: str, on_event: Any) -> str:
        self.attached.append(target_id)
        self.on_event = on_event
        return SESSION

    async def detach(self, session_id: str) -> None:
        self.detached.append(session_id)

    def methods(self, name: str) -> list[tuple[str, dict[str, Any], str | None]]:
        return [c for c in self.calls if c[0] == name]


class RecordingSink:
    """Keeps every frame; acks each one on arrival unless ``ack_on_arrival`` is off."""

    def __init__(self, stream_ref: dict[str, Any] | None = None, *, ack_on_arrival: bool = True):
        self.frames: list[tuple[dict[str, Any], bytes]] = []
        self.ack_on_arrival = ack_on_arrival
        self.stream_ref = stream_ref if stream_ref is not None else {}

    async def frame(self, header: dict[str, Any], blob: bytes) -> None:
        self.frames.append((dict(header), blob))
        stream = self.stream_ref.get("stream")
        if self.ack_on_arrival and stream is not None:
            stream.ack(header["seq"])


def make_stream(
    *,
    ack_on_arrival: bool = True,
    config: sc.LiveConfig | None = None,
    session_key: str = "chat-1",
    link_memory: sc.LinkMemory | None = None,
) -> tuple[sc.LiveStream, FakeRelay, FakeClock, RecordingSink]:
    relay = FakeRelay()
    clock = FakeClock()
    ref: dict[str, Any] = {}
    sink = RecordingSink(ref, ack_on_arrival=ack_on_arrival)
    stream = sc.LiveStream(
        relay,
        "target-1",
        sink,
        session_key=session_key,
        config=config,
        clock=clock,
        sleep=clock.sleep,
        auto_control=False,
        # Each stream its own memory unless a test shares one on purpose.
        link_memory=link_memory if link_memory is not None else sc.LinkMemory(),
    )
    ref["stream"] = stream
    return stream, relay, clock, sink
