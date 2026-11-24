"""A message cron run opens its session's crew log, so its ledger records and reads back.

The session ledger is a fold of the session's crew log, and ``record_update`` refuses a
session that has none. Both cron dispatch sites open the run's log after the claim, under
the job tab's name (``cron-<id>``): what a dashboard turn in that tab records, and what
``canonical_slot`` folds a read with no session id to (the post-compaction reinjection's
``render_snapshot``). A stateless run, new on every fire, opens none.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from crew_log_drain import assert_drained, settle
from slack_gateway_cron_exec_coverage_helpers import (
    _cron_message_cb,
    _job,
    _make_orchestrator,
    _message_arm_sessions,
    _mock_dashboard_state,
)

from kiro_crew import session_ledger as sl
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.crew_log import KIND_SESSION, CrewLog
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.crew_log.store_units import unit_header_slot
from kiro_crew.slack import gateway as _gw

_SNAPSHOT_LINES = ["goal: ledger check", "phase: checking", "next: stop after this call"]


@pytest.fixture(autouse=True)
def _crew_log_on(_floor_monkeypatch):
    """Crew log on in the data home the rootdir floor pins, no writer state carried over."""
    _floor_monkeypatch.setenv(crew_log_emit.CREW_LOG_ENV, "1")
    crew_log_emit.reset_caches()
    crew_log_projection.forget_slot_folds()
    yield
    settle()
    crew_log_projection.forget_slot_folds()


def _client(session_id: str) -> MagicMock:
    client = MagicMock()
    client.session_id = session_id
    client.served_model = "model-x"
    client.cwd = "/work"
    return client


def _record_then_snapshot(session_key: str, session_id: str) -> str:
    """Record from the run's live session, then read back with no session id."""
    sl.record_update(
        sl.ledger_key(session_key),
        session_id=session_id,
        goal="ledger check",
        next_step="stop after this call",
        phase="checking",
        event="started",
        event_kind="phase",
    )
    assert_drained()
    return sl.render_snapshot(sl.ledger_key(session_key))


def _orchestrator() -> Any:
    orch = _make_orchestrator()
    orch._cfg = KiroCrewConfig()
    orch.sessions = _message_arm_sessions()
    orch.dashboard_state = _mock_dashboard_state()
    return orch


@pytest.mark.asyncio
async def test_a_cron_run_records_its_ledger_and_a_session_less_read_finds_it(monkeypatch):
    monkeypatch.setattr(_gw.live, "snapshot", lambda: None)
    orch = _orchestrator()
    orch.sessions.get_or_create.return_value = (_client("acp-cron-1"), True, False)

    async with _cron_message_cb(orch, result_text="ok") as callback:
        await callback(_job(id="jl1", name="ledger check"))
    await asyncio.to_thread(assert_drained)

    assert await asyncio.to_thread(unit_header_slot, KIND_SESSION, "acp-cron-1") == "cron-jl1"
    snapshot = await asyncio.to_thread(_record_then_snapshot, "cron:jl1", "acp-cron-1")
    assert snapshot.splitlines()[1:] == _SNAPSHOT_LINES


@pytest.mark.asyncio
async def test_a_stateless_run_opens_no_crew_log(monkeypatch):
    monkeypatch.setattr(_gw.live, "snapshot", lambda: None)
    orch = _orchestrator()
    orch.sessions.get_or_create.return_value = (_client("acp-cron-run"), True, False)

    async with _cron_message_cb(orch, result_text="ok") as callback:
        await callback(_job(id="jl2", name="stateless", persistent_session=False))
    await asyncio.to_thread(assert_drained)

    orch.sessions.get_or_create.assert_awaited()
    assert not await asyncio.to_thread(CrewLog.exists, KIND_SESSION, "acp-cron-run")


@pytest.mark.asyncio
async def test_each_agent_sequence_step_records_into_its_own_crew_log(monkeypatch):
    monkeypatch.setattr(_gw.live, "snapshot", lambda: None)
    steps = {"cron:jls:kirocrew": "acp-step-a", "cron:jls:kirocrew-lite": "acp-step-b"}
    orch = _orchestrator()
    orch.sessions.get_or_create.side_effect = [
        (_client(sid), True, False) for sid in steps.values()
    ]
    job = _job(id="jls", name="ledger sequence", agent_sequence=["kirocrew", "kirocrew-lite"])

    async with _cron_message_cb(orch, result_text="ok") as callback:
        await callback(job)
    await asyncio.to_thread(assert_drained)

    for key, sid in steps.items():
        tab = "cron-jls-" + key.rsplit(":", 1)[1]
        assert await asyncio.to_thread(unit_header_slot, KIND_SESSION, sid) == tab
        snapshot = await asyncio.to_thread(_record_then_snapshot, key, sid)
        assert snapshot.splitlines()[1:] == _SNAPSHOT_LINES
