"""The request builder, runner mock and states the task-runner handler tests share.

``test_handlers_taskrunner_coverage.py`` and its ``_refine``, ``_request_contract`` and
``_off_loop`` siblings import these by name, and ``test_workflows_startup.py`` imports
``_body`` and ``_request`` through ``test_handlers_taskrunner_coverage``: the autouse SEL
stub, the URL the exfiltration scanner trips on, the runner mock with the attributes the
handlers reach for, the dashboard states, a planned run, the mocked request and the JSON
body reader.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from body_stream_helpers import BodyStreamPayload

from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.taskrunner import Step, TaskRun

# A URL whose oversized query payload trips the (domain-agnostic) exfiltration
# scanner — see test_security_exfil_urls.py::test_long_query_redacted_domain_agnostic.
_EXFIL_URL = "https://collector.example.com/ingest?data=" + "A" * 250

# ── Fixtures / helpers ──


@pytest.fixture(autouse=True)
def _stub_sel():
    """No real security-event-log writes from any handler under test."""
    with patch("kiro_crew.dashboard.handlers.taskrunner._sel") as sel:
        sel.return_value = MagicMock()
        yield sel


def _runner(tmp_path: Path) -> MagicMock:
    """A task runner mock with the attributes the handlers reach for."""
    runner = MagicMock()
    runner._runs = {}
    runner._stall_cancelled_ids = set()
    runner._work_dir = tmp_path / "work"
    runner._workspace_dir = None
    runner._agent = ""
    runner._plan_task = None
    runner._apersist_runs = AsyncMock()
    runner._group_parallel_tasks = MagicMock(return_value=[])
    runner._auto_name = MagicMock(side_effect=lambda text: text)
    runner._workflow_begin = AsyncMock()
    runner._capture_execution = MagicMock(
        return_value=ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")
    )
    runner._bind_run_execution = AsyncMock()

    async def _delete_run(task_id: str) -> bool:
        runner._runs.pop(task_id, None)
        runner._stall_cancelled_ids.discard(task_id)
        await runner._apersist_runs()
        return True

    runner.delete_run = AsyncMock(side_effect=_delete_run)
    runner.start_background = AsyncMock(return_value="tid-1")
    runner.update_task = AsyncMock(return_value={"index": 0})
    runner.update_plan = AsyncMock()
    runner.execute_plan = AsyncMock()
    runner.retry_from_task = AsyncMock()
    runner.plan = AsyncMock()
    runner.status = MagicMock(return_value={"runs": []})
    return runner


def _state(runner: MagicMock | None) -> SimpleNamespace:
    return SimpleNamespace(task_runner=runner)


def _request(
    state: Any,
    method: str = "POST",
    path: str = "/api/taskrunner",
    *,
    match_info: dict[str, str] | None = None,
    json_body: Any = None,
    raw_json_error: bool = False,
    request_app: str = "",
    with_content_length: bool = True,
    body_present: bool = False,
    session: str = "",
) -> web.Request:
    app = web.Application()
    app["state"] = state
    if raw_json_error:
        raw = b"{bad json"
    elif json_body is not None or body_present:
        # ``body_present`` distinguishes a body whose CONTENT is the JSON
        # literal ``null`` from no body at all -- the allow_absent handlers
        # branch on that difference, and conflating them hides a non-object
        # body behind a silent default.
        raw = json.dumps(json_body).encode()
    else:
        raw = b""
    headers = {"Content-Length": str(len(raw))} if (raw and with_content_length) else {}
    # A body must DECLARE JSON or ``read_bounded_json`` refuses it 415 before the
    # shape guard these tests are about ever runs. Every real client sets this.
    if raw:
        headers["Content-Type"] = "application/json"
    if session:
        headers["X-Session-Key"] = session
    req = make_mocked_request(
        method,
        path,
        app=app,
        match_info=match_info or {},
        headers=headers,
        payload=BodyStreamPayload(raw),
    )
    req["app"] = request_app
    if request_app == "":
        # The dashboard-user class carries a subject; the mutating routes are
        # owner-gated, and with no ``owner_id`` configured the signed local
        # bootstrap subject is the owner.
        req["user"] = "local-app"
    # Kept alive: the uncapped handlers (``max_bytes=None`` -- start, plan,
    # update_plan, update_task, from_chat, refine) consume ``request.json()``;
    # the capped ones drain the payload stream instead.
    if raw_json_error:
        req.json = AsyncMock(side_effect=ValueError("bad json"))  # type: ignore[method-assign]
    elif json_body is not None or body_present:
        req.json = AsyncMock(return_value=json_body)  # type: ignore[method-assign]
    return req


def _body(resp: web.Response) -> Any:
    assert resp.body is not None
    return json.loads(bytes(resp.body))  # type: ignore[arg-type]


def _chat_state(runner: MagicMock | None) -> SimpleNamespace:
    slot = SimpleNamespace(key="slot-1", title="", task=None, append=MagicMock())
    state = SimpleNamespace(
        task_runner=runner,
        _background_tasks=set(),
        get_slot=MagicMock(return_value=None),
        get_or_create_slot=MagicMock(return_value=slot),
        push_slots_update=MagicMock(),
    )
    return state


def _planned_run(task_id: str = "p1") -> TaskRun:
    return TaskRun(
        spec_path="",
        spec_content="",
        status="planned",
        task_id=task_id,
        tasks=[
            Step(index=1, title="first", description="do it", depends_on=[]),
            Step(index=2, title="second", description="then", depends_on=[1]),
        ],
    )
