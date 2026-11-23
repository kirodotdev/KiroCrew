"""The session handle's model-wait watchdog judges a prompt from its send.

A prompt that gets no frame at all is probed with ``session/cancel`` once it is
silent past the window its oracle reading earns, instead of waiting for the turn
ceiling. Before the turn's first frame of its own, a ``DEAD`` reading (no backend
socket, flat counters) is no proof of a finished turn whose frame was lost, so it
waits the ordinary stale window; after one, it still probes at once.

The handle runs on a manual clock that each empty queue poll advances by the
dispatch loop's own poll, so every window below is counted, not measured.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import session_handle
from kiro_crew.acp.liveness import VERDICT_DEAD, VERDICT_UNKNOWN, VERDICT_WORKING
from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
from kiro_crew.acp.session_handle_dispatch import QUEUE_POLL_SECS
from kiro_crew.acp.types import (
    METHOD_SESSION_UPDATE,
    METHOD_SUBAGENT_LIST_UPDATE,
    STOP_REASON_STALE_RECOVER,
    JsonRpcMessage,
)
from kiro_crew.testing.clock import ManualClock

# The shipped windows: 60 s before the oracle is asked, 600 s for UNKNOWN,
# 1800 s for a held connection, the 2 h cap for WORKING.
_WD = WatchdogSettings()
_REQ_ID = 7
_DEAD = (VERDICT_DEAD, "no established backend socket and flat counters (io +0B cpu +0t)")
_UNKNOWN = (VERDICT_UNKNOWN, "no runtime pid")
_HELD = (VERDICT_UNKNOWN, "established_flat: io +0B cpu +0t")
_BUSY = (VERDICT_WORKING, "backend activity (cpu +5t)")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    fake = ManualClock()
    fake.install(monkeypatch, session_handle)
    return fake


class _PollQueue(asyncio.Queue):
    """The session queue, whose empty poll times out at once and advances the clock
    by the poll the dispatch loop waits, so silence costs no host time."""

    def __init__(self, clock: ManualClock) -> None:
        super().__init__()
        self._clock = clock

    async def get(self):  # type: ignore[override]
        if self.empty():
            await asyncio.sleep(0)
            self._clock.advance(QUEUE_POLL_SECS)
            raise asyncio.TimeoutError
        return self.get_nowait()


class _SilentRuntime:
    """A runtime the prompt is written to and that never answers it."""

    def __init__(self, clock: ManualClock) -> None:
        self.pid = None
        self.is_alive = MagicMock(return_value=True)
        self.supports_image_prompt = False
        self.acp_backend = ""
        self._last_activity = clock.monotonic()
        self.cancels: list[float] = []

        async def _notify(method: str, *_a, **_k) -> None:
            if method == "session/cancel":
                self.cancels.append(clock.monotonic())

        self.send_notification = AsyncMock(side_effect=_notify)

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        pass

    async def send_request(self, method: str, params: dict) -> int:
        return _REQ_ID


def _handle(
    clock: ManualClock, reading: tuple[str, str]
) -> tuple[AcpSessionHandle, _SilentRuntime]:
    rt = _SilentRuntime(clock)
    handle = AcpSessionHandle("sA", _PollQueue(clock), rt, watchdog=_WD)

    async def _read(*, model_wait: bool) -> tuple[str, str]:
        return reading

    handle._consult_oracle_offloaded = _read  # type: ignore[method-assign]
    return handle, rt


def _text(session_id: str = "sA") -> JsonRpcMessage:
    return JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "method": METHOD_SESSION_UPDATE,
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "partial"},
                },
            },
        }
    )


def _fanned_out_roster() -> JsonRpcMessage:
    """An ownerless roster broadcast (no ``sessionId``) the runtime fanned out to
    several sessions, so it is marked ``fanout_no_owner``."""
    frame = JsonRpcMessage(method=METHOD_SUBAGENT_LIST_UPDATE, params={"subagents": []})
    frame.fanout_no_owner = True
    return frame


async def _silent_turn(handle: AcpSessionHandle, turn_secs: float, *frames: JsonRpcMessage) -> list:
    """The dispatch loop of a turn that reads *frames* and then nothing, for
    *turn_secs* of the manual clock."""
    handle._turn_done.clear()
    handle._stale_eligible = True
    for frame in frames:
        handle._queue.put_nowait(frame)
    return [ev async for ev in handle._dispatch_events(_REQ_ID, turn_secs)]


@pytest.mark.asyncio
async def test_a_prompt_with_no_frame_is_probed_once_past_the_stale_window(clock):
    """No frame at all, an UNKNOWN reading: probed at the first poll past the
    stale window, and not one poll before it."""
    handle, rt = _handle(clock, _UNKNOWN)
    sent = clock.monotonic()

    events = [ev async for ev in handle.prompt("please answer", _WD.stale_window_secs * 2)]

    assert len(rt.cancels) == 1, "the silent prompt was never probed"
    idle = rt.cancels[0] - sent
    assert _WD.stale_window_secs < idle <= _WD.stale_window_secs + QUEUE_POLL_SECS
    assert handle._stale_probe is False  # consumed by the unacked-probe recovery
    assert events[-1].stop_reason == STOP_REASON_STALE_RECOVER


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reading", "window"),
    [
        (_DEAD, _WD.stale_window_secs),
        (_UNKNOWN, _WD.stale_window_secs),
        (_HELD, _WD.model_silent_probe_secs),
        (_BUSY, _WD.tool_stall_hard_cap_secs),
    ],
    ids=["dead", "unknown", "established_flat", "working"],
)
async def test_before_a_first_frame_each_reading_waits_its_window(clock, reading, window):
    """DEAD before the turn's first own frame gets the UNKNOWN window, not the
    immediate probe at ``check_after_secs``; the other readings keep theirs."""
    handle, rt = _handle(clock, reading)
    start = clock.monotonic()

    await _silent_turn(handle, window * 2)

    assert rt.cancels, "the silent turn was never probed"
    assert window < rt.cancels[0] - start <= window + QUEUE_POLL_SECS


@pytest.mark.asyncio
async def test_after_an_own_frame_a_dead_reading_still_probes_at_once(clock):
    handle, rt = _handle(clock, _DEAD)
    start = clock.monotonic()

    await _silent_turn(handle, _WD.stale_window_secs, _text())

    assert rt.cancels, "the lost-frame wedge was never probed"
    assert rt.cancels[0] - start <= _WD.check_after_secs + QUEUE_POLL_SECS


@pytest.mark.asyncio
async def test_a_co_tenants_fanned_out_frame_is_not_this_turns_first_frame(clock):
    """An ownerless frame the runtime fanned out to several sessions says nothing
    about this prompt, so DEAD still waits the stale window after it."""
    handle, rt = _handle(clock, _DEAD)
    start = clock.monotonic()

    await _silent_turn(handle, _WD.stale_window_secs * 2, _fanned_out_roster())

    assert rt.cancels, "the silent turn was never probed"
    idle = rt.cancels[0] - start
    assert _WD.stale_window_secs < idle <= _WD.stale_window_secs + QUEUE_POLL_SECS
