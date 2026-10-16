"""A task gate's coordinator record names its run and task as fields.

Up to three task runs execute at once and their task indexes overlap, so a
project page must tell its own run's gates from the others in the global
``GET /api/approvals`` listing. The record carries ``run_task_id`` and
``task_index`` beside ``instance``; the page reads those fields and treats the
approval id as opaque. The contract test at the end pins the page to the same
field names the producer writes.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.sessions import api_approvals
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog
from kiro_crew.testing.wait import async_wait_until

_REPO = Path(__file__).resolve().parents[1]
_PAGE = _REPO / "website" / "src" / "pages" / "ProjectDetailPage.tsx"
_CLIENT = _REPO / "website" / "src" / "api" / "client" / "approvals.ts"


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    return state


async def _raise(state: DashboardState, approval_id: str, **kwargs) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(
        state.request_approval(approval_id, "taskrunner", "Task 2: t", **kwargs)
    )
    await async_wait_until(lambda: approval_id in state._pending_approvals)
    return task


@pytest.mark.asyncio
async def test_a_task_gate_record_and_listing_name_its_run_and_task(tmp_path) -> None:
    state = _make_state(tmp_path)
    gate = await _raise(state, "gate-opaque", run_task_id="run-a-1", task_index=2)
    other = await _raise(state, "cron-approval")
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/approvals", api_approvals)
    try:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/approvals")
            assert resp.status == 200
            listed = {r["id"]: r for r in await resp.json()}
        assert listed["gate-opaque"]["run_task_id"] == "run-a-1"
        assert listed["gate-opaque"]["task_index"] == 2
        assert listed["gate-opaque"]["instance"]
        # A record raised without a task names none.
        assert "run_task_id" not in listed["cron-approval"]
        assert "task_index" not in listed["cron-approval"]
        # The raise frame carries the same record.
        raised = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "approval"]
        assert {"run_task_id": "run-a-1", "task_index": 2}.items() <= raised[0].items()
    finally:
        for approval_id in ("gate-opaque", "cron-approval"):
            state.resolve_state_approval(approval_id, False)
        assert await gate is False
        assert await other is False


@pytest.mark.parametrize(("run_task_id", "task_index"), [("", 2), ("run-a", None), ("run-a", True)])
@pytest.mark.asyncio
async def test_a_partial_task_identity_names_no_task(tmp_path, run_task_id, task_index) -> None:
    state = _make_state(tmp_path)
    gate = await _raise(state, "gate", run_task_id=run_task_id, task_index=task_index)
    try:
        record = state._pending_approvals["gate"]
        assert "run_task_id" not in record
        assert "task_index" not in record
    finally:
        state.resolve_state_approval("gate", False)
        assert await gate is False


def test_the_project_page_reads_the_fields_the_record_carries() -> None:
    """The producer and the page agree on the field names, and the page no
    longer derives a gate's run or task from its id."""
    page = _PAGE.read_text(encoding="utf-8")
    client = _CLIENT.read_text(encoding="utf-8")
    assert re.search(r"\ba\.run_task_id\b", page)
    assert re.search(r"\ba\.task_index\b", page)
    assert "task-gate-" not in page
    assert ".match(" not in page.split("queryFn", 1)[1].split("refetchInterval", 1)[0]
    assert "run_task_id?: string" in client
    assert "task_index?: number" in client
