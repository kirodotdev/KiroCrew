"""A trigger for a job that ran and was removed says so instead of a bare "not found"."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, make_mocked_request

from kiro_crew.cron_history import CronHistoryStore, CronRunRecord
from kiro_crew.cron_trigger import trigger_cron_job_status
from kiro_crew.dashboard.handlers.cron import api_cron_run

_RAN_AT = datetime(2026, 11, 17, 12, 0, 5, tzinfo=timezone.utc).timestamp()


async def _no_job(_job_id: str) -> None:
    return None


async def _trigger_through_gateway(tmp_path: Path, job_id: str) -> tuple[bool, str, int | None]:
    history = CronHistoryStore(base_dir=tmp_path)
    await history.append(
        CronRunRecord(job_id="ab12cd34ef", started_at=_RAN_AT, finished_at=_RAN_AT + 60)
    )
    app = web.Application()
    app["state"] = SimpleNamespace(
        crons=SimpleNamespace(get_job_async=_no_job, get_history=lambda: history)
    )
    app.router.add_post("/api/crons/{job_id}/run", api_cron_run)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    secret_path = tmp_path / ".local_secret"
    secret_path.write_text("s")
    try:
        assert server.port is not None
        return await asyncio.to_thread(trigger_cron_job_status, job_id, server.port, secret_path)
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_removed_job_with_a_run_names_that_run(tmp_path: Path) -> None:
    ok, msg, status = await _trigger_through_gateway(tmp_path, "ab12cd34ef")
    shown = datetime.fromtimestamp(_RAN_AT).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    assert (ok, status) == (False, 404)
    assert msg == f"Job not found: ab12cd34ef (it already ran at {shown} and was removed)"


@pytest.mark.asyncio
async def test_traversal_shaped_id_keeps_the_plain_not_found(tmp_path: Path) -> None:
    app = web.Application()
    history = CronHistoryStore(base_dir=tmp_path)
    app["state"] = SimpleNamespace(
        crons=SimpleNamespace(get_job_async=_no_job, get_history=lambda: history)
    )
    request = make_mocked_request(
        "POST", "/api/crons/x/run", match_info={"job_id": "../escape"}, app=app
    )
    response = await api_cron_run(request)
    assert response.status == 404


@pytest.mark.asyncio
async def test_id_without_any_run_keeps_the_plain_not_found(tmp_path: Path) -> None:
    ok, msg, status = await _trigger_through_gateway(tmp_path, "ff00ff00ff")
    assert (ok, msg, status) == (False, "Job not found: ff00ff00ff", 404)
