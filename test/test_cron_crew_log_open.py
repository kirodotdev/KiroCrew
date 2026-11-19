"""A message cron run opens its session's crew log, so its ledger can record.

The session ledger (``session_ledger_record`` / ``session_ledger_read``) is a fold of
the session's crew log, and the record refuses a session that has none. The dashboard
runner and the channel dispatchers open their own sessions' logs every turn; a cron
run opens its own through the channel opener, ``messaging.dispatch.open_turn_crew_log``,
called from the run's dispatch after the claim and before the prompt. The log is filed
under the job tab's name (``cron-<id>``), the slot a dashboard turn on the same key
records, so a dashboard successor's ``previous`` edge can cite it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
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

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger as sl
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.crew_log.store_units import unit_header_slot
from kiro_crew.messaging.dispatch_crew_log import open_turn_crew_log
from kiro_crew.slack import gateway as _gw

SESSION = "acp-cron-1"


@pytest.fixture(autouse=True)
def _crew_log_on(_floor_monkeypatch):
    """Crew log on, and no writer state carried between tests.

    The data home is the one the rootdir floor already pins per test.
    """
    _floor_monkeypatch.setenv(crew_log_emit.CREW_LOG_ENV, "1")
    crew_log_emit.reset_caches()
    crew_log_projection.forget_slot_folds()
    yield
    settle()
    crew_log_projection.forget_slot_folds()


def _provider(session_id: str = SESSION) -> SimpleNamespace:
    return SimpleNamespace(session_id=session_id, served_model="model-x", cwd="/work")


def _record_and_read(session_key: str, session_id: str = SESSION) -> dict:
    state, _durable = sl.record_update(
        session_key,
        session_id=session_id,
        goal="ledger check",
        next_step="stop after this call",
        phase="checking",
        event="started",
        event_kind="phase",
    )
    assert state["goal"] == "ledger check"
    assert_drained()
    return sl.read_state(session_key, session_id)


def test_without_an_opener_a_cron_session_has_no_crew_log_to_record_into():
    with pytest.raises(sl.LedgerUnavailable, match="no crew log yet"):
        sl.record_update("cron:j1", session_id=SESSION, goal="ledger check")


def test_an_opened_log_lets_the_ledger_record_and_read_back():
    open_turn_crew_log(
        _provider(), session_key="cron:j1", agent="kirocrew", resumed=False, slot="cron-j1"
    )
    assert_drained()

    assert CrewLog.exists(lg.KIND_SESSION, SESSION)
    state = _record_and_read("cron:j1")
    assert (state["goal"], state["phase"], state["next"]) == (
        "ledger check",
        "checking",
        "stop after this call",
    )


def test_a_per_run_key_records_under_its_own_session():
    run_sid = "acp-cron-run-7"
    open_turn_crew_log(
        _provider(run_sid),
        session_key="cron:j1:run7",
        agent="kirocrew",
        resumed=False,
        slot="cron-j1-run7",
    )
    assert_drained()

    assert _record_and_read("cron:j1:run7", run_sid)["goal"] == "ledger check"


def test_a_provider_without_a_session_id_opens_nothing_and_does_not_raise():
    open_turn_crew_log(
        _provider(""), session_key="cron:j1", agent="kirocrew", resumed=False, slot="cron-j1"
    )
    assert_drained()

    assert not CrewLog.exists(lg.KIND_SESSION, SESSION)


def test_a_failing_emitter_never_fails_the_run(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("emitter down")

    monkeypatch.setattr(crew_log_emit, "on_session_opened", _boom)
    open_turn_crew_log(
        _provider(), session_key="cron:j1", agent="kirocrew", resumed=False, slot="cron-j1"
    )


def test_a_named_slot_is_the_one_the_header_records():
    open_turn_crew_log(
        _provider(), session_key="cron:j1", agent="kirocrew", resumed=False, slot="cron-j1"
    )
    assert_drained()

    assert unit_header_slot(lg.KIND_SESSION, SESSION) == "cron-j1"


def test_without_a_named_slot_a_channel_key_keeps_its_transcript_stem():
    open_turn_crew_log(_provider(), session_key="slack:C1:171", agent="kirocrew", resumed=False)
    assert_drained()

    assert unit_header_slot(lg.KIND_SESSION, SESSION) == "slack_C1_171"


@pytest.mark.asyncio
async def test_a_single_agent_cron_run_opens_its_crew_log(monkeypatch):
    """The dispatch itself opens the log, so the run's ledger records in its first turn."""
    monkeypatch.setattr(_gw.live, "snapshot", lambda: None)
    orch = _make_orchestrator()
    orch._cfg = KiroCrewConfig()
    orch.sessions = _message_arm_sessions()
    client = MagicMock()
    client.session_id = SESSION
    client.served_model = "model-x"
    client.cwd = "/work"
    orch.sessions.get_or_create.return_value = (client, True, False)
    orch.dashboard_state = _mock_dashboard_state()
    job = _job(id="jl1", name="ledger check")

    async with _cron_message_cb(orch, result_text="ok") as callback:
        await callback(job)
    await asyncio.to_thread(assert_drained)

    assert CrewLog.exists(lg.KIND_SESSION, SESSION)
    # The job tab's name, the slot a dashboard turn in that tab records.
    assert await asyncio.to_thread(unit_header_slot, lg.KIND_SESSION, SESSION) == "cron-jl1"
    state = await asyncio.to_thread(_record_and_read, "cron:jl1")
    assert state["goal"] == "ledger check"


@pytest.mark.asyncio
async def test_each_step_of_an_agent_sequence_opens_its_crew_log(monkeypatch):
    """The sequence dispatch site opens the log of every step session it prompts."""
    monkeypatch.setattr(_gw.live, "snapshot", lambda: None)
    opened: list[tuple[str, str]] = []
    real_opener = _gw.open_turn_crew_log

    def _spy(provider, *, session_key, **kwargs):
        opened.append((session_key, kwargs.get("slot", "")))
        real_opener(provider, session_key=session_key, **kwargs)

    monkeypatch.setattr(_gw, "open_turn_crew_log", _spy)
    orch = _make_orchestrator()
    orch._cfg = KiroCrewConfig()
    orch.sessions = _message_arm_sessions()
    clients = []
    for sid in ("acp-cron-step-a", "acp-cron-step-b"):
        client = MagicMock()
        client.session_id = sid
        client.served_model = "model-x"
        client.cwd = "/work"
        clients.append((client, True, False))
    orch.sessions.get_or_create.side_effect = clients
    orch.dashboard_state = _mock_dashboard_state()
    job = _job(id="jls", name="ledger sequence", agent_sequence=["kirocrew", "kirocrew-lite"])

    async with _cron_message_cb(orch, result_text="ok") as callback:
        await callback(job)
    await asyncio.to_thread(assert_drained)

    assert opened == [
        ("cron:jls:kirocrew", "cron-jls-kirocrew"),
        ("cron:jls:kirocrew-lite", "cron-jls-kirocrew-lite"),
    ]
    assert CrewLog.exists(lg.KIND_SESSION, "acp-cron-step-a")
    assert CrewLog.exists(lg.KIND_SESSION, "acp-cron-step-b")
    state = await asyncio.to_thread(_record_and_read, "cron:jls:kirocrew", "acp-cron-step-a")
    assert state["goal"] == "ledger check"
