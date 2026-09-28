"""Disabling an app removes its crons even when ``permissions.cron`` is absent.

Cleanup was gated on that grant while registration never required it, so a
grantless app kept firing after disable -- and revoking the grant first left its
jobs unreachable. The store here holds three owners, so a cleanup that wiped it
instead of selecting by owner fails rather than passes.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.cron import CronService

# No "cron" key at all: the exact manifest the defect let through.
_NO_GRANT_APP_INFO = {"manifest": {"permissions": {"jobs": True}}}


def _dispatcher_stub(svc: CronService) -> SimpleNamespace:
    """Cron service plus the startup-ownership probe `on_app_disable` calls."""

    async def _stop(app_name: str, *, bounded: bool = False) -> bool:
        return True

    return SimpleNamespace(_cron_service=svc, stop_detached_startup_hooks=_stop)


async def _svc_with_three_owners(tmp_path: Path) -> CronService:
    (tmp_path / "crons.json").write_text(json.dumps({"version": 2, "jobs": []}), encoding="utf-8")
    svc = CronService(base_dir=tmp_path)
    await svc.add_job_async("ours", "tick", every_secs=3600, created_by="app:demo")
    await svc.add_job_async("theirs", "tick", every_secs=3600, created_by="app:other")
    await svc.add_job_async("mine", "tick", every_secs=3600, created_by="U123456")
    return svc


def _owners(svc: CronService) -> list[str]:
    return sorted(j.created_by for j in svc.list_jobs())


@pytest.mark.asyncio
async def test_disable_without_the_cron_grant_still_removes_the_apps_crons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import hooks_integration as hi

    svc = await _svc_with_three_owners(tmp_path)
    monkeypatch.setattr(hi, "_lifecycle_dispatcher", _dispatcher_stub(svc))
    monkeypatch.setattr(hi, "sel", lambda: MagicMock())

    result = await hi.on_app_disable("demo", _NO_GRANT_APP_INFO, run_app_hooks=False)

    assert result.get("cron_cleanup") == "removed 1 job(s)", result
    assert _owners(svc) == ["U123456", "app:other"], "cleanup reached another owner's jobs"


@pytest.mark.asyncio
async def test_cleanup_helper_consults_no_manifest_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grant-independence is testable on its own: the helper takes only a name."""
    from kiro_crew.apps import hooks_integration as hi

    svc = await _svc_with_three_owners(tmp_path)
    monkeypatch.setattr(hi, "_lifecycle_dispatcher", _dispatcher_stub(svc))
    monkeypatch.setattr(hi, "sel", lambda: MagicMock())

    result: dict = {}
    await hi._cleanup_app_crons("demo", result)

    assert result == {"cron_cleanup": "removed 1 job(s)"}
    assert _owners(svc) == ["U123456", "app:other"]


@pytest.mark.asyncio
async def test_an_app_with_no_crons_reports_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NC: the unconditional call must stay silent when there is nothing to remove."""
    from kiro_crew.apps import hooks_integration as hi

    svc = await _svc_with_three_owners(tmp_path)
    monkeypatch.setattr(hi, "_lifecycle_dispatcher", _dispatcher_stub(svc))
    monkeypatch.setattr(hi, "sel", lambda: MagicMock())

    result = await hi.on_app_disable("absent", _NO_GRANT_APP_INFO, run_app_hooks=False)

    assert "cron_cleanup" not in result, result
    assert _owners(svc) == ["U123456", "app:demo", "app:other"]
