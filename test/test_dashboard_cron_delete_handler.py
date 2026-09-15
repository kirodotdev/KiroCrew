"""Tests for api_cron_delete HTTP handler (DELETE /api/crons/{id}).

The handler must pass its attribution to ``CronService.remove_job_async`` and
must not emit a second call-site audit. The service seam owns the record after
the store mutation settles.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronStoreBusy
from kiro_crew.dashboard.handlers.cron import api_cron_delete


def _make_app(state):
    app = web.Application()
    app["state"] = state
    app.router.add_delete("/api/crons/{job_id}", api_cron_delete)
    return app


def _make_state(existing_ids):
    """Fake state whose crons.remove_job_async removes only the known ids."""
    known = set(existing_ids)
    state = MagicMock()
    state.crons = MagicMock()

    async def remove_job_async(job_id, *, actor, source):
        assert actor == "dashboard"
        assert source == "api_cron_delete"
        if job_id in known:
            known.discard(job_id)
            return True
        return False

    state.crons.remove_job_async = AsyncMock(side_effect=remove_job_async)
    state.crons.get_history.return_value.delete_job_history = AsyncMock()
    state.push_refresh = MagicMock()
    return state


class TestApiCronDeleteAudit:
    @pytest.fixture(autouse=True)
    def stub_sel(self):
        # The handler must not retain a duplicate call-site audit.
        with patch("kiro_crew.dashboard.handlers.cron._sel") as sel_fn:
            recorder = MagicMock()
            sel_fn.return_value = recorder
            yield recorder

    @pytest.mark.asyncio
    async def test_delete_emits_sel_audit_event(self, stub_sel):
        state = _make_state(["job-1"])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/crons/job-1")
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
        state.crons.remove_job_async.assert_awaited_once_with(
            "job-1", actor="dashboard", source="api_cron_delete"
        )
        stub_sel.log_api_access.assert_not_called()
        # Delete behavior itself is unchanged: history purged + refresh pushed.
        state.crons.get_history().delete_job_history.assert_awaited_once_with("job-1")
        state.push_refresh.assert_called_once_with("crons")

    @pytest.mark.asyncio
    async def test_missing_job_audits_not_found(self, stub_sel):
        state = _make_state([])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/crons/ghost")
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is False
        state.crons.remove_job_async.assert_awaited_once_with(
            "ghost", actor="dashboard", source="api_cron_delete"
        )
        stub_sel.log_api_access.assert_not_called()
        state.push_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_busy_returns_409_without_audit(self, stub_sel):
        # A contended store means the delete never happened — there is no
        # mutation to audit, matching the create/update busy paths.
        state = _make_state(["job-1"])
        state.crons.remove_job_async = AsyncMock(side_effect=CronStoreBusy("busy"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/crons/job-1")
            assert resp.status == 409
        stub_sel.log_api_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_audit_lands_before_history_cleanup(self, stub_sel):
        # The job is already gone from the store when history cleanup runs, so
        # a history-store failure must not lose the audit record of the
        # completed delete.
        state = _make_state(["job-1"])
        state.crons.get_history.return_value.delete_job_history = AsyncMock(
            side_effect=RuntimeError("history store blip")
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/crons/job-1")
            # The handler does not swallow the history failure; the audit must
            # already have been written regardless of how the response ends.
            assert resp.status == 500
        state.crons.remove_job_async.assert_awaited_once_with(
            "job-1", actor="dashboard", source="api_cron_delete"
        )
        stub_sel.log_api_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_audit_failure_never_fails_a_completed_delete(self, stub_sel):
        # The first sel() of a process CONSTRUCTS the log (trust-dir creation,
        # HMAC key validation) and can raise. The job is already gone by then:
        # the delete must still report success, purge history, and refresh.
        state = _make_state(["job-1"])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/crons/job-1")
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
        state.crons.get_history().delete_job_history.assert_awaited_once_with("job-1")
        state.push_refresh.assert_called_once_with("crons")
        stub_sel.log_api_access.assert_not_called()
