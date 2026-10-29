"""Coverage tests for the auto-research builtin backend (``handlers.py``).

``test_auto_research.py`` already covers campaign CRUD, validation, stagnation
and the stall verdict. What was left almost entirely unexercised is everything
the module does *between* those pieces:

  * the **workflow execution mode** — ``_launch_workflow`` / ``_stop_workflow``
    and the ``_poll_workflow_campaign`` adapter that translates a Dynamic
    Workflow run's events into the cycle-file + SSE model the UI consumes,
    plus the three tiny ``workflow_run.json`` accessors it depends on;
  * the **watchdog loop** — the disabled-app suspension, the 24h trust expiry,
    trust re-establishment, loop re-arming, the count-advance transitions
    (COMPLETE / cycle cap / STAGNANT) and the idle-deadline settle;
  * the **SSE stream handler**, driven with a stubbed ``StreamResponse`` so no
    listening socket is bound;
  * the **grill question-tree** helpers and their HTTP endpoint;
  * the **guard rails** every handler runs first — the 401 when the gateway's
    auth middleware never ran and the 400 on a malformed campaign id or body.

Everything is patched at the workflow-service and autonudge boundary, so no
network, no subprocess and no real gateway is involved. ``DB_PATH`` /
``RESEARCH_DIR`` are pinned into ``tmp_path`` (the same
fixture shape ``test_auto_research.py`` uses) on top of the per-test
``KIROCREW_HOME`` that ``conftest.py`` already pins, so nothing is written
outside the temp tree. Handlers are invoked through aiohttp's own
``make_mocked_request`` rather than a live ``TestServer``, so no socket is bound
and no gateway task is started.

The report / nudge / add-question, artifact and knowledge routes, the action
dispatch, undecodable bodies and the loop and exploration helpers are in
``test_auto_research_handlers_coverage_campaign_routes.py``; the fixtures and
request builders both modules use are in ``auto_research_handlers_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from auto_research_handlers_coverage_helpers import _isolate_fixture  # noqa: F401
from auto_research_handlers_coverage_helpers import _no_autonudge  # noqa: F401
from auto_research_handlers_coverage_helpers import _no_stray_sse_queues  # noqa: F401
from auto_research_handlers_coverage_helpers import sse_fixture  # noqa: F401
from auto_research_handlers_coverage_helpers import (
    _app,
    _body,
    _campaign,
    _mk,
    _running,
    _status,
    _write_finding,
)

from kiro_crew.apps.builtins.auto_research import handlers as h
from kiro_crew.testing.wait import async_wait_until

# --- helpers ----------------------------------------------------------------


def _workflow_state(**svc_attrs: Any) -> SimpleNamespace:
    return SimpleNamespace(workflow_service=SimpleNamespace(**svc_attrs))


async def _drive_watchdog(app: Any, until, timeout: float = 10.0) -> bool:
    """Run one or more real watchdog iterations, stopping as soon as ``until()``.

    The loop is a ``while True`` driven by ``asyncio.sleep(POLL_INTERVAL)``;
    callers shorten POLL_INTERVAL, so this polls the observable side effect and
    always cancels the task (no leaked background work).

    Waiting is delegated to ``_await_until`` so both helpers share ONE budget:
    two call sites here wait on a status commit made from a worker thread AND
    the SSE emitted from that thread's ``call_soon_threadsafe`` hook, which is
    two scheduling hops, and the tighter of two budgets is what made those
    flake on a loaded shard. The timeout is a deadlock backstop; observable
    state is what ends a successful wait.
    """
    task = asyncio.ensure_future(h._watchdog_loop(app))
    try:
        return await _await_until(until, timeout=timeout)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.fixture
def fast_watchdog(monkeypatch: pytest.MonkeyPatch):
    """Shorten the poll interval and make the app look enabled by default."""
    monkeypatch.setattr(h, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(h, "is_app_enabled", lambda _name: True)


@pytest.fixture
def polls(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Counts watchdog polls of a campaign's findings dir.

    Lets a test sequence "baseline recorded" -> "new finding written" on an
    observed event instead of a wall-clock sleep, so the count-advance
    transitions cannot flake on a slow (or fast) runner.
    """
    real = h._list_cycle_files
    seen = {"n": 0}

    def _counted(cid: str):
        seen["n"] += 1
        return real(cid)

    monkeypatch.setattr(h, "_list_cycle_files", _counted)
    return seen


async def _await_until(predicate, timeout: float = 10.0) -> bool:
    """True once ``predicate()`` holds; a wait that never does fails by name."""
    await async_wait_until(predicate, timeout=timeout)
    return True


# --- workflow_run.json accessors -------------------------------------------


