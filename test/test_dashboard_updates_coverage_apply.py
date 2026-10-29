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
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

    @pytest.mark.asyncio
    async def test_a_huge_stderr_is_truncated_with_a_marker(self, monkeypatch):
        from kiro_crew import dep_sync

        state = MagicMock()

        def fake_sync(repo, target_py, emit=None, timeout=None, **_kw):
            # dep_sync passes pip's captured output through verbatim; the cap is
            # this endpoint's job because it is the one publishing it.
            emit("x" * 4000, True)
            return 1

        monkeypatch.setattr(dep_sync, "sync_or_reinstall", fake_sync)

        assert await updates._venv_pip_install("/tmp/proj", state) is False
        # The executor thread hands the message to the loop with
        # call_soon_threadsafe, so it lands on the NEXT loop iteration -- reading
        # call_args before yielding once raced it and read the "Installing" step.
        # flake-ok: pre-existing one-iteration yield for the executor's call_soon_threadsafe callback, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)

        step, detail = state.push_update_progress.call_args[0]
        assert step == "error"
        # Truncated rather than dropped: the actionable line is often mid-stderr.
        assert detail.endswith("…(truncated)")
        assert len(detail) < 1200


#: The commit the pre-apply pin resolves `@{u}` to in the apply tests.
_UPSTREAM_OID = b"0123456789abcdef0123456789abcdef01234567"


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
                # flake-ok: pre-existing installer stand-in that only the test's cancel ends, moved verbatim; tracked for a de-flake pass
                await hold.wait()
            except asyncio.CancelledError:
                stopped.append("installer")
                raise
            return True

        monkeypatch.setattr(update_provider, "apply_policy_update", _installer_running)
        return started, stopped

    @pytest.mark.asyncio
    async def test_a_running_policy_apply_is_tracked_for_the_shutdown_path(self, monkeypatch):
        """The apply runs as a task the gateway's shutdown can cancel and await.

        Inline in the handler, it is cancelled only by aiohttp's own teardown,
        which the gateway's graceful cap exits ahead of, so the installer would
        keep running unsupervised.
        """
        started, stopped = self._policy_installer(monkeypatch)
        req = _request({})
        handler = asyncio.create_task(updates.api_update_apply(req))
        await await_until(started, what="the apply to start")

        tracked = updates.policy_applies_in_flight()
        assert len(tracked) == 1
        tracked[0].cancel()  # what the gateway's shutdown does
        resp = await await_until(handler, what="the handler to answer")

        assert stopped == ["installer"]
        assert resp.status == 503
        assert json.loads(resp.body.decode())["code"] == "shutting_down"
        assert updates.policy_applies_in_flight() == []
        step, _detail = req.app["state"].push_update_progress.call_args[0]
        assert step == "failed"

    @pytest.mark.asyncio
    async def test_a_caller_that_stops_waiting_still_stops_the_apply(self, monkeypatch):
        """The tracking changes who else can cancel the apply, not the caller's arm."""
        started, stopped = self._policy_installer(monkeypatch)
        handler = asyncio.create_task(updates.api_update_apply(_request({})))
        await await_until(started, what="the apply to start")

        handler.cancel()
        with pytest.raises(asyncio.CancelledError):
            await await_until(handler, what="the handler to unwind")

        assert stopped == ["installer"]
        assert updates.policy_applies_in_flight() == []

    @pytest.mark.asyncio
    async def test_refuses_when_no_project_dir_is_configured(self):
        resp = await updates.api_update_apply(_request({}))
        assert resp.status == 400
        assert "KIROCREW_PROJECT_DIR" in json.loads(resp.body.decode())["error"]

    @pytest.mark.asyncio
    async def test_refuses_a_tarball_install_with_a_specific_message(self, monkeypatch, tmp_path):
        """No ``.git`` means ``git pull`` cannot update it — say so, not "pull failed"."""
        plain = tmp_path / "tarball"
        plain.mkdir()
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(plain))
        resp = await updates.api_update_apply(_request({}))
        assert resp.status == 409
        error = json.loads(resp.body.decode())["error"]
        assert "Not a git checkout" in error
        assert "kirocrew update" in error
        assert "cloud launch" not in error

    @pytest.mark.asyncio
    async def test_a_pinned_remote_is_refused_before_any_spinner_is_shown(
        self, monkeypatch, tmp_path
    ):
        """Governance is checked before ``push_refresh``.

        A dashboard token proves the caller is the operator, not that the fleet
        permits this host to pull from this remote -- and a blocked update must
        leave no "updating" overlay behind for the user to dismiss.
        """
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
        monkeypatch.setattr(
            updates, "resolve_remote_url", lambda _p, **_k: "https://example.invalid/x"
        )
        monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "remote is not permitted")

        req = _request({})
        resp = await updates.api_update_apply(req)

        assert resp.status == 403
        payload = json.loads(resp.body.decode())
        assert payload["governance"] is True
        assert payload["error"] == "remote is not permitted"
        req.app["state"].push_refresh.assert_not_called()
        assert req.app["state"]._background_tasks == set()

    @pytest.mark.asyncio
    async def test_a_hung_status_check_answers_500_rather_than_hanging(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
        monkeypatch.setattr(updates, "resolve_remote_url", lambda _p, **_k: "")
        monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "")
        _sequence_procs(monkeypatch, [_FakeProc(time_out=True, kill_raises=True)])

        req = _request({})
        resp = await updates.api_update_apply(req)

        assert resp.status == 500
        assert "Timed out" in json.loads(resp.body.decode())["error"]
        assert req.app["state"]._background_tasks == set()

    @pytest.mark.asyncio
    async def test_a_dirty_tree_is_refused_without_starting_the_worker(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
        monkeypatch.setattr(updates, "resolve_remote_url", lambda _p, **_k: "")
        monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "")
        _sequence_procs(monkeypatch, [_FakeProc(out=b" M src/kiro_crew/cli.py\n")])

        req = _request({})
        resp = await updates.api_update_apply(req)

        assert resp.status == 409
        assert "uncommitted changes" in json.loads(resp.body.decode())["error"]
        assert req.app["state"]._background_tasks == set()

    @pytest.mark.asyncio
    async def test_a_dirty_tree_refusal_names_its_reason_and_counts_the_changes(
        self, monkeypatch, tmp_path
    ):
        """The 409 carries a stable code and a count, so the UI can guide the user.

        The count is of changed entries only: no paths leave the gateway.
        """
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
        monkeypatch.setattr(updates, "resolve_remote_url", lambda _p, **_k: "")
        monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "")
        _sequence_procs(
            monkeypatch,
            [_FakeProc(out=b" M src/kiro_crew/cli.py\n?? notes.txt\n")],
        )

        req = _request({})
        resp = await updates.api_update_apply(req)

        assert resp.status == 409
        payload = json.loads(resp.body.decode())
        assert payload["code"] == "dirty_worktree"
        assert payload["changed"] == 2
        # Older clients read only ``error``; it keeps its wording.
        assert payload["error"] == "Working tree has uncommitted changes — commit or stash first"
        assert "cli.py" not in json.dumps(payload)
        assert req.app["state"]._background_tasks == set()

    @pytest.mark.asyncio
    async def test_a_revision_the_venv_cannot_run_is_refused_before_the_pull(
        self, monkeypatch, tmp_path
    ):
        """The floor is judged on the FETCHED commit, and the tree is left alone.

        pip would refuse the same revision during the reinstall, but only after
        the pull had moved the checkout to code this interpreter cannot import
        -- a state the running gateway then serves from memory while every
        later attempt repeats the pull and the refusal.
        """
        from kiro_crew import dep_sync

        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
        monkeypatch.setattr(updates, "resolve_remote_url", lambda _p, **_k: "")
        monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "")
        _sequence_procs(
            monkeypatch,
            [
                _FakeProc(out=b""),
                _FakeProc(out=b""),
                _FakeProc(out=b"0\t1\n"),
                _FakeProc(out=_UPSTREAM_OID + b"\n"),
            ],
        )
        seen: dict[str, object] = {}

        def _breach(repo, ref, target_py, **_kw):
            seen.update(repo=repo, ref=ref, target_py=target_py)
            return (
                "the incoming revision requires Python >=3.12 but this install's venv runs 3.11.9"
            )

        monkeypatch.setattr(dep_sync, "incoming_python_floor_breach", _breach)

        req = _request({})
        resp = await updates.api_update_apply(req)
        assert resp.status == 409
        body = json.loads(resp.body.decode())
        assert body["code"] == "python_floor"
        assert ">=3.12" in body["error"] and "3.11.9" in body["error"]
        # Judged against the PINNED upstream commit (the exact revision the
        # fast-forward would apply), with the interpreter this gateway runs.
        assert seen["ref"] == _UPSTREAM_OID.decode()
        assert seen["target_py"] == Path(sys.executable)
        # No worker, so no pull: the checkout stays where the venv can import it.
        assert req.app["state"]._background_tasks == set()
        assert not any(step == "pulling" for step, _ in self._progress(req.app["state"]))

    @pytest.mark.asyncio
    async def test_a_floor_git_cannot_read_refuses_rather_than_passing(self, monkeypatch, tmp_path):
        """ "Could not read the floor" is not "no floor": the pinned revision stays out."""
        from kiro_crew import dep_sync

        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
        monkeypatch.setattr(updates, "resolve_remote_url", lambda _p, **_k: "")
        monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "")
        _sequence_procs(
            monkeypatch,
            [
                _FakeProc(out=b""),
                _FakeProc(out=b""),
                _FakeProc(out=b"0\t1\n"),
                _FakeProc(out=_UPSTREAM_OID + b"\n"),
            ],
        )

        def _unreadable(repo, ref, target_py, **_kw):
            raise dep_sync.IncomingFloorUnreadable("git show timed out")

        monkeypatch.setattr(dep_sync, "incoming_python_floor_breach", _unreadable)

        req = _request({})
        resp = await updates.api_update_apply(req)
        assert resp.status == 409
        assert json.loads(resp.body.decode())["code"] == "git_read_failed"
        assert req.app["state"]._background_tasks == set()

    @pytest.mark.asyncio
    async def test_an_unresolvable_upstream_pin_is_refused(self, monkeypatch, tmp_path):
        """rev-parse @{u} failing means nothing to floor-check and nothing to land on."""
        from kiro_crew import dep_sync

        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
        monkeypatch.setattr(updates, "resolve_remote_url", lambda _p, **_k: "")
        monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "")
        _sequence_procs(
            monkeypatch,
            [
                _FakeProc(out=b""),
                _FakeProc(out=b""),
                _FakeProc(out=b"0\t1\n"),
                _FakeProc(out=b"", err=b"fatal: no upstream configured", returncode=128),
            ],
        )
        judged: list[str] = []
        monkeypatch.setattr(
            dep_sync, "incoming_python_floor_breach", lambda *a, **k: judged.append(a[1])
        )

        req = _request({})
        resp = await updates.api_update_apply(req)
        assert resp.status == 409
        assert json.loads(resp.body.decode())["code"] == "git_read_failed"
        # Nothing was judged and nothing was scheduled: no pin, no apply.
        assert judged == []
        assert req.app["state"]._background_tasks == set()

    @pytest.mark.asyncio
    async def test_a_hung_upstream_pin_is_a_500_and_reaps_the_process(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
        monkeypatch.setattr(updates, "resolve_remote_url", lambda _p, **_k: "")
        monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "")
        pin = _FakeProc(time_out=True)
        _sequence_procs(
            monkeypatch,
            [_FakeProc(out=b""), _FakeProc(out=b""), _FakeProc(out=b"0\t1\n"), pin],
        )

        req = _request({})
        resp = await updates.api_update_apply(req)
        assert resp.status == 500
        assert json.loads(resp.body.decode())["code"] == "git_read_failed"
        assert pin.killed and pin.communicate_calls == 2
        assert req.app["state"]._background_tasks == set()

    async def _drive_worker(self, monkeypatch, tmp_path, procs: list[_FakeProc | OSError]):
        """Accept the request, then await the background worker it scheduled."""
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

        req = _request({})
        state = req.app["state"]
        resp = await updates.api_update_apply(req)
        assert resp.status == 200
        self._argv_seen = argv_seen
        # The worker is registered on the state so it cannot be garbage collected
        # mid-flight; awaiting it here is what makes the test deterministic.
        assert len(state._background_tasks) == 1
        # flake-ok: pre-existing unbounded gather of the scheduled update worker, moved verbatim; tracked for a de-flake pass
        await asyncio.gather(*list(state._background_tasks))
        return state

    def _progress(self, state) -> list[tuple[str, str]]:
        return [tuple(call.args) for call in state.push_update_progress.call_args_list]

    @pytest.mark.asyncio
    async def test_a_hung_pull_is_reported_and_stops_the_worker(self, monkeypatch, tmp_path):
        state = await self._drive_worker(
            monkeypatch, tmp_path, [_FakeProc(time_out=True, kill_raises=True)]
        )
        oid = _UPSTREAM_OID.decode()[:12]
        assert ("error", f"Fast-forward to {oid} timed out (git merge --ff-only)") in (
            self._progress(state)
        )
        # No build and no restart followed the failure.
        assert not any(step == "restarting" for step, _ in self._progress(state))

    @pytest.mark.asyncio
    async def test_a_failed_pull_is_reported_and_stops_the_worker(self, monkeypatch, tmp_path):
        state = await self._drive_worker(monkeypatch, tmp_path, [_FakeProc(returncode=1)])
        steps = self._progress(state)
        assert any(
            step == "error"
            and detail.startswith(f"Fast-forward to {_UPSTREAM_OID.decode()[:12]} failed")
            for step, detail in steps
        )
        # The detail is what the failure card's agent hand-off sends along, so it
        # must name what actually ran (a fast-forward merge), never a pull.
        assert not any(step == "error" and "git pull" in detail for step, detail in steps)

    @pytest.mark.asyncio
    async def test_the_worker_fast_forwards_to_the_pinned_oid_without_refetching(
        self, monkeypatch, tmp_path
    ):
        """The commit that was floor-checked is the commit that lands.

        `git pull` would refetch and re-resolve the upstream, so a remote that
        moved after the check could land a revision nobody judged.
        """
        await self._drive_worker(monkeypatch, tmp_path, [_FakeProc(returncode=1)])
        worker_argv = self._argv_seen[4]
        assert worker_argv[:4] == ("git", "merge", "--ff-only", _UPSTREAM_OID.decode())
        assert not any(c[:2] == ("git", "pull") for c in self._argv_seen)

    @pytest.mark.asyncio
    async def test_the_worker_owns_the_bundle_gap(self, monkeypatch, tmp_path):
        """The build can empty the served bundle, so the watchdog must see an owner."""
        from kiro_crew import update_ownership

        seen = []

        async def _build(_proj, _state):
            seen.append(update_ownership.current_owner())

        async def _pip(_proj, _state):
            seen.append(update_ownership.current_owner())
            return False

        monkeypatch.setattr(updates, "_build_frontend", _build)
        monkeypatch.setattr(updates, "_venv_pip_install", _pip)

        await self._drive_worker(monkeypatch, tmp_path, [_FakeProc()])

        assert seen == ["the dashboard update", "the dashboard update"]
        assert update_ownership.current_owner() is None

    @pytest.mark.asyncio
    async def test_the_worker_hands_the_gap_to_the_restart(self, monkeypatch, tmp_path):
        """No update step is still open around the restart, so its own maximum binds."""
        from kiro_crew import update_ownership

        open_at_restart = []

        async def _restart(_state, *, resolver=None):
            open_at_restart.append(list(update_ownership._live))
            return True

        monkeypatch.setattr(updates, "_build_frontend", AsyncMock())
        monkeypatch.setattr(updates, "_venv_pip_install", AsyncMock(return_value=True))
        monkeypatch.setattr(updates, "_restart_gateway", _restart)

        await self._drive_worker(monkeypatch, tmp_path, [_FakeProc()])

        assert open_at_restart == [[]]

    @pytest.mark.asyncio
    async def test_a_failed_reinstall_refuses_the_watchdogs_relaunch(self, monkeypatch, tmp_path):
        from kiro_crew import update_ownership

        monkeypatch.setattr(updates, "_build_frontend", AsyncMock())
        monkeypatch.setattr(updates, "_venv_pip_install", AsyncMock(return_value=False))

        await self._drive_worker(monkeypatch, tmp_path, [_FakeProc()])

        assert update_ownership.restart_refusal() is not None

    @pytest.mark.asyncio
    async def test_a_later_update_whose_merge_fails_keeps_the_refusal(self, monkeypatch, tmp_path):
        """The unsynced tree the refusal describes is still on disk."""
        from kiro_crew import update_ownership

        update_ownership.refuse_restart("an earlier install did not complete")

        await self._drive_worker(monkeypatch, tmp_path, [_FakeProc(returncode=1)])

        assert update_ownership.restart_refusal() == "an earlier install did not complete"

    @pytest.mark.asyncio
    async def test_a_later_update_that_installs_its_tree_ends_the_refusal(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew import update_ownership

        update_ownership.refuse_restart("an earlier install did not complete")
        monkeypatch.setattr(updates, "_build_frontend", AsyncMock())
        monkeypatch.setattr(updates, "_venv_pip_install", AsyncMock(return_value=True))
        monkeypatch.setattr(updates, "_restart_gateway", AsyncMock(return_value=False))

        await self._drive_worker(monkeypatch, tmp_path, [_FakeProc()])

        assert update_ownership.restart_refusal() is None

    @pytest.mark.asyncio
    async def test_a_merge_that_never_spawned_records_no_refusal(self, monkeypatch, tmp_path):
        """A fork failure wrote nothing to the checkout, so nothing describes a moved tree."""
        from kiro_crew import update_ownership

        await self._drive_worker(
            monkeypatch, tmp_path, [BlockingIOError(11, "Resource temporarily unavailable")]
        )

        assert self._argv_seen[-1][:3] == ("git", "merge", "--ff-only")
        assert update_ownership.restart_refusal() is None

    @pytest.mark.asyncio
    async def test_an_update_that_raises_after_the_merge_refuses_the_relaunch(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew import update_ownership

        monkeypatch.setattr(
            updates, "_build_frontend", AsyncMock(side_effect=RuntimeError("vite crashed"))
        )

        await self._drive_worker(monkeypatch, tmp_path, [_FakeProc()])

        assert "after the tree moved" in (update_ownership.restart_refusal() or "")

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

    @pytest.mark.asyncio
    async def test_memory_is_copied_before_the_fast_forward(self, monkeypatch, tmp_path):
        """The same copy `kirocrew update` takes lands before the tree is rewritten."""
        resp, _state, events = await self._apply_with_copy(
            monkeypatch, tmp_path, (1, ""), [_FakeProc()]
        )

        assert resp.status == 200
        merges = [i for i, e in enumerate(events) if e[:3] == ("git", "merge", "--ff-only")]
        assert events.count(("memory-copy",)) == 1 and len(merges) == 1
        assert events.index(("memory-copy",)) < merges[0]

    @pytest.mark.asyncio
    async def test_a_failed_memory_copy_refuses_the_apply(self, monkeypatch, tmp_path):
        """No fresh copy, no rewrite: a clear error and no worker scheduled."""
        resp, state, events = await self._apply_with_copy(
            monkeypatch, tmp_path, (0, "1 store(s) not copied"), []
        )

        assert resp.status == 500
        body = json.loads(resp.body.decode())
        assert body["code"] == "memory_snapshot_failed"
        assert "1 store(s) not copied" in body["error"] and "Not updating" in body["error"]
        assert ("memory-copy",) in events
        assert not any(e[:2] == ("git", "merge") for e in events)
        assert state._background_tasks == set()
        assert ("failed", body["error"]) in self._progress(state)

    @pytest.mark.asyncio
    async def test_a_policy_apply_copies_memory_first_and_refuses_without_it(self, monkeypatch):
        """A provider that can apply rewrites the install too, so it gets the same gate."""
        from types import SimpleNamespace

        from kiro_crew.platform import update_provider, wheel_apply

        events: list[str] = []

        async def _apply():
            events.append("policy-apply")
            return False

        monkeypatch.setattr(
            update_provider, "resolve_provider", lambda: SimpleNamespace(can_apply=lambda: True)
        )
        monkeypatch.setattr(update_provider, "apply_policy_update", _apply)
        results = [(1, ""), (0, "disk full")]

        def _copy():
            events.append("memory-copy")
            return results.pop(0)

        monkeypatch.setattr(wheel_apply, "snapshot_memory_before_update", _copy)

        first = await updates.api_update_apply(_request({}))
        assert first.status == 500  # the stubbed provider answered "failed"
        assert events == ["memory-copy", "policy-apply"]

        refused = await updates.api_update_apply(_request({}))
        assert refused.status == 500
        assert json.loads(refused.body.decode())["code"] == "memory_snapshot_failed"
        assert events == ["memory-copy", "policy-apply", "memory-copy"]

    @pytest.mark.asyncio
    async def test_a_policy_apply_refuses_shutdown_during_memory_copy(self, monkeypatch):
        """Shutdown during the copy must refuse before the policy installer starts."""
        from types import SimpleNamespace

        from kiro_crew.platform import update_provider, wheel_apply

        shutdown = SimpleNamespace(stopping=False)
        shutdown.is_set = lambda: shutdown.stopping
        monkeypatch.setattr(updates, "shutdown_event", shutdown)
        monkeypatch.setattr(
            update_provider, "resolve_provider", lambda: SimpleNamespace(can_apply=lambda: True)
        )
        applied = AsyncMock(return_value=False)
        monkeypatch.setattr(update_provider, "apply_policy_update", applied)

        def _copy():
            shutdown.stopping = True
            return (1, "")

        monkeypatch.setattr(wheel_apply, "snapshot_memory_before_update", _copy)

        resp = await updates.api_update_apply(_request({}))

        assert resp.status == 503
        assert json.loads(resp.body.decode()) == {
            "error": "The gateway is shutting down",
            "code": "shutting_down",
        }
        applied.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unexpected_crash_surfaces_as_a_failed_update(self, monkeypatch, tmp_path):
        """An exception inside the worker must reach the UI, not just the log.

        The overlay has no other way to leave the "updating" state, so a
        swallowed error strands the user on a spinner forever.
        """
        from kiro_crew import dep_sync

        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
        monkeypatch.setattr(updates, "resolve_remote_url", lambda _p, **_k: "")
        monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "")
        monkeypatch.setattr(dep_sync, "incoming_python_floor_breach", lambda *a, **k: None)
        calls = {"n": 0}

        async def _exec(*args: str, **_kwargs: object):
            calls["n"] += 1
            if calls["n"] == 1:  # git status --porcelain: clean
                return _FakeProc(out=b"")
            if calls["n"] == 2:  # the diverged guard's own fetch
                return _FakeProc(out=b"")
            if calls["n"] == 3:  # the guard's rev-list: fast-forwardable
                return _FakeProc(out=b"0\t1\n")
            if calls["n"] == 4:  # the upstream OID pin
                return _FakeProc(out=_UPSTREAM_OID + b"\n")
            # The WORKER's first spawn (git pull) crashes: the point under test
            # is that an exception inside the worker reaches the UI.
            raise RuntimeError("fork failed")

        monkeypatch.setattr("asyncio.create_subprocess_exec", _exec)

        req = _request({})
        state = req.app["state"]
        await updates.api_update_apply(req)
        # flake-ok: pre-existing unbounded gather of the scheduled update worker, moved verbatim; tracked for a de-flake pass
        await asyncio.gather(*list(state._background_tasks))

        assert ("failed", "Update failed — check logs") in self._progress(state)
        state.push_refresh.assert_any_call("update_failed")


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
