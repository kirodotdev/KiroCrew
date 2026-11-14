"""Runtime-death attribution for a session start on a shared ACP process."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _await_pending, _make_runtime, _start_reader, _stop_reader
from test_dashboard_chat import TestAcpProcessDiedRecovery as _AcpDiedSuite

from kiro_crew.acp.runtime import AcpRuntimeDead
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.transport_errors import AcpProcessDied
from kiro_crew.acp.types import METHOD_SET_MODE
from kiro_crew.testing.wait import await_until


@pytest.mark.asyncio
async def test_session_new_death_tags_only_the_starting_request():
    """Real EOF tags the interrupted start without mutating a sibling request."""
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

    provider = AcpSessionProvider.__new__(AcpSessionProvider)
    provider._runtime = runtime
    translated_start = provider._translate_dead(starting_death)
    translated_sibling = provider._translate_dead(sibling_death)
    assert translated_start.session_start_failed is True
    assert translated_sibling.session_start_failed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tagged", "self_wake", "prior_retries", "expected_calls", "expected_requeues"),
    [
        (True, True, 0, 0, 1),
        (True, True, 3, 1, 0),
        (True, False, 3, 0, 0),
        (False, True, 3, 0, 0),
    ],
    ids=["recovering", "terminal-start", "human-turn", "sibling-request"],
)
async def test_process_death_arm_charges_only_the_tagged_loop_turn(
    tmp_path,
    monkeypatch,
    tagged,
    self_wake,
    prior_retries,
    expected_calls,
    expected_requeues,
):
    """The runner reports one terminal failed start, never a recovery attempt."""
    suite = _AcpDiedSuite()
    state, slot, client, run_chat = suite._make_state_and_slot(tmp_path)
    slot._acp_pipe_death_retries = prior_retries
    death = AcpProcessDied("runtime died during session/new")
    death.session_start_failed = tagged
    suite._make_stream_raise(client, death)
    service = MagicMock()
    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", service)

    with patch(
        "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
        new_callable=AsyncMock,
        return_value=False,
    ):
        await run_chat(
            state,
            slot,
            "test message",
            _directive_self_wake=self_wake,
            _directive_loop_id="loop-starting" if self_wake else "",
            _directive_loop_gen=1 if self_wake else 0,
        )

    assert service.notify_cycle_start_failed.call_count == expected_calls
    service.notify_cycle_failed.assert_not_called()
    assert len(slot._queue) == expected_requeues
