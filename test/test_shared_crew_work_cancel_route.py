"""``POST /api/shared-crew/work/{id}/cancel`` and the work list, end to end on a real service."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from test_shared_crew_entry_points import ALICE
from test_shared_crew_entry_points import service as _service

from kiro_crew.shared_crew import routes_admission
from kiro_crew.shared_crew.admission import OperationKind, OperationState

service = _service


def _cancel_request(work_id: str, body) -> web.Request:
    request = make_mocked_request(
        "POST", f"/api/shared-crew/work/{work_id}/cancel", match_info={"work_id": work_id}
    )
    request.json = AsyncMock(  # type: ignore[method-assign]
        return_value=body, side_effect=None if body is not ValueError else ValueError("bad")
    )
    return request


async def _queued(service, key="cancel-key-0001", operation=OperationKind.CHAT_TURN):
    # Occupy the one per-person slot so the second admission waits in the queue.
    await service.admit(
        budget_key=ALICE,
        session_key="dashboard:busy",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="occupy-0000001",
    )
    admission = await service.admit(
        budget_key=ALICE,
        session_key="dashboard:waiting",
        operation=operation,
        idempotency_key=key,
    )
    assert admission.queued and admission.work
    return admission.work


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [None, {}, {"expected_revision": "1"}, {"expected_revision": True}]
)
async def test_cancel_needs_an_integer_revision(service, body):
    resp = await routes_admission.api_work_cancel(_cancel_request("w_x", body))
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_cancel_of_unknown_work_is_404(service):
    resp = await routes_admission.api_work_cancel(
        _cancel_request("w_missing", {"expected_revision": 1})
    )
    assert resp.status == 404


@pytest.mark.asyncio
async def test_cancel_applies_then_a_stale_revision_conflicts(service):
    work = await _queued(service)
    resp = await routes_admission.api_work_cancel(
        _cancel_request(work.work_id, {"expected_revision": work.revision})
    )
    assert resp.status == 200
    assert json.loads(resp.text)["state"] == OperationState.CANCELLED.value
    again = await routes_admission.api_work_cancel(
        _cancel_request(work.work_id, {"expected_revision": work.revision})
    )
    assert again.status == 409
    assert json.loads(again.text)["current"]["work_id"] == work.work_id


@pytest.mark.asyncio
async def test_a_task_cancel_keeps_the_owner_gate(service, monkeypatch):
    from kiro_crew.dashboard.handlers import _shared

    work = await _queued(service, operation=OperationKind.TASK)
    denied = web.json_response({"error": "owner only"}, status=403)
    monkeypatch.setattr(
        _shared, "require_owner_for_dashboard_caller", AsyncMock(return_value=denied)
    )
    resp = await routes_admission.api_work_cancel(
        _cancel_request(work.work_id, {"expected_revision": work.revision})
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_the_work_list_reads_a_session_or_the_viewers_own(service):
    work = await _queued(service)
    long_key = make_mocked_request("GET", "/api/shared-crew/work?session_key=" + "k" * 600)
    assert (await routes_admission.api_work(long_key)).status == 400
    by_session = make_mocked_request(
        "GET",
        "/api/shared-crew/work?session_key=dashboard:waiting",
        app=SimpleNamespace(get=lambda _k, _d=None: None),
    )
    items = json.loads((await routes_admission.api_work(by_session)).text)["items"]
    assert [item["work_id"] for item in items] == [work.work_id]
    own = make_mocked_request("GET", "/api/shared-crew/work")
    assert json.loads((await routes_admission.api_work(own)).text)["items"] is not None
