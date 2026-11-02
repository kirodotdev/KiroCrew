"""A cron expression that matches no date saves, and every write surface says so.

``0 0 31 2 *`` (February 31st) is valid syntax with no next run. Jobs carrying
it keep saving -- refusing them would break stored jobs on their next edit --
so the add and update answers carry a caveat, and the next-run computation
logs one plain line for it instead of a traceback.
"""

from __future__ import annotations

import argparse
import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cli_commands import _cron
from kiro_crew.config.loader import config_dir
from kiro_crew.cron import CronJob, CronSchedule, CronService, compute_next_run_ts
from kiro_crew.cron_service import schedule
from kiro_crew.dashboard.handlers.cron import api_cron_update, api_crons_create
from kiro_crew.mcp_cron import _call_tool_inner

_NEVER = "0 0 31 2 *"


def _cron_job(expr: str) -> CronJob:
    return CronJob(
        id="never-job",
        name="n",
        message="m",
        timezone="UTC",
        schedule=CronSchedule(kind="cron", cron_expr=expr),
    )


@pytest.mark.parametrize("expr", [_NEVER, "0 0 30 2 *", "0 0 31 4 *", "0 0 31 2,4 *"])
def test_never_fires_warning_names_an_expression_with_no_date(expr: str) -> None:
    warning = schedule.never_fires_warning(CronSchedule(kind="cron", cron_expr=expr))
    assert expr in warning
    assert "never runs" in warning


@pytest.mark.parametrize(
    "sched",
    [
        CronSchedule(kind="cron", cron_expr="0 0 29 2 *"),  # leap days do happen
        CronSchedule(kind="cron", cron_expr="0 0 31 2,3 *"),
        CronSchedule(kind="cron", cron_expr="*/5 * * * *"),
        CronSchedule(kind="every", every_secs=300),
        CronSchedule(kind="at", at_ts=4_000_000_000.0),
    ],
)
def test_never_fires_warning_is_empty_for_a_schedule_that_fires(sched: CronSchedule) -> None:
    assert schedule.never_fires_warning(sched) == ""


def test_next_run_for_a_dateless_cron_logs_one_plain_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="kiro_crew.cron")
    assert compute_next_run_ts(_cron_job(_NEVER)) is None
    (record,) = [r for r in caplog.records if r.name == "kiro_crew.cron"]
    assert record.exc_info is None
    assert "never-job" in record.getMessage()
    assert _NEVER in record.getMessage()


# -- service: the job still saves (back-compat) --------------------------------


def test_service_still_saves_a_dateless_cron_on_add_and_update(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job("n", "m", cron_expr=_NEVER)
    assert job.schedule.cron_expr == _NEVER
    other = svc.add_job("o", "m", cron_expr="*/5 * * * *")
    updated = svc.update_job(other.id, cron_expr="0 0 30 2 *")
    assert updated is not None and updated.schedule.cron_expr == "0 0 30 2 *"


# -- MCP --------------------------------------------------------------------------


@pytest.fixture
def _mcp_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    monkeypatch.setattr("kiro_crew.mcp_cron.config_dir", lambda: tmp_path)
    monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:never-fires-slot")
    return tmp_path


@pytest.mark.usefixtures("_mcp_store")
def test_mcp_cron_add_and_update_answer_with_a_warning() -> None:
    name = f"never-{uuid.uuid4().hex[:8]}"
    added = _call_tool_inner("cron_add", {"name": name, "message": "hi", "cron_expr": _NEVER})
    assert added.startswith("Added job: ")
    assert "Warning: " in added and _NEVER in added
    (job,) = [j for j in CronService().list_jobs(include_disabled=True) if j.name == name]

    updated = _call_tool_inner("cron_update", {"job_id": job.id, "cron_expr": "0 0 30 2 *"})
    assert updated.startswith("Updated job: ")
    assert "Warning: " in updated and "0 0 30 2 *" in updated

    fine = _call_tool_inner("cron_update", {"job_id": job.id, "cron_expr": "*/5 * * * *"})
    assert "Warning" not in fine


# -- CLI --------------------------------------------------------------------------


def _add_ns(cron_expr: str) -> argparse.Namespace:
    return argparse.Namespace(
        cron_action="add",
        name="job",
        message="do the thing",
        every=None,
        cron_expr=cron_expr,
        at=None,
        timezone="",
        channel=None,
        agent="",
        script="",
        shell_command="",
        timeout=None,
        timeout_secs=None,
        model="",
        persistent_session=True,
        minimal_context=False,
        hide_in_chat=False,
        silent=False,
        folder="",
        approval_mode="",
    )


@pytest.fixture
def _cli_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (config_dir() / "crons").mkdir(parents=True)


@pytest.mark.usefixtures("_cli_home")
def test_cli_add_and_update_print_a_stderr_warning_and_save(capsys) -> None:
    with patch("kiro_crew.cli_commands.sel"):
        _cron(_add_ns(_NEVER))
    out, err = capsys.readouterr()
    assert out.startswith("Added job: ")
    assert err.startswith("Warning: ") and _NEVER in err
    (job,) = CronService(base_dir=config_dir()).list_jobs(include_disabled=True)
    assert job.schedule.cron_expr == _NEVER

    update = argparse.Namespace(
        cron_action="update",
        job_id=job.id,
        name=None,
        message=None,
        every_secs=None,
        cron_expr="0 0 30 2 *",
        channel=None,
    )
    with patch("kiro_crew.cli_commands.sel"):
        _cron(update)
    out, err = capsys.readouterr()
    assert out.startswith("Updated job: ")
    assert err.startswith("Warning: ") and "0 0 30 2 *" in err


@pytest.mark.usefixtures("_cli_home")
def test_cli_add_of_a_firing_cron_prints_no_warning(capsys) -> None:
    with patch("kiro_crew.cli_commands.sel"):
        _cron(_add_ns("*/5 * * * *"))
    _out, err = capsys.readouterr()
    assert "Warning" not in err


# -- dashboard REST ------------------------------------------------------------


@web.middleware
async def _owner(request: web.Request, handler):  # type: ignore[no-untyped-def]
    request["user"] = "local-app"
    request["app"] = ""
    return await handler(request)


def _app(crons: object) -> web.Application:
    app = web.Application(middlewares=[_owner])
    app["state"] = SimpleNamespace(
        crons=crons,
        push_refresh=MagicMock(),
        ack_notification=AsyncMock(),
        has_slot=MagicMock(return_value=False),
    )
    app.router.add_post("/api/crons", api_crons_create)
    app.router.add_route("*", "/api/crons/{job_id}", api_cron_update)
    return app


@pytest.mark.asyncio
async def test_dashboard_create_and_update_carry_a_warnings_list(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    async with TestClient(TestServer(_app(svc))) as client:
        resp = await client.post("/api/crons", json={"name": "n", "message": "m", "cron": _NEVER})
        body = json.loads(await resp.text())
        assert resp.status == 200, body
        assert body["ok"] is True
        (warning,) = body["warnings"]
        assert _NEVER in warning

        resp = await client.patch(f"/api/crons/{body['id']}", json={"cron": "0 0 30 2 *"})
        upd = json.loads(await resp.text())
        assert resp.status == 200, upd
        (warning,) = upd["warnings"]
        assert "0 0 30 2 *" in warning

        resp = await client.patch(f"/api/crons/{body['id']}", json={"cron": "*/5 * * * *"})
        fine = json.loads(await resp.text())
        assert resp.status == 200, fine
        assert "warnings" not in fine
    assert [j.schedule.cron_expr for j in svc.list_jobs()] == ["*/5 * * * *"]
