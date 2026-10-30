"""One invariant over the agent-session driver's records.

A thread this process put into ``processing`` or ``suspended`` keeps its record
until an ``active`` is sent for it or Slack refuses it for good. Whatever comes
in between (a capability latch from any later call, a failed native call, the
switch turned off, scopes the probe could not read), the turn-end clear still
attempts ``active``, and a native Stop pressed on it is still handled.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_slack_agent_session_status import _caps  # noqa: F401 - autouse fixture
from test_slack_agent_session_stop import FakeSessions, Orch, _env, stop_event  # noqa: F401

from kiro_crew.slack import features
from kiro_crew.slack.agent_sessions import fence, session_events
from kiro_crew.slack.agent_sessions.status import (
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_PROCESSING,
    SESSION_STATUS_SUSPENDED,
    AgentSessionStatus,
)


async def _latch_from_a_rename(orch: Orch) -> None:
    orch.web.errors["agents.sessions.rename"] = "feature_disabled"
    await orch.slack.set_thread_title("C1", "slack:1700.1", "Trip")
    del orch.web.errors["agents.sessions.rename"]
    assert features.latched() == {features.AGENT_SESSIONS: "feature_disabled"}


async def _latch_from_a_status_call(orch: Orch) -> None:
    orch.web.errors["agents.sessions.setStatus"] = "missing_scope"
    await orch.slack.set_session_status("C1", "1700.2", SESSION_STATUS_PROCESSING)
    del orch.web.errors["agents.sessions.setStatus"]
    assert features.AGENT_SESSIONS in features.latched()


async def _failed_approval_pause(orch: Orch) -> None:
    orch.web.errors["agents.sessions.setStatus"] = "internal_error"
    await orch.slack.set_session_status("C1", "1700.1", SESSION_STATUS_SUSPENDED)
    del orch.web.errors["agents.sessions.setStatus"]


async def _toggle_off(orch: Orch) -> None:
    orch._cfg.slack.agent_sessions = False


async def _scopes_unknown(orch: Orch) -> None:
    features.reset_capabilities()  # a reconnect whose probe could not read the scopes
    assert features.availability(features.AGENT_SESSIONS)[1] == features.REASON_SCOPES_UNKNOWN


DISRUPTIONS = {
    "latch from a rename": _latch_from_a_rename,
    "latch from another thread's status": _latch_from_a_status_call,
    "failed native call": _failed_approval_pause,
    "toggle off": _toggle_off,
    "scopes unknown": _scopes_unknown,
}
STARTS = [SESSION_STATUS_PROCESSING, SESSION_STATUS_SUSPENDED]


def _orch() -> Orch:
    orch = Orch(FakeSessions(live={"slack:1700.1"}))
    driver = orch.slack.agent_sessions
    assert isinstance(driver, AgentSessionStatus)
    # The production gate: the toggle AND what the install can run.
    driver._gate = lambda: features.agent_sessions_enabled(orch._cfg)
    return orch


async def _enter(orch: Orch, start: str) -> None:
    await orch.slack.set_session_status("C1", "1700.1", SESSION_STATUS_PROCESSING)
    if start == SESSION_STATUS_SUSPENDED:
        await orch.slack.set_session_status("C1", "1700.1", SESSION_STATUS_SUSPENDED)
    assert orch.slack.agent_sessions.owes_clear("C1", "1700.1")


def _active_sent_for_the_thread(orch: Orch) -> bool:
    return any(
        m == "agents.sessions.setStatus"
        and p.get("thread_ts") == "1700.1"
        and p.get("status") == SESSION_STATUS_ACTIVE
        for m, p in orch.web.calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("start", STARTS)
@pytest.mark.parametrize("disruption", list(DISRUPTIONS))
@pytest.mark.parametrize("ending", ["turn-end clear", "native Stop"])
async def test_a_working_thread_is_still_brought_to_active(
    start: str, disruption: str, ending: str
) -> None:
    orch = _orch()
    reset = fence.enter_turn("C1", "1700.1")
    try:
        await _enter(orch, start)
        await DISRUPTIONS[disruption](orch)
        driver = orch.slack.agent_sessions
        # Whatever happened, the thread is still owed its ``active``.
        assert driver.owes_clear("C1", "1700.1"), disruption
        orch.web.calls.clear()
        if ending == "turn-end clear":
            await orch.slack.set_thread_status("C1", "1700.1", "")
            assert _active_sent_for_the_thread(orch), (disruption, orch.web.calls)
            outcome: Any = None
        else:
            event = stop_event()
            assert session_events._feature_on(orch, event), disruption
            outcome = await session_events.handle_stopped(orch, event)
    finally:
        fence.exit_turn(reset)
    if ending == "native Stop":
        assert outcome == "soft", (disruption, outcome)
        assert orch.sessions.stops, disruption
        assert _active_sent_for_the_thread(orch), (disruption, orch.web.calls)
    # Delivered: nothing is owed any more.
    assert not driver.owes_clear("C1", "1700.1")


@pytest.mark.asyncio
@pytest.mark.parametrize("disruption", list(DISRUPTIONS))
async def test_a_failed_active_keeps_the_thread_owed(disruption: str) -> None:
    """An ``active`` Slack did not accept for a passing reason leaves the thread
    owed, so the next clear tries again; only a final refusal lets it go."""
    orch = _orch()
    await _enter(orch, SESSION_STATUS_PROCESSING)
    await DISRUPTIONS[disruption](orch)
    driver = orch.slack.agent_sessions
    orch.web.errors["agents.sessions.setStatus"] = "internal_error"
    await orch.slack.set_thread_status("C1", "1700.1", "")
    assert driver.owes_clear("C1", "1700.1"), disruption
    orch.web.errors["agents.sessions.setStatus"] = "channel_not_found"
    await orch.slack.set_thread_status("C1", "1700.1", "")
    assert not driver.owes_clear("C1", "1700.1"), disruption


def test_eviction_never_drops_a_thread_still_owed_its_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.slack.agent_sessions import status as status_mod

    monkeypatch.setattr(status_mod, "_MAX_TRACKED_SESSIONS", 3)
    orch = _orch()
    driver = orch.slack.agent_sessions
    working = driver._track(("C1", "1700.1"))
    working.status = SESSION_STATUS_PROCESSING
    for n in range(2, 8):
        driver._track(("C1", f"1700.{n}"))
    assert driver.owes_clear("C1", "1700.1")
    assert len(driver._records) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("disruption", ["latch from a rename", "toggle off", "scopes unknown"])
async def test_a_status_the_old_path_showed_meanwhile_is_cleared_too(disruption: str) -> None:
    """After the disruption, the turn's next status goes the old way. Its end
    then moves the native session to ``active`` AND clears that old status."""
    orch = _orch()
    await _enter(orch, SESSION_STATUS_PROCESSING)
    await DISRUPTIONS[disruption](orch)
    await orch.slack.set_thread_status("C1", "1700.1", "is using bash")
    assert orch.web.calls[-1][0] == "assistant.threads.setStatus"
    orch.web.calls.clear()
    await orch.slack.set_thread_status("C1", "1700.1", "")
    assert [(m, p.get("status")) for m, p in orch.web.calls] == [
        ("agents.sessions.setStatus", SESSION_STATUS_ACTIVE),
        ("assistant.threads.setStatus", ""),
    ]


@pytest.mark.asyncio
async def test_a_stop_settle_that_fails_is_retried_by_the_keepalive() -> None:
    """The stopped turn's own clear is muted, so a Stop's ``active`` that failed for
    a passing reason is retried: the keepalive comes back and, the turn being
    over, sends ``active`` at its next deadline."""
    import asyncio

    from test_slack_agent_session_status import Clock, make_client

    clock = Clock()

    async def fake_sleep(secs: float) -> None:
        clock.now += secs
        await asyncio.sleep(0)

    client, web = make_client(clock=clock, sleep=fake_sleep)
    driver = client.agent_sessions
    reset = fence.enter_turn("C1", "1700.1")
    try:
        await client.set_thread_status("C1", "1700.1", "is using bash")
    finally:
        fence.exit_turn(reset)
    web.errors["agents.sessions.setStatus"] = "internal_error"
    await driver.settle_after_stop("C1", "1700.1")
    assert driver.owes_clear("C1", "1700.1")
    del web.errors["agents.sessions.setStatus"]
    web.calls.clear()
    for _ in range(10):
        await asyncio.sleep(0)
    assert [p["status"] for _, p in web.calls] == [SESSION_STATUS_ACTIVE]
    assert not driver.owes_clear("C1", "1700.1")
    await driver.close()
