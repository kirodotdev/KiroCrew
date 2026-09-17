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
    auth middleware never ran, the 400 on a malformed campaign id or body, and
    the 404 / 409 / 503 taxonomy of the artifact / knowledge / question routes.

Everything is patched at the workflow-service, artifact-store, knowledge-store
and autonudge boundary, so no network, no subprocess and no real gateway is
involved. ``DB_PATH`` / ``RESEARCH_DIR`` are pinned into ``tmp_path`` (the same
fixture shape ``test_auto_research.py`` uses) on top of the per-test
``KIROCREW_HOME`` that ``conftest.py`` already pins, so nothing is written
outside the temp tree. Handlers are invoked through aiohttp's own
``make_mocked_request`` rather than a live ``TestServer``, so no socket is bound
and no gateway task is started.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from conftest import make_dir_link
from kiro_crew.apps.builtins.auto_research import handlers as h

BASE = "/api/apps/auto-research"


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path):
    """Pin the DB + research dir into tmp_path (same shape as test_auto_research)."""
    with (
        mock.patch.object(h, "DB_PATH", tmp_path / "t.db"),
        mock.patch.object(h, "RESEARCH_DIR", tmp_path / "r"),
    ):
        yield tmp_path


@pytest.fixture(autouse=True)
def _no_stray_sse_queues():
    """The SSE queue registry is module-global — fail loudly if a test leaks one."""
    before = list(h._sse_queues)
    yield
    assert h._sse_queues == before, "test leaked an SSE queue"


@pytest.fixture(autouse=True)
def _no_autonudge(monkeypatch: pytest.MonkeyPatch):
    """Default to 'no autonudge service' so nothing touches a live loop registry."""
    monkeypatch.setattr(h, "_autonudge_instance", lambda: None)


# --- helpers ----------------------------------------------------------------


def _app(**keys: Any) -> web.Application:
    """A real (unfrozen) Application so ``request.app.get(...)`` returns None for
    absent keys — a ``MagicMock`` app would make every ``is None`` guard false.
    """
    app = web.Application()
    for key, value in keys.items():
        app[key] = value
    return app


def _mk(
    method: str,
    path: str,
    *,
    app: web.Application | None = None,
    match: dict | None = None,
    body: Any = ...,
    authed: bool = True,
) -> web.Request:
    """A mocked aiohttp request for a handler under test.

    ``body`` is stubbed onto ``.json()`` (the pattern the issue-radar route tests
    use); pass ``None`` to model a payload that fails to decode.
    """
    req = make_mocked_request(method, f"{BASE}/{path}", app=app, match_info=match or {})
    if authed:
        req["user"] = "test-user"
    if body is not ...:
        if body is None:
            req.json = AsyncMock(side_effect=ValueError("bad json"))  # type: ignore[method-assign]
        else:
            req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _body(response: web.StreamResponse) -> dict:
    assert isinstance(response, web.Response)
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


def _campaign(**config: Any) -> str:
    cfg: dict[str, Any] = {"question": "How do teams handle API rate limiting today?"}
    cfg.update(config)
    return h.create_campaign(cfg)["id"]


def _running(cid: str, *, started_at: float | None = None, **cols: Any) -> None:
    """Force a campaign RUNNING, optionally back-dating started_at."""
    h.update_campaign_status(cid, h.CampaignStatus.RUNNING)
    if started_at is not None:
        cols["started_at"] = started_at
    if cols:
        db = h._get_db()
        sets = ", ".join(f"{k} = ?" for k in cols)
        db.execute(f"UPDATE campaigns SET {sets} WHERE id = ?", (*cols.values(), cid))
        db.commit()
        db.close()


def _status(cid: str) -> str:
    campaign = h.get_campaign(cid)
    assert campaign is not None
    return campaign["status"]


def _write_finding(cid: str, cycle: int, **fields: Any) -> Path:
    d = h._campaign_dir(cid)
    payload: dict[str, Any] = {"cycle": cycle, "summary": "s", "new_findings_count": 1}
    payload.update(fields)
    path = d / "findings" / ("cycle_%03d.json" % cycle)
    path.write_text(json.dumps(payload))
    return path


class _SSESink:
    """Captures ``_emit_sse`` payloads without a real stream consumer."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, event: dict) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.get("type") for e in self.events]


@pytest.fixture
def sse(monkeypatch: pytest.MonkeyPatch) -> _SSESink:
    sink = _SSESink()
    monkeypatch.setattr(h, "_emit_sse", sink)
    return sink


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

    Lets a test sequence "first watchdog poll" -> "new finding written" on an
    observed event instead of a wall-clock sleep, so the count-advance
    transitions cannot flake on a slow (or fast) runner.
    """
    real = h._list_cycle_files
    seen = {"n": 0}

    def _counted(cid: str):
        files = real(cid)
        seen["n"] += 1
        seen["last_count"] = len(files)
        return files

    monkeypatch.setattr(h, "_list_cycle_files", _counted)
    return seen


