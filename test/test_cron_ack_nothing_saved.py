"""``POST /api/crons/{id}/ack`` leaves a note unread when nothing was saved.

An ack for a job the store does not hold saves no summary. Marking the bell note
read then would show "Acknowledged" on the next load for an acknowledgement that
never landed, so the note is marked read only when the summary was saved. The
reply for a missing job is ``{"ok": false, "code": "job_not_found"}``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers.cron import api_cron_ack

pytestmark = pytest.mark.asyncio

_TS = "2026-11-10T00:00:00Z"


@web.middleware
async def _owner(request: web.Request, handler):  # type: ignore[no-untyped-def]
    request["user"] = "local-app"
    request["app"] = ""
    return await handler(request)


def _state(crons: CronService, job_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        crons=crons,
        push_refresh=MagicMock(),
        ack_notification=AsyncMock(),
        has_slot=MagicMock(return_value=False),
        _notification_log=[{"ts": _TS, "kind": "cron", "job_id": job_id}],
    )


async def _ack(state: SimpleNamespace, job_id: str, body: dict[str, Any]) -> tuple[int, Any]:
    app = web.Application(middlewares=[_owner])
    app["state"] = state
    app.router.add_post("/api/crons/{job_id}/ack", api_cron_ack)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/api/crons/{job_id}/ack", json=body)
        return resp.status, json.loads(await resp.text())


async def test_an_ack_for_a_missing_job_leaves_the_note_unread(tmp_path) -> None:
    crons = CronService(base_dir=tmp_path)
    state = _state(crons, "deadbeef")

    status, body = await _ack(state, "deadbeef", {"summary": "seen", "ts": _TS})

    assert (status, body) == (200, {"ok": False, "code": "job_not_found"})
    state.ack_notification.assert_not_awaited()


async def test_a_saved_ack_still_marks_the_note_read(tmp_path) -> None:
    crons = CronService(base_dir=tmp_path)
    job = crons.add_job(name="n", message="m", every_secs=3600)
    state = _state(crons, job.id)

    status, body = await _ack(state, job.id, {"summary": "seen", "ts": _TS})

    assert (status, body) == (200, {"ok": True})
    state.ack_notification.assert_awaited_once_with(_TS)
    assert crons.list_jobs(include_disabled=True)[0].acked_items == ["seen"]
