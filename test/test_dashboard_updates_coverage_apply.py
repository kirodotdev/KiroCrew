"""The dashboard update apply, ``POST /api/update``: its refusals and its background worker.

``POST /api/update`` refuses while the gateway shuts down or no project directory is
configured, and refuses a tarball install, a governance-pinned remote, a dirty tree, a
revision the venv cannot run, a Python floor git cannot read and an upstream pin git cannot
resolve; a hung status check or upstream pin answers 500. A running policy apply is tracked
so the shutdown path can cancel it, and a caller that stops waiting still stops it. The
worker fast-forwards to the pinned upstream commit, reports a hung or failed merge and an
unexpected crash through the update progress events, owns the bundle gap through the build
and the reinstall and hands it to the restart, and records, keeps or clears the watchdog's
restart refusal to match the tree. The pip-install report's truncation and the simulate and
cancel endpoints are pinned here too. The check, the auto-update switch and the changelog
cache live in ``test_dashboard_updates_coverage.py``, the log and stream surfaces in
``test_dashboard_updates_coverage_streams.py``, and the shared fixture and stubs in
``dashboard_updates_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from dashboard_updates_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _FakeProc,
    _git_proj,
    _isolated_module_state,
    _request,
    _sequence_procs,
)

from kiro_crew.dashboard.handlers import updates
from kiro_crew.testing.wait import await_until


class TestPipInstallFailureReport:
    """``_venv_pip_install`` — a long message is truncated, not dropped."""


#: The commit the pre-apply pin resolves `@{u}` to in the apply tests.
_UPSTREAM_OID = b"0123456789abcdef0123456789abcdef01234567"


async def _worker_settled(state) -> None:
    """Wait for the scheduled update worker to finish, then re-raise what it raised."""
    done, pending = await asyncio.wait(list(state._background_tasks), timeout=30)
    assert not pending, "the update worker did not finish within 30 s"
    for task in done:
        task.result()


class TestApplyRefusals:
    """``POST /api/update`` — every precondition, and the worker's own failures."""

    @pytest.mark.asyncio
    async def test_refuses_once_the_gateway_is_shutting_down(self, monkeypatch):
        """An apply started now would be cut off mid-write by the shutdown."""
        from types import SimpleNamespace

        from kiro_crew.platform import update_provider

        monkeypatch.setattr(updates, "shutdown_event", SimpleNamespace(is_set=lambda: True))
        applied = AsyncMock()
        monkeypatch.setattr(update_provider, "apply_policy_update", applied)

        resp = await updates.api_update_apply(_request({}))

        assert resp.status == 503
        assert json.loads(resp.body.decode())["code"] == "shutting_down"
        applied.assert_not_awaited()

    @staticmethod
    def _policy_installer(monkeypatch) -> tuple[asyncio.Event, list[str]]:
        """A policy apply that runs until cancelled and records its cancel arm.

        Stands in for ``CommandProvider.apply``, whose cancel arm stops the
        installer child with SIGTERM.
        """
        from kiro_crew.platform import update_provider

        started = asyncio.Event()
        hold = asyncio.Event()  # never set: only a cancel ends the apply
        stopped: list[str] = []

        async def _installer_running() -> bool | None:
            started.set()
            try:
                await await_until(hold, what="the installer stand-in to be cancelled")
            except asyncio.CancelledError:
                stopped.append("installer")
                raise
            return True

        monkeypatch.setattr(update_provider, "apply_policy_update", _installer_running)
        return started, stopped

    async def _drive_worker(
        self, monkeypatch, tmp_path, procs: list[_FakeProc | OSError], *, body: object = None
    ):
        """Accept the request, then await the background worker it scheduled.

        *body* is the JSON the client sent; the default is a bare POST.
        """
        from kiro_crew import dep_sync

        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
        monkeypatch.setattr(updates, "resolve_remote_url", lambda _p, **_k: "")
        monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "")
        # The fabricated checkout has no commits for the floor gate to read, and
        # an unreadable floor now refuses; these tests are about the worker
        # that runs AFTER the gate passes.
        monkeypatch.setattr(dep_sync, "incoming_python_floor_breach", lambda *a, **k: None)
        # Clean tree, then the diverged guard's own fetch, a fast-forwardable
        # rev-list count and the upstream OID pin, so the request reaches the
        # worker whose procs follow.
        argv_seen = _sequence_procs(
            monkeypatch,
            [
                _FakeProc(out=b""),
                _FakeProc(out=b""),
                _FakeProc(out=b"0\t1\n"),
                _FakeProc(out=_UPSTREAM_OID + b"\n"),
            ]
            + procs,
        )

        req = _request({} if body is None else body)
        req.can_read_body = body is not None
        state = req.app["state"]
        resp = await updates.api_update_apply(req)
        assert resp.status == 200
        self._argv_seen = argv_seen
        # The worker is registered on the state so it cannot be garbage collected
        # mid-flight; awaiting it here is what makes the test deterministic.
        assert len(state._background_tasks) == 1
        # The name the restart gate counts as a running dashboard update.
        assert next(iter(state._background_tasks)).get_name() == "dashboard-update"
        await _worker_settled(state)
        return state

    def _progress(self, state) -> list[tuple[str, str]]:
        return [tuple(call.args) for call in state.push_update_progress.call_args_list]

    async def _apply_with_copy(self, monkeypatch, tmp_path, copy_result, worker_procs):
        """POST an apply with the shared memory copy stubbed; return (resp, state, events).

        The copy is recorded into the same list as every spawn, so the order
        between it and the fast-forward is read off one sequence.
        """
        from kiro_crew import dep_sync
        from kiro_crew.platform import wheel_apply

        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
        monkeypatch.setattr(updates, "resolve_remote_url", lambda _p, **_k: "")
        monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "")
        monkeypatch.setattr(dep_sync, "incoming_python_floor_breach", lambda *a, **k: None)
        monkeypatch.setattr(updates, "_build_frontend", AsyncMock())
        monkeypatch.setattr(updates, "_venv_pip_install", AsyncMock(return_value=False))
        events = _sequence_procs(
            monkeypatch,
            [
                _FakeProc(out=b""),
                _FakeProc(out=b""),
                _FakeProc(out=b"0\t1\n"),
                _FakeProc(out=_UPSTREAM_OID + b"\n"),
            ]
            + worker_procs,
        )

        def _copy():
            events.append(("memory-copy",))
            return copy_result

        monkeypatch.setattr(wheel_apply, "snapshot_memory_before_update", _copy)

        req = _request({})
        state = req.app["state"]
        resp = await updates.api_update_apply(req)
        await asyncio.gather(*list(state._background_tasks))
        return resp, state, events


