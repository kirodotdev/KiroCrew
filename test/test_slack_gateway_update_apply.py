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
import contextlib
import inspect
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from real_dashboard_state import _settle_real_dashboard_states  # noqa: F401
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import (
    _fake_async_proc,
    _install_effect,
    _make_orchestrator,
    _mock_dashboard_state,
)

from kiro_crew import platform_compat
from kiro_crew.slack import gateway as gw
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.testing.wait import async_wait_until, await_until, finished

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

        `available` is true on commit distance alone, but this path runs
        `git reset --hard`, so acting on it would reset a developer's checkout after any
        upstream commit. The badge is lit instead.
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
    async def test_a_version_bump_is_announced_not_applied_to_a_running_gateway(self):
        """A git reset rewrites the tree the gateway serves from, so a release moving
        lights the badge and a person's Update applies it."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
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
        orch._auto_apply_update.assert_not_awaited()
        ds.push_refresh.assert_called_with("update_available")

    @pytest.mark.asyncio
    async def test_min_version_mandate_without_a_newer_build_notifies(self):
        """A below-floor git checkout with no newer build must NOT hard-reset.

        `update_available` is true on any commit distance, so resetting on it would drag
        the checkout to every intermediate commit without `__version__` reaching the floor.
        The mandatory branch reads `version_newer` and notifies instead.
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
    async def test_min_version_mandate_on_a_git_checkout_keeps_its_notice_and_applies_nothing(self):
        """The floor is a prominent notice, not a reset of the running checkout."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch._auto_apply_update = AsyncMock()
        import kiro_crew.dashboard.handlers as _h

        _h._update_info.clear()
        _h._update_info.update({"update_available": True, "can_apply": True, "version_newer": True})
        with patch.object(_h, "_do_update_check", new_callable=AsyncMock):
            with patch("kiro_crew.platform.update_governance.update_required", return_value=True):
                with _install_effect("mandatory"):
                    await orch._check_for_updates()
        orch._auto_apply_update.assert_not_awaited()
        ds.push_refresh.assert_called_with("update_available")

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

    # _check_console_script

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

    def test_check_console_script_probes_the_checkout_off_the_event_loop(self, tmp_path):
        """The startup probe stats and reads the checkout, which may be on a network
        mount, so none of it may run on the loop thread that serves every session."""
        (tmp_path / ".install-method").write_text("pip")
        venv_py = gw.dep_sync.project_venv_python(tmp_path)
        script = gw.dep_sync.console_script_path(venv_py)
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        orch = _make_orchestrator()
        seen: dict[str, int] = {}
        real_is_brazil = GatewayOrchestrator._is_brazil_install
        real_read_text = Path.read_text
        real_access = gw.os.access
        read_kwargs: dict = {}

        def is_brazil(proj):
            seen["is_brazil"] = threading.get_ident()
            return real_is_brazil(proj)

        def read_text(self_path, *args, **kwargs):
            if self_path.name == ".install-method":
                seen["read_method"] = threading.get_ident()
                read_kwargs.update(kwargs)
            return real_read_text(self_path, *args, **kwargs)

        def access(path, mode, *args, **kwargs):
            if Path(path) == script:
                seen["access"] = threading.get_ident()
            return real_access(path, mode, *args, **kwargs)

        async def run() -> int:
            loop_thread = threading.get_ident()
            await orch._check_console_script()
            return loop_thread

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}, clear=False),
            patch.object(GatewayOrchestrator, "_is_brazil_install", side_effect=is_brazil),
            patch.object(Path, "read_text", read_text),
            patch.object(gw.os, "access", side_effect=access),
            patch.object(gw, "sandboxed_spawn_argv_async", new_callable=AsyncMock) as spawn,
        ):
            loop_thread = asyncio.run(run())

        # The script is present and executable, so this is the healthy no-op path.
        spawn.assert_not_awaited()
        assert set(seen) == {"is_brazil", "read_method", "access"}
        assert all(ident != loop_thread for ident in seen.values()), seen
        # The marker is ASCII we wrote; decode it as such, not in the locale code page.
        assert read_kwargs.get("encoding") == "utf-8"

    def test_check_console_script_resolves_the_repair_module_off_the_event_loop(self, tmp_path):
        """On the repair path the dep_sync module path is resolved in the probe's
        thread too, not on the loop before the sandboxed spawn."""
        (tmp_path / ".install-method").write_text("pip")
        gw.dep_sync.project_venv_python(tmp_path).parent.mkdir(parents=True)
        orch = _make_orchestrator()
        dep_sync_file = Path(gw.dep_sync.__file__)
        real_resolve = Path.resolve
        seen: list[int] = []

        def resolve(self_path, *args, **kwargs):
            if self_path == dep_sync_file:
                seen.append(threading.get_ident())
            return real_resolve(self_path, *args, **kwargs)

        async def fake_prepare(cmd, **kwargs):
            return list(cmd), {}, None

        async def run() -> int:
            loop_thread = threading.get_ident()
            await orch._check_console_script()
            return loop_thread

        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}, clear=False),
            patch.object(Path, "resolve", resolve),
            patch.object(gw, "sandboxed_spawn_argv_async", side_effect=fake_prepare),
            patch.object(
                gw,
                "create_subprocess_limited",
                new_callable=AsyncMock,
                return_value=_fake_async_proc(),
            ) as spawn,
        ):
            loop_thread = asyncio.run(run())

        spawn.assert_awaited_once()
        assert seen and all(ident != loop_thread for ident in seen), seen

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

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX process groups")
    @pytest.mark.parametrize("repair", ["_check_missing_deps", "_check_console_script"])
    @pytest.mark.asyncio
    async def test_a_cancel_while_a_repair_child_starts_kills_its_group(
        self, repair, tmp_path, monkeypatch
    ):
        """A cancel that lands while the boot pip repair or the console-script repair is
        still starting kills that child's whole group.

        asyncio forks the child before it connects the pipes, and a cancel there
        SIGKILLs the leader alone, so the grandchild below would outlive the repair.
        """
        from asyncio import base_subprocess

        released = asyncio.Event()
        connect_pipes = base_subprocess.BaseSubprocessTransport._connect_pipes

        async def _held_connect_pipes(transport, waiter):
            await released.wait()
            await connect_pipes(transport, waiter)

        monkeypatch.setattr(
            base_subprocess.BaseSubprocessTransport, "_connect_pipes", _held_connect_pipes
        )
        real_exec = asyncio.create_subprocess_exec
        leader = tmp_path / "leader"
        # The grandchild is forked before the leader writes its pid, so the
        # group already has a second member when the test cancels; it outlives
        # every wait below, so only the group kill can empty the group.
        script = 'sleep 600 & echo $$ > "$1.tmp" && mv "$1.tmp" "$1"; wait'

        async def _exec(*_argv, **kwargs):
            return await real_exec("/bin/sh", "-c", script, "sh", str(leader), **kwargs)

        async def _prepare(cmd, **_kwargs):
            return list(cmd), None, None

        def _leader_pgid() -> int | None:
            return int(leader.read_text(encoding="utf-8")) if leader.is_file() else None

        (tmp_path / ".install-method").write_text("pip")
        gw.dep_sync.project_venv_python(tmp_path).parent.mkdir(parents=True)
        orch = _make_orchestrator()
        orch._REQUIRED_DEPS = [("kirocrew_test_absent_module", "absent-dist")]
        with (
            patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(tmp_path)}),
            patch("asyncio.create_subprocess_exec", side_effect=_exec),
            patch.object(gw, "create_subprocess_limited", side_effect=_exec),
            patch.object(gw, "sandboxed_spawn_argv_async", side_effect=_prepare),
        ):
            task = asyncio.create_task(getattr(orch, repair)())
            try:
                pgid = await async_wait_until(
                    _leader_pgid,
                    off_loop=True,
                    describe=lambda: f"the {repair} child never started",
                )
            finally:
                task.cancel()
                released.set()
            try:
                with pytest.raises(asyncio.CancelledError):
                    await finished(task, "the cancelled repair")
                await async_wait_until(
                    lambda: not platform_compat.pgroup_exists(pgid),
                    off_loop=True,
                    describe=lambda: f"the {repair} child's group outlived the cancel",
                )
            finally:
                # Only a group that still has a member is ours: an emptied pgid can be reused.
                if platform_compat.pgroup_exists(pgid):
                    with contextlib.suppress(OSError):
                        platform_compat.kill_process_group(pgid, platform_compat.SIGKILL)

    @pytest.mark.asyncio
    async def test_check_console_script_is_sandbox_routed_and_limited(self, tmp_path):
        """The repair spawn MUST route through the sandbox chokepoint.

        The child runs the project's own venv interpreter, so it needs OS isolation, a
        scrubbed env and the resource ceiling. ``create_subprocess_exec`` is poisoned.
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
