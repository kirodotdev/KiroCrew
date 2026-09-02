"""Tests for the job_id / run_id URL path-parameter guard on the cron routes.

PR #5789 (issue #5765) added a length/type guard to the ``folder_id`` URL path
parameter on the cron-folder PATCH/DELETE routes, matching the body-param guard
the job routes already apply. This locks in the parity follow-up (#5808): the
sibling ``job_id`` / ``run_id`` URL path parameters on the cron job routes are
now rejected with a 400 ``invalid_<name>`` BEFORE any lock acquisition, thread
dispatch, or state lookup — closing the same asymmetric-perimeter gap.

Each test drives the handler directly with a mocked request whose
``match_info`` carries an over-long or empty id, and asserts the downstream
state seam is never touched.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.handlers.cron import (
    api_cron_ack,
    api_cron_cancel,
    api_cron_delete,
    api_cron_enable,
    api_cron_history,
    api_cron_history_detail,
    api_cron_run,
    api_cron_script_source,
    api_cron_to_chat,
    api_cron_update,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.validation import MAX_SHORT_STRING

# An id one character past the bound. These ids are server-minted, so an
# over-long value only arrives from a malformed/hostile client.
_TOO_LONG = "a" * (MAX_SHORT_STRING + 1)


def _make_state() -> MagicMock:
    """A state whose every cron seam is a spy, so a guard breach is visible as
    an unexpected call."""
    state = MagicMock(spec=DashboardState)
    state.crons = MagicMock()
    state.crons.remove_job_async = AsyncMock(return_value=True)
    state.crons.update_job_async = AsyncMock()
    state.crons.get_job_async = AsyncMock(return_value=None)
    state.crons.list_jobs = MagicMock(return_value=[])
    state.crons.enable_job_async = AsyncMock(return_value=True)
    state.crons.ack_job_async = AsyncMock(return_value=True)
    state.crons.get_history = MagicMock()
    state.crons.get_history.return_value.get_job_history = AsyncMock(return_value=([], 0))
    state.crons.get_history.return_value.get_run_detail = AsyncMock(return_value=None)
    state.crons.get_history.return_value.delete_job_history = AsyncMock()
    state.push_refresh = MagicMock()
    return state


def _request(state, *, match_info, body=None, query=None):
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = match_info
    request.query = query or {}
    if body is not None:
        request.json = AsyncMock(return_value=body)
    else:
        request.json = AsyncMock(return_value={})
    return request


# (handler, match_info builder for a bad job_id, the state attr that must NOT be
# reached once the guard fires)
_JOB_ID_HANDLERS = [
    (api_cron_delete, "remove_job_async"),
    (api_cron_update, "update_job_async"),
    (api_cron_run, "get_job_async"),
    (api_cron_cancel, "list_jobs"),
    (api_cron_to_chat, "list_jobs"),
    (api_cron_enable, "enable_job_async"),
    (api_cron_ack, "ack_job_async"),
    (api_cron_history, "get_history"),
    (api_cron_history_detail, "get_history"),
    (api_cron_script_source, "get_job_async"),
]


class TestJobIdPathGuard:
    @pytest.mark.parametrize("handler,seam", _JOB_ID_HANDLERS)
    @pytest.mark.parametrize("bad_id", [_TOO_LONG, ""])
    @pytest.mark.asyncio
    async def test_rejects_bad_job_id_before_state(self, handler, seam, bad_id):
        """An empty or over-long job_id path param is a 400 invalid_job_id,
        and the downstream state seam is never reached."""
        state = _make_state()
        # history_detail also reads run_id; supply a valid one so the run_id
        # guard cannot be what rejects this request.
        match_info = {"job_id": bad_id, "run_id": "run-1"}
        request = _request(state, match_info=match_info, body={"enabled": True})
        resp = await handler(request)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_job_id"
        getattr(state.crons, seam).assert_not_called()


class TestRunIdPathGuard:
    """api_cron_history_detail reads BOTH job_id and run_id from the path."""

    @pytest.mark.parametrize("bad_id", [_TOO_LONG, ""])
    @pytest.mark.asyncio
    async def test_rejects_bad_run_id_before_state(self, bad_id):
        state = _make_state()
        request = _request(state, match_info={"job_id": "job-1", "run_id": bad_id})
        resp = await api_cron_history_detail(request)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_run_id"
        state.crons.get_history.return_value.get_run_detail.assert_not_called()


class TestValidIdsPass:
    """A valid, bounded id flows through the guard to the normal handler path
    (the guard must not narrow the accepted set)."""

    @pytest.mark.asyncio
    async def test_valid_job_id_reaches_state(self):
        state = _make_state()
        request = _request(state, match_info={"job_id": "job-1"})
        resp = await api_cron_delete(request)
        # Guard passed: the delete seam was invoked and returned ok.
        assert resp.status == 200
        state.crons.remove_job_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_max_length_job_id_is_accepted(self):
        """Exactly MAX_SHORT_STRING is the boundary — accepted, not rejected."""
        state = _make_state()
        request = _request(state, match_info={"job_id": "a" * MAX_SHORT_STRING})
        resp = await api_cron_delete(request)
        assert resp.status == 200
        state.crons.remove_job_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_valid_run_id_reaches_history(self):
        state = _make_state()
        request = _request(state, match_info={"job_id": "job-1", "run_id": "run-1"})
        resp = await api_cron_history_detail(request)
        # get_run_detail returns None -> 404 run not found, but the guard let it
        # through to the state seam, which is the point.
        assert resp.status == 404
        state.crons.get_history.return_value.get_run_detail.assert_awaited_once_with(
            "job-1", "run-1"
        )
