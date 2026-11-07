"""Runtime-death attribution for a session start on a shared ACP process."""

import asyncio

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _await_pending, _make_runtime, _start_reader, _stop_reader

from kiro_crew import autonudge as _an
from kiro_crew.acp.runtime import AcpRuntimeDead
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import METHOD_SET_MODE
from kiro_crew.dashboard.chat_turn.recovery import (
    _note_cycle_failure,
    _note_cycle_start_failure,
)
from kiro_crew.testing.wait import await_until

STARTING_SLOT = "chat-1-987"
SIBLING_SLOT = "chat-2-654"


@pytest.mark.asyncio
async def test_session_new_death_charges_only_the_starting_loop(monkeypatch):
    """Real EOF tags the interrupted start without mutating a sibling request."""
    start_failures: list[str] = []
    failed_cycles: list[tuple[str, str, int]] = []

    class _Svc:
        _inflight_adds: set = set()

        def notify_cycle_start_failed(self, slot_key: str) -> None:
            start_failures.append(slot_key)

        async def notify_cycle_failed(
            self, slot_key: str, *, loop_id: str, expected_generation: int
        ) -> None:
            failed_cycles.append((slot_key, loop_id, expected_generation))

        def stop(self) -> None:
            return None

    monkeypatch.setattr(_an, "_INSTANCE", _Svc(), raising=False)

    runtime, reader, _ = _make_runtime()
    reader_task = await _start_reader(runtime)
    starting = asyncio.create_task(runtime.create_session(mcp_servers=[]))
    sibling = None
    try:
        starting_request = await _await_pending(runtime)
        sibling = asyncio.create_task(runtime._send_and_await(METHOD_SET_MODE, {}))
        await _await_pending(runtime, exclude={starting_request})

        reader.feed_eof()
        with pytest.raises(AcpRuntimeDead) as starting_info:
            await await_until(starting, what="the session/new interrupted by runtime death")
        with pytest.raises(AcpRuntimeDead) as sibling_info:
            await await_until(sibling, what="the sibling request interrupted by runtime death")
    finally:
        for pending in (starting, sibling):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(
            *(task for task in (starting, sibling) if task), return_exceptions=True
        )
        await _stop_reader(reader_task)

    starting_death = starting_info.value
    sibling_death = sibling_info.value
    assert runtime._dead is True
    assert starting_death is not sibling_death
    assert getattr(starting_death, "session_start_failed", False) is True
    assert getattr(sibling_death, "session_start_failed", False) is False

    _note_cycle_start_failure(STARTING_SLOT, starting_death, self_wake=True)
    _note_cycle_start_failure(SIBLING_SLOT, sibling_death, self_wake=True)
    await _note_cycle_failure(
        STARTING_SLOT,
        starting_death,
        self_wake=True,
        loop_id="loop-starting",
        expected_generation=1,
    )
    assert start_failures == [STARTING_SLOT]
    assert failed_cycles == []

    provider = AcpSessionProvider.__new__(AcpSessionProvider)
    provider._runtime = runtime
    translated_start = provider._translate_dead(starting_death)
    translated_sibling = provider._translate_dead(sibling_death)
    start_failures.clear()
    _note_cycle_start_failure(STARTING_SLOT, translated_start, self_wake=True)
    _note_cycle_start_failure(SIBLING_SLOT, translated_sibling, self_wake=True)
    await _note_cycle_failure(
        STARTING_SLOT,
        translated_start,
        self_wake=True,
        loop_id="loop-starting",
        expected_generation=1,
    )
    assert start_failures == [STARTING_SLOT]
    assert failed_cycles == []
