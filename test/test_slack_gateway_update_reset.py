"""The unattended reset path of ``_auto_apply_update`` and the refusals guarding it.

Covers the public source-checkout update from the fetch on: discarding local edits,
the refusals for uncommitted, hidden or colliding paths, the frontend build, the
dependency install and repair, the restart or the relaunch refusal that follows,
and the preconditions checked before the reset (a repo-declared git driver, local
commits, a branch that does not track the remote).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import (
    _git_exec_fake,
    _ImportlibWithoutReload,
    _make_orchestrator,
    _mock_dashboard_state,
    _mock_sessions,
    _until_set_while_progressing,
    _within_lost_run,
)

from kiro_crew.slack import gateway as gw

# ═══════════════════════════════════════════════════════════════════════════
# Tests: _auto_apply_update discards local edits before staging frontend
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoApplyUpdateResetPath:
    """Public auto-update reset path: discards local edits, builds frontend, pips."""

    @pytest.fixture(autouse=True)
    def _permit_update_preconditions(self):
        """Neutralize the two seam preconditions so these tests keep their subject.

        `_auto_apply_update` refuses outright when the checkout declares a
        repo-named git driver, when the branch does not track the remote it resets
        to, or when a tracked edit is hidden by assume-unchanged. All read the REAL
        git metadata of ``KIROCREW_PROJECT_DIR``, which these tests point at a path
        that is not a repo — so without this they would all pass vacuously by
        refusing before reaching the fetch/reset sequence they exist to cover. The
        refusals have their own tests in ``TestAutoApplyUpdatePreconditions`` and
        ``TestAutoApplyUpdateResetPath``.

        The git binary is pinned too: ``_auto_apply_update`` resolves it through
        ``platform_compat.trusted_git_bin`` (fixed trusted directories, never
        PATH) and skips the whole update when that answers ``None``. Every spawn
        below is faked, so the value only has to be an argv[0]; without the pin,
        a host whose git lives outside those directories (a per-user Git for
        Windows install) made every test here pass or fail on the refusal branch
        instead of the sequence it covers. A test about the resolver itself
        patches it again explicitly, and that inner patch wins.
        """
        with (
            patch("kiro_crew.slack.gateway.hidden_worktree_edits", return_value=[]),
            patch(
                "kiro_crew.slack.gateway.repo_exec_config_reason",
                return_value="",
            ),
            patch("kiro_crew.slack.gateway.tracks_upstream", return_value=True),
            patch("kiro_crew.slack.gateway.commits_ahead", return_value=0),
            patch(
                "kiro_crew.slack.gateway.platform_compat.trusted_git_bin",
                return_value="/trusted/bin/git",
            ),
            # A completed update reloads the kiro_crew package; see
            # _ImportlibWithoutReload for what that left the next test.
            patch.object(gw, "importlib", _ImportlibWithoutReload()),
            patch(
                # The interpreter-floor gate reads the pinned commit with a real
                # `git show`; against a non-repo that read FAILS, and a failed read
                # refuses (its own tests are in TestAutoApplyUpdateResetPath).
                "kiro_crew.slack.gateway.dep_sync.incoming_python_floor_breach",
                return_value=None,
            ),
            # The pre-restart drain, shortened to nothing. Nothing in these tests
            # makes its condition true, so it polled at 10ms all the way to its
            # 30s deadline and then deferred the restart -- the same verdict these
            # tests already assert, reached thirty seconds later. Eleven tests
            # across the three TestAutoApplyUpdate* classes paid it in full:
            # ~330s of pure sleeping per full suite run, measured identically in
            # five runs. The branch still executes and the deferral still happens;
            # only the waiting goes. The 30.0 itself stays pinned by
            # test_restart_fences_then_closes_and_final_drains, which is the test
            # that is ABOUT it.
            patch.object(gw.GatewayOrchestrator, "_UPDATE_DRAIN_TIMEOUT_SECS", 0.0),
        ):
            yield

    @pytest.mark.asyncio
    async def test_a_floor_refusal_is_redacted_and_capped_before_it_is_pushed(self, tmp_path):
        """The refusal quotes the remote's `requires-python` and local paths verbatim.

        Every sibling push of text the gateway did not author is redacted and
        capped; the floor refusal is text the remote wrote, so it gets the same.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        secret = "https://evil.example/leak?token=AKIA" + "X" * 40
        breach = "the incoming revision requires Python >=3.12 (" + secret + ") " + "p" * 900
        with (
            patch.dict(os.environ, {"KIROCREW_PROJECT_DIR": str(tmp_path)}),
            patch(
                "kiro_crew.slack.gateway.dep_sync.incoming_python_floor_breach",
                return_value=breach,
            ),
            patch("asyncio.create_subprocess_exec", side_effect=self._scripted_git),
        ):
            await orch._auto_apply_update()
        pushed = [c.args for c in ds.push_update_progress.call_args_list if c.args[0] == "failed"]
        assert len(pushed) == 1
        detail = pushed[0][1]
        assert detail.startswith("Update refused: ")
        assert secret not in detail and "AKIA" not in detail
        assert len(detail) <= len("Update refused: ") + 500
        spawned = [[str(a) for a in c.args[1:3]] for c in self._spawns]
        assert ["reset", "--hard"] not in spawned

    @pytest.mark.asyncio
    async def test_a_floor_git_cannot_read_refuses_before_the_reset(self, tmp_path):
        """Unreadable is not absent: a git hiccup must not re-admit the stranded state."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        with (
            patch.dict(os.environ, {"KIROCREW_PROJECT_DIR": str(tmp_path)}),
            patch(
                "kiro_crew.slack.gateway.dep_sync.incoming_python_floor_breach",
                side_effect=gw.dep_sync.IncomingFloorUnreadable("git show timed out"),
            ),
            patch("asyncio.create_subprocess_exec", side_effect=self._scripted_git),
        ):
            await orch._auto_apply_update()
        pushed = [c.args for c in ds.push_update_progress.call_args_list if c.args[0] == "failed"]
        assert len(pushed) == 1 and "could not read" in pushed[0][1]
        spawned = [[str(a) for a in c.args[1:3]] for c in self._spawns]
        assert ["reset", "--hard"] not in spawned

    async def _scripted_git(self, *argv, **_kwargs):
        """Answer each pre-reset git step as a clean, behind-origin checkout would."""
        self.__dict__.setdefault("_spawns", []).append(MagicMock(args=argv))

        def _proc(rc: int, stdout: bytes = b"") -> MagicMock:
            proc = MagicMock()
            proc.returncode = rc
            proc.communicate = AsyncMock(return_value=(stdout, b""))
            proc.wait = AsyncMock(return_value=rc)
            return proc

        words = [str(a) for a in argv[1:]]
        if words[:2] == ["rev-parse", "--abbrev-ref"]:
            return _proc(0, b"main\n")
        if words[:1] == ["fetch"]:
            return _proc(0)
        if words[:2] == ["rev-parse", "--verify"]:
            return _proc(0, b"0123456789abcdef0123456789abcdef01234567\n")
        if words[:1] == ["diff"] and "--quiet" in words:
            return _proc(1)
        if words[:2] == ["status", "--porcelain"]:
            return _proc(0)
        if words[:2] == ["diff", "--name-only"]:
            return _proc(0)
        if words[:2] == ["reset", "--hard"]:
            return _proc(0)
        raise AssertionError(f"unexpected spawn on the git update path: {argv!r}")

    @pytest.mark.asyncio
    async def test_reset_then_frontend_then_pip(self):
        """A CLEAN checkout resets, then frontend build + pip install run.

        Public OSS flow (no internal workspace sync or toolbox step): branch → fetch →
        diff → status → reset → [kiro-cli optional] → build frontend → pip.

        A dirty tree is not exercised here: passing ` M file.py` and expecting
        the reset to run would encode a warn-and-destroy behaviour that
        `test_uncommitted_tracked_changes_refuse_the_reset` forbids. The
        happy path is a clean tree; the dirty tree is a refusal, not a variant
        of this flow.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        # Clean tracked tree: nothing for the reset to discard.
        _fake_exec = _git_exec_fake(status_out=b"")

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock
                    ) as mock_build:
                        with patch("os.execv"):
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        # Frontend build+stage runs, and the package is reinstalled.
        mock_build.assert_awaited()
        ds.push_update_progress.assert_any_call("building", "Building frontend…")
        ds.push_update_progress.assert_any_call("building", "Rebuilding package…")

    @staticmethod
    async def _run_git_apply(orch, *, sync=None, build=None, reset_rc=0, reset_spawn_error=None):
        _git_fake = _git_exec_fake(status_out=b"", reset_rc=reset_rc)

        async def _fake_exec(*args, **kwargs):
            if reset_spawn_error is not None and "reset" in args:
                raise reset_spawn_error
            return await _git_fake(*args, **kwargs)

        sync = sync or (lambda *a, **k: 0)
        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.dep_sync.sync_or_reinstall", side_effect=sync):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async", build or AsyncMock()
                    ):
                        with patch("os.execv"):
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

    @staticmethod
    async def _run_with_copy(
        orch,
        copy_result,
        *,
        status_out=b"",
        added_out=b"",
        project_dir="/tmp/proj",
        status_after_copy=None,
    ):
        """Run the git apply with the shared memory copy stubbed; return spawns and copies.

        The copy is recorded into the same list as every git spawn, so the order
        between it and the reset is read off one sequence. ``status_after_copy``
        is what `git status` reports once the copy has run, standing in for an
        edit made while it ran.
        """
        events: list = []
        copied = threading.Event()
        before = _git_exec_fake(status_out=status_out, added_out=added_out, record=events)
        after = _git_exec_fake(
            status_out=status_out if status_after_copy is None else status_after_copy,
            added_out=added_out,
            record=events,
        )

        def _copy():
            events.append(("memory-copy",))
            copied.set()
            return copy_result

        async def _exec(*args, **kwargs):
            return await (after if copied.is_set() else before)(*args, **kwargs)

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(project_dir)}),
            patch("asyncio.create_subprocess_exec", side_effect=_exec),
            patch(
                "kiro_crew.platform.wheel_apply.snapshot_memory_before_update", side_effect=_copy
            ),
            patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0),
            patch("kiro_crew.slack.gateway.build_frontend_async", AsyncMock()),
            patch("os.execv"),
            patch("shutil.which", return_value=None),
        ):
            await orch._auto_apply_update()
        return [tuple(str(a) for a in e) for e in events]

    @pytest.mark.asyncio
    async def test_memory_is_copied_before_the_reset(self):
        """The same copy `kirocrew update` takes lands before the tree is rewritten."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch.sessions = _mock_sessions()
        orch._restart_after_update = AsyncMock()

        events = await self._run_with_copy(orch, (1, ""))

        resets = [i for i, e in enumerate(events) if e[1:3] == ("reset", "--hard")]
        assert events.count(("memory-copy",)) == 1 and len(resets) == 1
        assert events.index(("memory-copy",)) < resets[0]

    @pytest.mark.asyncio
    async def test_memory_copy_runs_before_the_last_moment_checks(self):
        """A slow copy must not sit between the revalidation and the reset it guards."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch.sessions = _mock_sessions()
        orch._restart_after_update = AsyncMock()

        events = await self._run_with_copy(orch, (1, ""))

        reset = next(i for i, e in enumerate(events) if e[1:3] == ("reset", "--hard"))
        statuses = [i for i, e in enumerate(events[:reset]) if e[1:3] == ("status", "--porcelain")]
        added = [
            i
            for i, e in enumerate(events[:reset])
            if e[1:2] == ("diff",) and "--diff-filter=A" in e
        ]
        assert statuses and added
        copy_at = events.index(("memory-copy",))
        assert copy_at < statuses[-1]
        assert copy_at < added[-1]

    # Each refusal, driven with the fakes this class already uses. ``patches``
    # are (target, return_value); ``run`` goes to `_run_with_copy`.
    _REFUSALS = {
        "ahead": ([("kiro_crew.slack.gateway.commits_ahead", 2)], {}),
        "exec-config": (
            [("kiro_crew.slack.gateway.repo_exec_config_reason", "core.fsmonitor is set")],
            {},
        ),
        "dirty-status": ([], {"status_out": b" M file.py"}),
        "hidden-edit": ([("kiro_crew.slack.gateway.hidden_worktree_edits", ["file.py"])], {}),
        "untracked-collision": ([], {"added_out": b"newfile.txt\0"}),
        "interpreter-floor": (
            [
                (
                    "kiro_crew.slack.gateway.dep_sync.incoming_python_floor_breach",
                    "the incoming revision requires Python >=9.0",
                )
            ],
            {},
        ),
    }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("refusal", sorted(_REFUSALS))
    async def test_every_refusal_runs_before_any_memory_copy(self, refusal, tmp_path):
        """A checkout any check keeps refusing must not rotate backups every check."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()
        orch._restart_after_update = AsyncMock()
        (tmp_path / "newfile.txt").write_text("untracked\n")
        patches, run = self._REFUSALS[refusal]

        with contextlib.ExitStack() as stack:
            for target, value in patches:
                stack.enter_context(patch(target, return_value=value))
            events = await self._run_with_copy(orch, (1, ""), project_dir=tmp_path, **run)

        assert ("memory-copy",) not in events
        assert not any(e[1:3] == ("reset", "--hard") for e in events)
        orch._restart_after_update.assert_not_awaited()
        ds.push_refresh.assert_called_with("update_available")

    @pytest.mark.asyncio
    async def test_an_edit_made_during_the_copy_refuses_the_reset(self):
        """The checks re-run after the copy, so a tree dirtied while it ran is kept."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()
        orch._restart_after_update = AsyncMock()

        events = await self._run_with_copy(orch, (1, ""), status_after_copy=b" M file.py")

        assert events.count(("memory-copy",)) == 1
        assert not any(e[1:3] == ("reset", "--hard") for e in events)
        orch._restart_after_update.assert_not_awaited()
        ds.push_refresh.assert_called_with("update_available")

    @pytest.mark.asyncio
    async def test_a_failed_memory_copy_skips_the_update_and_says_so(self):
        """Nobody is watching: no reset, no exit, a notice and a failed progress line."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()
        orch._restart_after_update = AsyncMock()

        events = await self._run_with_copy(orch, (0, "1 store(s) not copied"))

        assert ("memory-copy",) in events
        assert not any(e[1:3] == ("reset", "--hard") for e in events)
        orch._restart_after_update.assert_not_awaited()
        failed = [
            c.args[1] for c in ds.push_update_progress.call_args_list if c.args[0] == "failed"
        ]
        assert len(failed) == 1 and "memory snapshot failed: 1 store(s) not copied" in failed[0]
        assert "kirocrew memory backups" in failed[0]
        ds.notify.assert_called_once()
        assert ds.notify.call_args.args[:2] == ("update", "Kiro Crew update skipped")

    @pytest.mark.asyncio
    async def test_the_rewrite_owns_the_bundle_gap(self):
        """The build and the reinstall are what the stale-asset watchdog stands down for."""
        from kiro_crew import update_ownership

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch.sessions = _mock_sessions()
        seen = []

        async def _build(*_a, **_k):
            seen.append(update_ownership.current_owner())

        def _sync(*_a, **_k):
            seen.append(update_ownership.current_owner())
            return 0

        await self._run_git_apply(orch, sync=_sync, build=_build)

        assert seen == ["the git auto-update"] * 2
        assert update_ownership.current_owner() is None

    @pytest.mark.asyncio
    async def test_the_rewrite_hands_the_gap_to_the_restart(self):
        """No update step is still open around the restart, so its own maximum binds."""
        from kiro_crew import update_ownership

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch.sessions = _mock_sessions()
        open_at_restart = []
        orch._restart_after_update = AsyncMock(
            side_effect=lambda _respawn: open_at_restart.append(list(update_ownership._live))
        )

        await self._run_git_apply(orch)

        assert open_at_restart == [[]]

    @pytest.mark.asyncio
    async def test_a_failed_sync_that_stays_up_refuses_the_watchdogs_relaunch(self):
        """The step keeps the gateway on its loaded code; the watchdog must too."""
        from kiro_crew import update_ownership

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch.sessions = _mock_sessions()

        await self._run_git_apply(orch, sync=lambda *a, **k: gw.dep_sync.REFUSED)

        assert update_ownership.restart_refusal() is not None

    @pytest.mark.asyncio
    async def test_a_later_update_that_fails_before_the_reset_keeps_the_refusal(self):
        """The unsynced tree the refusal describes is still on disk."""
        from kiro_crew import update_ownership

        update_ownership.refuse_restart("an earlier sync did not complete")
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch.sessions = _mock_sessions()

        await self._run_git_apply(orch, reset_rc=128)

        assert update_ownership.restart_refusal() == "an earlier sync did not complete"

    @pytest.mark.asyncio
    async def test_a_later_update_that_syncs_its_tree_ends_the_refusal(self):
        from kiro_crew import update_ownership

        update_ownership.refuse_restart("an earlier sync did not complete")
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch.sessions = _mock_sessions()
        orch._restart_after_update = AsyncMock()

        await self._run_git_apply(orch)

        assert update_ownership.restart_refusal() is None
        orch._restart_after_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_reset_that_never_spawned_records_no_refusal(self):
        """A fork failure wrote nothing to the checkout, so nothing describes a moved tree."""
        from kiro_crew import update_ownership

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch.sessions = _mock_sessions()

        await self._run_git_apply(
            orch, reset_spawn_error=BlockingIOError(11, "Resource temporarily unavailable")
        )

        assert update_ownership.restart_refusal() is None

    @pytest.mark.asyncio
    async def test_an_update_that_raises_after_the_reset_refuses_the_relaunch(self):
        """The tree moved and its dependencies never synced: a relaunch would die at import."""
        from kiro_crew import update_ownership

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch.sessions = _mock_sessions()

        await self._run_git_apply(orch, build=AsyncMock(side_effect=RuntimeError("vite crashed")))

        assert "after the tree moved" in (update_ownership.restart_refusal() or "")

    @pytest.mark.asyncio
    async def test_a_cancelled_update_never_starts_a_queued_reinstall(self, monkeypatch):
        """Cancelling the step while pip is still queued must cancel pip too.

        A shutdown cancels the update task; a reinstall that started anyway
        would rewrite the venv while the gateway exits.
        """
        import concurrent.futures
        import threading

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch.sessions = _mock_sessions()
        reinstall_queued = asyncio.Event()

        class _SaturatedPool(concurrent.futures.ThreadPoolExecutor):
            """One worker, held by the test; reports the next job it is handed."""

            def submit(self, fn, /, *args, **kwargs):
                future = super().submit(fn, *args, **kwargs)
                if fn != busy.wait:
                    reinstall_queued.set()  # run_in_executor submits on the loop thread
                return future

        busy = threading.Event()
        pool = _SaturatedPool(1)
        # The one worker is taken until the finally releases it. No timeout on
        # purpose: a wall-clock hold could end while a slow update was still on its
        # way to the reinstall, and the finally always releases it.
        # flake-ok: pre-existing unbounded worker hold the finally releases, moved verbatim; tracked for a de-flake pass
        pool.submit(busy.wait)
        ran = []

        async def _build(*_a, **_k):
            # From here on the reinstall goes to the saturated pool.
            monkeypatch.setattr(gw, "subprocess_executor", lambda: pool)

        # The update ahead of the reinstall makes about nine executor hops; count
        # them (submitted and finished) so the wait below can tell slow from stuck.
        loop = asyncio.get_running_loop()
        hops = [0]
        real_run_in_executor = loop.run_in_executor

        def _counted(executor, func, *args):
            hops[0] += 1
            future = real_run_in_executor(executor, func, *args)
            future.add_done_callback(lambda _f: hops.__setitem__(0, hops[0] + 1))
            return future

        monkeypatch.setattr(loop, "run_in_executor", _counted)
        task = asyncio.ensure_future(
            self._run_git_apply(orch, sync=lambda *a, **k: ran.append(1) or 0, build=_build)
        )
        try:
            # The reinstall is queued behind the busy worker: cancel now.
            await _until_set_while_progressing(
                reinstall_queued, lambda: hops[0], "the update reaching the queued reinstall"
            )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await _within_lost_run(task, "the cancelled update")
        finally:
            busy.set()
            # flake-ok: pre-existing unbounded pool join after the release, moved verbatim; tracked for a de-flake pass
            pool.shutdown(wait=True)
        assert ran == []

    @pytest.mark.asyncio
    async def test_uncommitted_tracked_changes_refuse_the_reset(self):
        """An unattended update must not delete a developer's uncommitted work.

        This check refuses rather than logging a warning and resetting anyway:
        a warn-and-reset would make the boot-time path the one place that could
        silently destroy uncommitted edits. It refuses like the committed-work
        and exec-config checks immediately above it, and defers to `kirocrew
        update` -- where a human chose the destructive semantics.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(status_out=b" M file.py\n", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch(
                    "kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock
                ) as mock_build:
                    with patch("os.execv") as mock_execv:
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        # The destructive step never ran.
        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        # And nothing downstream of it ran either.
        mock_build.assert_not_awaited()
        mock_execv.assert_not_called()
        ds.push_refresh.assert_any_call("update_available")

    @pytest.mark.asyncio
    async def test_untracked_files_alone_do_not_refuse(self):
        """`reset --hard` preserves untracked files, so they are not a reason to stop.

        Task specs and notes live untracked in a checkout. Refusing on them would
        disable auto-update for essentially every real install, which is the same
        silent no-op this PR exists to remove.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(status_out=b"?? notes.md\n", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock
                    ):
                        with patch("os.execv"):
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        assert any("reset" in [str(a) for a in args] for args in spawned), spawned

    @pytest.mark.asyncio
    async def test_an_unresolvable_git_refuses_the_whole_update(self):
        """With no trustworthy git, the unattended path does nothing at all.

        Not even the branch probe: the first spawn would already be the planted
        shim, and it is the one that decides every later step.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch(
                    "kiro_crew.slack.gateway.platform_compat.trusted_git_bin",
                    return_value=None,
                ):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async",
                        new_callable=AsyncMock,
                    ) as mock_build:
                        with patch("os.execv") as mock_execv:
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        assert spawned == [], spawned
        mock_build.assert_not_awaited()
        mock_execv.assert_not_called()

    @pytest.mark.asyncio
    async def test_every_git_step_runs_the_resolved_binary(self):
        """One resolution, used by every step -- no bare `git` anywhere.

        Resolved once rather than per call so the whole sequence runs the same
        binary; re-resolving per spawn would leave a window for the answer to
        change mid-update.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch(
                    "kiro_crew.slack.gateway.platform_compat.trusted_git_bin",
                    return_value="/trusted/bin/git",
                ):
                    with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0):
                        with patch(
                            "kiro_crew.slack.gateway.build_frontend_async",
                            new_callable=AsyncMock,
                        ):
                            with patch("os.execv"):
                                with patch("shutil.which", return_value=None):
                                    await orch._auto_apply_update()

        git_calls = [
            [str(a) for a in args] for args in spawned if args and str(args[0]).endswith("git")
        ]
        assert git_calls, spawned
        for argv in git_calls:
            assert argv[0] == "/trusted/bin/git", argv

    @pytest.mark.asyncio
    async def test_a_hidden_tracked_edit_refuses(self):
        """An assume-unchanged edit must stop the unattended reset.

        `status --porcelain` reports a clean tree for it, so check 3 passes and the
        reset would silently overwrite the developer's edit.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch(
                    "kiro_crew.slack.gateway.hidden_worktree_edits",
                    return_value=["config/local.py"],
                ):
                    with patch("os.execv") as mock_execv:
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()
        ds.push_refresh.assert_any_call("update_available")

    @pytest.mark.asyncio
    async def test_an_unknown_hidden_edit_state_refuses(self):
        """`None` cannot prove the tree is safe, so it fails closed.

        Same rule as an unknown ahead-count and an unreadable status.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.slack.gateway.hidden_worktree_edits", return_value=None):
                    with patch("os.execv") as mock_execv:
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_untracked_collision_refuses(self, tmp_path):
        """`reset --hard` OVERWRITES an untracked file the target adds.

        This is the data-loss case that survives a clean tracked tree:
        `git status --porcelain` reports the local file as `??`, and the
        tracked-change refusal deliberately skips those -- so without this check
        the reset silently replaces it. Verified against real git while
        developing the fix.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        # The path the target would add EXISTS locally (untracked).
        (tmp_path / "newfile.txt").write_text("MY PRECIOUS UNTRACKED WORK\n")

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(added_out=b"newfile.txt\0", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch(
                    "kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock
                ) as mock_build:
                    with patch("os.execv") as mock_execv:
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_build.assert_not_awaited()
        mock_execv.assert_not_called()
        ds.push_refresh.assert_any_call("update_available")
        # The file is still the developer's.
        assert (tmp_path / "newfile.txt").read_text() == "MY PRECIOUS UNTRACKED WORK\n"

    @pytest.mark.asyncio
    async def test_an_obstructing_untracked_ancestor_refuses(self, tmp_path):
        """Target adds `pkg/mod.py`; locally `pkg` is an untracked FILE.

        git must replace that file with a directory, destroying it -- but
        `lexists("pkg/mod.py")` is False, so checking only the full path misses
        it. Verified against real git while developing the fix.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        (tmp_path / "pkg").write_text("MY PRECIOUS NOTES\n")

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(added_out=b"pkg/mod.py\0", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("os.execv") as mock_execv:
                    with patch("shutil.which", return_value=None):
                        await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()
        assert (tmp_path / "pkg").read_text() == "MY PRECIOUS NOTES\n"

    @pytest.mark.asyncio
    async def test_a_symlinked_directory_ancestor_refuses(self, tmp_path):
        """`isdir` follows a symlink, so the guard called it a plain directory.

        Verified against real git: target adds `pkg/mod.py`, local untracked `pkg`
        is a symlink to a directory, and the reset REPLACED the symlink with a real
        directory -- destroying the developer's deliberate structure while the
        collision guard reported nothing.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        real = tmp_path / "elsewhere"
        real.mkdir()
        link = tmp_path / "pkg"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("this platform cannot create directory symlinks here")

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(added_out=b"pkg/mod.py\0", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("os.execv") as mock_execv:
                    with patch("shutil.which", return_value=None):
                        await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()
        # The developer's symlink is still a symlink.
        assert link.is_symlink()

    @pytest.mark.asyncio
    async def test_the_collision_scan_does_not_run_on_the_event_loop(self, tmp_path):
        """`no-blocking-call-on-event-loop`: the ancestor walk is offloaded.

        `_obstructions` stats every added path AND each of its ancestors, so a
        large update would run an unbounded stat walk on the loop thread and stall
        every chat and the heartbeat.

        Asserts the THREAD the scan's own probes run on, not which executor object
        some offload was handed: the first version of this test asserted the
        latter and passed even with the scan inline, because another offload in
        the same function already used that executor. Thread identity is the
        property the rule is actually about.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        loop_thread = threading.current_thread()
        probe_threads: list[threading.Thread] = []
        real_lexists = os.path.lexists

        def recording_lexists(path):
            if "scanned-target.py" in str(path):
                probe_threads.append(threading.current_thread())
            return real_lexists(path)

        _fake_exec = _git_exec_fake(added_out=b"deep/nested/scanned-target.py\0")

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("os.path.lexists", side_effect=recording_lexists):
                    with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0):
                        with patch(
                            "kiro_crew.slack.gateway.build_frontend_async",
                            new_callable=AsyncMock,
                        ):
                            with patch("os.execv"):
                                with patch("shutil.which", return_value=None):
                                    await orch._auto_apply_update()

        assert probe_threads, "the collision scan never probed the added path"
        assert all(
            t is not loop_thread for t in probe_threads
        ), f"scan ran on the event-loop thread: {[t.name for t in probe_threads]}"

    @pytest.mark.asyncio
    async def test_a_junction_ancestor_refuses(self, tmp_path):
        """A Windows junction must be treated as a link, not a directory.

        `os.path.islink` returns False for a junction, so the round-20 check would
        read one as a plain directory and let the reset write THROUGH it, outside
        the checkout. `AGENTS.md` names `is_link_or_junction` as the required form
        for exactly this. Asserted by faking the junction verdict, since a real
        junction cannot be created on POSIX.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        (tmp_path / "pkg").mkdir()  # a plain dir to islink, a junction to the helper

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(added_out=b"pkg/mod.py\0", record=spawned)

        def fake_link_or_junction(path):
            return str(path).endswith("pkg")

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch(
                    "kiro_crew.slack.gateway.platform_compat.is_link_or_junction",
                    side_effect=fake_link_or_junction,
                ):
                    with patch("os.execv") as mock_execv:
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_directory_ancestor_is_not_an_obstruction(self, tmp_path):
        """An existing DIRECTORY ancestor is normal and must not refuse.

        Every update that adds a file into an existing package would otherwise
        stop -- the over-refusal that would disable auto-update in practice.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        (tmp_path / "pkg").mkdir()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(added_out=b"pkg/mod.py\0", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock
                    ):
                        with patch("os.execv"):
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        assert any("reset" in [str(a) for a in args] for args in spawned), spawned

    @pytest.mark.asyncio
    async def test_a_non_utf8_added_path_is_still_matched(self, tmp_path):
        """A path byte that is not valid UTF-8 must not decode into a miss.

        Under `errors="replace"` the name becomes `bad\ufffdname.txt`, which does
        not exist on disk, so the guard passes while the reset overwrites the real
        file. `os.fsdecode` round-trips it.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        raw = b"bad\xffname.txt"
        try:
            (tmp_path / os.fsdecode(raw)).write_bytes(b"MY PRECIOUS\n")
        except (OSError, UnicodeError):
            pytest.skip("this filesystem rejects non-UTF-8 names")

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(added_out=raw + b"\0", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("os.execv") as mock_execv:
                    with patch("shutil.which", return_value=None):
                        await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_collision_refusal_logs_an_encodable_record(self, tmp_path, caplog):
        """Reproduces the CI shard crash without needing xdist.

        The collision refusal is the one log line carrying a filename straight
        from git output. When that name is not valid UTF-8, an unsanitized record
        raises inside the handler -- `logging` drops it, and `pytest-xdist`, which
        serializes reports as UTF-8, dies with `DumpError` and takes the WHOLE
        shard with it. That is what happened: one test killed shard 4 on both
        Python versions while passing locally, because the local run disabled
        `-n auto`.

        Asserting encodability here is what makes the guard durable -- reverting
        the sanitizer fails this test in a plain single-process run.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        raw = b"bad\xffname.txt"
        # `os.fsdecode` itself RAISES on Windows (UTF-8 + surrogatepass cannot
        # decode an invalid start byte), so it belongs inside the guard: the
        # hazard does not exist on a platform whose filenames are UTF-16, and this
        # must SKIP there rather than error.
        try:
            name = os.fsdecode(raw)
            name.encode("utf-8")
        except UnicodeDecodeError:
            pytest.skip("this platform cannot represent a non-UTF-8 name")
        except UnicodeEncodeError:
            pass
        else:
            pytest.skip("this platform's fsdecode produced an encodable name")
        try:
            (tmp_path / name).write_bytes(b"MY PRECIOUS\n")
        except (OSError, UnicodeError):
            pytest.skip("this filesystem rejects non-UTF-8 names")

        _fake_exec = _git_exec_fake(added_out=raw + b"\0")

        with caplog.at_level(logging.WARNING):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
                with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                    with patch("os.execv"):
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        # The refusal must have been recorded...
        refusals = [r for r in caplog.records if "already" in r.getMessage()]
        assert refusals, [r.getMessage() for r in caplog.records]
        # ...and EVERY record must survive a UTF-8 log sink / xdist report.
        for record in caplog.records:
            record.getMessage().encode("utf-8")

    @pytest.mark.asyncio
    async def test_the_added_paths_query_disables_rename_detection(self, tmp_path):
        """Without `--no-renames` the collision guard is silently bypassable.

        Rename detection is on by default for porcelain diffs, so a pure `git mv`
        upstream is ONE `R` entry -- and `--diff-filter=A` excludes it, leaving the
        destination path absent from the added list while the reset still
        overwrites an untracked local file there.
        `test_governance_updates` proves that against real git; this pins the flag
        so the argv cannot regress.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock
                    ):
                        with patch("os.execv"):
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        added = [
            [str(a) for a in args]
            for args in spawned
            if any(str(a).startswith("--diff-filter") for a in args)
        ]
        assert added, spawned
        for argv in added:
            assert "--no-renames" in argv, argv

    @pytest.mark.asyncio
    async def test_added_paths_that_do_not_exist_locally_do_not_refuse(self, tmp_path):
        """Most updates add files. Only a COLLISION is a reason to stop.

        Refusing whenever the target adds anything would disable auto-update for
        ordinary upstream commits -- the silent no-op this PR exists to remove,
        reintroduced by an over-broad guard.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        # Target adds two paths; neither exists in tmp_path.
        _fake_exec = _git_exec_fake(added_out=b"a/new.py\0b/other.py\0", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock
                    ):
                        with patch("os.execv"):
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        assert any("reset" in [str(a) for a in args] for args in spawned), spawned

    @pytest.mark.asyncio
    async def test_an_unlistable_added_set_refuses(self, tmp_path):
        """If the added-path list cannot be read, safety cannot be proven.

        Fails closed, like the unreadable-status and unknown-ahead-count cases.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(added_rc=1, record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock):
                    with patch("os.execv") as mock_execv:
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unreadable_status_refuses(self):
        """An unreadable work-tree status cannot prove the tree is clean.

        The next step is irreversible, so an unanswerable question is treated as
        the unsafe answer -- the same fail-closed rule the ahead-count uses when
        `commits_ahead` returns None.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(status_rc=1, record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock):
                    with patch("os.execv") as mock_execv:
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        assert not any("reset" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_refusal_skips_the_core_dep_repair_entirely(self):
        """A REFUSED sync must not be followed by a repair into the same venv.

        REFUSED means the sync stopped before touching anything — most
        importantly when the venv serves a DIFFERENT checkout. The core-dep
        repair writes into exactly that venv, so running it after a refusal
        would perform the mutation the guard exists to prevent, and the restart
        would then bring up the wrong checkout with changed dependencies.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(record=spawned)

        from kiro_crew import dep_sync

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=dep_sync.REFUSED):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async",
                        new_callable=AsyncMock,
                    ):
                        with patch("os.execv") as mock_execv:
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        # No pip spawn at all: the repair is what would have written to the venv.
        assert not any("pip" in [str(a) for a in args] for args in spawned), spawned
        mock_execv.assert_not_called()
        steps = [c.args[0] for c in ds.push_update_progress.call_args_list]
        assert "restarting" not in steps

    @pytest.mark.asyncio
    async def test_reset_target_is_resolved_through_the_full_remote_ref(self):
        """The target capture must spell `refs/remotes/origin/<branch>`.

        The short `origin/<branch>` is ambiguous in the attacker's favour:
        rev-parse's disambiguation order checks `refs/tags/<name>` BEFORE
        `refs/remotes/<name>`, so a tag literally named `origin/main` resolves
        instead of the remote-tracking branch. The update's own fetch auto-follows
        tags, so publishing that tag upstream creates it locally. git writes
        "refname is ambiguous" to stderr and still prints the TAG's OID on stdout,
        which is the stream this capture reads -- so the short form does not fail
        loudly, it resets to attacker code.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        spawned: list[tuple] = []
        _fake_exec = _git_exec_fake(branch=b"main\n", record=spawned)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock):
                    with patch("os.execv"):
                        with patch("shutil.which", return_value=None):
                            await orch._auto_apply_update()

        revparses = [
            [str(a) for a in args]
            for args in spawned
            if "rev-parse" in [str(a) for a in args]
            and "--abbrev-ref" not in [str(a) for a in args]
        ]
        assert revparses, spawned
        assert any("refs/remotes/origin/main^{commit}" in argv for argv in revparses), revparses
        # The bare form must not be what git is asked to resolve.
        assert not any("origin/main^{commit}" in argv for argv in revparses), revparses

    @pytest.mark.asyncio
    async def test_no_restart_after_any_unclean_sync_even_when_the_repair_works(self):
        """A nonzero sync never restarts, even when the core-dep repair succeeds.

        Every nonzero result names something the restart cannot fix on its own.
        Dependencies may still be unsatisfied — the repair covers only the CORE
        deps, not whatever the revision actually added. Or the revision repointed
        the console script, which no dependency install rewrites: this restart uses
        `-m kiro_crew` and would survive it, but the next restart through the
        service manager runs `kirocrew` and would not. Staying up on
        already-imported modules tells the operator now instead of then.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        # diff --quiet reports changes; everything else, INCLUDING the
        # core-dep repair, succeeds.
        _fake_exec = _git_exec_fake()

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                # rc=1, not REFUSED: an install that ran and came back unclean.
                with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=1):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async",
                        new_callable=AsyncMock,
                    ):
                        with patch("os.execv") as mock_execv:
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        mock_execv.assert_not_called()
        orch.sessions.close_all.assert_not_called()
        steps = [c.args[0] for c in ds.push_update_progress.call_args_list]
        assert "error" in steps
        assert "restarting" not in steps

    @pytest.mark.asyncio
    async def test_a_failed_install_with_a_failed_repair_does_not_restart(self):
        """Restarting into unsatisfied dependencies would kill the gateway.

        The reset already moved the tree to the new revision. If neither the
        dependency install nor the core-dep repair succeeded, the process this
        restart brings up is a revision whose dependencies are known to be
        missing — it dies at import. Staying up on already-imported modules
        leaves the operator a working gateway to finish the install from.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        # Every git step succeeds; the dependency install AND the core-dep
        # repair both fail, which is the condition this test is about.
        _fake_exec = _git_exec_fake(other_rc=1)

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
            with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=1):
                    with patch(
                        "kiro_crew.slack.gateway.build_frontend_async",
                        new_callable=AsyncMock,
                    ):
                        with patch("os.execv") as mock_execv:
                            with patch("shutil.which", return_value=None):
                                await orch._auto_apply_update()

        mock_execv.assert_not_called()
        orch.sessions.close_all.assert_not_called()
        steps = [c.args[0] for c in ds.push_update_progress.call_args_list]
        assert "error" in steps
        assert "restarting" not in steps


class TestAutoApplyUpdatePreconditions:
    """The two refusals guarding the unattended reset, at the gateway level.

    The seam functions have their own unit tests; these assert the gateway
    actually HONOURS them — that it returns before spawning anything, rather than
    computing a refusal and proceeding anyway.
    """

    @pytest.mark.asyncio
    async def test_repo_declared_driver_refuses_before_running_a_driver(self):
        """A repo-named filter/textconv driver would be run BY these git commands.

        `-c` cannot pin an arbitrary driver name, so the run is refused. What must
        be proven is that the refusal lands before the commands that would EXECUTE
        such a driver — `status`, `diff` and `reset`. The branch probe ahead of it
        is a pure ref read that runs no driver, and it carries the neutralizer env
        regardless.
        """
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        argvs = []

        async def _fake_exec(*args, **kwargs):
            argvs.append(args)
            proc = AsyncMock()
            proc.kill = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"main\n", b""))
            proc.wait = AsyncMock(return_value=0)
            return proc

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
            patch(
                "kiro_crew.slack.gateway.repo_exec_config_reason",
                return_value="repository declares filter.evil.smudge",
            ),
            patch("kiro_crew.slack.gateway.is_primary_branch", return_value=True),
            patch("kiro_crew.slack.gateway.tracks_upstream", return_value=True),
        ):
            await orch._auto_apply_update()

        for forbidden in ("status", "diff", "reset", "fetch"):
            assert not any(forbidden in a for a in argvs), (forbidden, argvs)

    @pytest.mark.asyncio
    async def test_local_commits_refuse_before_the_reset(self):
        """A checkout ahead of origin must not be `reset --hard` unattended.

        Revalidated after the fetch, immediately before the reset, because the
        availability verdict was reached in an earlier pass and a checkout is a
        live tree. Asserts no `reset` spawn — a warning that still reset would
        already have destroyed the commits.
        """
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        argvs = []

        async def _fake_exec(*args, **kwargs):
            argvs.append(args)
            proc = AsyncMock()
            proc.kill = MagicMock()
            # `git diff --quiet` must report a difference so the run reaches the
            # revalidation rather than stopping at "already up to date".
            proc.returncode = 1 if "diff" in args else 0
            proc.communicate = AsyncMock(return_value=(b"main\n", b""))
            proc.wait = AsyncMock(return_value=proc.returncode)
            return proc

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
            patch(
                "kiro_crew.slack.gateway.repo_exec_config_reason",
                return_value="",
            ),
            patch("kiro_crew.slack.gateway.tracks_upstream", return_value=True),
            patch("kiro_crew.slack.gateway.commits_ahead", return_value=2),
        ):
            await orch._auto_apply_update()

        assert not any("reset" in a for a in argvs), argvs

    @pytest.mark.asyncio
    async def test_unknown_ahead_count_also_refuses(self):
        """`None` means git could not answer, which must not read as zero."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        argvs = []

        async def _fake_exec(*args, **kwargs):
            argvs.append(args)
            proc = AsyncMock()
            proc.kill = MagicMock()
            proc.returncode = 1 if "diff" in args else 0
            proc.communicate = AsyncMock(return_value=(b"main\n", b""))
            proc.wait = AsyncMock(return_value=proc.returncode)
            return proc

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
            patch(
                "kiro_crew.slack.gateway.repo_exec_config_reason",
                return_value="",
            ),
            patch("kiro_crew.slack.gateway.tracks_upstream", return_value=True),
            patch("kiro_crew.slack.gateway.commits_ahead", return_value=None),
        ):
            await orch._auto_apply_update()

        assert not any("reset" in a for a in argvs), argvs

    @pytest.mark.asyncio
    async def test_branch_not_tracking_origin_refuses_before_fetch(self):
        """The check measures `@{u}`; the apply resets `origin/<branch>`.

        When they are different remotes the reset would discard commits, so the
        apply stops. Only the branch probe may have run by then — nothing that
        fetches or writes.
        """
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()

        argvs = []

        async def _fake_exec(*args, **kwargs):
            argvs.append(args)
            proc = AsyncMock()
            proc.kill = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"main\n", b""))
            proc.wait = AsyncMock(return_value=0)
            return proc

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
            patch(
                "kiro_crew.slack.gateway.repo_exec_config_reason",
                return_value="",
            ),
            patch("kiro_crew.slack.gateway.tracks_upstream", return_value=False),
        ):
            await orch._auto_apply_update()

        assert not any("fetch" in a for a in argvs), argvs
        assert not any("reset" in a for a in argvs), argvs
