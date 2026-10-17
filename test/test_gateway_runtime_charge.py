"""Injecting a sub-agent's report into its parent, and whose fault a death is.

Both the channel and the cron routes hand the composed report to one retry loop.
A landed injection acknowledges the report. A provider that died after its
prompt-busy retries, or an ACP process that died, ends the attempt with a failure
notice and resets the parent only when the death was the parent's own runtime:
on a shared runtime that reset would take the co-tenants' sessions with it. A
backend that lost the parent's session is answered once by a reset and a fresh
claim, and the retry runs on the client that claim returned. Any other ACP error
is retried twice, cancelling the parent's prompt first, then raised.

These tests drive that loop through the real ``on_done`` and pin each arm.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_gateway_subagent_wiring import (
    _Done,
    _RecordingAsyncio,
    harness,
    make_info,
    recording_clock,
)

from kiro_crew import runtime_death
from kiro_crew.acp.client import AcpError, AcpProcessDied
from kiro_crew.slack import gateway as gw_mod


@pytest.fixture
def done(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Done]:
    yield from harness(monkeypatch)


@pytest.fixture
def clock(done: _Done, monkeypatch: pytest.MonkeyPatch) -> _RecordingAsyncio:
    return recording_clock(done, monkeypatch)


@pytest.fixture
def kept(done: _Done, monkeypatch: pytest.MonkeyPatch) -> _Done:
    """A cron parent whose session outlives this report (a sibling is queued).

    Every reset left in the record is then one the retry loop made.
    """
    monkeypatch.setattr(gw_mod, "_subagent_queued_count", AsyncMock(return_value=1))
    return done


def _resets(done: _Done) -> list[Any]:
    return [c for c in done.calls if c[0] == "reset"]


@pytest.mark.asyncio
async def test_a_landed_injection_acknowledges_the_report(kept: _Done) -> None:
    info = make_info(parent="cron:j1")

    await kept.done(info)

    assert info._report_delivered is True
    assert kept.streamed_clients == [kept.client]
    kept.mgr.notify_injection_failed.assert_not_called()


@pytest.mark.parametrize(
    ("death", "reason"),
    [
        (gw_mod.PromptBusyExhaustedError("busy"), "provider dead after prompt-busy retries"),
        (AcpProcessDied("exited"), "ACP process died"),
    ],
    ids=["prompt-busy", "process-died"],
)
@pytest.mark.asyncio
async def test_the_parents_own_runtime_death_resets_it_and_reports(
    kept: _Done, death: BaseException, reason: str
) -> None:
    kept.stream_results = [death]
    info = make_info(parent="cron:j1")

    await kept.done(info)

    assert _resets(kept) == [("reset", "cron:j1")]
    kept.mgr.notify_injection_failed.assert_called_once_with(info, reason=reason)
    kept.orch._deliver_cron_response.assert_not_awaited()
    assert info._report_delivered is False


@pytest.mark.parametrize(
    "death",
    [gw_mod.PromptBusyExhaustedError("busy"), AcpProcessDied("exited")],
    ids=["prompt-busy", "process-died"],
)
@pytest.mark.asyncio
async def test_a_shared_runtime_death_leaves_the_parent_alone(
    kept: _Done, death: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[Any] = []

    def _caused(client: Any) -> bool:
        asked.append(client)
        return False

    monkeypatch.setattr(runtime_death, "caused_by_this_session", _caused)
    kept.stream_results = [death]
    info = make_info(parent="cron:j1")

    await kept.done(info)

    assert asked == [kept.client]
    assert _resets(kept) == []
    kept.mgr.notify_injection_failed.assert_called_once()


@pytest.mark.asyncio
async def test_a_failed_reset_after_a_death_still_reports(kept: _Done) -> None:
    kept.sessions.reset = AsyncMock(side_effect=RuntimeError("reset failed"))
    kept.stream_results = [AcpProcessDied("exited")]
    info = make_info(parent="cron:j1")

    await kept.done(info)

    kept.mgr.notify_injection_failed.assert_called_once_with(info, reason="ACP process died")


# ── a lost parent session ────────────────────────────────────────────────────


@pytest.fixture
def lost(kept: _Done, monkeypatch: pytest.MonkeyPatch) -> _Done:
    """Every ``AcpError`` the stream raises reads as a lost session, before any tool."""
    monkeypatch.setattr(gw_mod, "acp_error_is_session_not_found", lambda exc: True)
    monkeypatch.setattr(gw_mod, "acp_error_after_tool_activity", lambda exc: False)
    return kept


@pytest.mark.asyncio
async def test_a_lost_parent_session_is_reloaded_once_and_retried_on_the_new_client(
    lost: _Done,
) -> None:
    fresh = MagicMock(name="fresh client")
    lost.clients = [lost.client, fresh]
    lost.stream_results = [AcpError("session gone"), "reply"]
    info = make_info(parent="cron:j1")

    await lost.done(info)

    assert lost.streamed_clients == [lost.client, fresh]
    assert [c for c in lost.calls if c[0] in ("acquire", "reset")] == [
        ("acquire", "cron:j1"),
        ("reset", "cron:j1"),
        ("acquire", "cron:j1"),
    ]
    assert info._report_delivered is True


@pytest.mark.asyncio
async def test_a_second_loss_gives_up_with_its_reason(lost: _Done) -> None:
    lost.stream_results = [AcpError("gone once"), AcpError("gone twice")]
    info = make_info(parent="slack:C1:1700000000.1")

    await lost.done(info)

    (call,) = lost.mgr.notify_injection_failed.call_args_list
    assert call.kwargs["reason"] == (
        f"attempt 1 failed: {gw_mod.SESSION_NOT_FOUND_GIVE_UP_TEXT} gone twice"
    )
    assert len(_resets(lost)) == 1


@pytest.mark.asyncio
async def test_a_loss_after_tool_activity_is_not_replayed(
    lost: _Done, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gw_mod, "acp_error_after_tool_activity", lambda exc: True)
    lost.stream_results = [AcpError("gone mid-tool")]
    info = make_info(parent="slack:C1:1700000000.1")

    await lost.done(info)

    (call,) = lost.mgr.notify_injection_failed.call_args_list
    assert call.kwargs["reason"] == (
        f"attempt 1 failed: {gw_mod.SESSION_NOT_FOUND_NOT_REPLAYED_TEXT} gone mid-tool"
    )
    assert _resets(lost) == []
    assert len(lost.streamed_clients) == 1


# ── any other ACP error ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_acp_error_is_retried_twice_with_backoff_then_raised(
    kept: _Done, clock: _RecordingAsyncio
) -> None:
    kept.stream_results = [AcpError("e1"), AcpError("e2"), AcpError("e3")]
    info = make_info(parent="cron:j1")

    await kept.done(info)

    retries = [c for c in kept.calls if c[0] in ("stream", "cancel", "sleep")]
    # The third error is raised at once; the route's own cleanup cancels last.
    assert [c[0] for c in retries] == [
        "stream",
        "cancel",
        "sleep",
        "stream",
        "cancel",
        "sleep",
        "stream",
        "cancel",
    ]
    assert [c[1] for c in retries if c[0] == "sleep"] == [1, 2]
    assert info._report_undelivered is True


@pytest.mark.asyncio
async def test_a_failed_cancel_between_attempts_does_not_stop_the_retry(
    kept: _Done, clock: _RecordingAsyncio
) -> None:
    kept.sessions.cancel_current = AsyncMock(side_effect=RuntimeError("cancel failed"))
    kept.stream_results = [AcpError("e1"), "reply"]
    info = make_info(parent="cron:j1")

    await kept.done(info)

    assert len(kept.streamed_clients) == 2
    assert ("sleep", 1) in kept.calls
    assert info._report_delivered is True
