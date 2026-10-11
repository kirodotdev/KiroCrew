"""A caller-requested cron removal deletes the job's run history on every surface.

The dashboard's delete route is one of several ways to remove a job: the MCP
``cron_remove`` tool and the CLI call :meth:`CronService.remove_job`, the MCP
``cron_remove_all`` tool calls :meth:`CronService.remove_jobs_sync`, and the
messaging commands and the apps SDK call :meth:`CronService.remove_job_async`.
Each of them deletes ``cron-history/<job id>.jsonl`` and the job's index rows,
so a removed job leaves no history file behind. An automated one-shot removal
(``one_shot_path`` set) keeps the history: the run path appends that run's
record after the removal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.cron as cron_mod
import kiro_crew.mcp_cron as mcp_cron
from kiro_crew.cron import CronService
from kiro_crew.cron_history import CronRunRecord
from kiro_crew.dashboard.handlers.cron import api_cron_delete


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_mod.sel, "sel", lambda: MagicMock())
    svc = CronService(base_dir=tmp_path)
    assert svc.get_history().enabled
    return svc


def _history_file(service: CronService, tmp_path: Path, job_id: str) -> Path:
    service.get_history()._append_sync(
        CronRunRecord(job_id=job_id, started_at=1.0, finished_at=2.0, summary="ran")
    )
    path = tmp_path / "cron-history" / f"{job_id}.jsonl"
    assert path.exists()
    return path


def _index_job_ids(tmp_path: Path) -> set[str]:
    index = tmp_path / "cron-history" / "_index.jsonl"
    text = index.read_text(encoding="utf-8") if index.exists() else ""
    return {line.split('"job_id": "')[1].split('"')[0] for line in text.splitlines() if line}


def test_remove_job_deletes_the_history(service, tmp_path):
    job = service.add_job("direct", "run", every_secs=3600)
    path = _history_file(service, tmp_path, job.id)

    assert service.remove_job(job.id, actor="cli", source="cli")

    assert not path.exists()
    assert job.id not in _index_job_ids(tmp_path)


def test_remove_job_async_deletes_the_history(service, tmp_path):
    job = service.add_job("chat", "run", every_secs=3600)
    path = _history_file(service, tmp_path, job.id)

    assert asyncio.run(service.remove_job_async(job.id, actor="slack", source="slack"))

    assert not path.exists()
    assert job.id not in _index_job_ids(tmp_path)


def test_cron_remove_tool_deletes_the_history(service, tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_cron, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_cron, "_authz_session_key", lambda: "sess-a")
    job = service.add_job("mcp", "run", every_secs=3600, session_key="sess-a")
    path = _history_file(service, tmp_path, job.id)

    assert mcp_cron._call_tool_inner("cron_remove", {"job_id": job.id}) == f"Removed job: {job.id}"

    assert not path.exists()


def test_a_failed_history_delete_does_not_fail_the_removal(service, tmp_path, monkeypatch):
    job = service.add_job("flaky", "run", every_secs=3600)
    _history_file(service, tmp_path, job.id)

    def _boom(_job_id: str) -> bool:
        raise RuntimeError("history down")

    monkeypatch.setattr(service.get_history(), "delete_job_history_sync", _boom)

    assert service.remove_job(job.id, actor="cli", source="cli")
    assert service.get_job(job.id) is None


def test_an_automated_one_shot_removal_keeps_the_history(service, tmp_path):
    job = service.add_job("once", "run", at_ts=1.0)
    path = _history_file(service, tmp_path, job.id)

    removed = asyncio.run(
        service.remove_job_async(job.id, actor="cron", source="cron", one_shot_path="cron_gateway")
    )

    assert removed
    assert path.exists(), "the run path still appends this run's record after the removal"


def test_remove_jobs_deletes_the_history(service, tmp_path):
    first = service.add_job("a", "run", every_secs=3600)
    second = service.add_job("b", "run", every_secs=3600)
    paths = [_history_file(service, tmp_path, job.id) for job in (first, second)]

    removed, missing = asyncio.run(
        service.remove_jobs([first.id, second.id, "ghost"], actor="chat", source="chat")
    )

    assert sorted(removed) == sorted([first.id, second.id])
    assert missing == ["ghost"]
    assert not any(path.exists() for path in paths)
    assert not {first.id, second.id} & _index_job_ids(tmp_path)


def test_cron_remove_all_tool_deletes_the_history(service, tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_cron, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_cron, "_authz_session_key", lambda: "sess-a")
    monkeypatch.setattr(mcp_cron, "sel", lambda: MagicMock())
    mine = [
        service.add_job(name, "run", every_secs=3600, session_key="sess-a") for name in ("a", "b")
    ]
    other = service.add_job("other", "run", every_secs=3600, session_key="sess-b")
    paths = [_history_file(service, tmp_path, job.id) for job in mine]
    other_path = _history_file(service, tmp_path, other.id)

    assert mcp_cron._call_tool_inner("cron_remove_all", {}) == "Removed 2 job(s)."

    left = [path.name for path in paths if path.exists()]
    assert not left, f"cron_remove_all removed this session's jobs and left their history: {left}"
    assert not {job.id for job in mine} & _index_job_ids(tmp_path)
    assert other_path.exists(), "another session's job keeps its history"


def test_control_the_dashboard_route_deletes_the_history_once(service, tmp_path, monkeypatch):
    job = service.add_job("dash", "run", every_secs=3600)
    path = _history_file(service, tmp_path, job.id)
    history = service.get_history()
    real_delete = history._delete_job_history_sync
    deletes: list[str] = []

    def _counting_delete(job_id: str) -> bool:
        deletes.append(job_id)
        return real_delete(job_id)

    monkeypatch.setattr(history, "_delete_job_history_sync", _counting_delete)
    state = MagicMock()
    state.crons = service
    app = web.Application()
    app["state"] = state
    app.router.add_delete("/api/crons/{job_id}", api_cron_delete)

    async def _delete() -> int:
        async with TestClient(TestServer(app)) as client:
            resp = await client.delete(f"/api/crons/{job.id}")
            return resp.status

    assert asyncio.run(_delete()) == 200
    assert not path.exists()
    assert deletes == [job.id], "the dashboard route deletes the history exactly once"
