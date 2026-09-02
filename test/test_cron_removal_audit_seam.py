"""Regression coverage for CronService-owned removal auditing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import kiro_crew.cron as cron_mod
from kiro_crew.cron import CronService, CronStoreBusy


class _Recorder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def log_api_access(self, **event) -> None:
        self.events.append(event)


@pytest.fixture
def recorder(monkeypatch) -> _Recorder:
    value = _Recorder()
    monkeypatch.setattr(cron_mod, "sel", SimpleNamespace(sel=lambda: value))
    return value


def test_single_remove_audits_at_service_seam(tmp_path, recorder):
    service = CronService(base_dir=tmp_path)
    job = service.add_job("cleanup", "run", every_secs=3600)

    assert service.remove_job(job.id, actor="cli", source="cli") is True

    assert recorder.events == [
        {
            "caller": "cli",
            "operation": "cron.remove",
            "outcome": "allowed",
            "source": "cli",
            "resources": f"job_id={job.id}",
        }
    ]


def test_missing_single_remove_is_audited(tmp_path, recorder):
    service = CronService(base_dir=tmp_path)

    assert service.remove_job("ghost", actor="mcp", source="mcp") is False

    assert recorder.events[0]["outcome"] == "not_found"
    assert recorder.events[0]["resources"] == "job_id=ghost reason=not_found"


@pytest.mark.asyncio
async def test_batch_remove_audits_requested_deleted_and_missing(tmp_path, recorder):
    service = CronService(base_dir=tmp_path)
    first = service.add_job("first", "run", every_secs=3600)
    second = service.add_job("second", "run", every_secs=3600)

    result = await service.remove_jobs([first.id, "ghost", second.id], actor="U123", source="slack")

    assert result == ([first.id, second.id], ["ghost"])
    event = recorder.events[0]
    assert event["caller"] == "U123"
    assert event["operation"] == "cron.batch_delete"
    assert event["outcome"] == "ok"
    assert first.id in event["resources"]
    assert second.id in event["resources"]
    assert "ghost" in event["resources"]


def test_sync_batch_remove_uses_the_same_audit_seam(tmp_path, recorder):
    service = CronService(base_dir=tmp_path)
    job = service.add_job("cleanup", "run", every_secs=3600)

    result = service.remove_jobs_sync([job.id, "ghost"], actor="mcp-session", source="mcp")

    assert result == ([job.id], ["ghost"])
    assert recorder.events[0]["caller"] == "mcp-session"
    assert recorder.events[0]["operation"] == "cron.batch_delete"


def test_store_failure_emits_no_audit(tmp_path, recorder, monkeypatch):
    service = CronService(base_dir=tmp_path)

    def busy(_job_id):
        raise CronStoreBusy("busy")

    monkeypatch.setattr(service, "_remove_job_locked", busy)
    with pytest.raises(CronStoreBusy):
        service.remove_job("job", actor="dashboard", source="api_cron_delete")

    assert recorder.events == []


def test_audit_failure_does_not_undo_saved_removal(tmp_path, monkeypatch):
    service = CronService(base_dir=tmp_path)
    job = service.add_job("cleanup", "run", every_secs=3600)

    def unavailable():
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(cron_mod, "sel", SimpleNamespace(sel=unavailable))

    assert service.remove_job(job.id, actor="cli", source="cli") is True
    assert service.get_job(job.id) is None
