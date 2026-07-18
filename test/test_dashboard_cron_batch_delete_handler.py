"""Tests for api_cron_batch_delete HTTP handler (DELETE /api/crons).

Mirrors the single-delete contract (remove_job + delete_job_history) but over a
list of ids, with per-id failure isolation and a single refresh push.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.cron import api_cron_batch_delete


def _make_app(state):
    app = web.Application()
    app["state"] = state
    app.router.add_delete("/api/crons", api_cron_batch_delete)
    return app


def _make_state(existing_ids):
    """Fake state whose crons.remove_job returns True only for known ids."""
    known = set(existing_ids)
    state = MagicMock()
    state.crons = MagicMock()

    def remove_job(job_id):
        # Mirror CronService.remove_job: True when the job existed and was removed.
        if job_id in known:
            known.discard(job_id)
            return True
        return False

    state.crons.remove_job = MagicMock(side_effect=remove_job)
    state.crons.get_history.return_value.delete_job_history = AsyncMock()
    state.push_refresh = MagicMock()
    return state


class TestApiCronBatchDelete:
    @pytest.fixture(autouse=True)
    def stub_sel(self):
        # Stub the SEL recorder so tests don't write real audit events; expose
        # it so the audit-event test can assert on it.
        with patch("kiro_crew.dashboard.handlers.cron._sel") as sel_fn:
            recorder = MagicMock()
            sel_fn.return_value = recorder
            yield recorder

    @pytest.mark.asyncio
    async def test_deletes_all_valid_ids(self):
        state = _make_state(["a", "b", "c"])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/crons", json={"ids": ["a", "b", "c"]})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert sorted(data["deleted"]) == ["a", "b", "c"]
            assert data["failed"] == []
        assert state.crons.remove_job.call_count == 3
        assert state.crons.get_history().delete_job_history.await_count == 3
        # One refresh for the whole batch, not one per id.
        state.push_refresh.assert_called_once_with("crons")

    @pytest.mark.asyncio
    async def test_partial_failure_reports_failed_and_still_refreshes(self):
        state = _make_state(["a"])  # only "a" exists; "ghost" does not
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/crons", json={"ids": ["a", "ghost"]})
            assert resp.status == 200
            data = await resp.json()
            assert data["deleted"] == ["a"]
            assert data["failed"] == ["ghost"]
        # history only purged for the one that was actually removed
        assert state.crons.get_history().delete_job_history.await_count == 1
        state.push_refresh.assert_called_once_with("crons")

    @pytest.mark.asyncio
    async def test_all_missing_does_not_refresh(self):
        state = _make_state([])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/crons", json={"ids": ["x", "y"]})
            assert resp.status == 200
            data = await resp.json()
            assert data["deleted"] == []
            assert sorted(data["failed"]) == ["x", "y"]
            assert data["ok"] is False  # nothing deleted -> ok:false
        state.push_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_ids_are_deduplicated(self):
        state = _make_state(["a", "b"])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/crons", json={"ids": ["a", "a", "b", "a"]})
            assert resp.status == 200
            data = await resp.json()
            assert sorted(data["deleted"]) == ["a", "b"]
        # remove_job invoked once per unique id, not once per occurrence
        assert state.crons.remove_job.call_count == 2

    @pytest.mark.asyncio
    async def test_empty_ids_is_400(self):
        state = _make_state([])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/crons", json={"ids": []})
            assert resp.status == 400
        state.crons.remove_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_ids_key_is_400(self):
        state = _make_state([])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/crons", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_non_string_ids_is_400(self):
        state = _make_state([])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/crons", json={"ids": [1, 2, 3]})
            assert resp.status == 400
        state.crons.remove_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_over_limit_is_400(self):
        state = _make_state([])
        too_many = [f"id{i}" for i in range(501)]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/crons", json={"ids": too_many})
            assert resp.status == 400
        state.crons.remove_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self):
        state = _make_state([])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete(
                "/api/crons", data="not-json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_history_cleanup_failure_still_counts_as_deleted(self):
        # If remove_job succeeds but delete_job_history raises, the job is
        # already gone -> it MUST be reported as deleted (not failed), the batch
        # must continue, and the refresh must still fire.
        state = _make_state(["a", "b"])

        def flaky_history(job_id):
            if job_id == "a":
                raise RuntimeError("history store blip")
            return None

        state.crons.get_history.return_value.delete_job_history.side_effect = flaky_history
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/crons", json={"ids": ["a", "b"]})
            assert resp.status == 200
            data = await resp.json()
            assert sorted(data["deleted"]) == ["a", "b"]
            assert data["failed"] == []
        assert state.crons.remove_job.call_count == 2
        state.push_refresh.assert_called_once_with("crons")

    @pytest.mark.asyncio
    async def test_emits_sel_audit_event(self, stub_sel):
        state = _make_state(["a", "b"])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/crons", json={"ids": ["a", "b", "ghost"]})
            assert resp.status == 200
        stub_sel.log_api_access.assert_called_once()
        kw = stub_sel.log_api_access.call_args.kwargs
        assert kw["operation"] == "cron.batch_delete"
        assert kw["source"] == "api_cron_batch_delete"
        # requested ids + outcome captured for auditability
        assert "a" in kw["resources"] and "ghost" in kw["resources"]

    @pytest.mark.asyncio
    async def test_real_cronservice_delete_succeeds_and_scheduler_survives(self, tmp_path):
        """Regression: batch-delete must run remove_job on the event loop.

        The mock-based tests above never caught that the handler offloaded
        remove_job to a worker thread: remove_job -> _arm_timer ->
        asyncio.create_task() raises "RuntimeError: no running event loop" off
        the loop, AFTER the on-disk delete. That reported a completed delete as
        `failed` (retry-that-never-succeeds) and left the scheduler's timer
        cancelled — killing all future cron runs. This uses a REAL running
        CronService so a regression re-raises on the loop-vs-thread boundary.
        """
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        await svc.start()
        try:
            job = svc.add_job("t", "echo hi", every_secs=3600)
            state = MagicMock()
            state.crons = svc
            state.push_refresh = MagicMock()
            with patch("kiro_crew.dashboard.handlers.cron._sel") as sel_fn:
                sel_fn.return_value = MagicMock()
                async with TestClient(TestServer(_make_app(state))) as client:
                    resp = await client.delete("/api/crons", json={"ids": [job.id]})
                    assert resp.status == 200
                    data = await resp.json()
                    # The delete actually succeeded, so it must be reported as such.
                    assert data["deleted"] == [job.id]
                    assert data["failed"] == []
                    assert data["ok"] is True
            # Job is gone AND the scheduler is still alive (timer armed, running).
            assert svc.get_job(job.id) is None
            assert svc._running is True
            assert svc._timer_task is not None and not svc._timer_task.done()
        finally:
            await svc.stop()