async def _await_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


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
        launched = await h._launch_workflow(
            _mk("PATCH", cid, app=_app(state=SimpleNamespace())), cid
        )
        assert launched is False
        assert _status(cid) == h.CampaignStatus.FAILED
        assert sse.types() == ["failed"]
        assert "unavailable" in (h.get_campaign(cid) or {})["error_message"]

    @pytest.mark.asyncio
    async def test_absent_state_fails_the_campaign(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        assert await h._launch_workflow(_mk("PATCH", cid, app=_app()), cid) is False
        assert _status(cid) == h.CampaignStatus.FAILED

    @pytest.mark.asyncio
    async def test_unknown_campaign_is_a_no_op(self, _isolate: Path, sse):
        _campaign()  # create the schema
        start = AsyncMock(return_value={"run_id": "r"})
        launched = await h._launch_workflow(
            _mk("PATCH", "deadbeef", app=_app(state=_workflow_state(start=start))), "deadbeef"
        )
        assert launched is False
        start.assert_not_awaited()
        assert sse.events == []

    @pytest.mark.asyncio
    async def test_successful_start_persists_the_run_id(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow", max_cycles=7)
        start = AsyncMock(return_value={"run_id": "run-42"})
        launched = await h._launch_workflow(
            _mk("PATCH", cid, app=_app(state=_workflow_state(start=start))), cid
        )
        assert launched is True
        assert h._read_workflow_run_id(cid) == "run-42"
        assert start.await_args.kwargs["name"] == "research-" + cid
        assert start.await_args.kwargs["args"]["max_rounds"] == 7
        assert sse.events == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["start", "resume"], ids=["start", "resume"])
    async def test_run_id_publication_failure_cancels_and_returns_structured_error(
        self,
        _isolate: Path,
        monkeypatch: pytest.MonkeyPatch,
        action: str,
    ):
        cid = _campaign(execution_mode="workflow")
        if action == "resume":
            _running(cid)
            h.update_campaign_status(cid, h.CampaignStatus.PAUSED)
        previous = h.get_campaign(cid)
        assert previous is not None
        start = AsyncMock(return_value={"run_id": "run-orphan"})
        cancel = AsyncMock(return_value=True)
        monkeypatch.setattr(
            h,
            "_write_workflow_run_id",
            MagicMock(side_effect=OSError("disk full")),
        )

        response = await h._handle_action(
            _mk(
                "PATCH",
                f"campaigns/{cid}",
                app=_app(state=_workflow_state(start=start, cancel=cancel)),
                match={"id": cid},
                body={"action": action},
            )
        )

        assert response.status == 500
        assert _body(response) == {
            "error": "disk full",
            "code": "campaign_action_failed",
        }
        cancel.assert_awaited_once_with("run-orphan")
        assert h._read_workflow_run_id(cid) is None
        current = h.get_campaign(cid)
        assert current is not None
        assert current["status"] == previous["status"]
        assert current["started_at"] == previous["started_at"]
        assert current["completed_at"] == previous["completed_at"]
        assert current["error_message"] == previous["error_message"]

    @pytest.mark.asyncio
    async def test_start_raising_fails_the_campaign(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        start = AsyncMock(side_effect=RuntimeError("engine down"))
        launched = await h._launch_workflow(
            _mk("PATCH", cid, app=_app(state=_workflow_state(start=start))), cid
        )
        assert launched is False
        assert _status(cid) == h.CampaignStatus.FAILED
        assert "Workflow start failed" in (h.get_campaign(cid) or {})["error_message"]
        assert sse.types() == ["failed"]

    @pytest.mark.asyncio
    async def test_start_without_a_run_id_fails_the_campaign(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        start = AsyncMock(return_value=None)
        launched = await h._launch_workflow(
            _mk("PATCH", cid, app=_app(state=_workflow_state(start=start))), cid
        )
        assert launched is False
        assert _status(cid) == h.CampaignStatus.FAILED
        assert "no run ID" in (h.get_campaign(cid) or {})["error_message"]
        assert h._read_workflow_run_id(cid) is None


class TestStopWorkflow:
    @pytest.mark.asyncio
    async def test_cancels_the_recorded_run(self, _isolate: Path):
        cid = _campaign(execution_mode="workflow")
        h._write_workflow_run_id(cid, "run-9")
        cancel = AsyncMock()
        await h._stop_workflow(
            _mk("PATCH", cid, app=_app(state=_workflow_state(cancel=cancel))), cid
        )
        cancel.assert_awaited_once_with("run-9")

    @pytest.mark.asyncio
    async def test_no_run_id_means_no_cancel(self, _isolate: Path):
        cid = _campaign(execution_mode="workflow")
        cancel = AsyncMock()
        await h._stop_workflow(
            _mk("PATCH", cid, app=_app(state=_workflow_state(cancel=cancel))), cid
        )
        cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_failure_is_swallowed(self, _isolate: Path):
        cid = _campaign(execution_mode="workflow")
        h._write_workflow_run_id(cid, "run-9")
        cancel = AsyncMock(side_effect=RuntimeError("gone"))
        await h._stop_workflow(
            _mk("PATCH", cid, app=_app(state=_workflow_state(cancel=cancel))), cid
        )
        cancel.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_absent_service_is_a_no_op(self, _isolate: Path):
        cid = _campaign(execution_mode="workflow")
        h._write_workflow_run_id(cid, "run-9")
        await h._stop_workflow(_mk("PATCH", cid, app=_app()), cid)


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
        await h._poll_workflow_campaign(
            cid, _workflow_state(result=MagicMock()), h.get_campaign(cid)["started_at"]
        )
        assert sse.events == []

    @pytest.mark.asyncio
    async def test_lost_snapshot_within_the_hour_is_tolerated(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        await h._poll_workflow_campaign(
            cid,
            _workflow_state(result=MagicMock(return_value=None)),
            h.get_campaign(cid)["started_at"],
        )
        assert _status(cid) == h.CampaignStatus.RUNNING
        assert sse.events == []

    @pytest.mark.asyncio
    async def test_lost_snapshot_after_an_hour_fails_the_campaign(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        run_file = h._campaign_dir(cid) / h._WORKFLOW_RUN_FILE
        run_file.write_text(json.dumps({"run_id": "run-1", "ts": time.time() - 7200}))
        await h._poll_workflow_campaign(
            cid,
            _workflow_state(result=MagicMock(return_value=None)),
            h.get_campaign(cid)["started_at"],
        )
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
        await h._poll_workflow_campaign(
            cid,
            _workflow_state(result=MagicMock(return_value=None)),
            h.get_campaign(cid)["started_at"],
        )
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
        await h._poll_workflow_campaign(
            cid,
            _workflow_state(result=MagicMock(return_value=snap)),
            h.get_campaign(cid)["started_at"],
        )
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
        await h._poll_workflow_campaign(
            cid,
            _workflow_state(result=MagicMock(return_value=snap)),
            h.get_campaign(cid)["started_at"],
        )
        cycles = [json.loads(p.read_text())["cycle"] for p in h._list_cycle_files(cid)]
        assert cycles == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_bare_label_is_used_verbatim_as_the_insight(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        snap = _snapshot(events=_investigate_events("investigate-no-colon"))
        await h._poll_workflow_campaign(
            cid,
            _workflow_state(result=MagicMock(return_value=snap)),
            h.get_campaign(cid)["started_at"],
        )
        finding = json.loads(h._list_cycle_files(cid)[0].read_text())
        assert finding["key_insight"] == "investigate-no-colon"

    @pytest.mark.asyncio
    async def test_finished_run_writes_the_report_and_completes(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        snap = _snapshot(status="finished", result={"report": "# Report\nAll done."})
        await h._poll_workflow_campaign(
            cid,
            _workflow_state(result=MagicMock(return_value=snap)),
            h.get_campaign(cid)["started_at"],
        )
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
        await h._poll_workflow_campaign(
            cid,
            _workflow_state(result=MagicMock(return_value=snap)),
            h.get_campaign(cid)["started_at"],
        )
        assert (h._campaign_dir(cid) / "FINDINGS.md").read_text() == "one\n\ntwo"

    @pytest.mark.asyncio
    async def test_finished_run_with_no_result_writes_a_placeholder(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        snap = _snapshot(status="finished", result="not-a-dict")
        await h._poll_workflow_campaign(
            cid,
            _workflow_state(result=MagicMock(return_value=snap)),
            h.get_campaign(cid)["started_at"],
        )
        assert (h._campaign_dir(cid) / "FINDINGS.md").read_text() == "(no findings gathered)"
        assert _status(cid) == h.CampaignStatus.COMPLETE

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["failed", "cancelled"])
    async def test_terminal_failure_states_fail_the_campaign(self, _isolate: Path, sse, status):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        snap = _snapshot(status=status, error="engine exploded")
        await h._poll_workflow_campaign(
            cid,
            _workflow_state(result=MagicMock(return_value=snap)),
            h.get_campaign(cid)["started_at"],
        )
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
        await h._poll_workflow_campaign(
            cid,
            _workflow_state(result=MagicMock(return_value=snap)),
            h.get_campaign(cid)["started_at"],
        )
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
        await h._poll_workflow_campaign(
            cid,
            _workflow_state(result=MagicMock(return_value=snap)),
            h.get_campaign(cid)["started_at"],
        )
        written = (h._campaign_dir(cid) / "FINDINGS.md").read_text()
        assert "hunter2" not in written
        assert written == "[REDACTED]"

    @pytest.mark.asyncio
    async def test_poll_never_raises_into_the_watchdog(self, _isolate: Path, sse):
        cid = _campaign(execution_mode="workflow")
        _running(cid)
        h._write_workflow_run_id(cid, "run-1")
        boom = MagicMock(side_effect=RuntimeError("snapshot store on fire"))
        await h._poll_workflow_campaign(
            cid, _workflow_state(result=boom), h.get_campaign(cid)["started_at"]
        )
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
    async def test_trust_is_reestablished_and_a_manual_app_pause_is_rearmed(
        self, _isolate: Path, fast_watchdog, monkeypatch: pytest.MonkeyPatch
    ):
        """Manual inactivity is the app-disable compatibility path, not a bound."""
        cid = _campaign()
        _running(cid)
        slot = SimpleNamespace(_trust=False, running=True)
        state = SimpleNamespace(_slots={f"research-{cid}": slot})
        loop = SimpleNamespace(id="loop-1", active=False, stopped_reason="manual")
        svc = SimpleNamespace(get_by_slot=MagicMock(return_value=loop), update=AsyncMock())
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        assert await _drive_watchdog(
            {"state": state}, lambda: slot._trust and svc.update.await_count > 0
        )
        assert svc.update.await_args.kwargs == {"active": True}
        assert svc.update.await_args.args[0] == "loop-1"

    @pytest.mark.asyncio
    async def test_manual_pause_at_spent_cycle_cap_settles_before_reactivation(
        self, _isolate: Path, fast_watchdog, sse, monkeypatch: pytest.MonkeyPatch
    ):
        """App-disable may win the reason race after the final delivered cycle."""
        cid = _campaign(auto_approve=True, max_cycles=2)
        _running(cid)
        _write_finding(cid, 1)
        _write_finding(cid, 2)
        loop = SimpleNamespace(
            id="loop-capped",
            active=False,
            stopped_reason="manual",
            max_cycles=2,
            cycle_count=2,
            max_runtime_secs=0,
        )
        svc = SimpleNamespace(
            get_by_slot=MagicMock(return_value=loop),
            update=AsyncMock(),
            remove=AsyncMock(),
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)

        assert await _drive_watchdog(
            {"state": None},
            lambda: _status(cid) == h.CampaignStatus.COMPLETE and "complete" in sse.types(),
        )
        svc.update.assert_not_awaited()
        svc.remove.assert_awaited_once_with("loop-capped")

    @pytest.mark.asyncio
    async def test_terminal_bound_waits_for_inflight_worker_turn(
        self, _isolate: Path, fast_watchdog, monkeypatch: pytest.MonkeyPatch
    ):
        """A final worker finding can still complete a run whose bound just expired."""
        cid = _campaign(auto_approve=True, max_cycles=1)
        _running(cid)
        _write_finding(cid, 1, verification={"passed": True})
        slot = SimpleNamespace(_trust=True, running=True)
        state = SimpleNamespace(_slots={f"research-{cid}": slot})
        loop = SimpleNamespace(
            id="loop-capped",
            active=False,
            stopped_reason="cycle_cap",
            max_cycles=1,
            cycle_count=1,
            max_runtime_secs=0,
        )
        svc = SimpleNamespace(
            get_by_slot=MagicMock(return_value=loop),
            update=AsyncMock(),
            remove=AsyncMock(),
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)

        task = asyncio.ensure_future(h._watchdog_loop({"state": state}))
        try:
            await asyncio.sleep(0.05)
            assert _status(cid) == h.CampaignStatus.RUNNING
            svc.remove.assert_not_awaited()

            slot.running = False
            assert await _await_until(
                lambda: _status(cid) == h.CampaignStatus.COMPLETE and svc.remove.await_count == 1
            )
        finally:
            task.cancel()
            await task

        svc.remove.assert_awaited_once_with("loop-capped")

    @pytest.mark.asyncio
    async def test_terminal_bound_waits_for_a_between_stage_worker_turn(
        self, _isolate: Path, fast_watchdog, monkeypatch: pytest.MonkeyPatch
    ):
        """A multi-stage worker turn reads ``running`` False *between* stages,
        with only ``_in_stage_execution`` set. The terminal-bound settle gate
        must treat that as in-flight; otherwise a bound expiring between stages
        terminalizes the campaign before its final stage persists findings
        (crash/data-loss). Mirror of test_terminal_bound_waits_for_inflight_worker_turn
        but with the between-stage flag instead of ``running``."""
        cid = _campaign(auto_approve=True, max_cycles=1)
        _running(cid)
        _write_finding(cid, 1, verification={"passed": True})
        slot = SimpleNamespace(_trust=True, running=False, _in_stage_execution=True)
        state = SimpleNamespace(_slots={f"research-{cid}": slot})
        loop = SimpleNamespace(
            id="loop-capped",
            active=False,
            stopped_reason="cycle_cap",
            max_cycles=1,
            cycle_count=1,
            max_runtime_secs=0,
        )
        svc = SimpleNamespace(
            get_by_slot=MagicMock(return_value=loop),
            update=AsyncMock(),
            remove=AsyncMock(),
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)

        task = asyncio.ensure_future(h._watchdog_loop({"state": state}))
        try:
            await asyncio.sleep(0.05)
            # Between stages -> still in flight -> not settled.
            assert _status(cid) == h.CampaignStatus.RUNNING
            svc.remove.assert_not_awaited()

            # The plan's final stage finishes and clears the flag.
            slot._in_stage_execution = False
            assert await _await_until(
                lambda: _status(cid) == h.CampaignStatus.COMPLETE and svc.remove.await_count == 1
            )
        finally:
            task.cancel()
            await task

        svc.remove.assert_awaited_once_with("loop-capped")

    @pytest.mark.asyncio
    async def test_active_runtime_budget_precedes_a_verified_final_finding(
        self,
        _isolate: Path,
        fast_watchdog,
        polls,
        sse,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Budget expiry during an in-flight turn cannot publish COMPLETE.

        AutoNudge persists the inactive runtime-budget row after delivery. The
        watchdog can observe the verified finding first while the loop is still
        active, so the live counters must fence the count-advance path too.
        """
        cid = _campaign(auto_approve=True, max_cycles=30)
        _running(cid)
        _write_finding(cid, 1)
        slot = SimpleNamespace(_trust=True, running=True)
        state = SimpleNamespace(_slots={f"research-{cid}": slot})
        loop = SimpleNamespace(
            id="loop-budget",
            active=True,
            stopped_reason="",
            max_cycles=30,
            cycle_count=1,
            max_runtime_secs=60,
            created_ts=time.time() - 120,
        )
        svc = SimpleNamespace(
            get_by_slot=MagicMock(return_value=loop),
            update=AsyncMock(),
            remove=AsyncMock(),
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)

        polls["n"] = 0
        task = asyncio.ensure_future(h._watchdog_loop({"state": state}))
        try:
            assert await _await_until(lambda: polls["n"] >= 1 and polls.get("last_count") == 1)
            _write_finding(cid, 2, verification={"passed": True})
            assert await _await_until(lambda: svc.get_by_slot.call_count >= 2)
            assert _status(cid) == h.CampaignStatus.RUNNING

            slot.running = False
            assert await _await_until(
                lambda: _status(cid) == h.CampaignStatus.STOPPED and svc.remove.await_count == 1
            )
            assert await _await_until(lambda: "stopped" in sse.types())
        finally:
            task.cancel()
            await task

        assert "complete" not in sse.types()
        assert (h.get_campaign(cid) or {})["total_cycles"] == 2
        assert [finding["cycle"] for finding in h.get_findings(cid)] == [1, 2]
        svc.update.assert_awaited_once_with("loop-budget", active=False)
        svc.remove.assert_awaited_once_with("loop-budget")

    @pytest.mark.asyncio
    async def test_verified_finding_waits_for_its_inflight_owner(
        self,
        _isolate: Path,
        fast_watchdog,
        polls,
        sse,
        monkeypatch: pytest.MonkeyPatch,
    ):
        cid = _campaign(auto_approve=True, max_cycles=30)
        _running(cid)
        _write_finding(cid, 1)
        slot = SimpleNamespace(_trust=True, running=True)
        state = SimpleNamespace(_slots={f"research-{cid}": slot})
        loop = SimpleNamespace(
            id="loop-active",
            active=True,
            stopped_reason="",
            max_cycles=30,
            cycle_count=1,
            max_runtime_secs=0,
            created_ts=time.time(),
        )
        svc = SimpleNamespace(
            get_by_slot=MagicMock(return_value=loop),
            update=AsyncMock(),
            remove=AsyncMock(),
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)

        polls["n"] = 0
        task = asyncio.ensure_future(h._watchdog_loop({"state": state}))
        try:
            assert await _await_until(lambda: polls["n"] >= 1 and polls.get("last_count") == 1)
            _write_finding(cid, 2, verification={"passed": True})
            assert await _await_until(lambda: svc.get_by_slot.call_count >= 2)
            assert _status(cid) == h.CampaignStatus.RUNNING
            assert (h.get_campaign(cid) or {})["total_cycles"] == 0

            slot.running = False
            assert await _await_until(
                lambda: _status(cid) == h.CampaignStatus.COMPLETE and svc.remove.await_count == 1
            )
        finally:
            task.cancel()
            await task

        assert (h.get_campaign(cid) or {})["total_cycles"] == 2
        assert sse.types() == ["new_finding", "complete"]
        svc.update.assert_awaited_once_with("loop-active", active=False)
        svc.remove.assert_awaited_once_with("loop-active")

    @pytest.mark.asyncio
    async def test_raised_live_cap_defers_completion_until_new_cap(
        self,
        _isolate: Path,
        fast_watchdog,
        polls,
        monkeypatch: pytest.MonkeyPatch,
    ):
        cid = _campaign(auto_approve=True, max_cycles=2)
        _running(cid)
        _write_finding(cid, 1)
        slot = SimpleNamespace(_trust=True, running=True)
        state = SimpleNamespace(_slots={f"research-{cid}": slot})
        loop = SimpleNamespace(
            id="loop-raised-cap",
            active=True,
            stopped_reason="",
            max_cycles=2,
            cycle_count=1,
            max_runtime_secs=0,
            created_ts=time.time(),
        )
        svc = SimpleNamespace(
            get_by_slot=MagicMock(return_value=loop),
            update=AsyncMock(),
            remove=AsyncMock(),
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)

        polls["n"] = 0
        task = asyncio.create_task(h._watchdog_loop({"state": state}))
        try:
            assert await _await_until(lambda: polls["n"] >= 1 and polls.get("last_count") == 1)
            _write_finding(cid, 2, verification={"passed": True})
            loop.max_cycles = 3
            loop.cycle_count = 2
            slot.running = False
            assert await _await_until(lambda: (h.get_campaign(cid) or {})["total_cycles"] == 2)
            assert _status(cid) == h.CampaignStatus.RUNNING
            svc.remove.assert_not_awaited()

            _write_finding(cid, 3, verification={"passed": True})
            loop.cycle_count = 3
            assert await _await_until(lambda: _status(cid) == h.CampaignStatus.COMPLETE)
        finally:
            task.cancel()
            await task

        svc.remove.assert_awaited_once_with("loop-raised-cap")

    @pytest.mark.asyncio
    async def test_lowered_live_cap_completes_after_inflight_owner_exits(
        self,
        _isolate: Path,
        fast_watchdog,
        polls,
        monkeypatch: pytest.MonkeyPatch,
    ):
        cid = _campaign(auto_approve=True, max_cycles=3)
        _running(cid)
        _write_finding(cid, 1)
        slot = SimpleNamespace(_trust=True, running=True)
        state = SimpleNamespace(_slots={f"research-{cid}": slot})
        loop = SimpleNamespace(
            id="loop-lowered-cap",
            active=True,
            stopped_reason="",
            max_cycles=3,
            cycle_count=1,
            max_runtime_secs=0,
            created_ts=time.time(),
        )
        svc = SimpleNamespace(
            get_by_slot=MagicMock(return_value=loop),
            update=AsyncMock(),
            remove=AsyncMock(),
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)

        polls["n"] = 0
        task = asyncio.create_task(h._watchdog_loop({"state": state}))
        try:
            assert await _await_until(lambda: polls["n"] >= 1 and polls.get("last_count") == 1)
            _write_finding(cid, 2, verification={"passed": True})
            loop.max_cycles = 2
            loop.cycle_count = 2
            assert _status(cid) == h.CampaignStatus.RUNNING
            slot.running = False
            assert await _await_until(lambda: _status(cid) == h.CampaignStatus.COMPLETE)
        finally:
            task.cancel()
            await task

        svc.remove.assert_awaited_once_with("loop-lowered-cap")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        (
            "initial_cap",
            "live_cap",
            "loop_mode",
            "expected_status",
            "expected_outcome",
        ),
        [
            (2, 3, "same", h.CampaignStatus.RUNNING, h._SettlementOutcome.NO_VERDICT),
            (3, 2, "same", h.CampaignStatus.COMPLETE, h._SettlementOutcome.SETTLED),
            (2, 2, "same", h.CampaignStatus.COMPLETE, h._SettlementOutcome.SETTLED),
            (2, 2, "missing", h.CampaignStatus.COMPLETE, h._SettlementOutcome.SETTLED),
            (2, 2, "replacement", h.CampaignStatus.RUNNING, h._SettlementOutcome.STALE),
        ],
        ids=["raised", "lowered", "stable", "missing-fallback", "replacement"],
    )
    async def test_MUTATION_settlement_uses_post_await_live_cap(
        self,
        _isolate: Path,
        monkeypatch: pytest.MonkeyPatch,
        initial_cap: int,
        live_cap: int,
        loop_mode: str,
        expected_status: str,
        expected_outcome: h._SettlementOutcome,
    ):
        """The final loop read, after bookkeeping awaits, owns completion."""
        cid = _campaign(auto_approve=True, max_cycles=initial_cap)
        _running(cid)
        _write_finding(cid, 1)
        _write_finding(cid, 2, verification={"passed": True})
        captured = SimpleNamespace(
            id="loop-lock-cap",
            active=True,
            stopped_reason="",
            max_cycles=initial_cap,
            cycle_count=2,
        )
        if loop_mode == "missing":
            captured = None
            authoritative = None
        elif loop_mode == "replacement":
            authoritative = SimpleNamespace(
                id="replacement-loop",
                active=True,
                stopped_reason="",
                max_cycles=live_cap,
                cycle_count=2,
            )
        else:
            authoritative = SimpleNamespace(
                id="loop-lock-cap",
                active=True,
                stopped_reason="",
                max_cycles=live_cap,
                cycle_count=2,
            )
        calls = {"n": 0}

        def _current_loop(_slot_key: str):
            calls["n"] += 1
            return captured if calls["n"] <= 2 else authoritative

        svc = SimpleNamespace(
            # Capture, lock-entry snapshot, then pre/post-verdict authority.
            get_by_slot=MagicMock(side_effect=_current_loop),
            update=AsyncMock(),
            remove=AsyncMock(),
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        started = (h.get_campaign(cid) or {})["started_at"]

        outcome = await h._settle_campaign_from_watchdog(
            cid,
            h._list_cycle_files(cid),
            {cid: 0},
            {cid: time.time()},
            observed_started_at=started,
            required_cycle_count=initial_cap,
            trigger=h._SettlementTrigger.FINDING,
        )

        assert outcome == expected_outcome
        assert _status(cid) == expected_status
        expected_calls = 3 if loop_mode == "replacement" else 4
        assert svc.get_by_slot.call_count == expected_calls

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("drift", "expected_outcome", "expected_status"),
        [
            ("raise-cycle-cap", h._SettlementOutcome.STALE, h.CampaignStatus.RUNNING),
            ("lift-runtime", h._SettlementOutcome.STALE, h.CampaignStatus.RUNNING),
            ("spend-runtime", h._SettlementOutcome.SETTLED, h.CampaignStatus.STOPPED),
            ("replace-loop", h._SettlementOutcome.STALE, h.CampaignStatus.RUNNING),
            ("opposite-bound", h._SettlementOutcome.SETTLED, h.CampaignStatus.STOPPED),
        ],
    )
    async def test_MUTATION_settlement_reclassifies_complete_terminal_authority(
        self,
        _isolate: Path,
        monkeypatch: pytest.MonkeyPatch,
        drift: str,
        expected_outcome: h._SettlementOutcome,
        expected_status: str,
    ):
        """Every terminal-bound field is re-read after bookkeeping awaits."""
        cid = _campaign(auto_approve=True, max_cycles=2)
        _running(cid)
        _write_finding(cid, 1)
        _write_finding(cid, 2, verification={"passed": True})
        now = time.time()
        captured = SimpleNamespace(
            id="loop-authority",
            active=False,
            stopped_reason="cycle_cap",
            max_cycles=2,
            cycle_count=2,
            max_runtime_secs=0,
            created_ts=now,
        )
        authoritative = captured
        trigger = h._SettlementTrigger.TERMINAL
        stopped_reason = "cycle_cap"
        required_cycle_count = 2
        if drift == "raise-cycle-cap":
            authoritative = SimpleNamespace(
                id="loop-authority",
                active=True,
                stopped_reason="cycle_cap",
                max_cycles=3,
                cycle_count=2,
                max_runtime_secs=0,
                created_ts=now,
            )
        elif drift == "lift-runtime":
            captured = SimpleNamespace(
                id="loop-authority",
                active=False,
                stopped_reason="runtime_budget",
                max_cycles=30,
                cycle_count=2,
                max_runtime_secs=60,
                created_ts=now - 120,
            )
            authoritative = SimpleNamespace(
                id="loop-authority",
                active=True,
                stopped_reason="runtime_budget",
                max_cycles=30,
                cycle_count=2,
                max_runtime_secs=0,
                created_ts=now - 120,
            )
            stopped_reason = "runtime_budget"
            required_cycle_count = 0
        elif drift == "spend-runtime":
            captured = SimpleNamespace(
                id="loop-authority",
                active=True,
                stopped_reason="",
                max_cycles=30,
                cycle_count=2,
                max_runtime_secs=60,
                created_ts=now,
            )
            authoritative = captured
            # Entry, lock, and first classification snapshots are unspent;
            # the first post-verdict snapshot crosses the unchanged loop's
            # derived deadline, and the second classification observes it.
            monkeypatch.setattr(
                h,
                "runtime_budget_exceeded",
                MagicMock(side_effect=[False, False, False, True, True, True]),
            )
            trigger = h._SettlementTrigger.FINDING
            stopped_reason = ""
            required_cycle_count = 30
        elif drift == "replace-loop":
            authoritative = SimpleNamespace(
                id="replacement-loop",
                active=False,
                stopped_reason="runtime_budget",
                max_cycles=30,
                cycle_count=2,
                max_runtime_secs=60,
                created_ts=now - 120,
            )
            trigger = h._SettlementTrigger.FINDING
            stopped_reason = ""
            required_cycle_count = 2
        elif drift == "opposite-bound":
            authoritative = SimpleNamespace(
                id="loop-authority",
                active=False,
                stopped_reason="runtime_budget",
                max_cycles=3,
                cycle_count=2,
                max_runtime_secs=60,
                created_ts=now - 120,
            )

        calls = {"n": 0}

        def _current_loop(_slot_key: str):
            calls["n"] += 1
            return captured if calls["n"] <= 2 else authoritative

        svc = SimpleNamespace(
            get_by_slot=MagicMock(side_effect=_current_loop),
            update=AsyncMock(),
            remove=AsyncMock(),
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        started = (h.get_campaign(cid) or {})["started_at"]

        outcome = await h._settle_campaign_from_watchdog(
            cid,
            h._list_cycle_files(cid),
            {cid: 0},
            {cid: now},
            observed_started_at=started,
            stopped_reason=stopped_reason,
            required_cycle_count=required_cycle_count,
            trigger=trigger,
        )

        assert outcome == expected_outcome
        assert _status(cid) == expected_status
        if expected_outcome == h._SettlementOutcome.SETTLED:
            svc.remove.assert_awaited_once_with("loop-authority")
        else:
            svc.remove.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_MUTATION_stale_authority_never_falls_through_to_stagnation(
        self,
        _isolate: Path,
        fast_watchdog,
        polls,
        monkeypatch: pytest.MonkeyPatch,
    ):
        cid = _campaign(auto_approve=True, max_cycles=30)
        _running(cid)
        _write_finding(cid, 1)
        settlement = AsyncMock(return_value=h._SettlementOutcome.STALE)
        stagnation = MagicMock(return_value=True)
        monkeypatch.setattr(h, "_settle_campaign_from_watchdog", settlement)
        monkeypatch.setattr(h, "check_stagnation", stagnation)

        polls["n"] = 0
        task = asyncio.create_task(h._watchdog_loop({"state": None}))
        try:
            assert await _await_until(lambda: polls["n"] >= 1 and polls.get("last_count") == 1)
            _write_finding(cid, 2, new_findings_count=0)
            assert await _await_until(lambda: settlement.await_count >= 2)
        finally:
            task.cancel()
            await task

        assert _status(cid) == h.CampaignStatus.RUNNING
        stagnation.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_cycle_cap_rejects_verified_but_thin_generation(
        self,
        _isolate: Path,
        fast_watchdog,
        polls,
        sse,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Verification cannot replace cap-many current-generation evidence."""
        cid = _campaign(auto_approve=True, max_cycles=2)
        _running(cid)
        slot = SimpleNamespace(_trust=True, running=False)
        state = SimpleNamespace(_slots={f"research-{cid}": slot})
        loop = SimpleNamespace(
            id="loop-cap",
            active=True,
            stopped_reason="",
            max_cycles=2,
            cycle_count=1,
            max_runtime_secs=0,
            created_ts=time.time(),
        )
        svc = SimpleNamespace(
            get_by_slot=MagicMock(return_value=loop),
            update=AsyncMock(),
            remove=AsyncMock(),
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)

        polls["n"] = 0
        task = asyncio.ensure_future(h._watchdog_loop({"state": state}))
        try:
            assert await _await_until(lambda: polls["n"] >= 1)
            _write_finding(cid, 1, verification={"passed": True})
            loop.cycle_count = 2
            assert await _await_until(
                lambda: _status(cid) == h.CampaignStatus.FAILED and svc.remove.await_count == 1
            )
            assert await _await_until(lambda: "failed" in sse.types())
        finally:
            task.cancel()
            await task

        assert "complete" not in sse.types()
        assert [finding["cycle"] for finding in h.get_findings(cid)] == [1]
        svc.update.assert_awaited_once_with("loop-cap", active=False)
        svc.remove.assert_awaited_once_with("loop-cap")

    @pytest.mark.asyncio
    async def test_terminal_bound_does_not_settle_a_run_resumed_into_the_launch_window(
        self, _isolate: Path, fast_watchdog, monkeypatch: pytest.MonkeyPatch
    ):
        """Opus regression: a Resume marks the campaign RUNNING (new started_at)
        before ``_launch_loop`` swaps the spent loop. A watchdog poll landing in
        that window must NOT terminalize the just-resumed run. The terminal-bound
        branch now defers one poll for a newly-observed run (mirroring the
        AUTONUDGE_STOP branch), by which point the new active loop is installed
        and the whole block is skipped. Without the guard the first poll settles
        the spent prior loop's bound against the new run and flips it to FAILED."""
        cid = _campaign(auto_approve=True, max_cycles=30)
        _running(cid)  # new started_at -> run_newly_observed on the first poll
        _write_finding(cid, 1)
        spent = SimpleNamespace(
            id="loop-capped",
            active=False,
            stopped_reason="cycle_cap",
            max_cycles=30,
            cycle_count=30,
            max_runtime_secs=0,
        )
        launched = SimpleNamespace(
            id="loop-capped",
            active=True,
            stopped_reason="",
            max_cycles=30,
            cycle_count=0,
            max_runtime_secs=0,
        )
        calls = {"n": 0}

        def _get_by_slot(_slot_key: str):
            calls["n"] += 1
            # First observation: launch still in flight, old spent loop present.
            # Subsequent observations: launch completed, new active loop in place.
            return spent if calls["n"] == 1 else launched

        svc = SimpleNamespace(
            get_by_slot=MagicMock(side_effect=_get_by_slot),
            update=AsyncMock(),
            remove=AsyncMock(),
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)

        assert await _drive_watchdog({"state": SimpleNamespace(_slots={})}, lambda: calls["n"] >= 3)
        assert _status(cid) == h.CampaignStatus.RUNNING
        svc.remove.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_count_advance_completion_scopes_to_the_current_generation(
        self, _isolate: Path, fast_watchdog, polls, sse
    ):
        """Opus regression: the primary (non-stalled) count-advance completion
        must count only THIS generation's cycle files. A resumed capped campaign
        whose prior generation already left cap-many findings on disk must not
        COMPLETE on a single new file; it completes only once the CURRENT
        generation reaches the cap. Mirrors ``_stalled_campaign_verdict``'s
        identity-snapshot fence on the live path."""
        cid = _campaign(auto_approve=True, max_cycles=2)
        _write_finding(cid, 1)
        _write_finding(cid, 2)  # prior generation already at the cap
        _running(cid)  # Resume snapshots both prior payload identities
        polls["n"] = 0
        task = asyncio.ensure_future(h._watchdog_loop({"state": None}))
        try:
            assert await _await_until(lambda: polls["n"] >= 1)  # first poll observed
            _write_finding(cid, 3)  # ONE new file this generation (total 3 >= cap)
            # Total count exceeds the cap, but only one file is this generation's,
            # so completion is refused and the run keeps going.
            assert await _await_until(lambda: polls["n"] >= 3)
            assert _status(cid) == h.CampaignStatus.RUNNING
            _write_finding(cid, 4)  # second new file: current generation hits cap
            assert await _await_until(lambda: _status(cid) == h.CampaignStatus.COMPLETE)
            assert await _await_until(lambda: "complete" in sse.types())
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_count_advance_refuses_corrupt_current_generation_evidence(
        self, _isolate: Path, fast_watchdog, polls, sse
    ):
        """Raw file count is only a change detector, never completion proof."""
        cid = _campaign(auto_approve=True, max_cycles=1)
        _running(cid)
        polls["n"] = 0
        task = asyncio.ensure_future(h._watchdog_loop({"state": None}))
        try:
            assert await _await_until(lambda: polls["n"] >= 1)
            findings = h._campaign_dir(cid) / "findings"
            findings.mkdir(parents=True, exist_ok=True)
            (findings / "cycle_001.json").write_text("{not json", encoding="utf-8")
            assert await _await_until(lambda: polls["n"] >= 3)
            assert _status(cid) == h.CampaignStatus.RUNNING
            assert "complete" not in sse.types()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_count_advance_never_completes_an_unbounded_zero_cap(
        self, _isolate: Path, fast_watchdog, polls, sse
    ):
        """Zero means unlimited; one readable finding must not end the run."""
        cid = _campaign(auto_approve=True, max_cycles=0)
        _running(cid)
        polls["n"] = 0
        task = asyncio.ensure_future(h._watchdog_loop({"state": None}))
        try:
            assert await _await_until(lambda: polls["n"] >= 1)
            _write_finding(cid, 1)
            assert await _await_until(lambda: polls["n"] >= 3)
            assert _status(cid) == h.CampaignStatus.RUNNING
            assert "complete" not in sse.types()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def test_cycle_cap_requires_readable_evidence_for_every_delivered_cycle(self, _isolate: Path):
        """Scheduler spend alone cannot promote an incomplete campaign."""
        cid = _campaign(auto_approve=True, max_cycles=2)
        _running(cid)
        readable = _write_finding(cid, 1)
        unreadable = h._campaign_dir(cid) / "findings" / "cycle_002.json"
        unreadable.write_text("{not json", encoding="utf-8")
        original_bytes = {path: path.read_bytes() for path in (readable, unreadable)}

        status, message = h._stalled_campaign_verdict(
            cid,
            [readable, unreadable],
            stopped_reason="cycle_cap",
            required_cycle_count=2,
        )

        assert status == h.CampaignStatus.FAILED
        assert message == "No activity — research stalled. Resume to continue."
        assert {path: path.read_bytes() for path in original_bytes} == original_bytes

        h.update_campaign_status(cid, status, error_message=message)
        resumed = h.update_campaign_status(cid, h.CampaignStatus.RUNNING)
        assert resumed["status"] == h.CampaignStatus.RUNNING
        assert len(h._list_cycle_files(cid)) == 2

    def test_runtime_budget_stop_remains_distinct_from_manual_pause(self, _isolate: Path):
        """A spent wall-clock budget stops the campaign instead of auto-resuming it."""
        cid = _campaign(auto_approve=True)
        _write_finding(cid, 1)
        status, message = h._stalled_campaign_verdict(
            cid,
            [h._campaign_dir(cid) / "cycle-001.json"],
            stopped_reason="runtime_budget",
        )
        assert status == h.CampaignStatus.STOPPED
        assert message == "Research time budget reached — findings are preserved."

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

        # Count only watchdog observations; Start/Resume snapshot capture is
        # independent of the cheap poll path.
        polls["n"] = 0
        task = asyncio.ensure_future(h._watchdog_loop({"state": state}))
        try:
            assert await _await_until(lambda: polls["n"] >= 1)  # first poll observed
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
        # Count only watchdog observations; snapshot capture is independent.
        polls["n"] = 0
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
        # Count only watchdog observations; snapshot capture is independent.
        polls["n"] = 0
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
        svc.update.assert_awaited_once_with(terminating_loop.id, active=False)
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
            for _ in range(200):
                await asyncio.sleep(0.005)
                if h._sse_queues:
                    break
            assert h._sse_queues, "handler never registered its queue"
            h._emit_sse({"type": "new_finding", "campaign_id": "otherone"})
            h._emit_sse({"type": "complete", "campaign_id": cid})
            for _ in range(200):
                await asyncio.sleep(0.005)
                if writes:
                    break
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
        await h._grill_expand_children(pool, "the main question", [], "ghost")
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
    async def test_tree_read_uses_the_campaign_descriptor_gate(self, _isolate: Path):
        cid = _campaign(grill_tree=[{"id": "n1", "kind": "research", "text": "How?"}])
        with mock.patch.object(h, "_read_campaign_file_bytes", return_value=None) as reader:
            resp = await h._handle_grill_tree(
                _mk("GET", f"campaigns/{cid}/grill-tree", app=_app(), match={"id": cid})
            )
        assert _body(resp) == {"tree": []}
        assert reader.call_args.args[1] == ("grill_tree.json",)
        assert reader.call_args.kwargs["max_bytes"] == h._REPORT_VIEW_MAX_BYTES

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


# --- report / nudge / add-question ----------------------------------------


class TestReportAndNudge:
    def test_read_report_of_an_invalid_id_is_empty(self):
        assert h._read_report("../etc") == ""

    def test_read_report_of_an_unreadable_file_is_empty(self, _isolate: Path):
        cid = _campaign()
        report = h._campaign_dir(cid) / "FINDINGS.md"
        report.write_text("body")
        with mock.patch.object(h, "_read_campaign_file_bytes", return_value=None):
            assert h._read_report(cid) == ""

    def test_large_report_keeps_a_bounded_prefix_and_recent_utf8_evidence(self, _isolate: Path):
        cid = _campaign()
        report = h._campaign_dir(cid) / "FINDINGS.md"
        # The cap cuts inside this three-byte code point. Replacement decoding
        # must remain safe, and current evidence must still be visible.
        report.write_bytes(b"A" * (h._REPORT_VIEW_MAX_BYTES - 1) + "€tail".encode("utf-8"))
        _write_finding(cid, 41, summary="最新の証拠")

        rendered = h._read_report(cid)

        assert len(rendered.encode("utf-8")) < h._REPORT_VIEW_MAX_BYTES * 2
        assert rendered.startswith("A" * 100)
        assert "�" in rendered
        assert "Report view limited" in rendered
        assert "Recent cycle evidence" in rendered
        assert "最新の証拠" in rendered

    @pytest.mark.parametrize(
        "raw",
        [
            b"# R\xc3\xa9sum\xc3\xa9\n\xe6\x9c\x80\xe6\x96\xb0",
            b"# R\xc3\xa9sum\xc3\xa9\r\n\xe6\x9c\x80\xe6\x96\xb0",
            b"# R\xc3\xa9sum\xc3\xa9\r\xe6\x9c\x80\xe6\x96\xb0",
        ],
        ids=["lf", "crlf", "cr"],
    )
    def test_small_report_uses_canonical_lf(self, _isolate: Path, raw: bytes):
        cid = _campaign()
        report = h._campaign_dir(cid) / "FINDINGS.md"
        report.write_bytes(raw)
        assert h._read_report(cid) == "# Résumé\n最新"

    @pytest.mark.parametrize(
        ("size", "oversized"),
        [
            pytest.param(h._REPORT_VIEW_MAX_BYTES - 1, False, id="max-minus-one"),
            pytest.param(h._REPORT_VIEW_MAX_BYTES, False, id="exactly-max"),
            pytest.param(h._REPORT_VIEW_MAX_BYTES + 1, True, id="max-plus-one"),
        ],
    )
    def test_report_view_boundary_augments_only_oversized_reports(
        self,
        _isolate: Path,
        size: int,
        oversized: bool,
    ):
        cid = _campaign()
        report = h._campaign_dir(cid) / "FINDINGS.md"
        report.write_bytes(b"x" * size)
        _write_finding(cid, 1, summary="boundary evidence")

        rendered = h._read_report(cid)

        assert rendered.startswith("x" * min(size, h._REPORT_VIEW_MAX_BYTES))
        assert ("Report view limited" in rendered) is oversized
        assert ("Recent cycle evidence" in rendered) is oversized
        assert ("boundary evidence" in rendered) is oversized

    def test_large_report_and_recent_findings_share_the_no_link_gate(self, _isolate: Path):
        cid = _campaign()
        report = h._campaign_dir(cid) / "FINDINGS.md"
        report.write_bytes(b"x" * (h._REPORT_VIEW_MAX_BYTES + 1))
        _write_finding(cid, 1, summary="recent")
        real = h._read_campaign_file_bytes

        with mock.patch.object(h, "_read_campaign_file_bytes", wraps=real) as gate:
            rendered = h._read_report(cid)

        assert "recent" in rendered
        assert gate.call_count == 2
        report_call, finding_call = gate.call_args_list
        assert report_call.args[1] == ("FINDINGS.md",)
        assert report_call.kwargs["max_bytes"] == h._REPORT_VIEW_MAX_BYTES + 1
        assert report_call.kwargs["allow_truncate"] is True
        assert finding_call.args[1] == ("findings", "cycle_001.json")

    @pytest.mark.asyncio
    async def test_report_endpoint_serves_the_findings_file(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("# Key finding")
        resp = await h._handle_report(
            _mk("GET", f"campaigns/{cid}/report", app=_app(), match={"id": cid})
        )
        assert _body(resp)["report"] == "# Key finding"

    @pytest.mark.asyncio
    async def test_report_endpoint_missing_file_is_a_genuine_empty_report(self, _isolate: Path):
        cid = _campaign()

        resp = await h._handle_report(
            _mk("GET", f"campaigns/{cid}/report", app=_app(), match={"id": cid})
        )

        assert resp.status == 200
        assert _body(resp) == {"report": ""}

    @pytest.mark.asyncio
    async def test_report_endpoint_read_refusal_is_not_a_success(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("owned", encoding="utf-8")
        monkeypatch.setattr(h, "_read_campaign_file_bytes", lambda *_a, **_kw: None)

        resp = await h._handle_report(
            _mk("GET", f"campaigns/{cid}/report", app=_app(), match={"id": cid})
        )

        assert resp.status == 409
        assert _body(resp)["code"] == "findings_refused"

    @pytest.mark.asyncio
    async def test_report_endpoint_identity_refusal_is_not_a_success(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        cid = _campaign()
        monkeypatch.setattr(h, "_campaign_identity", lambda _cid: None)

        resp = await h._handle_report(
            _mk("GET", f"campaigns/{cid}/report", app=_app(), match={"id": cid})
        )

        assert resp.status == 409
        assert _body(resp)["code"] == "findings_refused"

    @pytest.mark.asyncio
    async def test_report_endpoint_keeps_event_loop_responsive(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        cid = _campaign()

        def slow_report(_cid: str) -> str:
            time.sleep(0.2)
            return "bounded"

        monkeypatch.setattr(h, "_read_report_for_response", slow_report)
        started = asyncio.get_running_loop().time()
        task = asyncio.create_task(
            h._handle_report(_mk("GET", f"campaigns/{cid}/report", app=_app(), match={"id": cid}))
        )
        await asyncio.sleep(0.02)
        assert asyncio.get_running_loop().time() - started < 0.1
        assert not task.done()
        assert _body(await task)["report"] == "bounded"

    @pytest.mark.asyncio
    async def test_nudge_is_refused_in_workflow_mode(self, _isolate: Path):
        cid = _campaign(execution_mode="workflow")
        resp = await h._handle_nudge(
            _mk("POST", f"campaigns/{cid}/nudge", app=_app(), match={"id": cid}, body={"text": "x"})
        )
        assert resp.status == 409
        assert "workflow mode" in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_nudge_requires_text(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_nudge(
            _mk("POST", f"campaigns/{cid}/nudge", app=_app(), match={"id": cid}, body={"text": ""})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "text required"

    @pytest.mark.asyncio
    async def test_add_question_is_refused_in_workflow_mode(self, _isolate: Path):
        cid = _campaign(execution_mode="workflow")
        resp = await h._handle_add_question(
            _mk(
                "POST",
                f"campaigns/{cid}/questions",
                app=_app(),
                match={"id": cid},
                body={"text": "q"},
            )
        )
        assert resp.status == 409

    @pytest.mark.asyncio
    async def test_add_question_requires_text(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_add_question(
            _mk(
                "POST",
                f"campaigns/{cid}/questions",
                app=_app(),
                match={"id": cid},
                body={"text": " "},
            )
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_add_question_of_a_missing_campaign_is_a_404(self, _isolate: Path):
        _campaign()
        resp = await h._handle_add_question(
            _mk(
                "POST",
                "campaigns/deadbeef/questions",
                app=_app(),
                match={"id": "deadbeef"},
                body={"text": "q"},
            )
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_add_question_appends_and_rewrites_the_brief(self, _isolate: Path, sse):
        cid = _campaign()
        resp = await h._handle_add_question(
            _mk(
                "POST",
                f"campaigns/{cid}/questions",
                app=_app(),
                match={"id": cid},
                body={"text": "What does the cap cost?"},
            )
        )
        subs = _body(resp)["sub_questions"]
        assert subs == [{"text": "What does the cap cost?", "origin": "manual", "status": "open"}]
        brief = (h._campaign_dir(cid) / "brief.md").read_text()
        assert "What does the cap cost?" in brief
        assert sse.types() == ["question_added"]


# --- artifact export ------------------------------------------------------


class _FakeArtifact:
    def __init__(self, slug: str) -> None:
        self.slug = slug


class TestArtifactRoutes:
    @pytest.mark.asyncio
    async def test_report_status_without_the_artifact_system(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(h, "_HAS_ARTIFACTS", False)
        cid = _campaign()
        resp = await h._handle_report_status(_mk("GET", "s", app=_app(), match={"id": cid}))
        assert _body(resp) == {"slug": None}

    @pytest.mark.asyncio
    async def test_report_status_of_a_missing_campaign_is_a_404(self, _isolate: Path):
        _campaign()
        resp = await h._handle_report_status(_mk("GET", "s", app=_app(), match={"id": "deadbeef"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_report_status_without_an_export_is_null(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_report_status(_mk("GET", "s", app=_app(), match={"id": cid}))
        assert _body(resp) == {"slug": None}

    @pytest.mark.asyncio
    async def test_report_status_returns_a_live_slug(self, _isolate: Path):
        cid = _campaign()
        _set_slug(cid, "slug-1")
        store = MagicMock()
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_report_status(_mk("GET", "s", app=_app(), match={"id": cid}))
        assert _body(resp) == {"slug": "slug-1"}
        store.get.assert_called_once_with("slug-1")

    @pytest.mark.asyncio
    async def test_report_status_hides_a_deleted_artifact(self, _isolate: Path):
        cid = _campaign()
        _set_slug(cid, "slug-1")
        store = MagicMock()
        store.get.side_effect = h.ArtifactNotFoundError("gone")
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_report_status(_mk("GET", "s", app=_app(), match={"id": cid}))
        assert _body(resp) == {"slug": None}

    @pytest.mark.asyncio
    async def test_report_status_survives_a_broken_store(self, _isolate: Path):
        cid = _campaign()
        _set_slug(cid, "slug-1")
        store = MagicMock()
        store.get.side_effect = RuntimeError("store on fire")
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_report_status(_mk("GET", "s", app=_app(), match={"id": cid}))
        assert _body(resp) == {"slug": None}

    @pytest.mark.asyncio
    async def test_to_artifact_without_the_artifact_system_is_a_503(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(h, "_HAS_ARTIFACTS", False)
        cid = _campaign()
        resp = await h._handle_to_artifact(_mk("POST", "a", app=_app(), match={"id": cid}))
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_to_artifact_without_findings_is_a_404(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_to_artifact(_mk("POST", "a", app=_app(), match={"id": cid}))
        assert resp.status == 404
        assert _body(resp)["error"] == "No findings yet"
        assert _body(resp)["code"] == "findings_missing"

    @pytest.mark.asyncio
    async def test_to_artifact_routes_findings_through_complete_campaign_reader(
        self, _isolate: Path
    ):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings")
        with mock.patch.object(h, "_read_campaign_file_bytes", return_value=None) as reader:
            resp = await h._handle_to_artifact(_mk("POST", "a", app=_app(), match={"id": cid}))
        assert resp.status == 409
        assert _body(resp)["code"] == "findings_refused"
        assert reader.call_args.args[1] == ("FINDINGS.md",)
        assert reader.call_args.kwargs["max_bytes"] == h._REPORT_EXPORT_MAX_BYTES
        assert reader.call_args.kwargs.get("allow_truncate", False) is False

    @pytest.mark.asyncio
    async def test_to_artifact_exports_complete_large_report_without_view_banner(
        self, _isolate: Path
    ):
        cid = _campaign()
        tail = "AUTHORITATIVE_EXPORT_TAIL"
        findings = "A" * (h._REPORT_VIEW_MAX_BYTES + 1) + tail
        (h._campaign_dir(cid) / "FINDINGS.md").write_text(findings)
        store = MagicMock()
        store.create.return_value = _FakeArtifact("slug-large")

        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_to_artifact(_mk("POST", "a", app=_app(), match={"id": cid}))

        assert resp.status == 201
        exported = store.create.call_args.kwargs["content"]
        assert tail in exported
        assert "Report view limited" not in exported
        assert "Recent cycle evidence" not in exported

    @pytest.mark.asyncio
    async def test_to_artifact_rejects_report_over_complete_export_bound(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_bytes(b"x" * (h._REPORT_EXPORT_MAX_BYTES + 1))

        resp = await h._handle_to_artifact(_mk("POST", "a", app=_app(), match={"id": cid}))

        assert resp.status == 413
        assert _body(resp)["code"] == "findings_too_large"

    @pytest.mark.asyncio
    async def test_to_artifact_falls_back_to_a_mechanical_render(self, _isolate: Path):
        cid = _campaign(sub_questions=[{"text": "Sub one", "status": "answered"}])
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("Line one\n\nLine two")
        store = MagicMock()
        store.create.return_value = _FakeArtifact("slug-new")
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_to_artifact(_mk("POST", "a", app=_app(), match={"id": cid}))
        assert resp.status == 201
        assert _body(resp) == {
            "slug": "slug-new",
            "name": _body(resp)["name"],
            "regenerated": False,
        }
        html = store.create.call_args.kwargs["content"]
        assert "<!DOCTYPE html>" in html
        assert "Line one" in html
        assert "✅ Sub one" in html
        assert _get_slug(cid) == "slug-new"

    @pytest.mark.asyncio
    async def test_to_artifact_prefers_the_llm_authored_html(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings")
        pool = SimpleNamespace(
            send=AsyncMock(return_value="```html\n<!DOCTYPE html><p>authored</p>\n```")
        )
        store = MagicMock()
        store.create.return_value = _FakeArtifact("slug-new")
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_to_artifact(
                _mk("POST", "a", app=_app(auto_research_llm_pool=pool), match={"id": cid})
            )
        assert resp.status == 201
        assert store.create.call_args.kwargs["content"] == "<!DOCTYPE html><p>authored</p>"

    @pytest.mark.asyncio
    async def test_to_artifact_falls_back_when_the_llm_fails(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings")
        pool = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("pool down")))
        store = MagicMock()
        store.create.return_value = _FakeArtifact("slug-new")
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_to_artifact(
                _mk("POST", "a", app=_app(auto_research_llm_pool=pool), match={"id": cid})
            )
        assert resp.status == 201
        assert "<!DOCTYPE html>" in store.create.call_args.kwargs["content"]

    @pytest.mark.asyncio
    async def test_to_artifact_reuses_a_live_slug_as_a_new_version(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings")
        _set_slug(cid, "slug-old")
        store = MagicMock()
        store.update.return_value = _FakeArtifact("slug-old")
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_to_artifact(_mk("POST", "a", app=_app(), match={"id": cid}))
        assert resp.status == 200
        assert _body(resp)["regenerated"] is True
        store.create.assert_not_called()
        assert store.update.call_args.kwargs["snapshot"] is True

    @pytest.mark.asyncio
    async def test_to_artifact_rebinds_when_the_stored_slug_is_dead(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings")
        _set_slug(cid, "slug-dead")
        store = MagicMock()
        store.get.side_effect = h.ArtifactNotFoundError("gone")
        store.create.return_value = _FakeArtifact("slug-fresh")
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_to_artifact(_mk("POST", "a", app=_app(), match={"id": cid}))
        assert resp.status == 201
        assert _body(resp)["regenerated"] is False
        assert _get_slug(cid) == "slug-fresh"

    def test_render_escapes_hostile_findings_and_bare_sub_questions(self):
        html = h._render_findings_html(
            "<script>q</script>",
            ["bare one", {"text": "d", "origin": "emergent"}],
            "a\n\nb",
            3,
            "a1b2c3d4",
        )
        assert "<script>q</script>" not in html
        assert "&lt;script&gt;" in html
        assert "🔍 bare one" in html
        assert "(emergent)" in html
        assert "3 cycles" in html


def _set_slug(cid: str, slug: str) -> None:
    db = h._get_db()
    db.execute("UPDATE campaigns SET report_artifact_slug = ? WHERE id = ?", (slug, cid))
    db.commit()
    db.close()


def _get_slug(cid: str) -> str | None:
    db = h._get_db()
    row = db.execute("SELECT report_artifact_slug FROM campaigns WHERE id = ?", (cid,)).fetchone()
    db.close()
    return row["report_artifact_slug"] if row else None


# --- knowledge library ----------------------------------------------------


class TestKnowledgeRoutes:
    @pytest.mark.asyncio
    async def test_status_without_a_knowledge_store_is_false(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_knowledge_status(_mk("GET", "k", app=_app(), match={"id": cid}))
        assert _body(resp) == {"in_library": False}

    @pytest.mark.asyncio
    async def test_status_reports_an_existing_source(self, _isolate: Path):
        cid = _campaign()
        store = MagicMock()
        store.get_source_by_uri.return_value = {"id": 7}
        app = _app(state=SimpleNamespace(knowledge_store=store))
        resp = await h._handle_knowledge_status(_mk("GET", "k", app=app, match={"id": cid}))
        assert _body(resp) == {"in_library": True, "source_id": 7}
        expected = str((h._campaign_dir(cid) / "findings_for_knowledge.md").resolve())
        store.get_source_by_uri.assert_called_once_with(expected)

    @pytest.mark.asyncio
    async def test_status_reports_absence(self, _isolate: Path):
        cid = _campaign()
        store = MagicMock()
        store.get_source_by_uri.return_value = None
        app = _app(state=SimpleNamespace(knowledge_store=store))
        resp = await h._handle_knowledge_status(_mk("GET", "k", app=app, match={"id": cid}))
        assert _body(resp) == {"in_library": False}

    @pytest.mark.asyncio
    async def test_status_survives_a_broken_store(self, _isolate: Path):
        cid = _campaign()
        store = MagicMock()
        store.get_source_by_uri.side_effect = RuntimeError("index corrupt")
        app = _app(state=SimpleNamespace(knowledge_store=store))
        resp = await h._handle_knowledge_status(_mk("GET", "k", app=app, match={"id": cid}))
        assert _body(resp) == {"in_library": False}

    @pytest.mark.asyncio
    async def test_ingest_without_findings_is_a_404(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_to_knowledge(_mk("POST", "k", app=_app(), match={"id": cid}))
        assert resp.status == 404
        assert _body(resp)["code"] == "findings_missing"

    @pytest.mark.asyncio
    async def test_ingest_routes_findings_through_complete_campaign_reader(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings")
        app = _app(
            state=SimpleNamespace(knowledge_store=MagicMock()),
            knowledge_pipeline=SimpleNamespace(ingest_text=AsyncMock()),
        )
        with mock.patch.object(h, "_read_campaign_file_bytes", return_value=None) as reader:
            resp = await h._handle_to_knowledge(_mk("POST", "k", app=app, match={"id": cid}))
        assert resp.status == 409
        assert _body(resp)["code"] == "findings_refused"
        assert reader.call_args.args[1] == ("FINDINGS.md",)
        assert reader.call_args.kwargs["max_bytes"] == h._REPORT_EXPORT_MAX_BYTES
        assert reader.call_args.kwargs.get("allow_truncate", False) is False

    @pytest.mark.asyncio
    async def test_ingest_exports_complete_large_report_without_view_banner(self, _isolate: Path):
        cid = _campaign()
        tail = "AUTHORITATIVE_KNOWLEDGE_TAIL"
        findings = "K" * (h._REPORT_VIEW_MAX_BYTES + 1) + tail
        campaign = h._campaign_dir(cid)
        (campaign / "FINDINGS.md").write_text(findings)
        store = MagicMock()
        store.get_source_by_uri.return_value = None
        store.add_source.return_value = 11
        pipeline = SimpleNamespace(ingest_text=AsyncMock())
        app = _app(
            state=SimpleNamespace(knowledge_store=store),
            knowledge_pipeline=pipeline,
        )

        resp = await h._handle_to_knowledge(_mk("POST", "k", app=app, match={"id": cid}))

        assert resp.status == 201
        exported = (campaign / "findings_for_knowledge.md").read_text()
        assert tail in exported
        assert "Report view limited" not in exported
        assert "Recent cycle evidence" not in exported
        await _drain_bg_tasks(app)

    @pytest.mark.asyncio
    async def test_ingest_rejects_report_over_complete_export_bound(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_bytes(b"x" * (h._REPORT_EXPORT_MAX_BYTES + 1))
        app = _app(
            state=SimpleNamespace(knowledge_store=MagicMock()),
            knowledge_pipeline=SimpleNamespace(ingest_text=AsyncMock()),
        )

        resp = await h._handle_to_knowledge(_mk("POST", "k", app=app, match={"id": cid}))

        assert resp.status == 413
        assert _body(resp)["code"] == "findings_too_large"

    @pytest.mark.asyncio
    async def test_ingest_without_a_store_is_a_503(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings")
        resp = await h._handle_to_knowledge(_mk("POST", "k", app=_app(), match={"id": cid}))
        assert resp.status == 503
        assert "Knowledge Library unavailable" in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_ingest_without_a_pipeline_is_a_503(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings")
        app = _app(state=SimpleNamespace(knowledge_store=MagicMock()))
        resp = await h._handle_to_knowledge(_mk("POST", "k", app=app, match={"id": cid}))
        assert resp.status == 503
        assert "pipeline" in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_ingest_refuses_a_duplicate(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings")
        store = MagicMock()
        store.get_source_by_uri.return_value = {"id": 3}
        app = _app(
            state=SimpleNamespace(knowledge_store=store),
            knowledge_pipeline=SimpleNamespace(ingest_text=AsyncMock()),
        )
        resp = await h._handle_to_knowledge(_mk("POST", "k", app=app, match={"id": cid}))
        assert resp.status == 409
        assert _body(resp) == {"error": "Already in Knowledge Library", "id": 3}

    @pytest.mark.asyncio
    async def test_ingest_writes_a_sanitized_copy_and_marks_it_synced(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings body")
        store = MagicMock()
        store.get_source_by_uri.return_value = None
        store.add_source.return_value = 11
        pipeline = SimpleNamespace(ingest_text=AsyncMock())
        app = _app(state=SimpleNamespace(knowledge_store=store), knowledge_pipeline=pipeline)
        resp = await h._handle_to_knowledge(_mk("POST", "k", app=app, match={"id": cid}))
        assert resp.status == 201
        assert _body(resp) == {"id": 11, "status": "ingesting"}
        sanitized = h._campaign_dir(cid) / "findings_for_knowledge.md"
        assert sanitized.read_text() == "findings body"
        await _drain_bg_tasks(app)
        pipeline.ingest_text.assert_awaited_once()
        statuses = [c.args[0] for c in store.db.execute.call_args_list]
        assert any("'synced'" in s for s in statuses)

    @pytest.mark.asyncio
    async def test_ingest_store_statements_run_off_the_event_loop(self, _isolate: Path):
        """Every store statement for the
        to-knowledge flow must run in a worker thread, so a lock wait on the
        store's busy timeout stalls a thread instead of the event loop (which
        the watchdog would kill). Pins add_source, the syncing/synced UPDATEs,
        and their commits to non-loop threads."""
        import threading

        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings body")
        loop_thread = threading.get_ident()
        seen_threads: list[int] = []

        def _record(*_a, **_k):
            seen_threads.append(threading.get_ident())
            return MagicMock()

        store = MagicMock()
        store.get_source_by_uri.side_effect = lambda *_a: (_record(), None)[1]
        store.add_source.side_effect = lambda **_k: (_record(), 11)[1]
        store.db.execute.side_effect = _record
        store.db.commit.side_effect = _record
        pipeline = SimpleNamespace(ingest_text=AsyncMock())
        app = _app(state=SimpleNamespace(knowledge_store=store), knowledge_pipeline=pipeline)
        resp = await h._handle_to_knowledge(_mk("POST", "k", app=app, match={"id": cid}))
        assert resp.status == 201
        await _drain_bg_tasks(app)
        # get_source_by_uri + add_source + 2 UPDATEs + 2 commits all recorded,
        # none on the loop.
        assert len(seen_threads) >= 6
        assert all(t != loop_thread for t in seen_threads)

    @pytest.mark.asyncio
    async def test_ingest_failure_marks_the_source_errored(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings body")
        store = MagicMock()
        store.get_source_by_uri.return_value = None
        store.add_source.return_value = 12
        pipeline = SimpleNamespace(ingest_text=AsyncMock(side_effect=RuntimeError("boom")))
        app = _app(state=SimpleNamespace(knowledge_store=store), knowledge_pipeline=pipeline)
        await h._handle_to_knowledge(_mk("POST", "k", app=app, match={"id": cid}))
        await _drain_bg_tasks(app)
        statuses = [c.args[0] for c in store.db.execute.call_args_list]
        assert any("'error'" in s for s in statuses)


async def _drain_bg_tasks(app: web.Application) -> None:
    tasks = list(app.get("_bg_tasks") or ())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# --- assorted helpers -----------------------------------------------------


class TestAssortedHelpers:
    @pytest.mark.asyncio
    async def test_launch_loop_of_an_unknown_campaign_stops_after_the_row_lookup(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _campaign()  # create the schema
        state = SimpleNamespace(get_or_create_slot=MagicMock())
        svc = SimpleNamespace(add=AsyncMock(), get_by_slot=MagicMock(return_value=None))
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        await h._launch_loop(_mk("PATCH", "x", app=_app(state=state)), "deadbeef")
        state.get_or_create_slot.assert_not_called()
        svc.add.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_launch_loop_arms_the_worker_without_a_conversation_log(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        cid = _campaign(name="Rate limiting study")
        slot = SimpleNamespace(key=f"research-{cid}", title="", _titled=False, _trust=False)
        state = SimpleNamespace(
            get_or_create_slot=MagicMock(return_value=slot),
            push_slot_title=MagicMock(),
            push_slots_update=MagicMock(),
            conversation_log=None,
        )
        svc = SimpleNamespace(add=AsyncMock(), get_by_slot=MagicMock(return_value=None))
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        await h._launch_loop(_mk("PATCH", "x", app=_app(state=state)), cid)
        assert slot.title == "Rate limiting study"
        assert slot._titled is True
        assert slot._trust is True
        state.push_slot_title.assert_called_once()
        svc.add.assert_awaited_once()
        assert svc.add.await_args.kwargs["slot_key"] == slot.key

    @pytest.mark.asyncio
    async def test_suspend_deactivates_research_loops_and_clears_trust(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        research = SimpleNamespace(id="l1", slot_key="research-a1b2c3d4", active=True)
        other = SimpleNamespace(id="l2", slot_key="chat-1", active=True)
        svc = SimpleNamespace(
            list_all=MagicMock(return_value=[research, other]), update=AsyncMock()
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        slot = SimpleNamespace(_trust=True)
        await h._suspend_research_loops_while_disabled(
            SimpleNamespace(_slots={"research-a1b2c3d4": slot})
        )
        svc.update.assert_awaited_once_with("l1", active=False)
        assert slot._trust is False

    @pytest.mark.asyncio
    async def test_suspend_tolerates_a_failing_deactivation(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        loop = SimpleNamespace(id="l1", slot_key="research-a1b2c3d4", active=True)
        svc = SimpleNamespace(
            list_all=MagicMock(return_value=[loop]), update=AsyncMock(side_effect=RuntimeError("x"))
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        await h._suspend_research_loops_while_disabled(None)
        svc.update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_suspend_without_a_service_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(h, "_autonudge_instance", lambda: None)
        await h._suspend_research_loops_while_disabled(None)

    @pytest.mark.asyncio
    async def test_stop_loop_without_a_service_or_loop_is_a_no_op(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        await h._stop_loop("a1b2c3d4", remove=True)  # no autonudge at all
        svc = SimpleNamespace(get_by_slot=MagicMock(return_value=None), remove=AsyncMock())
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        await h._stop_loop("a1b2c3d4", remove=True)
        svc.remove.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("remove", [True, False], ids=["remove", "pause"])
    async def test_stop_loop_removes_or_deactivates(
        self, remove: bool, monkeypatch: pytest.MonkeyPatch
    ):
        loop = SimpleNamespace(id="l1")
        svc = SimpleNamespace(
            get_by_slot=MagicMock(return_value=loop), remove=AsyncMock(), update=AsyncMock()
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        await h._stop_loop("a1b2c3d4", remove=remove)
        if remove:
            svc.remove.assert_awaited_once_with("l1")
        else:
            svc.update.assert_awaited_once_with("l1", active=False)

    def test_audit_without_the_sel_module_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(h, "sel", None)
        h._audit("campaign_created", "a1b2c3d4")  # must not raise

    def test_redaction_fails_closed_without_the_security_module(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(h, "_HAS_SECURITY", False)
        out = h._redact_finding(
            {"s": "secret", "l": ["a", {"n": "b"}], "d": {"k": "c"}, "i": 3, "none": None}
        )
        assert out == {
            "s": "[REDACTED]",
            "l": ["[REDACTED]", {"n": "[REDACTED]"}],
            "d": {"k": "[REDACTED]"},
            "i": 3,
            "none": None,
        }

    def test_update_status_rejects_an_invalid_id(self):
        assert h.update_campaign_status("../etc", h.CampaignStatus.RUNNING) == {
            "error": "invalid campaign_id"
        }

    def test_pending_question_reads_and_tolerates_junk(self, _isolate: Path):
        cid = _campaign()
        assert h._pending_question(cid) is None
        qp = h._campaign_dir(cid) / "questions.json"
        qp.write_text('{"question": "Which DB?"}')
        assert h._pending_question(cid) == "Which DB?"
        qp.write_text("{not json")
        assert h._pending_question(cid) is None

    def test_unattended_mode_discards_a_stray_question(self, _isolate: Path):
        cid = _campaign()
        qp = h._campaign_dir(cid) / "questions.json"
        qp.write_text('{"question": "Which DB?"}')
        assert h._should_pause_for_question(cid, True) is False
        assert not qp.exists()

    def test_attended_mode_pauses_on_a_question(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / "questions.json").write_text('{"question": "Which DB?"}')
        assert h._should_pause_for_question(cid, False) is True

    def test_no_question_never_pauses(self, _isolate: Path):
        assert h._should_pause_for_question(_campaign(), False) is False

    def test_get_findings_skips_unreadable_files(self, _isolate: Path):
        cid = _campaign()
        _write_finding(cid, 1, summary="good")
        (h._campaign_dir(cid) / "findings" / "cycle_002.json").write_text("{broken")
        findings = h.get_findings(cid)
        assert [f["summary"] for f in findings] == ["good"]

    def test_get_findings_of_a_campaign_without_a_dir_is_empty(self, _isolate: Path):
        assert h.get_findings("a1b2c3d4") == []

    def test_delete_of_an_invalid_id_reports_an_error(self):
        assert h.delete_campaign("../etc") == {"error": "invalid campaign_id"}

    def test_fork_name_does_not_double_prefix(self):
        once = h._fork_name("Rate limiting")
        assert once == h._fork_name(once)

    def test_reserve_zone_math(self):
        assert h._reserve_cycles(30, 0.15) == 5
        assert h._reserve_cycles(0, 0.15) == 0
        assert h._in_reserve_zone(24, 30, 0.15) is False
        assert h._in_reserve_zone(25, 30, 0.15) is True
        assert h._in_reserve_zone(99, 0, 0.15) is False

    def test_cycle_files_of_a_missing_dir_is_empty(self, tmp_path: Path):
        assert h._cycle_finding_files(tmp_path / "nowhere") == []

    def test_stagnation_of_an_invalid_id_is_false(self):
        assert h.check_stagnation("../etc") is False

    def test_worker_done_of_an_invalid_id_is_absent(self):
        assert h._read_worker_done("../etc") is None

    def test_worker_done_rejects_a_non_regular_marker(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / h._WORKER_DONE_FILENAME).mkdir()
        assert h._read_worker_done(cid) is None

    def test_clear_marker_of_an_invalid_id_is_a_no_op(self):
        h._clear_worker_done_marker("../etc")  # must not raise

    def test_tree_node_redaction_passes_primitives_and_recurses_lists(self):
        assert h._redact_tree_node(7) == 7
        assert h._redact_tree_node(None) is None
        assert h._redact_tree_node(["plain", ["nested"]]) == ["plain", ["nested"]]


class TestActionDispatch:
    """``_handle_action``'s worker dispatch: agent loop vs Dynamic Workflow run."""

    @pytest.fixture
    def dispatch(self, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
        calls = SimpleNamespace(
            launch_workflow=AsyncMock(),
            stop_workflow=AsyncMock(),
            launch_loop=AsyncMock(),
            stop_loop=AsyncMock(),
        )
        monkeypatch.setattr(h, "_launch_workflow", calls.launch_workflow)
        monkeypatch.setattr(h, "_stop_workflow", calls.stop_workflow)
        monkeypatch.setattr(h, "_launch_loop", calls.launch_loop)
        monkeypatch.setattr(h, "_stop_loop", calls.stop_loop)
        return calls

    async def _act(self, cid: str, action: str) -> web.StreamResponse:
        return await h._handle_action(
            _mk("PATCH", f"campaigns/{cid}", app=_app(), match={"id": cid}, body={"action": action})
        )

    @pytest.mark.asyncio
    async def test_malformed_body_is_a_400(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_action(
            _mk("PATCH", f"campaigns/{cid}", app=_app(), match={"id": cid}, body=None)
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_action_is_a_400(self, _isolate: Path):
        resp = await self._act(_campaign(), "teleport")
        assert resp.status == 400
        assert "Unknown action" in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_action_on_a_missing_campaign_is_a_404(self, _isolate: Path):
        _campaign()
        resp = await self._act("deadbeef", "start")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_aliased_campaign_cannot_start_or_acquire_slot_ownership(
        self, _isolate: Path, dispatch
    ):
        requested = _campaign()
        owner = _campaign()
        requested_dir = h._campaign_dir(requested)
        requested_dir.joinpath("status.json").unlink()
        requested_dir.joinpath("findings").rmdir()
        requested_dir.rmdir()
        make_dir_link(requested_dir, h._campaign_dir(owner))

        response = await self._act(requested, "start")

        assert response.status == 400
        assert _body(response)["code"] == "campaign_identity_invalid"
        assert _status(requested) == h.CampaignStatus.READY
        assert _status(owner) == h.CampaignStatus.READY
        dispatch.launch_loop.assert_not_awaited()
        dispatch.launch_workflow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_replaced_real_campaign_directory_cannot_acquire_slot_ownership(
        self, _isolate: Path, dispatch
    ):
        requested = _campaign()
        owner = _campaign()
        requested_dir = h._campaign_dir(requested)
        owner_dir = h._campaign_dir(owner)
        requested_dir.rename(requested_dir.with_name("parked-requested"))
        owner_dir.rename(requested_dir)

        response = await self._act(requested, "start")

        assert response.status == 400
        assert _body(response)["code"] == "campaign_identity_invalid"
        dispatch.launch_loop.assert_not_awaited()
        dispatch.launch_workflow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_on_a_running_campaign_is_a_409(self, _isolate: Path, dispatch):
        cid = _campaign()
        _running(cid)
        resp = await self._act(cid, "start")
        assert resp.status == 409
        dispatch.launch_loop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_status_write_is_reported_as_a_404(
        self, _isolate: Path, dispatch, monkeypatch: pytest.MonkeyPatch
    ):
        cid = _campaign()
        monkeypatch.setattr(h, "update_campaign_status", lambda *a, **k: {"error": "vanished"})
        resp = await self._act(cid, "start")
        assert resp.status == 404
        dispatch.launch_loop.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["start", "resume"], ids=["start", "resume"])
    async def test_postcommit_running_sidecar_failure_is_structured_after_rollback(
        self,
        _isolate: Path,
        dispatch,
        monkeypatch: pytest.MonkeyPatch,
        action: str,
    ):
        """A failed RUNNING sidecar returns a stable error after restoring the row."""
        cid = _campaign()
        if action == "resume":
            _running(cid)
            h.update_campaign_status(cid, h.CampaignStatus.FAILED, error_message="stalled")
        previous = h.get_campaign(cid)
        assert previous is not None
        real_write_status = h.write_status

        def fail_running_sidecar(campaign_id, status, **kwargs):
            if status == h.CampaignStatus.RUNNING:
                raise OSError("status store unavailable")
            return real_write_status(campaign_id, status, **kwargs)

        monkeypatch.setattr(h, "write_status", fail_running_sidecar)
        response = await self._act(cid, action)
        assert response.status == 500
        assert _body(response) == {
            "error": "status store unavailable",
            "code": "campaign_action_failed",
        }

        current = h.get_campaign(cid)
        assert current is not None
        assert current["status"] == previous["status"]
        assert current["error_message"] == previous["error_message"]
        assert current["started_at"] == previous["started_at"]
        assert current["completed_at"] == previous["completed_at"]
        dispatch.launch_loop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rollback_sidecar_failure_is_structured_after_database_restore(
        self, _isolate: Path, dispatch, monkeypatch: pytest.MonkeyPatch
    ):
        cid = _campaign()
        previous = h.get_campaign(cid)
        assert previous is not None
        monkeypatch.setattr(
            h,
            "write_status",
            MagicMock(side_effect=OSError("status store unavailable")),
        )

        response = await self._act(cid, "start")

        assert response.status == 500
        assert _body(response) == {
            "error": "status store unavailable; rollback storage recovered as ready",
            "code": "campaign_action_failed",
        }
        current = h.get_campaign(cid)
        assert current is not None
        assert current["status"] == previous["status"] == h.CampaignStatus.READY
        dispatch.launch_loop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rollback_database_failure_forces_a_durable_failed_row(
        self, _isolate: Path, dispatch, monkeypatch: pytest.MonkeyPatch
    ):
        cid = _campaign(execution_mode="workflow")
        dispatch.launch_workflow.side_effect = OSError("run store unavailable")
        monkeypatch.setattr(
            h,
            "_restore_campaign_after_failed_launch",
            MagicMock(side_effect=sqlite3.OperationalError("rollback write failed")),
        )

        response = await self._act(cid, "start")

        assert response.status == 500
        assert _body(response) == {
            "error": "run store unavailable; rollback storage recovered as failed",
            "code": "campaign_action_failed",
        }
        current = h.get_campaign(cid)
        assert current is not None
        assert current["status"] == h.CampaignStatus.FAILED
        assert "previous state could not be restored" in current["error_message"]

    @pytest.mark.asyncio
    async def test_double_database_failure_never_claims_structured_recovery(
        self, _isolate: Path, dispatch, monkeypatch: pytest.MonkeyPatch
    ):
        cid = _campaign(execution_mode="workflow")
        dispatch.launch_workflow.side_effect = OSError("run store unavailable")
        monkeypatch.setattr(
            h,
            "_restore_campaign_after_failed_launch",
            MagicMock(side_effect=sqlite3.OperationalError("rollback write failed")),
        )
        monkeypatch.setattr(
            h,
            "_force_failed_after_rollback_storage_error",
            MagicMock(side_effect=sqlite3.OperationalError("fail-safe write failed")),
        )
        monkeypatch.setattr(h, "_persisted_campaign_status", lambda _cid: "running")

        with pytest.raises(h._CampaignRollbackUnsafe):
            await self._act(cid, "start")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["start", "resume"], ids=["start", "resume"])
    async def test_workflow_mode_start_launches_a_run(self, _isolate: Path, dispatch, action):
        cid = _campaign(execution_mode="workflow")
        if action == "resume":
            _running(cid)
            h.update_campaign_status(cid, h.CampaignStatus.PAUSED)
        resp = await self._act(cid, action)
        assert resp.status == 200
        dispatch.launch_workflow.assert_awaited_once()
        dispatch.launch_loop.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["start", "resume"], ids=["start", "resume"])
    async def test_workflow_storage_failure_is_structured_after_rollback(
        self, _isolate: Path, dispatch, action: str
    ):
        cid = _campaign(execution_mode="workflow")
        if action == "resume":
            _running(cid)
            h.update_campaign_status(cid, h.CampaignStatus.PAUSED)
        previous = h.get_campaign(cid)
        assert previous is not None
        dispatch.launch_workflow.side_effect = OSError("workflow run store full")

        response = await self._act(cid, action)
        assert response.status == 500
        assert _body(response) == {
            "error": "workflow run store full",
            "code": "campaign_action_failed",
        }
        current = h.get_campaign(cid)
        assert current is not None
        assert current["status"] == previous["status"]
        assert current["started_at"] == previous["started_at"]
        assert current["completed_at"] == previous["completed_at"]
        assert current["error_message"] == previous["error_message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["start", "resume"], ids=["start", "resume"])
    async def test_workflow_launch_cancellation_propagates_after_rollback(
        self, _isolate: Path, dispatch, action: str
    ):
        cid = _campaign(execution_mode="workflow")
        if action == "resume":
            _running(cid)
            h.update_campaign_status(cid, h.CampaignStatus.PAUSED)
        previous = h.get_campaign(cid)
        assert previous is not None
        dispatch.launch_workflow.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await self._act(cid, action)
        current = h.get_campaign(cid)
        assert current is not None
        assert current["status"] == previous["status"]
        assert current["started_at"] == previous["started_at"]
        assert current["completed_at"] == previous["completed_at"]
        assert current["error_message"] == previous["error_message"]

    @pytest.mark.asyncio
    async def test_workflow_programming_error_propagates_after_rollback(
        self, _isolate: Path, dispatch
    ):
        cid = _campaign(execution_mode="workflow")
        previous = h.get_campaign(cid)
        assert previous is not None
        dispatch.launch_workflow.side_effect = ValueError("bad launch result")

        with pytest.raises(ValueError, match="bad launch result"):
            await self._act(cid, "start")
        current = h.get_campaign(cid)
        assert current is not None
        assert current["status"] == previous["status"]
        assert current["started_at"] == previous["started_at"]
        assert current["completed_at"] == previous["completed_at"]
        assert current["error_message"] == previous["error_message"]

    @pytest.mark.asyncio
    async def test_agent_mode_start_arms_the_loop(self, _isolate: Path, dispatch):
        cid = _campaign()
        await self._act(cid, "start")
        dispatch.launch_loop.assert_awaited_once()
        dispatch.launch_workflow.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["agent", "workflow"])
    async def test_unsuccessful_launch_result_restores_prior_campaign(
        self, _isolate: Path, dispatch, mode
    ):
        cid = _campaign(execution_mode=mode)
        previous = h.get_campaign(cid)
        assert previous is not None
        if mode == "workflow":
            dispatch.launch_workflow.return_value = False
        else:
            dispatch.launch_loop.return_value = False

        response = await self._act(cid, "start")
        assert response.status == 500
        assert _body(response) == {
            "error": f"Auto Research {mode} worker could not be launched",
            "code": "campaign_action_failed",
        }

        current = h.get_campaign(cid)
        assert current is not None
        assert current["status"] == previous["status"] == h.CampaignStatus.READY
        assert current["started_at"] == previous["started_at"]
        assert current["completed_at"] == previous["completed_at"]
        assert current["error_message"] == previous["error_message"]

    @pytest.mark.asyncio
    async def test_resume_replacement_wins_over_waiting_watchdog_settlement(
        self, _isolate: Path, dispatch, monkeypatch: pytest.MonkeyPatch
    ):
        """A settlement that captured the retained loop before waiting for the
        transition lock must not classify the replacement run after Resume's
        slow loop arm commits."""
        cid = _campaign()
        _running(cid)
        h.update_campaign_status(cid, h.CampaignStatus.STOPPED)
        old_loop = SimpleNamespace(id="old-loop", active=False)
        replacement = SimpleNamespace(id="replacement-loop", active=True)
        current = {"loop": old_loop}
        svc = SimpleNamespace(
            get_by_slot=MagicMock(side_effect=lambda _slot: current["loop"]),
            update=AsyncMock(),
            remove=AsyncMock(),
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        verdict = MagicMock(side_effect=AssertionError("replacement run was classified"))
        monkeypatch.setattr(h, "_stalled_campaign_verdict", verdict)
        launch_started = asyncio.Event()
        release_launch = asyncio.Event()

        async def slow_launch(*_args, **_kwargs):
            launch_started.set()
            await release_launch.wait()
            current["loop"] = replacement
            return True

        dispatch.launch_loop.side_effect = slow_launch
        resume = asyncio.create_task(self._act(cid, "resume"))
        await launch_started.wait()
        observed_started_at = (h.get_campaign(cid) or {})["started_at"]
        settlement = asyncio.create_task(
            h._settle_campaign_from_watchdog(
                cid,
                [],
                {cid: 0},
                {cid: 1.0},
                observed_started_at=observed_started_at,
                stopped_reason="cycle_cap",
            )
        )
        await asyncio.sleep(0)
        assert not settlement.done(), "settlement did not wait for Resume's transition lock"

        release_launch.set()
        response = await resume
        await settlement

        assert response.status == 200
        assert (h.get_campaign(cid) or {})["status"] == h.CampaignStatus.RUNNING
        assert current["loop"] is replacement
        verdict.assert_not_called()
        svc.update.assert_not_awaited()
        svc.remove.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_watchdog_settlement_wins_before_resume_replacement(
        self, _isolate: Path, dispatch, monkeypatch: pytest.MonkeyPatch
    ):
        """Opposite ordering: settlement owns the lock first, Resume waits, then
        starts from the settled row and installs its replacement without a
        cancellation or database deadlock."""
        cid = _campaign()
        _running(cid)
        observed_started_at = (h.get_campaign(cid) or {})["started_at"]
        old_loop = SimpleNamespace(id="old-loop", active=False)
        replacement = SimpleNamespace(id="replacement-loop", active=True)
        current = {"loop": old_loop}

        async def remove_old(loop_id: str) -> None:
            assert loop_id == old_loop.id
            current["loop"] = None

        svc = SimpleNamespace(
            get_by_slot=MagicMock(side_effect=lambda _slot: current["loop"]),
            update=AsyncMock(),
            remove=AsyncMock(side_effect=remove_old),
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        monkeypatch.setattr(
            h,
            "_stalled_campaign_verdict",
            lambda *_args, **_kwargs: (h.CampaignStatus.STOPPED, "bound reached"),
        )
        terminal_write_started = threading.Event()
        release_terminal_write = threading.Event()
        real_update = h.update_campaign_status

        def slow_terminal_write(campaign_id: str, status: str, **kwargs):
            if status == h.CampaignStatus.STOPPED:
                terminal_write_started.set()
                assert release_terminal_write.wait(timeout=5)
            return real_update(campaign_id, status, **kwargs)

        monkeypatch.setattr(h, "update_campaign_status", slow_terminal_write)

        async def launch_replacement(*_args, **_kwargs):
            current["loop"] = replacement
            return True

        dispatch.launch_loop.side_effect = launch_replacement
        settlement = asyncio.create_task(
            h._settle_campaign_from_watchdog(
                cid,
                [],
                {cid: 0},
                {cid: 1.0},
                observed_started_at=observed_started_at,
                stopped_reason="cycle_cap",
            )
        )
        assert await asyncio.to_thread(terminal_write_started.wait, 5)
        resume = asyncio.create_task(self._act(cid, "resume"))
        await asyncio.sleep(0)
        assert not resume.done(), "Resume bypassed the settlement transition lock"

        release_terminal_write.set()
        await settlement
        response = await resume

        assert response.status == 200
        assert (h.get_campaign(cid) or {})["status"] == h.CampaignStatus.RUNNING
        assert current["loop"] is replacement
        svc.remove.assert_awaited_once_with(old_loop.id)
        dispatch.launch_loop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancelled_resume_keeps_running_when_replacement_commits(
        self, _isolate: Path, dispatch
    ):
        """Cancellation waits for launch and rolls back only an actual failure."""
        cid = _campaign()
        _running(cid)
        h.update_campaign_status(cid, h.CampaignStatus.FAILED, error_message="stalled")
        launch_started = asyncio.Event()
        release_launch = asyncio.Event()

        async def committed_launch(*_args, **_kwargs):
            launch_started.set()
            await release_launch.wait()
            return True

        dispatch.launch_loop.side_effect = committed_launch
        request_task = asyncio.create_task(self._act(cid, "resume"))
        await launch_started.wait()
        request_task.cancel()
        release_launch.set()

        with pytest.raises(asyncio.CancelledError):
            await request_task

        campaign = h.get_campaign(cid)
        assert campaign is not None
        assert campaign["status"] == h.CampaignStatus.RUNNING
        assert campaign["error_message"] is None
        dispatch.launch_loop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancelled_resume_waits_for_status_write_and_launch(
        self, _isolate: Path, dispatch, monkeypatch: pytest.MonkeyPatch
    ):
        """Cancellation cannot release the lock between RUNNING commit and launch."""
        import threading

        cid = _campaign()
        _running(cid)
        h.update_campaign_status(cid, h.CampaignStatus.FAILED, error_message="stalled")
        real_update = h.update_campaign_status
        write_started = threading.Event()
        release_write = threading.Event()

        def slow_running_write(campaign_id, status, **kwargs):
            if status == h.CampaignStatus.RUNNING:
                write_started.set()
                assert release_write.wait(timeout=5)
            return real_update(campaign_id, status, **kwargs)

        monkeypatch.setattr(h, "update_campaign_status", slow_running_write)
        request_task = asyncio.create_task(self._act(cid, "resume"))
        assert await asyncio.to_thread(write_started.wait, 5)
        request_task.cancel()
        await asyncio.sleep(0)
        assert not request_task.done()
        release_write.set()

        with pytest.raises(asyncio.CancelledError):
            await request_task

        campaign = h.get_campaign(cid)
        assert campaign is not None
        assert campaign["status"] == h.CampaignStatus.RUNNING
        assert campaign["error_message"] is None
        dispatch.launch_loop.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["agent", "workflow"])
    async def test_pause_stops_the_right_worker(self, _isolate: Path, dispatch, mode):
        cid = _campaign(execution_mode=mode)
        _running(cid)
        resp = await self._act(cid, "pause")
        assert resp.status == 200
        if mode == "workflow":
            dispatch.stop_workflow.assert_awaited_once()
            dispatch.stop_loop.assert_not_awaited()
        else:
            dispatch.stop_loop.assert_awaited_once_with(cid, remove=False)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["agent", "workflow"])
    async def test_stop_tears_the_right_worker_down(self, _isolate: Path, dispatch, mode):
        cid = _campaign(execution_mode=mode)
        _running(cid)
        resp = await self._act(cid, "stop")
        assert resp.status == 200
        if mode == "workflow":
            dispatch.stop_workflow.assert_awaited_once()
        else:
            dispatch.stop_loop.assert_awaited_once_with(cid, remove=True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["agent", "workflow"])
    async def test_delete_tears_the_right_worker_down(self, _isolate: Path, dispatch, mode):
        cid = _campaign(execution_mode=mode)
        _running(cid)
        resp = await h._handle_delete(
            _mk("DELETE", f"campaigns/{cid}", app=_app(), match={"id": cid})
        )
        assert resp.status == 200
        if mode == "workflow":
            dispatch.stop_workflow.assert_awaited_once()
        else:
            dispatch.stop_loop.assert_awaited_once_with(cid, remove=True)

    @pytest.mark.asyncio
    async def test_delete_of_a_missing_campaign_is_a_404(self, _isolate: Path, dispatch):
        _campaign()
        resp = await h._handle_delete(
            _mk("DELETE", "campaigns/deadbeef", app=_app(), match={"id": "deadbeef"})
        )
        assert resp.status == 404


class TestMalformedBodies:
    @pytest.mark.asyncio
    async def test_create_rejects_an_undecodable_body(self, _isolate: Path):
        resp = await h._handle_create(_mk("POST", "campaigns", app=_app(), body=None))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_nudge_rejects_an_undecodable_body(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_nudge(_mk("POST", "n", app=_app(), match={"id": cid}, body=None))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_add_question_rejects_an_undecodable_body(self, _isolate: Path):
        cid = _campaign()
        resp = await h._handle_add_question(
            _mk("POST", "q", app=_app(), match={"id": cid}, body=None)
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_nudge_clears_a_pending_question_and_resumes(self, _isolate: Path):
        cid = _campaign()
        _running(cid)
        h.update_campaign_status(cid, h.CampaignStatus.NEEDS_INPUT)
        (h._campaign_dir(cid) / "questions.json").write_text('{"question": "Which DB?"}')
        resp = await h._handle_nudge(
            _mk("POST", "n", app=_app(), match={"id": cid}, body={"text": "Use SQLite"})
        )
        assert _body(resp) == {"ok": True}
        assert not (h._campaign_dir(cid) / "questions.json").exists()
        assert _status(cid) == h.CampaignStatus.RUNNING

    @pytest.mark.asyncio
    async def test_to_artifact_of_an_orphaned_findings_dir_is_a_404(self, _isolate: Path):
        _campaign()  # create the schema
        (h._campaign_dir("deadbeef") / "FINDINGS.md").write_text("orphan")
        store = MagicMock()
        with mock.patch.object(h, "ArtifactStore", return_value=store):
            resp = await h._handle_to_artifact(
                _mk("POST", "a", app=_app(), match={"id": "deadbeef"})
            )
        assert resp.status == 404
        store.create.assert_not_called()


class TestEmergentExploration:
    def test_ingest_of_an_invalid_id_is_empty(self):
        assert h._ingest_emergent_questions("../etc") == []

    def test_ingest_consumes_a_malformed_file(self, _isolate: Path):
        cid = _campaign()
        emergent = h._campaign_dir(cid) / h._EMERGENT_FILENAME
        emergent.write_text("{not json")
        assert h._ingest_emergent_questions(cid) == []
        assert not emergent.exists()  # consumed regardless of validity

    def test_ingest_consumes_a_non_list_payload(self, _isolate: Path):
        cid = _campaign()
        emergent = h._campaign_dir(cid) / h._EMERGENT_FILENAME
        emergent.write_text(json.dumps({"text": "not a list"}))
        assert h._ingest_emergent_questions(cid) == []
        assert not emergent.exists()

    def test_ingest_accepts_bare_string_items(self, _isolate: Path):
        cid = _campaign()
        (h._campaign_dir(cid) / h._EMERGENT_FILENAME).write_text(
            json.dumps(["What is the retry budget?"])
        )
        admitted = h._ingest_emergent_questions(cid)
        assert [a["text"] for a in admitted] == ["What is the retry budget?"]

    def test_ingest_discards_the_file_in_workflow_mode(self, _isolate: Path):
        cid = _campaign(execution_mode="workflow")
        emergent = h._campaign_dir(cid) / h._EMERGENT_FILENAME
        emergent.write_text(json.dumps(["ignored"]))
        assert h._ingest_emergent_questions(cid) == []
        assert not emergent.exists()

    def test_activate_of_an_invalid_id_is_empty(self):
        assert h._activate_emergent("../etc") == []

    def test_activate_is_skipped_in_workflow_mode(self, _isolate: Path):
        cid = _campaign(execution_mode="workflow")
        self._seed_pending(cid, ["queued question"])
        assert h._activate_emergent(cid) == []

    def test_activate_with_a_zero_budget_activates_nothing(self, _isolate: Path):
        cid = _campaign(max_subquestions_per_round=0)
        self._seed_pending(cid, ["queued question"])
        assert h._activate_emergent(cid) == []

    @staticmethod
    def _seed_pending(cid: str, texts: list[str]) -> None:
        from kiro_crew.apps.builtins.auto_research import subquestion_queue as sq

        d = h._campaign_dir(cid)
        queue = sq.load_queue(d)
        sq.enqueue(queue, [{"text": t, "priority": 0.9} for t in texts], depth=1, max_admit=5)
        sq.save_queue(d, queue)
        assert sq.pending_count(queue) == len(texts)

    def test_finalize_mode_is_signaled_only_once(self, _isolate: Path):
        cid = _campaign()
        assert h._enter_finalize(cid) is True
        assert h._enter_finalize(cid) is False  # flag already on disk
        assert "FINALIZE MODE" in (h._campaign_dir(cid) / "guidance.txt").read_text()

    def test_finalize_of_an_invalid_id_is_false(self):
        assert h._enter_finalize("../etc") is False

    def test_advance_never_raises(self, _isolate: Path, monkeypatch: pytest.MonkeyPatch):
        cid = _campaign()
        monkeypatch.setattr(h, "_should_finalize", MagicMock(side_effect=RuntimeError("db gone")))
        h._advance_exploration(cid)  # swallowed — the watchdog must survive