class TestWorkflowRunFile:
    def test_write_records_run_id_and_cycle_offset(self, _isolate: Path):
        cid = _campaign()
        _write_finding(cid, 1)
        _write_finding(cid, 2)
        h._write_workflow_run_id(cid, "run-1")
        payload = json.loads((h._campaign_dir(cid) / h._WORKFLOW_RUN_FILE).read_text())
        assert payload["run_id"] == "run-1"
        assert payload["cycle_offset"] == 2
        assert h._read_workflow_run_id(cid) == "run-1"
        assert h._read_workflow_cycle_offset(cid) == 2

    def test_absent_file_reads_as_zero_offset_and_no_run_id(self, _isolate: Path):
        cid = _campaign()
        assert h._read_workflow_cycle_offset(cid) == 0
        assert h._read_workflow_run_id(cid) is None

    def test_invalid_id_reads_as_zero_offset_and_no_run_id(self):
        assert h._read_workflow_cycle_offset("../etc") == 0
        assert h._read_workflow_run_id("../etc") is None

    def test_malformed_file_reads_as_zero_offset_and_no_run_id(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / h._WORKFLOW_RUN_FILE).write_text("{not json")
        assert h._read_workflow_cycle_offset(cid) == 0
        assert h._read_workflow_run_id(cid) is None

    def test_non_numeric_offset_reads_as_zero(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / h._WORKFLOW_RUN_FILE).write_text(
            json.dumps({"run_id": "r", "cycle_offset": "many"})
        )
        assert h._read_workflow_cycle_offset(cid) == 0
        assert h._read_workflow_run_id(cid) == "r"

    def test_blank_run_id_reads_as_none(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / h._WORKFLOW_RUN_FILE).write_text(json.dumps({"run_id": ""}))
        assert h._read_workflow_run_id(cid) is None

    def test_execution_mode_defaults_and_round_trips(self, _isolate: Path):
        assert h._campaign_execution_mode(_campaign()) == h.DEFAULT_EXECUTION_MODE
        assert h._campaign_execution_mode(_campaign(execution_mode="workflow")) == "workflow"

    def test_execution_mode_of_unknown_campaign_is_the_default(self, _isolate: Path):
        _campaign()  # ensure the schema exists
        assert h._campaign_execution_mode("deadbeef") == h.DEFAULT_EXECUTION_MODE


# --- _launch_workflow / _stop_workflow -------------------------------------


