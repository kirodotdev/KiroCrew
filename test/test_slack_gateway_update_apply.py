"""The update check and the source-checkout auto-apply.

Covers ``_check_for_updates`` and the verdicts it acts on, ``_auto_apply_update``'s
early returns and failure path, the git and venv (``pip install -e .``) apply paths,
and the dependency repair: ``_is_brazil_install``, ``_check_missing_deps`` with its
pip install, and the console-script check and repair. The reset path is
``test_slack_gateway_update_reset.py``; the managed-venv apply is
``test_slack_gateway_update_wheel.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import (
    _fake_async_proc,
    _git_exec_fake,
    _ImportlibWithoutReload,
    _install_effect,
    _make_orchestrator,
    _mock_dashboard_state,
    _mock_sessions,
)

from kiro_crew.slack import gateway as gw
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.testing.wait import await_until

# ═══════════════════════════════════════════════════════════════════════════
# Tests: _check_for_updates
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckForUpdates:
    """Update check logic."""

    @pytest.mark.asyncio
    async def test_no_update_available(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        with patch("kiro_crew.dashboard.handlers._do_update_check", new_callable=AsyncMock):
            with patch("kiro_crew.dashboard.handlers._update_info", {"update_available": False}):
                await orch._check_for_updates()

    @pytest.mark.asyncio
    async def test_update_available_no_auto(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch._auto_apply_update = AsyncMock()
        import kiro_crew.dashboard.handlers as _h
        from kiro_crew.platform.governance import UpdatePins

        fake_cfg = MagicMock()
        fake_cfg.auto_update = False
        # The check's verdict and nothing else: an earlier test's keys (a provider's
        # managed_by) must not decide the branch. The module fixture restores it.
        _h._update_info.clear()
        _h._update_info.update({"update_available": True, "version": "9.9.9"})
        with patch.object(_h, "_do_update_check", new_callable=AsyncMock):
            with patch("kiro_crew.config.KiroCrewConfig.load", return_value=fake_cfg):
                with patch(
                    "kiro_crew.platform.governance.active_update_pins",
                    return_value=UpdatePins(),
                ):
                    await orch._check_for_updates()
        orch._auto_apply_update.assert_not_awaited()
        ds.push_refresh.assert_called_with("update_available")

    @pytest.mark.asyncio
    async def test_commit_distance_alone_lights_the_badge_but_does_not_auto_apply(self):
        """A git checkout behind upstream with an UNCHANGED version is not reset.

        `available` is true on commit distance alone so the dashboard stops
        claiming "you're on the latest version". This path is not the dashboard:
        it applies `git reset --hard`, so acting on commit distance would reset a
        developer's checkout within 12 hours of any upstream commit, where before
        it only did so at a release. The badge is lit instead; the dashboard's own
        apply (`git pull`, dirty tree refused) is the non-destructive way in.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch._auto_apply_update = AsyncMock()
        import kiro_crew.dashboard.handlers as _h

        fake_cfg = MagicMock()
        fake_cfg.auto_update = True
        _h._update_info.clear()
        _h._update_info.update(
            {"update_available": True, "can_apply": True, "version_newer": False}
        )
        with patch.object(_h, "_do_update_check", new_callable=AsyncMock):
            with patch("kiro_crew.config.KiroCrewConfig.load", return_value=fake_cfg):
                with patch(
                    "kiro_crew.platform.update_governance.update_required",
                    return_value=False,
                ):
                    with _install_effect():
                        await orch._check_for_updates()
        orch._auto_apply_update.assert_not_awaited()
        ds.push_refresh.assert_called_with("update_available")

    @pytest.mark.asyncio
    async def test_a_version_bump_still_auto_applies(self):
        """The pre-existing trigger is unchanged: a release moved, so apply."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch._auto_apply_update = AsyncMock()
        import kiro_crew.dashboard.handlers as _h

        fake_cfg = MagicMock()
        fake_cfg.auto_update = True
        _h._update_info.clear()
        _h._update_info.update({"update_available": True, "can_apply": True, "version_newer": True})
        with patch.object(_h, "_do_update_check", new_callable=AsyncMock):
            with patch("kiro_crew.config.KiroCrewConfig.load", return_value=fake_cfg):
                with patch(
                    "kiro_crew.platform.update_governance.update_required",
                    return_value=False,
                ):
                    with _install_effect():
                        await orch._check_for_updates()
        orch._auto_apply_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_min_version_mandate_without_a_newer_build_notifies(self):
        """A below-floor git checkout with no newer build must NOT hard-reset.

        The mandate is real, but `_auto_apply_update` resets hard onto the
        upstream tip, and `update_available` is true for a checkout on any commit
        distance — released or not. Resetting on that alone drags the checkout to
        every intermediate commit on every cycle and at every boot without the
        running `__version__` ever moving toward the floor. The mandatory branch
        now reads the same `version_newer` signal the voluntary branch does, so a
        floor with no newer build available notifies instead of resetting.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch._auto_apply_update = AsyncMock()
        import kiro_crew.dashboard.handlers as _h
        from kiro_crew.platform.update_capability import CHECK_SUCCEEDED

        # A git checkout (`can_apply`) below the floor whose check SUCCEEDED
        # but found no newer `__version__` (version_newer False). The wheel
        # layout's equivalent no-newer-build path is
        # test_mandatory_wheel_no_newer_build_notifies.
        _h._update_info.clear()
        _h._update_info.update(
            {
                "update_available": False,
                "can_apply": True,
                "version_newer": False,
                "check_status": CHECK_SUCCEEDED,
            }
        )
        with patch.object(_h, "_do_update_check", new_callable=AsyncMock):
            with patch("kiro_crew.platform.update_governance.update_required", return_value=True):
                with _install_effect("mandatory"):
                    await orch._check_for_updates()
        orch._auto_apply_update.assert_not_awaited()
        ds.push_refresh.assert_called_with("update_available")
        # The notify path refreshes the badge but does NOT clobber the shared
        # check cache: the verdict the check wrote (no update available, check
        # succeeded) survives, mirroring the wheel branch.
        assert _h._update_info["update_available"] is False
        assert _h._update_info["check_status"] == CHECK_SUCCEEDED

    @pytest.mark.asyncio
    async def test_min_version_mandate_applies_when_a_newer_build_is_available(self):
        """The floor still drives an apply, but only toward a newer build.

        `version_newer` true means origin carries a build whose `__version__`
        outranks the running one, so applying moves the host forward (toward or
        past the floor) rather than churning. This is the half of the mandate the
        gate preserves.
        """
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch._auto_apply_update = AsyncMock()
        import kiro_crew.dashboard.handlers as _h

        _h._update_info.clear()
        _h._update_info.update({"update_available": True, "can_apply": True, "version_newer": True})
        with patch.object(_h, "_do_update_check", new_callable=AsyncMock):
            with patch("kiro_crew.platform.update_governance.update_required", return_value=True):
                with _install_effect("mandatory"):
                    await orch._check_for_updates()
        orch._auto_apply_update.assert_awaited_once()
        # The floor-mandated apply is marked so a no-op outcome stays visible.
        assert orch._auto_apply_update.await_args.kwargs.get("mandatory") is True

    @pytest.mark.asyncio
    async def test_update_check_exception_handled(self):
        orch = _make_orchestrator()
        orch._auto_apply_update = AsyncMock()
        with patch(
            "kiro_crew.dashboard.handlers._do_update_check",
            new_callable=AsyncMock,
            side_effect=RuntimeError("network"),
        ) as check:
            await orch._check_for_updates()
        check.assert_awaited_once()
        orch._auto_apply_update.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _auto_apply_update
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoApplyUpdate:
    """Auto-update logic (public OSS flow: git reset → frontend → pip → restart)."""

    @pytest.mark.asyncio
    async def test_no_project_dir_returns_early(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": ""}, clear=False):
            with patch(
                "kiro_crew.slack.gateway.platform_compat.trusted_git_bin",
                return_value="/trusted/bin/git",
            ) as resolve_git:
                with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
                    await orch._auto_apply_update()
        # The env read is the gate: nothing past it, not even the git lookup, runs.
        resolve_git.assert_not_called()
        spawn.assert_not_awaited()
        ds.push_update_progress.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_mainline_branch_skips(self):
        orch = _make_orchestrator()
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"feat/test\n", b""))
        proc.returncode = 0
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}, clear=False):
            with (
                patch(
                    "kiro_crew.slack.gateway.platform_compat.trusted_git_bin",
                    return_value="/trusted/bin/git",
                ),
                # Permissive, so only the branch gate can stop the run before the
                # next precondition (the repo-named exec config) is read.
                patch(
                    "kiro_crew.slack.gateway.repo_exec_config_reason", return_value=""
                ) as exec_config,
                patch("kiro_crew.slack.gateway.tracks_upstream", return_value=True),
                patch(
                    "asyncio.create_subprocess_exec",
                    new_callable=AsyncMock,
                    return_value=proc,
                ) as spawn,
            ):
                await orch._auto_apply_update()
        # Only the branch probe ran: no fetch follows a non-primary branch.
        exec_config.assert_not_called()
        spawn.assert_awaited_once()
        assert "--abbrev-ref" in spawn.await_args.args
        ds.push_update_progress.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_path_resolves_the_restart_hint_off_the_event_loop(self):
        # The except handler prints `restart_command_hint()`, which stats the
        # per-user unit file under the account's home; a disconnected network
        # home holds that stat for as long as it is disconnected. Called on the
        # loop thread, the wait freezes chat and the liveness heartbeat together
        # with nothing in-band to clear it. The double below stands in for that
        # slow stat and blocks until the LOOP sets an event: only a hint
        # resolved off the loop lets the loop run the task that sets it, so a
        # hint called synchronously on the loop thread times out and answers
        # `LOOP BLOCKED` instead of the command — deterministic, not a timing
        # race, because nothing but the loop can set the event.
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        loop_ran = threading.Event()

        def slow_hint() -> str:
            # flake-ok: pre-existing two-second bound on the loop-served event, moved verbatim; tracked for a de-flake pass
            return "kirocrew restart" if loop_ran.wait(timeout=2.0) else "LOOP BLOCKED"

        async def loop_is_serving() -> None:
            # flake-ok: pre-existing event-loop yield in the serving probe, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)
            loop_ran.set()

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}, clear=False),
            # The first statement inside the handler's `try` raises, so the
            # failure path is reached before any spawn.
            patch.object(gw, "git_command_env", side_effect=RuntimeError("disk gone")),
            patch.object(gw, "restart_command_hint", slow_hint),
        ):
            serving = asyncio.ensure_future(loop_is_serving())
            await orch._auto_apply_update()
            # flake-ok: pre-existing unbounded await of the serving probe, moved verbatim; tracked for a de-flake pass
            await serving

        orch.dashboard_state.push_update_progress.assert_called_once_with(
            "failed", "Restart failed — run: kirocrew restart"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _is_brazil_install and _check_missing_deps
# ═══════════════════════════════════════════════════════════════════════════


class TestBrazilInstallAndDeps:
    """Static helper and dep repair."""

    @pytest.mark.parametrize(
        ("method_text", "expected"),
        [
            pytest.param("brazil", True, id="test_is_brazil_install_with_method_file"),
            pytest.param("pip", False, id="test_is_brazil_install_pip"),
        ],
    )
    def test_is_brazil_install_reads_method_file(self, tmp_path, method_text, expected):
        (tmp_path / ".install-method").write_text(method_text)
        assert GatewayOrchestrator._is_brazil_install(str(tmp_path)) is expected

    def test_is_brazil_install_no_file_no_brazil(self, tmp_path):
        with patch("shutil.which", return_value=None):
            assert GatewayOrchestrator._is_brazil_install(str(tmp_path)) is False

    def test_check_missing_deps_no_missing(self):
        orch = _make_orchestrator()
        with patch("importlib.util.find_spec", return_value=MagicMock()) as find_spec:
            with patch.object(GatewayOrchestrator, "_is_brazil_install") as is_brazil:
                with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
                    asyncio.run(orch._check_missing_deps())
        assert find_spec.call_count == len(GatewayOrchestrator._REQUIRED_DEPS)
        is_brazil.assert_not_called()
        spawn.assert_not_awaited()

    def test_check_missing_deps_brazil_skips(self):
        orch = _make_orchestrator()
        with patch("importlib.util.find_spec", return_value=None):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/proj"}):
                with patch.object(
                    GatewayOrchestrator, "_is_brazil_install", return_value=True
                ) as is_brazil:
                    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
                        asyncio.run(orch._check_missing_deps())
        is_brazil.assert_called_once_with("/proj")
        spawn.assert_not_awaited()

    # --- _check_console_script -------------------------------------------------

    def test_check_console_script_skips_when_no_project_dir(self):
        orch = _make_orchestrator()
        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": ""}, clear=False):
            with patch.object(gw, "sandboxed_spawn_argv_async", new_callable=AsyncMock) as spawn:
                asyncio.run(orch._check_console_script())
        spawn.assert_not_awaited()

    def test_check_console_script_skips_brazil(self, tmp_path):
        (tmp_path / ".install-method").write_text("brazil")
        orch = _make_orchestrator()
        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}, clear=False):
            with patch.object(gw, "sandboxed_spawn_argv_async", new_callable=AsyncMock) as spawn:
                asyncio.run(orch._check_console_script())
        spawn.assert_not_awaited()

    def test_check_console_script_skips_non_pip(self, tmp_path):
        # No .install-method (or a non-pip one) means this is not a pip install.
        orch = _make_orchestrator()
        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}, clear=False):
            with patch.object(GatewayOrchestrator, "_is_brazil_install", return_value=False):
                with patch.object(
                    gw, "sandboxed_spawn_argv_async", new_callable=AsyncMock
                ) as spawn:
                    asyncio.run(orch._check_console_script())
        spawn.assert_not_awaited()

    def test_check_console_script_skips_when_script_present_and_executable(self, tmp_path):
        (tmp_path / ".install-method").write_text("pip")
        venv_py = gw.dep_sync.project_venv_python(tmp_path)
        script = gw.dep_sync.console_script_path(venv_py)
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        orch = _make_orchestrator()
        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}, clear=False):
            with patch.object(gw, "sandboxed_spawn_argv_async", new_callable=AsyncMock) as spawn:
                asyncio.run(orch._check_console_script())
        spawn.assert_not_awaited()

    def test_check_console_script_reinstalls_when_missing(
        self, tmp_path, nonbundled_python_with_user_site
    ):
        (tmp_path / ".install-method").write_text("pip")
        venv_py = gw.dep_sync.project_venv_python(tmp_path)
        # The interpreter directory exists but the entry point is absent.
        venv_py.parent.mkdir(parents=True)
        proc = _fake_async_proc()
        orch = _make_orchestrator()
        seen = {}

        async def fake_prepare(cmd, **kwargs):
            seen["argv"] = list(cmd)
            seen["kwargs"] = kwargs
            return list(cmd), {"SCRUBBED": "1"}, None

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}, clear=False):
            with patch.object(gw, "sandboxed_spawn_argv_async", side_effect=fake_prepare):
                with patch.object(
                    gw,
                    "create_subprocess_limited",
                    new_callable=AsyncMock,
                    return_value=proc,
                ) as spawn:
                    asyncio.run(orch._check_console_script())

        # The dep_sync argv is asserted at the sandbox seam's input, which is
        # where it is now composed.
        assert seen["argv"] == [
            sys.executable,
            "-s",
            str(Path(gw.dep_sync.__file__).resolve()),
            "--repair-missing-package",
            str(tmp_path),
            str(venv_py),
        ]
        spawn.assert_awaited_once()
        assert spawn.await_args.args == tuple(seen["argv"])
        assert spawn.await_args.kwargs["cwd"] == str(tmp_path)
        assert spawn.await_args.kwargs["env"] == {"SCRUBBED": "1"}
        assert spawn.await_args.kwargs["start_new_session"] is gw.platform_compat.IS_POSIX

    def test_check_console_script_reinstalls_on_dangling_symlink(self, tmp_path):
        (tmp_path / ".install-method").write_text("pip")
        venv_py = gw.dep_sync.project_venv_python(tmp_path)
        script = gw.dep_sync.console_script_path(venv_py)
        script.parent.mkdir(parents=True)
        # Dangling symlink: exists() is False, so the reinstall must fire.
        try:
            script.symlink_to(tmp_path / "does-not-exist")
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
        proc = _fake_async_proc()
        orch = _make_orchestrator()

        async def fake_prepare(cmd, **kwargs):
            return list(cmd), {}, None

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}, clear=False):
            with patch.object(gw, "sandboxed_spawn_argv_async", side_effect=fake_prepare):
                with patch.object(
                    gw,
                    "create_subprocess_limited",
                    new_callable=AsyncMock,
                    return_value=proc,
                ) as spawn:
                    asyncio.run(orch._check_console_script())
        spawn.assert_awaited_once()

    def test_check_console_script_skipped_when_sandbox_unavailable(self, tmp_path):
        """Fail CLOSED: no sandbox backend means the repair does NOT run.

        Running the project's own venv interpreter unsandboxed is the exposure
        the routing removes, so an unavailable sandbox must skip the repair
        rather than fall back to a bare spawn.
        """
        (tmp_path / ".install-method").write_text("pip")
        venv_py = gw.dep_sync.project_venv_python(tmp_path)
        venv_py.parent.mkdir(parents=True)
        orch = _make_orchestrator()
        unavailable = gw.SandboxUnavailableError(
            "no sandbox backend",
            "no_backend",
            "unshare(CLONE_NEWNS) failed with errno 1 (EPERM)",
        )

        with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}, clear=False):
            with patch.object(
                gw,
                "sandboxed_spawn_argv_async",
                side_effect=unavailable,
            ):
                with patch.object(gw, "create_subprocess_limited", new_callable=AsyncMock) as spawn:
                    with patch.object(gw.logger, "error") as log_error:
                        asyncio.run(orch._check_console_script())

        spawn.assert_not_awaited()
        # The skip must name the machine-readable reason, not just that it
        # skipped -- an undiagnosable failure is the class this guards.
        logged = log_error.call_args.args
        assert "no_backend" in logged
        assert any("EPERM" in str(part) for part in logged)

    @pytest.mark.asyncio
    async def test_check_console_script_cancellation_kills_and_reaps_child(self, tmp_path):
        """Gateway shutdown must not leave dep_sync or its pip descendants running."""
        (tmp_path / ".install-method").write_text("pip")
        venv_py = gw.dep_sync.project_venv_python(tmp_path)
        venv_py.parent.mkdir(parents=True)
        started = asyncio.Event()
        never = asyncio.Event()
        proc = _fake_async_proc()

        async def _communicate():
            started.set()
            # flake-ok: pre-existing parked stub the cancellation unblocks, moved verbatim; tracked for a de-flake pass
            await never.wait()
            return b"", b""

        proc.communicate = AsyncMock(side_effect=_communicate)
        orch = _make_orchestrator()
        cleanup = tmp_path / "launcher-profile"
        cleanup.write_text("")

        async def fake_prepare(cmd, **kwargs):
            return list(cmd), {}, str(cleanup)

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}, clear=False),
            patch.object(gw, "sandboxed_spawn_argv_async", side_effect=fake_prepare),
            patch.object(
                gw,
                "create_subprocess_limited",
                new_callable=AsyncMock,
                return_value=proc,
            ),
            patch.object(orch, "_kill_startup_child", new_callable=AsyncMock) as kill,
            patch.object(orch, "_reap_startup_child", new_callable=AsyncMock) as reap,
        ):
            task = asyncio.create_task(orch._check_console_script())
            await await_until(started)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                # flake-ok: pre-existing unbounded await of the cancelled task, moved verbatim; tracked for a de-flake pass
                await task

        kill.assert_awaited_once_with(proc)
        reap.assert_awaited_once_with(proc)
        # The sandbox launcher temp file must not leak, even on cancellation.
        assert not cleanup.exists()

    @pytest.mark.asyncio
    async def test_check_console_script_is_sandbox_routed_and_limited(self, tmp_path):
        """The repair spawn MUST route through the sandbox chokepoint.

        The child EXECUTES the project's own venv interpreter (dep_sync probes
        it), so it must get OS isolation, a scrubbed env, and the kernel
        resource ceiling. Reverting to a bare ``asyncio.create_subprocess_exec``
        fails this two ways: the seams below are never awaited, and the poisoned
        ``create_subprocess_exec`` raises the moment the old path touches it.
        """
        (tmp_path / ".install-method").write_text("pip")
        venv_py = gw.dep_sync.project_venv_python(tmp_path)
        venv_py.parent.mkdir(parents=True)
        proc = _fake_async_proc()
        orch = _make_orchestrator()
        prepared = {}

        async def fake_prepare(cmd, **kwargs):
            prepared["argv"] = list(cmd)
            prepared["kwargs"] = kwargs
            return list(cmd), {"SCRUBBED": "1"}, None

        def _boom(*args, **kwargs):  # a revert to the bare exec path lands here
            raise AssertionError("bare asyncio.create_subprocess_exec is forbidden")

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}, clear=False),
            patch.object(gw, "sandboxed_spawn_argv_async", side_effect=fake_prepare) as prep,
            patch.object(
                gw,
                "create_subprocess_limited",
                new_callable=AsyncMock,
                return_value=proc,
            ) as limited,
            patch("asyncio.create_subprocess_exec", side_effect=_boom),
        ):
            await orch._check_console_script()

        # Routed through the chokepoint, with the real synchronous preparer.
        prep.assert_awaited_once()
        assert prepared["kwargs"]["_prepare"] is gw.sandboxed_spawn_argv
        # Keeps the project/venv writable so pip can rewrite the entry point,
        # and refuses to leak our interpreter paths into the child's probe.
        assert prepared["kwargs"]["mode"] == "strict"
        assert prepared["kwargs"]["strip_python_env"] is True
        # Spawned via the resource-limited launcher with the scrubbed env.
        limited.assert_awaited_once()
        assert limited.await_args.kwargs["env"] == {"SCRUBBED": "1"}
        assert limited.await_args.kwargs["start_new_session"] is gw.platform_compat.IS_POSIX

    @pytest.mark.asyncio
    async def test_console_script_repair_task_is_tracked_without_blocking(self):
        orch = _make_orchestrator()
        started = asyncio.Event()
        release = asyncio.Event()

        async def _repair():
            started.set()
            # flake-ok: pre-existing parked stub the test releases, moved verbatim; tracked for a de-flake pass
            await release.wait()

        with patch.object(orch, "_check_console_script", side_effect=_repair):
            task = orch._schedule_console_script_repair()
            await await_until(started)
            assert task in orch._background_tasks
            assert not task.done()
            release.set()
            # flake-ok: pre-existing unbounded await of the released task, moved verbatim; tracked for a de-flake pass
            await task
            # flake-ok: pre-existing event-loop yield before the done-callback check, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

        assert task not in orch._background_tasks
        assert orch._console_script_repair_task is None

    def test_console_script_repair_is_scheduled_only_after_http_bind(self):
        """The potentially 300-second repair must never gate socket readiness."""
        source = inspect.getsource(GatewayOrchestrator.run)
        scheduled = source.index("self._schedule_console_script_repair()")
        assert source.index("await self._init_dashboard()") < scheduled
        assert source.index("await self._init_api_server()") < scheduled


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _auto_apply_update git path
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoApplyUpdateGitPath:
    """Git-based auto-update (non-toolbox)."""

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
    async def test_fetch_fails_returns_early(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        record: list = []
        _fake_exec = _git_exec_fake(fetch_rc=1, record=record)

        with patch("kiro_crew.env.is_toolbox_install", return_value=False):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
                with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                    await orch._auto_apply_update()
        assert [args for args in record if "fetch" in args], "the fetch never ran"
        assert not [args for args in record if "reset" in args]
        ds.clear_update_progress.assert_called()

    @pytest.mark.asyncio
    async def test_no_diff_returns_early(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        # diff --quiet reports no difference, so the run stops before the reset.
        record: list = []
        _fake_exec = _git_exec_fake(diff_rc=0, record=record)

        with patch("kiro_crew.env.is_toolbox_install", return_value=False):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
                with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                    await orch._auto_apply_update()
        # A voluntary apply settles silently: it clears the bar and does not
        # light the badge. (A "pulling" progress fires during the fetch above;
        # the distinction from a mandatory no-diff is the final outcome.)
        assert [args for args in record if "diff" in args], "the diff never ran"
        assert not [args for args in record if "reset" in args]
        ds.clear_update_progress.assert_called()
        ds.push_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_mandatory_no_diff_is_visible_to_the_operator(self):
        """A floor-mandated no-diff apply refreshes the badge without a restart.

        Reaching the no-diff branch means the required code is already on disk
        and a restart is what remains. The ``restarting`` step is reserved for
        the moment the gateway is about to exec itself: it arms the SPA's reload
        latch, which a later reconnect consumes. No exec happens here, so the step
        must NOT be pushed — doing so would reload a tab over an unrelated
        reconnect and leave the progress bar stuck mid-"restarting". The mandated
        no-diff path therefore clears the bar (so it is not stuck) and refreshes
        the badge (so the pending update stays visible), unlike the voluntary
        no-diff path which only clears.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds

        _fake_exec = _git_exec_fake(diff_rc=0)

        with patch("kiro_crew.env.is_toolbox_install", return_value=False):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
                with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                    await orch._auto_apply_update(mandatory=True)
        # The reload latch must not be armed: no "restarting" step is pushed.
        assert not any(
            call.args and call.args[0] == "restarting"
            for call in ds.push_update_progress.call_args_list
        )
        # The bar is cleared (not left stuck) and the badge is refreshed so the
        # pending mandatory update stays visible — the signal that distinguishes
        # it from the voluntary no-diff path, which does not refresh.
        ds.clear_update_progress.assert_called()
        ds.push_refresh.assert_called_with("update_available")


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _auto_apply_update venv path
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoApplyUpdateVenvPath:
    """Venv-based auto-update (pip install -e .)."""

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
    async def test_venv_update_full_path(self):
        """Full venv update: fetch, diff, reset, pip install, restart."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        _fake_exec = _git_exec_fake()

        with patch("kiro_crew.env.is_toolbox_install", return_value=False):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
                with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                    with patch(
                        "kiro_crew.dep_sync.sync_or_reinstall", return_value=0
                    ) as mock_install:
                        with patch.object(
                            GatewayOrchestrator, "_is_brazil_install", return_value=False
                        ):
                            with patch(
                                "kiro_crew.slack.gateway.build_frontend_async",
                                new_callable=AsyncMock,
                            ):
                                with patch("os.execv"):
                                    with patch("shutil.which", return_value=None):
                                        await orch._auto_apply_update()

        # The install runs through the shared entry point, which picks a reinstall
        # or a dependency-only sync — the gateway is normally started through the
        # console script pip would have to rewrite.
        assert mock_install.call_count == 1
        assert str(mock_install.call_args[0][0]) == str(Path("/tmp/proj"))
        assert str(mock_install.call_args[0][1]) == sys.executable

        ds.push_update_progress.assert_any_call("pulling", "Fetching latest changes…")
        ds.push_update_progress.assert_any_call("building", "Building frontend…")

    @pytest.mark.asyncio
    async def test_kiro_cli_update_timeout_kills_child_and_stays_nonfatal(self):
        """A hung `kiro-cli update` is tree-killed AND the update stays non-fatal.

        Both halves matter. If the 120s timeout is swallowed by a bare
        ``except Exception`` → DEBUG, the run falls through to the frontend
        build and dep reinstall while the ABANDONED `kiro-cli update` keeps
        mutating the installation concurrently — the
        same half-replaced-install race the wheel path's CancelledError branch
        exists to prevent. A kill-only assertion would pass on a fix that
        turned the timeout fatal; the non-fatal half pins that the surrounding
        contract (log at DEBUG, continue the update) is unchanged.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        _git_fake = _git_exec_fake()
        kiro_procs: list = []

        async def _fake_exec(*args, **kwargs):
            argv = [a for a in args if isinstance(a, str)]
            # The resolved absolute path is argv0 now, not the bare name.
            if argv and Path(argv[0]).name == "kiro-cli":
                proc = AsyncMock()
                proc.kill = MagicMock()
                proc.returncode = None
                # The wait never completes inside its 120s budget; raising the
                # timeout from the awaited side is the gateway tests' precedent for
                # an expired `asyncio.wait_for` (the arm under test catches
                # the same exception either way).
                proc.wait = AsyncMock(side_effect=asyncio.TimeoutError())
                proc.communicate = AsyncMock(return_value=(b"", b""))
                kiro_procs.append(proc)
                return proc
            return await _git_fake(*args, **kwargs)

        killed: list = []

        async def _fake_kill_and_reap(proc):
            killed.append(proc)

        with patch("kiro_crew.env.is_toolbox_install", return_value=False):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
                with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                    with patch(
                        "kiro_crew.dep_sync.sync_or_reinstall", return_value=0
                    ) as mock_install:
                        with patch.object(
                            GatewayOrchestrator, "_is_brazil_install", return_value=False
                        ):
                            with patch(
                                "kiro_crew.slack.gateway.build_frontend_async",
                                new_callable=AsyncMock,
                            ) as mock_build:
                                with patch("os.execv"):
                                    # Resolves: the optional kiro-cli step runs.
                                    with patch(
                                        "kiro_crew.kiro_cli.resolve_kiro_cli",
                                        return_value="/usr/bin/kiro-cli",
                                    ):
                                        # The gateway resolves _kill_and_reap
                                        # function-locally on every call, so
                                        # patching the source module reaches it.
                                        with patch(
                                            "kiro_crew.platform.update_provider._kill_and_reap",
                                            side_effect=_fake_kill_and_reap,
                                        ):
                                            await orch._auto_apply_update()

        # Half 1: the hung child was killed (tree kill + bounded reap).
        assert kiro_procs, "the kiro-cli update spawn never happened"
        assert killed == kiro_procs
        # Half 2: the timeout stayed NON-FATAL — the update continued into the
        # frontend build and the dependency install exactly as before.
        mock_build.assert_awaited_once()
        assert mock_install.call_count == 1

    @pytest.mark.asyncio
    async def test_kiro_cli_update_execs_resolved_absolute_path(self):
        """The kiro-cli update spawns the RESOLVED path, never a bare argv0.

        A bare `"kiro-cli"` argv0 is re-resolved off the gateway's inherited
        `PATH` inside `exec`, and that `PATH` can lead with an agent-writable
        directory — so a planted shim would run unattended as the gateway user.
        Asserting the absolute path reached `create_subprocess_exec` is what
        pins the lookup to the resolver: with the bare name restored, argv0 is
        `"kiro-cli"` and this fails.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        _git_fake = _git_exec_fake()
        kiro_argvs: list[list[str]] = []

        async def _fake_exec(*args, **kwargs):
            argv = [a for a in args if isinstance(a, str)]
            if argv and Path(argv[0]).name == "kiro-cli":
                kiro_argvs.append(argv)
                proc = AsyncMock()
                proc.returncode = 0
                proc.wait = AsyncMock(return_value=0)
                proc.communicate = AsyncMock(return_value=(b"", b""))
                return proc
            return await _git_fake(*args, **kwargs)

        with patch("kiro_crew.env.is_toolbox_install", return_value=False):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
                with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                    with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0):
                        with patch.object(
                            GatewayOrchestrator, "_is_brazil_install", return_value=False
                        ):
                            with patch(
                                "kiro_crew.slack.gateway.build_frontend_async",
                                new_callable=AsyncMock,
                            ):
                                with patch("os.execv"):
                                    with patch(
                                        "kiro_crew.kiro_cli.resolve_kiro_cli",
                                        return_value="/opt/pinned/bin/kiro-cli",
                                    ):
                                        await orch._auto_apply_update()

        assert kiro_argvs, "the kiro-cli update spawn never happened"
        assert kiro_argvs[0][0] == "/opt/pinned/bin/kiro-cli"

    @pytest.mark.asyncio
    async def test_kiro_cli_update_resolves_without_inherited_path(self):
        """The candidate set excludes the inherited `PATH`.

        Pinning argv0 is not enough on its own: with the inherited `PATH` in
        the candidate set, a leading agent-writable directory still gets to
        name the binary this unattended path resolves to. Asserting the
        keyword is what pins that — the default is `True`, so a call that
        forgets it fails here.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        _git_fake = _git_exec_fake()

        async def _fake_exec(*args, **kwargs):
            argv = [a for a in args if isinstance(a, str)]
            if argv and Path(argv[0]).name == "kiro-cli":
                proc = AsyncMock()
                proc.returncode = 0
                proc.wait = AsyncMock(return_value=0)
                proc.communicate = AsyncMock(return_value=(b"", b""))
                return proc
            return await _git_fake(*args, **kwargs)

        with patch("kiro_crew.env.is_toolbox_install", return_value=False):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
                with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                    with patch("kiro_crew.dep_sync.sync_or_reinstall", return_value=0):
                        with patch.object(
                            GatewayOrchestrator, "_is_brazil_install", return_value=False
                        ):
                            with patch(
                                "kiro_crew.slack.gateway.build_frontend_async",
                                new_callable=AsyncMock,
                            ):
                                with patch("os.execv"):
                                    with patch(
                                        "kiro_crew.kiro_cli.resolve_kiro_cli",
                                        return_value="/opt/pinned/bin/kiro-cli",
                                    ) as mock_resolve:
                                        await orch._auto_apply_update()

        # The pin excludes the inherited PATH; the second call is the probe that
        # decides whether a PATH-only install is worth a log line.
        assert mock_resolve.call_args_list[0].kwargs == {"include_inherited_path": False}

    @pytest.mark.asyncio
    async def test_kiro_cli_update_skipped_when_unresolvable(self):
        """An unresolvable kiro-cli is SKIPPED, not exec'd by bare name.

        Fail-closed, matching what the git path does when `trusted_git_bin`
        returns `None`. The rest of the update is unaffected: this backend is
        optional, so the frontend build and dependency install still run.
        """
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.sessions = _mock_sessions()

        _git_fake = _git_exec_fake()
        kiro_argvs: list[list[str]] = []

        async def _fake_exec(*args, **kwargs):
            argv = [a for a in args if isinstance(a, str)]
            if argv and Path(argv[0]).name == "kiro-cli":
                kiro_argvs.append(argv)
            return await _git_fake(*args, **kwargs)

        with patch("kiro_crew.env.is_toolbox_install", return_value=False):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/tmp/proj"}):
                with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                    with patch(
                        "kiro_crew.dep_sync.sync_or_reinstall", return_value=0
                    ) as mock_install:
                        with patch.object(
                            GatewayOrchestrator, "_is_brazil_install", return_value=False
                        ):
                            with patch(
                                "kiro_crew.slack.gateway.build_frontend_async",
                                new_callable=AsyncMock,
                            ) as mock_build:
                                with patch("os.execv"):
                                    with patch(
                                        "kiro_crew.kiro_cli.resolve_kiro_cli",
                                        return_value=None,
                                    ):
                                        await orch._auto_apply_update()

        assert kiro_argvs == []
        mock_build.assert_awaited_once()
        assert mock_install.call_count == 1


class TestCheckMissingDepsPip:
    """Dep repair via pip install."""

    def test_pip_install_on_missing_dep(self, nonbundled_python_without_user_site):
        orch = _make_orchestrator()
        with patch("importlib.util.find_spec", return_value=None):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/proj"}):
                with patch.object(GatewayOrchestrator, "_is_brazil_install", return_value=False):
                    mock_exec = AsyncMock(return_value=_fake_async_proc(returncode=0))
                    with patch("asyncio.create_subprocess_exec", mock_exec):
                        asyncio.run(orch._check_missing_deps())
                    mock_exec.assert_awaited_once()
                    # Pin the command shape so a refactor cannot silently stop
                    # installing through this interpreter with user site disabled.
                    import sys as _sys

                    args = mock_exec.await_args.args
                    assert args[:5] == (
                        _sys.executable,
                        "-s",
                        "-m",
                        "pip",
                        "install",
                    )

    def test_pip_install_failure(self):
        orch = _make_orchestrator()
        with patch("importlib.util.find_spec", return_value=None):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/proj"}):
                with patch.object(GatewayOrchestrator, "_is_brazil_install", return_value=False):
                    mock_exec = AsyncMock(
                        return_value=_fake_async_proc(returncode=1, stderr=b"error")
                    )
                    with patch("asyncio.create_subprocess_exec", mock_exec):
                        asyncio.run(orch._check_missing_deps())  # should not raise

    def test_pip_install_timeout_kills_child(self):
        """A wedged pip is killed and reaped; boot continues without raising."""
        orch = _make_orchestrator()
        proc = MagicMock()
        proc.returncode = None
        proc.kill = MagicMock()

        async def _communicate():
            if proc.kill.called:
                return (b"", b"")  # post-kill reap returns immediately
            # flake-ok: pre-existing parked stub the timeout cancels, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- parked stub the code under test cancels, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(3600)  # hang until wait_for cancels us
            return (b"", b"")

        proc.communicate = MagicMock(side_effect=_communicate)
        with patch("importlib.util.find_spec", return_value=None):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/proj"}):
                with patch.object(GatewayOrchestrator, "_is_brazil_install", return_value=False):
                    with patch.object(GatewayOrchestrator, "_DEP_INSTALL_TIMEOUT_SECS", 0.05):
                        with patch(
                            "asyncio.create_subprocess_exec",
                            AsyncMock(return_value=proc),
                        ):
                            asyncio.run(orch._check_missing_deps())  # no raise
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_version_probe_transport_error_does_not_abort_boot(self):
        """_warn_if_kiro_cli_outdated must never raise.

        A pipe/transport error from communicate() propagating out of this
        helper would abort run() before the dashboard binds. The child is
        still killed+reaped best-effort.
        """
        calls = {"n": 0}

        async def _communicate():
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("broken pipe")
            return (b"", b"")  # the post-kill reap

        proc = MagicMock()
        proc.returncode = None
        proc.kill = MagicMock()
        proc.communicate = MagicMock(side_effect=_communicate)
        orch = _make_orchestrator()
        with patch("kiro_crew.kiro_cli.resolve_kiro_cli", return_value="/opt/pinned/bin/kiro-cli"):
            with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
                await orch._warn_if_kiro_cli_outdated()  # must not raise
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_pip_install_cancellation_kills_child(self):
        """Gateway shutdown mid-install must not orphan pip.

        A leaked pip would race the NEXT boot's install of the same
        distributions — the half-installed state this repair path exists to
        fix.
        """
        proc = MagicMock()
        proc.returncode = None
        proc.kill = MagicMock()

        async def _communicate():
            # flake-ok: pre-existing parked stub the cancellation unblocks, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- parked stub the test cancels, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(3600)  # hang until cancelled
            return (b"", b"")

        proc.communicate = MagicMock(side_effect=_communicate)
        orch = _make_orchestrator()
        with patch("importlib.util.find_spec", return_value=None):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/proj"}):
                with patch.object(GatewayOrchestrator, "_is_brazil_install", return_value=False):
                    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
                        task = asyncio.create_task(orch._check_missing_deps())
                        # flake-ok: pre-existing sleep barrier before the cancel, moved verbatim; tracked for a de-flake pass
                        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
                        await asyncio.sleep(0.05)  # let it reach the await
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            # flake-ok: pre-existing unbounded await of the cancelled task, moved verbatim; tracked for a de-flake pass
                            await task
        proc.kill.assert_called_once()
