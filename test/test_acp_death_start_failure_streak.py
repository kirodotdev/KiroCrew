"""Runtime-death attribution for a session start on a shared ACP process."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _await_pending, _make_runtime, _start_reader, _stop_reader
from test_dashboard_chat import TestAcpProcessDiedRecovery as _AcpDiedSuite

from kiro_crew.acp.runtime import AcpRuntimeDead
from kiro_crew.acp.types import METHOD_SET_MODE
from kiro_crew.testing.wait import await_until


async def _real_start_and_sibling_deaths():
    """Feed a real EOF to a runtime with a pending session/new and a sibling request."""
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

    return runtime, starting_info.value, sibling_info.value


@pytest.mark.asyncio
async def test_session_new_death_tags_only_the_starting_request():
    """Real EOF tags the interrupted start without mutating a sibling request."""
    runtime, starting_death, sibling_death = await _real_start_and_sibling_deaths()
    assert runtime._dead is True
    assert starting_death is not sibling_death
    assert (
        starting_death.__cause__,
        starting_death.__suppress_context__,
    ) == (None, True)
    assert getattr(starting_death, "session_start_died", False) is True
    assert getattr(sibling_death, "session_start_died", False) is False
    # The card's tag stays off both: a human chat keeps its plain row and Resume.
    assert getattr(starting_death, "session_start_failed", False) is False
    assert getattr(sibling_death, "session_start_failed", False) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("which", "self_wake", "expected_start_calls"),
    [
        ("start", True, 1),
        ("start", False, 0),
        ("sibling", True, 0),
    ],
    ids=["loop-start", "human-start", "loop-sibling"],
)
async def test_real_start_death_reaches_on_error_and_charges_only_the_loop(
    tmp_path, monkeypatch, which, self_wake, expected_start_calls
):
    """A real tagged death raised by the session start goes through run_chat to _on_error."""
    from kiro_crew.dashboard.chat_utils_injected_rows import SESSION_START_FAILED_KIND

    _, starting_death, sibling_death = await _real_start_and_sibling_deaths()
    death = starting_death if which == "start" else sibling_death
    suite = _AcpDiedSuite()
    state, slot, _client, run_chat = suite._make_state_and_slot(tmp_path)
    state.sessions.get_or_create = AsyncMock(side_effect=death)
    state.sessions.record_failure = AsyncMock(return_value=False)
    service = MagicMock()
    service.notify_cycle_failed = AsyncMock()
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

    assert service.notify_cycle_start_failed.call_count == expected_start_calls
    if which == "start":
        # A failed start has its own streak; the reached-a-session bound stays out.
        service.notify_cycle_failed.assert_not_called()
    errors = [m for m in slot.messages if m.get("role") == "error"]
    assert errors, "the death must end in an error row"
    # Same card as main for every population: no start-failed kind, so Resume stays.
    assert all((m.get("meta") or {}).get("kind") != SESSION_START_FAILED_KIND for m in errors)