class TestSimulateAndCancel:
    """The two overlay-driving endpoints."""

    @pytest.mark.asyncio
    async def test_a_missing_body_is_treated_as_empty_not_an_error(self, monkeypatch):
        """The simulator is a local test aid; an absent body must not 400."""
        req = _request(ValueError("no body"))
        state = req.app["state"]
        resp = await updates.api_update_simulate(req)
        assert resp.status == 200
        assert json.loads(resp.body.decode())["status"] == "simulating"
        for task in list(state._background_tasks):
            task.cancel()

    @pytest.mark.asyncio
    async def test_a_simulated_rejection_uses_the_caller_s_message(self):
        resp = await updates.api_update_simulate(
            _request({"reject": True, "reject_message": "nope"})
        )
        assert resp.status == 409
        assert json.loads(resp.body.decode())["error"] == "nope"

    @pytest.mark.asyncio
    async def test_cancel_clears_the_overlay_on_both_sides_of_the_failed_event(self):
        req = _request({})
        state = req.app["state"]
        resp = await updates.api_update_cancel(req)
        assert resp.status == 200
        # Cleared, then a "failed" event so clients drop the overlay, then cleared
        # again -- a single clear would race clients that had not yet polled.
        assert state.clear_update_progress.call_count == 2
        state.push_update_progress.assert_called_once_with("failed", "Update cancelled by user")
