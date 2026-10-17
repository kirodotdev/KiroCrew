"""Two chats at once through the running gateway: the seam behind bug 16.

The report: one session streams, every other one sits in ``thinking`` until
it finishes, then a blob arrives. Reproduced here as a fairness contract on
the real ``POST /api/chat`` SSE stream. Each measured turn is the fake model's
``[[SLOW_HOLD:<token>]]`` stream: its first chunk, then a hold inside the fake
until this test creates the token's release file, then the rest. So each half
of the contract is observed while the other turn is provably mid-turn rather
than raced against a stream delay: slot B must stream while slot A is held,
and A must stream again once released while B is held. Nothing inside the
gateway process is patched or timed; only what the two SSE clients see, in
the order they see it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable

import pytest

from kiro_crew.testing.fake_acp_backend import (
    SLOW_CHUNKS,
    SLOW_HOLD_AFTER_CHUNKS,
    SLOW_HOLD_DIR_ENV,
    SLOW_HOLD_WAIT_SECS,
    slow_hold_path,
    slow_hold_trigger,
)

#: The item budget. Every wait in this file is derived from it so that a
#: wedged turn fails as a readable ``pytest.fail`` before pytest-timeout
#: kills the worker (testing-conventions: a lost-run ceiling sits under the
#: module's own mark).
ITEM_SECS = 180
pytestmark = [pytest.mark.integration, pytest.mark.timeout(ITEM_SECS)]

#: Bound on one warm-up turn end to end: a cold session start plus one canned
#: reply, a few seconds in practice. A quarter of the item budget.
TURN_SECS = ITEM_SECS / 4

#: Lost-run ceiling on ONE awaited signal: a chunk, or both turns finishing
#: once released. Each is a warm slot relaying one frame: the worst case
#: measured over 20 runs on a shared host at load 20-30 was 0.12 s, so a
#: quarter of the item budget is far above 10x that. It stays under the fake's
#: own ``SLOW_HOLD_WAIT_SECS`` (asserted below), so a signal that never comes
#: is reported here by name rather than as the fake's timeout error.
SIGNAL_SECS = ITEM_SECS / 4

#: The aiohttp TOTAL timeout for one measured SSE stream. Each wait on the
#: stream is bounded by ``SIGNAL_SECS`` and A's stream spans four of them, so
#: the client never ends a stream before the signal that is late is named; the
#: item budget is the only bound above it.
STREAM_SECS = ITEM_SECS


class _Turn:
    """One ``POST /api/chat`` SSE stream: what arrived, and in which order."""

    def __init__(self, label: str, arrivals: list[tuple[str, str]]) -> None:
        self.label = label
        self.chunks = 0
        self.done = False
        self.events: list[dict] = []
        self.changed = asyncio.Event()
        self._arrivals = arrivals

    def _note(self, kind: str) -> None:
        self._arrivals.append((self.label, kind))
        self.changed.set()

    async def run(self, gw, slot: str, message: str, *, timeout: float = STREAM_SECS) -> None:
        # The client timeout is TOTAL, so it must outlast the whole stream, not
        # the POST alone: the harness default is sized for a one-shot answer.
        resp = await gw.post("/api/chat", {"message": message, "slot": slot}, timeout=timeout)
        assert resp.status == 200, (self.label, resp.status, await resp.text())
        async for raw in resp.content:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                self.done = True
                self._note("done")
                return
            event = json.loads(payload)
            self.events.append(event)
            if event.get("type") == "chunk":
                self.chunks += 1
                self._note("chunk")
        pytest.fail(f"{self.label}: stream ended without [DONE]")


async def _wait_until(
    turn: _Turn, task: "asyncio.Task[None]", reached: Callable[[], bool], what: str
) -> None:
    """Block until ``reached()`` holds, re-checking on every event *turn* streams.

    A turn that ends first (a non-200 answer, a transport error, the fake's
    own hold ceiling) is surfaced as its own error at once, not as a deadline
    miss with the diagnostic lost in the task. A signal that never comes
    fails by name at ``SIGNAL_SECS``, with the time it waited.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()

    async def _signal() -> None:
        while True:
            turn.changed.clear()
            if reached():
                return
            if task.done():
                await task
                pytest.fail(f"{turn.label} ended before {what}: {turn.events[-3:]}")
            changed = asyncio.ensure_future(turn.changed.wait())
            try:
                await asyncio.wait({changed, task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                changed.cancel()

    try:
        await asyncio.wait_for(_signal(), timeout=SIGNAL_SECS)
    except asyncio.TimeoutError:
        if task.done():
            # The stream itself ended with this error (its own client timeout,
            # a transport fault): report that, not a missing signal.
            await task
        pytest.fail(
            f"{turn.label}: {what} did not arrive within {SIGNAL_SECS:.0f}s "
            f"(waited {loop.time() - started:.1f}s; last events {turn.events[-3:]})"
        )


@pytest.mark.asyncio
async def test_a_second_chat_streams_while_the_first_is_still_streaming(
    gateway_boot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slot A starts a held turn; once its first chunk is in, slot B starts
    one. B must stream while A is held mid-turn, and A, released, must stream
    while B is held mid-turn (bug 16: the report was that B waits for A).

    Both slots are warmed with one plain turn first, so the measured part
    holds concurrent STREAMING only: a cold session start is unbounded and
    host-dependent, and has nothing to do with the contract.
    """
    assert SLOW_HOLD_WAIT_SECS > SIGNAL_SECS, "the fake's hold ceiling would fire first"
    holds = tmp_path / "fake-acp-holds"
    holds.mkdir()
    # Set before the boot: each slot's agent child is spawned from this
    # process's environment when its first turn starts.
    monkeypatch.setenv(SLOW_HOLD_DIR_ENV, str(holds))

    def release(token: str) -> None:
        Path(slow_hold_path(str(holds), token)).touch()

    arrivals: list[tuple[str, str]] = []
    async with gateway_boot() as gw:
        slot_a = (await gw.post_json("/api/chat/slots", {}))["key"]
        slot_b = (await gw.post_json("/api/chat/slots", {}))["key"]
        await asyncio.wait_for(
            asyncio.gather(
                _Turn("warm A", []).run(gw, slot_a, "hello", timeout=TURN_SECS),
                _Turn("warm B", []).run(gw, slot_b, "hello", timeout=TURN_SECS),
            ),
            timeout=TURN_SECS,
        )
        a, b = _Turn("A", arrivals), _Turn("B", arrivals)
        task_b: asyncio.Task[None] | None = None
        task_a = asyncio.create_task(a.run(gw, slot_a, f"{slow_hold_trigger('turn-a')} first"))
        try:
            await _wait_until(
                a, task_a, lambda: a.chunks >= SLOW_HOLD_AFTER_CHUNKS, "its first chunk"
            )
            task_b = asyncio.create_task(b.run(gw, slot_b, f"{slow_hold_trigger('turn-b')} second"))
            await _wait_until(
                b,
                task_b,
                lambda: b.chunks >= SLOW_HOLD_AFTER_CHUNKS,
                "a first chunk while A was held mid-turn (B waited for A: bug 16)",
            )
            assert not a.done and a.chunks == SLOW_HOLD_AFTER_CHUNKS, (a.chunks, a.events[-3:])
            release("turn-a")
            await _wait_until(
                a,
                task_a,
                lambda: a.chunks > SLOW_HOLD_AFTER_CHUNKS,
                "a chunk after its release while B was held mid-turn (A stalled once B streamed)",
            )
            assert not b.done and b.chunks == SLOW_HOLD_AFTER_CHUNKS, (b.chunks, b.events[-3:])
            release("turn-b")
            await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=SIGNAL_SECS)
        finally:
            # Never leave a fake held into the teardown.
            release("turn-a")
            release("turn-b")
            pending = [t for t in (task_a, task_b) if t is not None and not t.done()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    assert a.done and b.done
    chunk_order = [label for label, kind in arrivals if kind == "chunk"]
    held = SLOW_HOLD_AFTER_CHUNKS
    assert chunk_order[: 2 * held + 1] == ["A"] * held + ["B"] * held + ["A"], arrivals[:8]
    assert a.chunks == SLOW_CHUNKS, a.chunks
    assert b.chunks == SLOW_CHUNKS, b.chunks
