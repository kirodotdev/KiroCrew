"""Two chats at once through the running gateway: the seam behind bug 16.

The report: one session streams, every other one sits in ``thinking`` until
it finishes, then a blob arrives. Reproduced here as a fairness contract on
the real ``POST /api/chat`` SSE stream with the fake model's ``[[SLOW]]``
turn (30 chunks, half a second apart): a second slot's turn, started while
the first is streaming, must start streaming before the first ends, and the
first must keep streaming while the second starts. Nothing inside the
process is patched or timed; only what the two SSE clients see.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from kiro_crew.testing.fake_acp_backend import SLOW_CHUNKS, SLOW_TRIGGER

pytestmark = pytest.mark.integration

#: Generous bound on one turn end to end (a cold session start plus 30 chunks).
TURN_SECS = 120.0


class _Turn:
    """One ``POST /api/chat`` SSE stream, with the wall-clock of what arrived."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.started = 0.0
        self.first_chunk: float | None = None
        self.chunk_times: list[float] = []
        self.done: float | None = None
        self.events: list[dict] = []

    async def run(self, gw, slot: str, message: str) -> None:
        self.started = time.monotonic()
        resp = await gw.post("/api/chat", {"message": message, "slot": slot})
        assert resp.status == 200, (self.label, resp.status, await resp.text())
        async for raw in resp.content:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                self.done = time.monotonic()
                return
            event = json.loads(payload)
            self.events.append(event)
            if event.get("type") == "chunk":
                now = time.monotonic()
                self.chunk_times.append(now)
                if self.first_chunk is None:
                    self.first_chunk = now
        pytest.fail(f"{self.label}: stream ended without [DONE]")

    def chunks_before(self, t: float) -> int:
        return sum(1 for c in self.chunk_times if c < t)


async def _wait_first_chunk(turn: _Turn, *, secs: float) -> None:
    deadline = time.monotonic() + secs
    while turn.first_chunk is None:
        if turn.done is not None:
            pytest.fail(f"{turn.label} finished without a chunk: {turn.events[-3:]}")
        if time.monotonic() > deadline:
            pytest.fail(f"{turn.label}: no chunk within {secs}s")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_a_second_chat_streams_while_the_first_is_still_streaming(gateway_boot) -> None:
    """Slot A starts a slow turn; once its chunks flow, slot B starts one.
    B's first chunk must arrive before A's last, and A must keep producing
    chunks once B is streaming (bug 16: the report was that B waits for A).

    Both slots are warmed with one plain turn first, so the measured window
    holds concurrent STREAMING only: a cold session start is unbounded and
    host-dependent, and inside the window it would eat the margin the fake
    model's thirty chunks give the order assertion.
    """
    async with gateway_boot() as gw:
        slot_a = (await gw.post_json("/api/chat/slots", {}))["key"]
        slot_b = (await gw.post_json("/api/chat/slots", {}))["key"]
        await asyncio.wait_for(
            asyncio.gather(
                _Turn("warm A").run(gw, slot_a, "hello"),
                _Turn("warm B").run(gw, slot_b, "hello"),
            ),
            timeout=TURN_SECS,
        )
        a, b = _Turn("A"), _Turn("B")

        task_a = asyncio.create_task(a.run(gw, slot_a, f"{SLOW_TRIGGER} first"))
        await _wait_first_chunk(a, secs=TURN_SECS)
        b_started = time.monotonic()
        task_b = asyncio.create_task(b.run(gw, slot_b, f"{SLOW_TRIGGER} second"))
        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2 * TURN_SECS)

        assert a.done is not None and b.done is not None
        assert b.first_chunk is not None
        assert b.first_chunk < a.done, (
            "B did not stream until A had finished: "
            f"B first chunk at +{b.first_chunk - b_started:.1f}s, "
            f"A finished at +{a.done - b_started:.1f}s after B was sent"
        )
        a_chunks_after_b_started = len(a.chunk_times) - a.chunks_before(b.first_chunk)
        assert a_chunks_after_b_started > 0, (
            "A produced no chunks once B was streaming: "
            f"A chunk count {len(a.chunk_times)} of {SLOW_CHUNKS}"
        )
        assert len(a.chunk_times) == SLOW_CHUNKS, len(a.chunk_times)
        assert len(b.chunk_times) == SLOW_CHUNKS, len(b.chunk_times)