class TestLaunchWorkflow:
    @pytest.mark.asyncio
    async def test_missing_workflow_service_fails_the_campaign(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        await h._launch_workflow(_mk("PATCH", cid, app=_app(state=SimpleNamespace())), cid)
        assert _status(cid) == h.CampaignStatus.FAILED
        assert sse.types() == ["failed"]
        assert "unavailable" in (h.get_campaign(cid) or {})["error_message"]

    @pytest.mark.asyncio
    async def test_absent_state_fails_the_campaign(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        await h._launch_workflow(_mk("PATCH", cid, app=_app()), cid)
        assert _status(cid) == h.CampaignStatus.FAILED

    @pytest.mark.asyncio
    async def test_unknown_campaign_is_a_no_op(self, _isolate: Path, sse):
        _campaign()  # create the schema
        start = AsyncMock(return_value={"run_id": "r"})
        await h._launch_workflow(
            _mk("PATCH", "deadbeef", app=_app(state=_workflow_state(start=start))), "deadbeef"
        )
        start.assert_not_awaited()
        assert sse.events == []

    @pytest.mark.asyncio
    async def test_successful_start_persists_the_run_id(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow", max_cycles=7)
        start = AsyncMock(return_value={"run_id": "run-42"})
        await h._launch_workflow(
            _mk("PATCH", cid, app=_app(state=_workflow_state(start=start))), cid
        )
        assert h._read_workflow_run_id(cid) == "run-42"
        assert start.await_args.kwargs["name"] == "research-" + cid
        assert start.await_args.kwargs["args"]["max_rounds"] == 7
        assert sse.events == []

    @pytest.mark.asyncio
    async def test_start_raising_fails_the_campaign(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        start = AsyncMock(side_effect=RuntimeError("engine down"))
        await h._launch_workflow(
            _mk("PATCH", cid, app=_app(state=_workflow_state(start=start))), cid
        )
        assert _status(cid) == h.CampaignStatus.FAILED
        assert "Workflow start failed" in (h.get_campaign(cid) or {})["error_message"]
        assert sse.types() == ["failed"]

    @pytest.mark.asyncio
    async def test_start_without_a_run_id_fails_the_campaign(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        start = AsyncMock(return_value=None)
        await h._launch_workflow(
            _mk("PATCH", cid, app=_app(state=_workflow_state(start=start))), cid
        )
        assert _status(cid) == h.CampaignStatus.FAILED
        assert "no run ID" in (h.get_campaign(cid) or {})["error_message"]
        assert h._read_workflow_run_id(cid) is None


class _WorkflowRuns:
    """A workflow service holding each run's status, so a cancel leaves state behind."""

    def __init__(self, **runs: str) -> None:
        self.runs = dict(runs)

    async def cancel(self, run_id: str) -> None:
        if run_id not in self.runs:
            raise KeyError(run_id)
        self.runs[run_id] = "cancelled"


class TestStopWorkflow:
    @pytest.mark.asyncio
    async def test_cancels_the_recorded_run(self, _isolate: Path):
        cid = _campaign(execution_mode="workflow")
        h._write_workflow_run_id(cid, "run-9")
        service = _WorkflowRuns(**{"run-9": "running", "run-1": "running"})
        await h._stop_workflow(
            _mk("PATCH", cid, app=_app(state=SimpleNamespace(workflow_service=service))), cid
        )
        assert service.runs == {"run-9": "cancelled", "run-1": "running"}

    @pytest.mark.asyncio
    async def test_no_run_id_means_no_cancel(self, _isolate: Path):
        cid = _campaign(execution_mode="workflow")
        service = _WorkflowRuns(**{"run-1": "running"})
        await h._stop_workflow(
            _mk("PATCH", cid, app=_app(state=SimpleNamespace(workflow_service=service))), cid
        )
        assert service.runs == {"run-1": "running"}

    @pytest.mark.asyncio
    async def test_cancel_failure_is_swallowed(
        self, _isolate: Path, caplog: pytest.LogCaptureFixture
    ):
        from kiro_crew.apps.builtins.auto_research.campaign import LOGGER_NAME

        cid = _campaign(execution_mode="workflow")
        h._write_workflow_run_id(cid, "run-9")
        cancel = AsyncMock(side_effect=RuntimeError("gone"))
        with caplog.at_level("ERROR", logger=LOGGER_NAME):
            await h._stop_workflow(
                _mk("PATCH", cid, app=_app(state=_workflow_state(cancel=cancel))), cid
            )
        cancel.assert_awaited_once()
        failures = [r for r in caplog.records if r.name == LOGGER_NAME]
        assert [r.getMessage() for r in failures] == [
            f"auto_research: workflow cancel failed for {cid}"
        ]
        assert str(failures[0].exc_info[1]) == "gone"

    @pytest.mark.asyncio
    async def test_absent_service_is_a_no_op(self, _isolate: Path):
        cid = _campaign(execution_mode="workflow")
        h._write_workflow_run_id(cid, "run-9")
        await h._stop_workflow(_mk("PATCH", cid, app=_app()), cid)
        # Nothing was cancelled, so the run id stays recorded for a later stop.
        assert h._read_workflow_run_id(cid) == "run-9"


# --- _poll_workflow_campaign ----------------------------------------------


def _snapshot(*, status: str = "running", events: list | None = None, **extra: Any) -> dict:
    snap: dict[str, Any] = {"status": status, "events": events or []}
    snap.update(extra)
    return snap


def _investigate_events(*labels: str, ok: bool = True, summary: str = "found it") -> list[dict]:
    events: list[dict] = []
    for i, label in enumerate(labels):
        agent_id = "a%d" % i
        events.append({"type": "agent_started", "data": {"agent_id": agent_id, "label": label}})
        events.append(
            {
                "type": "agent_finished",
                "data": {"agent_id": agent_id, "ok": ok, "result_summary": summary},
            }
        )
    return events


class TestPollWorkflowCampaign:
    @pytest.mark.asyncio
    async def test_absent_service_or_run_id_is_a_no_op(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        await h._poll_workflow_campaign(cid, None, h.get_campaign(cid)["started_at"])
        await h._poll_workflow_campaign(cid, _workflow_state(result=MagicMock()), h.get_campaign(cid)["started_at"])
        assert sse.events == []

    @pytest.mark.asyncio
    async def test_lost_snapshot_within_the_hour_is_tolerated(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        await h._poll_workflow_campaign(cid, _workflow_state(result=MagicMock(return_value=None)), h.get_campaign(cid)["started_at"])
        assert _status(cid) == h.CampaignStatus.RUNNING
        assert sse.events == []

    @pytest.mark.asyncio
    async def test_lost_snapshot_after_an_hour_fails_the_campaign(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        run_file = h._campaign_dir(cid) / h._WORKFLOW_RUN_FILE
        run_file.write_text(json.dumps({"run_id": "run-1", "ts": time.time() - 7200}))
        await h._poll_workflow_campaign(cid, _workflow_state(result=MagicMock(return_value=None)), h.get_campaign(cid)["started_at"])
        assert _status(cid) == h.CampaignStatus.FAILED
        assert "snapshot lost" in (h.get_campaign(cid) or {})["error_message"]
        # The terminal status commit and the SSE emit are two scheduling events:
        # _sse_from_thread hands _emit_sse to the loop via call_soon_threadsafe
        # from the worker thread, so a direct await of the poll can return before
        # that callback has run. Wait on the signal this test asserts on.
        assert await _await_until(lambda: "failed" in sse.types())
        assert sse.types() == ["failed"]

    @pytest.mark.asyncio
    async def test_lost_snapshot_with_malformed_run_file_is_tolerated(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        d = h._campaign_dir(cid)
        (d / h._WORKFLOW_RUN_FILE).write_text(json.dumps({"run_id": "r", "ts": "yesterday"}))
        await h._poll_workflow_campaign(cid, _workflow_state(result=MagicMock(return_value=None)), h.get_campaign(cid)["started_at"])
        assert _status(cid) == h.CampaignStatus.RUNNING
        assert sse.events == []

    @pytest.mark.asyncio
    async def test_finished_investigations_become_cycle_findings(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        snap = _snapshot(
            events=_investigate_events("investigate: how is it rate limited", "plan: outline")
        )
        state = _workflow_state(result=MagicMock(return_value=snap))
        await h._poll_workflow_campaign(cid, state, h.get_campaign(cid)["started_at"])
        files = h._list_cycle_files(cid)
        assert len(files) == 1  # only the investigate agent produced a cycle
        finding = json.loads(files[0].read_text())
        assert finding["cycle"] == 1
        assert finding["key_insight"] == "how is it rate limited"
        assert finding["summary"] == "found it"
        assert (h.get_campaign(cid) or {})["total_cycles"] == 1
        assert sse.types() == ["new_finding"]

    @pytest.mark.asyncio
    async def test_failed_investigations_are_skipped(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        snap = _snapshot(events=_investigate_events("investigate: nope", ok=False))
        await h._poll_workflow_campaign(cid, _workflow_state(result=MagicMock(return_value=snap)), h.get_campaign(cid)["started_at"])
        assert h._list_cycle_files(cid) == []
        assert sse.events == []

    @pytest.mark.asyncio
    async def test_repeat_poll_does_not_rewrite_an_existing_cycle(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        snap = _snapshot(events=_investigate_events("investigate: a"))
        state = _workflow_state(result=MagicMock(return_value=snap))
        await h._poll_workflow_campaign(cid, state, h.get_campaign(cid)["started_at"])
        first = h._list_cycle_files(cid)[0].read_text()
        await h._poll_workflow_campaign(cid, state, h.get_campaign(cid)["started_at"])
        assert len(h._list_cycle_files(cid)) == 1
        assert h._list_cycle_files(cid)[0].read_text() == first
        assert sse.types() == ["new_finding"]  # no duplicate event

    @pytest.mark.asyncio
    async def test_cycle_offset_appends_after_a_resume(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        _write_finding(cid, 1)
        _write_finding(cid, 2)
        h._write_workflow_run_id(cid, "run-2")  # records offset 2
        snap = _snapshot(events=_investigate_events("investigate: resumed"))
        await h._poll_workflow_campaign(cid, _workflow_state(result=MagicMock(return_value=snap)), h.get_campaign(cid)["started_at"])
        cycles = [json.loads(p.read_text())["cycle"] for p in h._list_cycle_files(cid)]
        assert cycles == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_bare_label_is_used_verbatim_as_the_insight(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        snap = _snapshot(events=_investigate_events("investigate-no-colon"))
        await h._poll_workflow_campaign(cid, _workflow_state(result=MagicMock(return_value=snap)), h.get_campaign(cid)["started_at"])
        finding = json.loads(h._list_cycle_files(cid)[0].read_text())
        assert finding["key_insight"] == "investigate-no-colon"

    @pytest.mark.asyncio
    async def test_finished_run_writes_the_report_and_completes(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        snap = _snapshot(status="finished", result={"report": "# Report\nAll done."})
        await h._poll_workflow_campaign(cid, _workflow_state(result=MagicMock(return_value=snap)), h.get_campaign(cid)["started_at"])
        assert (h._campaign_dir(cid) / "FINDINGS.md").read_text() == "# Report\nAll done."
        assert _status(cid) == h.CampaignStatus.COMPLETE
        # The terminal status commit and the SSE emit are two scheduling events:
        # _sse_from_thread hands _emit_sse to the loop via call_soon_threadsafe
        # from the worker thread, so a direct await of the poll can return before
        # that callback has run. Wait on the signal this test asserts on.
        assert await _await_until(lambda: "complete" in sse.types())
        assert sse.types() == ["complete"]

    @pytest.mark.asyncio
    async def test_finished_run_falls_back_to_the_findings_list(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        snap = _snapshot(status="finished", result={"findings": ["one", "two"]})
        await h._poll_workflow_campaign(cid, _workflow_state(result=MagicMock(return_value=snap)), h.get_campaign(cid)["started_at"])
        assert (h._campaign_dir(cid) / "FINDINGS.md").read_text() == "one\n\ntwo"

    @pytest.mark.asyncio
    async def test_finished_run_with_no_result_writes_a_placeholder(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        snap = _snapshot(status="finished", result="not-a-dict")
        await h._poll_workflow_campaign(cid, _workflow_state(result=MagicMock(return_value=snap)), h.get_campaign(cid)["started_at"])
        assert (h._campaign_dir(cid) / "FINDINGS.md").read_text() == "(no findings gathered)"
        assert _status(cid) == h.CampaignStatus.COMPLETE

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["failed", "cancelled"])
    async def test_terminal_failure_states_fail_the_campaign(self, _isolate: Path, sse, status):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        snap = _snapshot(status=status, error="engine exploded")
        await h._poll_workflow_campaign(cid, _workflow_state(result=MagicMock(return_value=snap)), h.get_campaign(cid)["started_at"])
        assert _status(cid) == h.CampaignStatus.FAILED
        assert (h.get_campaign(cid) or {})["error_message"] == "engine exploded"
        # The terminal status commit and the SSE emit are two scheduling events:
        # _sse_from_thread hands _emit_sse to the loop via call_soon_threadsafe
        # from the worker thread, so a direct await of the poll can return before
        # that callback has run. Wait on the signal this test asserts on.
        assert await _await_until(lambda: "failed" in sse.types())
        assert sse.types() == ["failed"]

    @pytest.mark.asyncio
    async def test_terminal_failure_without_an_error_gets_a_default_message(
        self, _isolate: Path, sse
    ):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        snap = _snapshot(status="failed")
        await h._poll_workflow_campaign(cid, _workflow_state(result=MagicMock(return_value=snap)), h.get_campaign(cid)["started_at"])
        assert "without completing" in (h.get_campaign(cid) or {})["error_message"]

    @pytest.mark.asyncio
    async def test_report_is_stripped_when_the_redactors_are_unavailable(
        self, _isolate: Path, sse, monkeypatch: pytest.MonkeyPatch
    ):
        """Fail-closed: no redactors means LLM text is masked, never persisted raw."""
        monkeypatch.setattr(h, "_HAS_SECURITY", False)
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        snap = _snapshot(status="finished", result={"report": "token=hunter2"})
        await h._poll_workflow_campaign(cid, _workflow_state(result=MagicMock(return_value=snap)), h.get_campaign(cid)["started_at"])
        written = (h._campaign_dir(cid) / "FINDINGS.md").read_text()
        assert "hunter2" not in written
        assert written == "[REDACTED]"

    @pytest.mark.asyncio
    async def test_poll_never_raises_into_the_watchdog(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        boom = MagicMock(side_effect=RuntimeError("snapshot store on fire"))
        await h._poll_workflow_campaign(cid, _workflow_state(result=boom), h.get_campaign(cid)["started_at"])
        assert _status(cid) == h.CampaignStatus.RUNNING


# --- the watchdog loop -----------------------------------------------------


class TestWatchdogLoop:
    @pytest.mark.asyncio
    async def test_disabled_app_suspends_research_loops(
        self, _isolate: Path, fast_watchdog, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(h, "is_app_enabled", lambda _name: False)
        suspend = AsyncMock()
        monkeypatch.setattr(h, "_suspend_research_loops_while_disabled", suspend)
        cid = _campaign()
        _running(cid)
        assert await _drive_watchdog({"state": None}, lambda: suspend.await_count > 0)
        assert _status(cid) == h.CampaignStatus.RUNNING  # untouched while disabled

    @pytest.mark.asyncio
    async def test_workflow_mode_campaign_is_delegated_to_the_adapter(
        self, _isolate: Path, fast_watchdog, monkeypatch: pytest.MonkeyPatch
    ):
        poll = AsyncMock()
        monkeypatch.setattr(h, "_poll_workflow_campaign", poll)
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        assert await _drive_watchdog({"state": None}, lambda: poll.await_count > 0)
        assert poll.await_args.args[0] == cid
        # The adapter owns a workflow campaign: no agent-mode bookkeeping touched it.
        assert _status(cid) == h.CampaignStatus.RUNNING
        assert (h.get_campaign(cid) or {})["total_cycles"] == 0

    @pytest.mark.asyncio
    async def test_expired_trust_forces_reauthorization(self, _isolate: Path, fast_watchdog, sse):
        cid = _campaign()
        _running(cid, started_at=time.time() - (h._TRUST_TTL_SECS + 60))
        slot = SimpleNamespace(_trust=True, running=False)
        state = SimpleNamespace(_slots={f"research-{cid}": slot})
        assert await _drive_watchdog(
            {"state": state},
            # Wait on BOTH the status commit and the independently-scheduled SSE
            # emit (see test_new_verified_finding_completes_the_campaign) -- the
            # assertion below reads sse.types(), so the SSE half must be observed
            # before _drive_watchdog's finally cancels the loop.
            lambda: _status(cid) == h.CampaignStatus.NEEDS_INPUT and "needs_input" in sse.types(),
        )
        assert slot._trust is False
        question = json.loads((h._campaign_dir(cid) / "questions.json").read_text())
        assert "24h" in question["question"]
        assert "needs_input" in sse.types()

    @pytest.mark.asyncio
    async def test_trust_is_reestablished_and_a_paused_loop_rearmed(
        self, _isolate: Path, fast_watchdog, monkeypatch: pytest.MonkeyPatch
    ):
        from kiro_crew.autonudge import AutoNudgeService

        cid = _campaign()
        _running(cid)
        slot = SimpleNamespace(_trust=False, running=True)
        state = SimpleNamespace(_slots={f"research-{cid}": slot})
        svc = AutoNudgeService(base_dir=_isolate / "autonudge")
        await svc.start()
        try:
            loop = await svc.add(slot_key=f"research-{cid}", message="run", idle_secs=60)
            await svc.update(loop.id, active=False)
            monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
            assert await _drive_watchdog({"state": state}, lambda: slot._trust and loop.active)
            assert svc.get_by_slot(f"research-{cid}") is loop
            assert loop.active is True
            assert slot._trust is True
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_pending_question_pauses_an_attended_campaign(
        self, _isolate: Path, fast_watchdog, sse
    ):
        cid = _campaign(auto_approve=False)
        _running(cid)
        (h._campaign_dir(cid) / "questions.json").write_text('{"question": "Which DB?"}')
        assert await _drive_watchdog(
            {"state": None},
            lambda: _status(cid) == h.CampaignStatus.NEEDS_INPUT and "needs_input" in sse.types(),
        )
        assert "needs_input" in sse.types()

    @pytest.mark.asyncio
    async def test_new_verified_finding_completes_the_campaign(
        self, _isolate: Path, fast_watchdog, polls, sse, monkeypatch: pytest.MonkeyPatch
    ):
        advance = MagicMock()
        monkeypatch.setattr(h, "_advance_exploration", advance)
        cid = _campaign(auto_approve=True, max_cycles=30)
        _running(cid)
        _write_finding(cid, 1)
        state = SimpleNamespace(_slots={})

        task = asyncio.ensure_future(h._watchdog_loop({"state": state}))
        try:
            assert await _await_until(lambda: polls["n"] >= 1)  # baseline count recorded
            _write_finding(cid, 2, verification={"passed": True})
            assert await _await_until(lambda: _status(cid) == h.CampaignStatus.COMPLETE)
            # The status commit (worker thread) and the SSE emit (call_soon_threadsafe
            # from _sse_from_thread's on_commit hook) are two separate scheduling
            # events, not one step — waiting on status alone proves the commit
            # happened, not that the loop has drained the queued SSE callback yet.
            # Wait on the SIGNAL THIS TEST ASSERTS ON, or task.cancel() below can win
            # the race and the "complete" event goes missing from sse.types().
            assert await _await_until(lambda: "complete" in sse.types())
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert (h.get_campaign(cid) or {})["total_cycles"] == 2
        assert sse.types() == ["new_finding", "complete"]
        advance.assert_called_once_with(cid)

    @pytest.mark.asyncio
    async def test_reaching_the_cycle_cap_completes_the_campaign(
        self, _isolate: Path, fast_watchdog, polls, sse
    ):
        cid = _campaign(auto_approve=True, max_cycles=2)
        _running(cid)
        _write_finding(cid, 1)
        task = asyncio.ensure_future(h._watchdog_loop({"state": None}))
        try:
            assert await _await_until(lambda: polls["n"] >= 1)
            _write_finding(cid, 2)  # unverified, but hits max_cycles
            assert await _await_until(lambda: _status(cid) == h.CampaignStatus.COMPLETE)
            # See test_new_verified_finding_completes_the_campaign: the status commit
            # and the SSE emit are scheduled independently, so wait on the SSE signal
            # this test actually asserts on rather than relying on task.cancel()'s
            # timing to have let the queued callback run first.
            assert await _await_until(lambda: "complete" in sse.types())
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert sse.types() == ["new_finding", "complete"]

    @pytest.mark.asyncio
    async def test_repeated_empty_cycles_mark_the_campaign_stagnant(
        self, _isolate: Path, fast_watchdog, polls, sse
    ):
        cid = _campaign(auto_approve=True, max_cycles=30)
        _running(cid)
        for i in range(1, 6):
            _write_finding(cid, i, new_findings_count=0)
        task = asyncio.ensure_future(h._watchdog_loop({"state": None}))
        try:
            assert await _await_until(lambda: polls["n"] >= 1)
            _write_finding(cid, 6, new_findings_count=0)
            assert await _await_until(lambda: _status(cid) == h.CampaignStatus.STAGNANT)
            # See test_new_verified_finding_completes_the_campaign: status commit and
            # SSE emit are scheduled independently, so wait on the SSE signal this
            # test actually asserts on.
            assert await _await_until(lambda: "stagnant" in sse.types())
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert "stagnant" in sse.types()

    @pytest.mark.asyncio
    async def test_idle_deadline_settles_the_campaign_and_tears_the_loop_down(
        self, _isolate: Path, fast_watchdog, sse, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(h, "_unresponsive_deadline", lambda _idle: 0)
        terminating_loop = SimpleNamespace(id="terminating-loop", active=True)
        svc = SimpleNamespace(
            get_by_slot=MagicMock(return_value=terminating_loop),
            remove=AsyncMock(),
            update=AsyncMock(),
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        cid = _campaign(auto_approve=True)
        _running(cid)
        _write_finding(cid, 1)
        assert await _drive_watchdog(
            {"state": None},
            # Wait on BOTH the status commit and the independently-scheduled SSE
            # emit (see test_new_verified_finding_completes_the_campaign) — the
            # assertion below reads sse.types(), so the SSE half must be observed
            # before _drive_watchdog's finally cancels the loop.
            lambda: _status(cid) == h.CampaignStatus.FAILED and "failed" in sse.types(),
        )
        assert "stalled" in (h.get_campaign(cid) or {})["error_message"]
        svc.update.assert_awaited_once_with(
            terminating_loop.id, active=False, stopped_reason="campaign_failed"
        )
        svc.remove.assert_awaited_once_with(terminating_loop.id)
        assert "failed" in sse.types()

    @pytest.mark.asyncio
    async def test_a_busy_worker_slot_refreshes_liveness(
        self, _isolate: Path, fast_watchdog, polls, sse, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(h, "_unresponsive_deadline", lambda _idle: 0)
        cid = _campaign(auto_approve=True)
        _running(cid)
        _write_finding(cid, 1)
        slot = SimpleNamespace(_trust=True, running=True)
        state = SimpleNamespace(_slots={f"research-{cid}": slot})
        # An already-expired deadline would settle the campaign on the second
        # poll — a running slot must keep it alive instead.
        assert await _drive_watchdog({"state": state}, lambda: polls["n"] >= 3)
        assert _status(cid) == h.CampaignStatus.RUNNING
        assert sse.events == []

    @pytest.mark.asyncio
    async def test_a_failing_poll_is_logged_and_the_loop_survives(
        self, _isolate: Path, fast_watchdog, monkeypatch: pytest.MonkeyPatch
    ):
        calls = {"n": 0}

        def _boom():
            calls["n"] += 1
            raise RuntimeError("db unavailable")

        monkeypatch.setattr(h, "_get_db", _boom)
        # Two failures prove the loop caught the first one and kept polling.
        assert await _drive_watchdog({"state": None}, lambda: calls["n"] >= 2)


# --- SSE stream handler ----------------------------------------------------


class TestStreamHandler:
    @pytest.mark.asyncio
    async def test_only_matching_campaign_events_are_written(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        writes: list[bytes] = []

        async def _prepare(self, request):  # noqa: ANN001 — stub signature mirrors aiohttp
            return None

        async def _write(self, data):  # noqa: ANN001
            writes.append(bytes(data))

        monkeypatch.setattr(web.StreamResponse, "prepare", _prepare)
        monkeypatch.setattr(web.StreamResponse, "write", _write)
        cid = _campaign()
        req = _mk("GET", f"campaigns/{cid}/stream", app=_app(), match={"id": cid})

        task = asyncio.ensure_future(h._handle_stream(req))
        try:
            await async_wait_until(
                lambda: h._sse_queues, describe=lambda: "handler never registered its queue"
            )
            h._emit_sse({"type": "new_finding", "campaign_id": "otherone"})
            h._emit_sse({"type": "complete", "campaign_id": cid})
            await async_wait_until(lambda: writes, describe=lambda: "the stream wrote nothing")
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert len(writes) == 1
        payload = json.loads(writes[0].decode("utf-8").removeprefix("data: ").strip())
        assert payload == {"type": "complete", "campaign_id": cid}
        assert h._sse_queues == []  # the finally block deregistered the queue

    @pytest.mark.asyncio
    async def test_invalid_campaign_id_is_rejected_before_streaming(self, _isolate: Path):
        req = _mk("GET", "campaigns/nope/stream", app=_app(), match={"id": "nope"})
        resp = await h._handle_stream(req)
        assert resp.status == 400

    def test_emit_drops_events_for_a_full_queue(self):
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        h._sse_queues.append(q)
        try:
            h._emit_sse({"type": "a"})
            h._emit_sse({"type": "b"})  # dropped, must not raise
            assert q.qsize() == 1
        finally:
            h._sse_queues.remove(q)


# --- grill question tree ---------------------------------------------------


class TestGrillHelpers:
    def test_node_depth_counts_ancestors(self):
        tree = [
            {"id": "n1", "parent": None},
            {"id": "n2", "parent": "n1"},
            {"id": "n3", "parent": "n2"},
        ]
        assert h._node_depth(tree, "n1") == 0
        assert h._node_depth(tree, "n3") == 2
        assert h._node_depth(tree, "missing") == -1

    def test_node_depth_survives_a_parent_cycle(self):
        tree = [{"id": "a", "parent": "b"}, {"id": "b", "parent": "a"}]
        assert h._node_depth(tree, "a") >= 1  # terminates instead of looping forever

    def test_compact_tree_renders_answers_and_skips_non_dicts(self):
        rendered = h._compact_tree(
            ["junk", {"id": "n1", "kind": "clarifier", "text": "Which DB?", "answer": "SQLite"}]
        )
        assert "junk" not in rendered
        assert "[n1] clarifier: Which DB?" in rendered
        assert "answered: SQLite" in rendered

    def test_compact_tree_of_an_empty_tree_says_first_round(self):
        assert "first round" in h._compact_tree([])

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("no array here", id="no-brackets"),
            pytest.param("]before[", id="reversed-brackets"),
            pytest.param("[{not json}]", id="malformed"),
        ],
    )
    def test_unparseable_llm_replies_yield_no_nodes(self, raw):
        assert h._parse_grill_nodes(raw) == []

    def test_parse_keeps_only_well_formed_nodes(self):
        raw = (
            'prose [{"kind": "clarifier", "text": " Which DB? ", "recommended": " SQLite "},'
            '{"kind": "research", "text": "How is it limited?"},'
            '{"kind": "bogus", "text": "x"}, {"kind": "research", "text": "  "}, "loose"] tail'
        )
        assert h._parse_grill_nodes(raw) == [
            {"kind": "clarifier", "text": "Which DB?", "recommended": "SQLite"},
            {"kind": "research", "text": "How is it limited?"},
        ]

    @pytest.mark.asyncio
    async def test_expand_children_without_a_pool_returns_nothing(self):
        assert await h._grill_expand_children(None, "q", [], None) == []

    @pytest.mark.asyncio
    async def test_expand_children_targets_the_named_node(self):
        pool = SimpleNamespace(send=AsyncMock(return_value='[{"kind":"research","text":"t"}]'))
        tree = [{"id": "n1", "kind": "clarifier", "text": "Which DB?", "recommended": "SQLite"}]
        nodes = await h._grill_expand_children(pool, "the main question", tree, "n1")
        assert nodes == [{"kind": "research", "text": "t"}]
        prompt = pool.send.await_args.args[0]
        assert "[n1] clarifier: Which DB?" in prompt
        assert "(answer: SQLite)" in prompt
        assert "UNTRUSTED" in prompt

    @pytest.mark.asyncio
    async def test_expand_children_for_an_unknown_node_falls_back_to_the_root(self):
        pool = SimpleNamespace(send=AsyncMock(return_value="[]"))
        nodes = await h._grill_expand_children(pool, "the main question", [], "ghost")
        assert nodes == []
        assert "root question" in pool.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_expand_children_degrades_when_the_pool_fails(self):
        pool = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("pool down")))
        assert await h._grill_expand_children(pool, "the main question", [], None) == []

    def test_fenced_untrusted_text_uses_a_fresh_nonce(self):
        first, second = h._fence_untrusted("x"), h._fence_untrusted("x")
        assert first != second
        assert "x" in first

    def test_new_node_ids_are_unique(self):
        assert h._new_node_id() != h._new_node_id()
        assert h._new_node_id().startswith("n")


class TestGrillExpandEndpoint:
    @pytest.mark.asyncio
    async def test_malformed_body_is_a_400(self):
        resp = await h._handle_grill_expand(_mk("POST", "grill/expand", app=_app(), body=None))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_short_question_is_a_400(self):
        resp = await h._handle_grill_expand(
            _mk("POST", "grill/expand", app=_app(), body={"question": "too short"})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "Question too short"

    @pytest.mark.asyncio
    async def test_non_list_tree_is_a_400(self):
        resp = await h._handle_grill_expand(
            _mk(
                "POST",
                "grill/expand",
                app=_app(),
                body={"question": "A properly long research question", "tree": {"a": 1}},
            )
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "tree must be a list"

    @pytest.mark.asyncio
    async def test_unknown_node_id_is_a_400(self):
        resp = await h._handle_grill_expand(
            _mk(
                "POST",
                "grill/expand",
                app=_app(),
                body={"question": "A properly long research question", "node_id": "ghost"},
            )
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "Unknown node_id"

    @pytest.mark.asyncio
    async def test_max_depth_stops_expansion(self):
        tree = [{"id": "n0", "parent": None}]
        for i in range(1, h._MAX_GRILL_DEPTH + 1):
            tree.append({"id": f"n{i}", "parent": f"n{i - 1}"})
        resp = await h._handle_grill_expand(
            _mk(
                "POST",
                "grill/expand",
                app=_app(),
                body={
                    "question": "A properly long research question",
                    "tree": tree,
                    "node_id": f"n{h._MAX_GRILL_DEPTH}",
                },
            )
        )
        assert _body(resp) == {"nodes": [], "reason": "max_depth"}

    @pytest.mark.asyncio
    async def test_children_are_normalized_capped_and_shaped(self):
        raw = [{"kind": "clarifier", "text": "c%d" % i, "recommended": "r"} for i in range(7)]
        with mock.patch.object(h, "_grill_expand_children", AsyncMock(return_value=raw)):
            resp = await h._handle_grill_expand(
                _mk(
                    "POST",
                    "grill/expand",
                    app=_app(auto_research_llm_pool=object()),
                    body={"question": "A properly long research question"},
                )
            )
        nodes = _body(resp)["nodes"]
        assert len(nodes) == h._GRILL_CHILD_CAP
        assert nodes[0]["kind"] == "clarifier"
        assert nodes[0]["recommended"] == "r"
        assert nodes[0]["origin"] == ""
        assert nodes[0]["status"] == "open"
        assert nodes[0]["parent"] is None

    @pytest.mark.asyncio
    async def test_unknown_kind_becomes_research_and_blank_text_is_dropped(self):
        raw = [{"kind": "wat", "text": "keep me"}, {"kind": "research", "text": "   "}]
        with mock.patch.object(h, "_grill_expand_children", AsyncMock(return_value=raw)):
            resp = await h._handle_grill_expand(
                _mk(
                    "POST",
                    "grill/expand",
                    app=_app(),
                    body={"question": "A properly long research question"},
                )
            )
        nodes = _body(resp)["nodes"]
        assert len(nodes) == 1
        assert nodes[0]["kind"] == "research"
        assert nodes[0]["origin"] == "grill"
        assert nodes[0]["recommended"] == ""


class TestGrillTreeEndpoint:
    @pytest.mark.asyncio
    async def test_invalid_campaign_id_is_a_400(self, _isolate: Path):
        resp = await h._handle_grill_tree(
            _mk("GET", "campaigns/../x/grill-tree", app=_app(), match={"id": "../x"})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_absent_tree_is_an_empty_list(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_grill_tree(
            _mk("GET", f"campaigns/{cid}/grill-tree", app=_app(), match={"id": cid})
        )
        assert _body(resp) == {"tree": []}

    @pytest.mark.asyncio
    async def test_malformed_tree_file_is_an_empty_list(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "grill_tree.json").write_text("{oops")
        resp = await h._handle_grill_tree(
            _mk("GET", f"campaigns/{cid}/grill-tree", app=_app(), match={"id": cid})
        )
        assert _body(resp) == {"tree": []}

    @pytest.mark.asyncio
    async def test_non_list_tree_file_is_dropped_entirely(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "grill_tree.json").write_text(json.dumps({"id": "n1"}))
        resp = await h._handle_grill_tree(
            _mk("GET", f"campaigns/{cid}/grill-tree", app=_app(), match={"id": cid})
        )
        assert _body(resp) == {"tree": []}

    @pytest.mark.asyncio
    async def test_stored_nodes_are_served(self, _isolate: Path):
        cid = _campaign(grill_tree=[{"id": "n1", "kind": "research", "text": "How?"}])
        resp = await h._handle_grill_tree(
            _mk("GET", f"campaigns/{cid}/grill-tree", app=_app(), match={"id": cid})
        )
        tree = _body(resp)["tree"]
        assert len(tree) == 1
        assert tree[0]["text"] == "How?"


# --- guard rails shared by every handler -----------------------------------


ROUTES: list[tuple[str, Any, bool]] = [
    ("validate", h._handle_validate, False),
    ("grill_expand", h._handle_grill_expand, False),
    ("create", h._handle_create, False),
    ("list", h._handle_list, False),
    ("get", h._handle_get, True),
    ("report", h._handle_report, True),
    ("grill_tree", h._handle_grill_tree, True),
    ("action", h._handle_action, True),
    ("delete", h._handle_delete, True),
    ("nudge", h._handle_nudge, True),
    ("add_question", h._handle_add_question, True),
    ("to_knowledge", h._handle_to_knowledge, True),
    ("knowledge_status", h._handle_knowledge_status, True),
    ("to_artifact", h._handle_to_artifact, True),
    ("report_status", h._handle_report_status, True),
    ("stream", h._handle_stream, True),
]


class TestAuthGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("name,handler,needs_id", ROUTES, ids=[r[0] for r in ROUTES])
    async def test_every_handler_401s_without_a_middleware_user(self, name, handler, needs_id):
        """The gateway middleware sets request['user']; absent it we must fail closed."""
        req = _mk(
            "POST",
            "x",
            app=_app(),
            match={"id": "a1b2c3d4"} if needs_id else None,
            body={},
            authed=False,
        )
        resp = await handler(req)
        assert resp.status == 401
        assert _body(resp) == {"error": "Unauthorized"}


class TestInvalidCampaignId:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler",
        [
            h._handle_get,
            h._handle_report,
            h._handle_action,
            h._handle_delete,
            h._handle_nudge,
            h._handle_add_question,
            h._handle_to_knowledge,
            h._handle_knowledge_status,
            h._handle_to_artifact,
            h._handle_report_status,
        ],
        ids=lambda f: f.__name__,
    )
    async def test_traversal_id_is_a_400(self, _isolate: Path, handler):
        resp = await handler(
            _mk("POST", "x", app=_app(), match={"id": "../../etc"}, body={"action": "start"})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "Invalid campaign ID"


class TestBodyValidation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload", [None, ["not", "an", "object"]], ids=["undecodable", "list"]
    )
    async def test_non_object_bodies_are_rejected(self, payload):
        req = _mk("POST", "validate", app=_app(), body=payload)
        resp = await h._handle_validate(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_rejects_a_failing_validation(self, _isolate: Path):
        resp = await h._handle_create(_mk("POST", "campaigns", app=_app(), body={"question": "x"}))
        assert resp.status == 400
        assert _body(resp)["error"] == "Validation failed"

    @pytest.mark.asyncio
    async def test_get_of_a_missing_campaign_is_a_404(self, _isolate: Path):
        _campaign()
        resp = await h._handle_get(
            _mk("GET", "campaigns/deadbeef", app=_app(), match={"id": "deadbeef"})
        )
        assert resp.status == 404
