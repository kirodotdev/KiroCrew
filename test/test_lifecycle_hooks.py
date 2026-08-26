"""Property tests for Lifecycle Hook Dispatcher.

Feature: app-sdk-gateway-hooks
Properties 9, 14: Deterministic ordering and shell-before-Python.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.lifecycle import LifecycleDispatcher
from kiro_crew.apps.manifest import app_name_error

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_info(name: str, *, on_startup: str = "", on_shutdown: str = "") -> dict[str, Any]:
    """Create a minimal app info dict with hooks."""
    return {
        "name": name,
        "enabled": True,
        "manifest": {
            "backend": {
                "hooks": {
                    "on_startup": on_startup,
                    "on_shutdown": on_shutdown,
                }
            },
            "permissions": {},
        },
    }


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

def _app_names() -> st.SearchStrategy[list[str]]:
    """Generate lists of unique app names the admission contract actually accepts.

    The dispatcher creates ``apps/<name>/data`` for every app it starts, so a
    name production would never admit describes an impossible state rather than
    a bug worth finding. The regex is the kebab-case grammar narrowed to a
    leading letter and the original 3-11 char range, which keeps the filter
    rejection rate near zero; ``app_name_error`` then removes the reserved and
    unportable names, so this file never carries a second copy of that list.
    """
    return st.lists(
        st.from_regex(r"[a-z][a-z0-9]{2,6}(?:-[a-z0-9]{1,3})?", fullmatch=True).filter(
            lambda name: app_name_error(name) is None
        ),
        min_size=2,
        max_size=8,
        unique=True,
    )


def test_generator_cannot_sample_an_inadmissible_app_name() -> None:
    """Deterministic guard for the sampling domain.

    ``dispatch_startup`` creates ``apps/<name>/data`` for every app it starts.
    ``nul`` is kebab-case and inside the length range, so the grammar alone still
    reaches it — on Windows that mkdir fails with WinError 3. The dispatcher is
    not the bug: the admission contract that let such an app exist was, and it
    now refuses the name, so this strategy must not invent one either. The
    exclusion is delegated to ``app_name_error`` rather than restated, so the
    test domain cannot drift away from what production admits.
    """
    grammar = r"[a-z][a-z0-9]{2,6}(?:-[a-z0-9]{1,3})?"
    assert re.fullmatch(grammar, "nul"), "grammar no longer reaches the name under test"
    assert app_name_error("nul") is not None, "production must refuse it first"
    assert app_name_error("null-app") is None, "ordinary names stay in the domain"


# ---------------------------------------------------------------------------
# Property 9: Lifecycle hook invocation order is deterministic
# ---------------------------------------------------------------------------


class TestLifecycleHookOrdering:
    """Property 9: Lifecycle hook invocation order is deterministic.

    **Validates: Requirements 4.5**
    """

    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(names=_app_names())
    def test_startup_order_is_lexicographic(
        self, names: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Startup hooks are invoked in lexicographic order by app name."""
        import uuid
        from unittest.mock import patch

        work_dir = tmp_path / uuid.uuid4().hex
        work_dir.mkdir()
        # dispatch_startup → _build_context → app_dir(name)/"data".mkdir() resolves
        # against config_dir() == ~/.kirocrew unless KIROCREW_HOME is isolated. Each
        # generated name would otherwise leak a real apps/<name>/data/ dir (one per
        # hypothesis example → thousands over a dev's test history). Pin it to tmp.
        monkeypatch.setenv("KIROCREW_HOME", str(work_dir))

        apps = [_make_app_info(n, on_startup="backend.hooks:on_startup") for n in names]
        dispatcher = LifecycleDispatcher()

        # Track invocation order by patching _invoke
        invocation_order: list[str] = []

        async def tracking_invoke(
            app_name: str, hook_path: str, ctx: Any, *, phase: str = ""
        ) -> bool:
            invocation_order.append(app_name)
            return True

        loop = asyncio.new_event_loop()
        try:
            with patch.object(dispatcher, "_invoke", side_effect=tracking_invoke):
                loop.run_until_complete(dispatcher.dispatch_startup(apps))
        finally:
            loop.close()

        assert invocation_order == sorted(names)

    def test_shutdown_order_is_reverse_lexicographic(self) -> None:
        """Shutdown hooks are invoked in reverse lexicographic order."""
        from unittest.mock import patch

        names = ["alpha", "beta", "gamma", "delta"]
        apps = [_make_app_info(n, on_shutdown="backend.hooks:on_shutdown") for n in names]
        dispatcher = LifecycleDispatcher()

        invocation_order: list[str] = []

        async def tracking_invoke(
            app_name: str, hook_path: str, ctx: Any, *, phase: str = ""
        ) -> bool:
            invocation_order.append(app_name)
            return True

        loop = asyncio.new_event_loop()
        try:
            with patch.object(dispatcher, "_invoke", side_effect=tracking_invoke):
                loop.run_until_complete(dispatcher.dispatch_shutdown(apps))
        finally:
            loop.close()

        assert invocation_order == sorted(names, reverse=True)


# ---------------------------------------------------------------------------
# Property 14: Shell-before-Python hook ordering
# ---------------------------------------------------------------------------


class TestShellBeforePython:
    """Property 14: Shell-before-Python hook ordering.

    **Validates: Requirements 7.4**

    This property is enforced by handle_enable_app in routes.py which:
    1. Runs _run_lifecycle_script(on_enable) first (shell)
    2. Then calls on_app_enable() (Python hooks)
    """

    @pytest.mark.asyncio
    async def test_shell_runs_before_python_on_enable(self) -> None:
        """Shell script executes before Python hook during enable.

        Mocks both _run_lifecycle_script and on_app_enable in the
        handle_enable_app flow and asserts shell is called first.
        """
        import sys
        from unittest.mock import MagicMock, patch

        call_order: list[str] = []

        async def mock_shell(*args, **kwargs):
            call_order.append("shell")
            return {"output": "", "failed": False}

        async def mock_python(*args, **kwargs):
            call_order.append("python")
            return None

        fake_app_info = {
            "name": "test-app",
            "manifest": {"setup": {"onEnable": "echo hello"}},
            "resources": "gateway",
            "enabled": True,
        }

        # Pre-mock dashboard.server to avoid circular import with mimir
        if "kiro_crew.dashboard.server" not in sys.modules:
            sys.modules["kiro_crew.dashboard.server"] = MagicMock()

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=fake_app_info),
            patch("kiro_crew.apps.routes.enable_app", return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True})),
            patch("kiro_crew.apps.routes.register_app", return_value=MagicMock(to_dict=lambda: {})),
            patch("kiro_crew.apps.routes.start_app_backend", return_value=None),
            patch("kiro_crew.apps.routes._run_lifecycle_script", side_effect=mock_shell),
            patch("kiro_crew.apps.routes.on_app_enable", side_effect=mock_python),
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
        ):
            from kiro_crew.apps.routes import handle_enable_app

            # Build a minimal fake request
            request = MagicMock()
            request.match_info = {"name": "test-app"}
            request.app = {"state": MagicMock()}

            await handle_enable_app(request)

        assert call_order == ["shell", "python"], f"Expected shell before python, got: {call_order}"


# ---------------------------------------------------------------------------
# Additional unit tests
# ---------------------------------------------------------------------------


class TestLifecycleDispatcherEdgeCases:
    """Edge case tests for LifecycleDispatcher."""

    def test_no_hooks_declared_is_noop(self) -> None:
        """Apps without hooks are silently skipped."""
        apps = [{"name": "no-hooks", "manifest": {"backend": {}}, "enabled": True}]
        dispatcher = LifecycleDispatcher()

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(dispatcher.dispatch_startup(apps))
            assert result == []
        finally:
            loop.close()

    def test_empty_app_list_is_noop(self) -> None:
        """Empty app list produces no invocations."""
        dispatcher = LifecycleDispatcher()
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(dispatcher.dispatch_startup([]))
            assert result == []
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Per-hook timeout at the dispatch boundary (issue #5443)
# ---------------------------------------------------------------------------


class TestLifecycleHookTimeout:
    """A hung async hook is bounded, cancelled, observed, and does not block peers.

    See app-kit-platform.md §7.1. These exercise the real ``_invoke`` (including
    the SEL record and health bookkeeping); only ``_resolve_hook`` — the import —
    is stubbed, so the timeout/cancellation path is under test, not mocked away.
    """

    @staticmethod
    def _app_info(name: str, hook: str = "backend.hooks:on_startup") -> dict[str, Any]:
        return _make_app_info(name, on_startup=hook)

    @pytest.fixture(autouse=True)
    def _isolate_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # _build_context → app_dir(name)/"data".mkdir(); pin it off the real home.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))

    def test_gateway_cleanup_does_not_spend_shutdown_hook_budget(self) -> None:
        """Gateway cleanup must check retained startup ownership without waiting."""
        import kiro_crew.apps.lifecycle as lifecycle_mod

        assert lifecycle_mod._GATEWAY_STARTUP_CLEANUP_TIMEOUT_SEC == 0.0

    @pytest.mark.asyncio
    async def test_gateway_shutdown_runs_after_retained_startup_already_settled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A settled retained startup permits its app's shutdown hook."""
        import kiro_crew.apps.lifecycle as lifecycle_mod

        monkeypatch.setattr(lifecycle_mod, "_HOOK_TIMEOUT_SEC", 0.05)
        release = asyncio.Event()
        events: list[str] = []

        async def startup(_ctx: Any) -> None:
            events.append("startup-start")
            await release.wait()
            events.append("startup-finish")

        async def shutdown(_ctx: Any) -> None:
            events.append("shutdown")

        dispatcher = LifecycleDispatcher()
        monkeypatch.setattr(
            dispatcher,
            "_resolve_hook",
            lambda _name, path: startup if path == "hooks:on_startup" else shutdown,
        )
        app = _make_app_info(
            "ordered-app",
            on_startup="hooks:on_startup",
            on_shutdown="hooks:on_shutdown",
        )

        assert await dispatcher.dispatch_startup([app]) == []
        assert events == ["startup-start"]
        release.set()
        assert await asyncio.wait_for(
            dispatcher.stop_detached_startup_hooks("ordered-app"), timeout=1
        )

        assert await dispatcher.dispatch_shutdown([app]) == ["ordered-app"]
        assert events == ["startup-start", "startup-finish", "shutdown"]

    @pytest.mark.asyncio
    async def test_gateway_shutdown_skips_hook_when_startup_remains_owned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bounded ownership failure must not overlap shutdown with startup."""
        import kiro_crew.apps.lifecycle as lifecycle_mod

        monkeypatch.setattr(lifecycle_mod, "_HOOK_TIMEOUT_SEC", 0.02)
        monkeypatch.setattr(
            lifecycle_mod, "_GATEWAY_STARTUP_CLEANUP_TIMEOUT_SEC", 0.02
        )
        release = asyncio.Event()
        shutdown_called = False

        async def startup(_ctx: Any) -> None:
            await release.wait()

        async def shutdown(_ctx: Any) -> None:
            nonlocal shutdown_called
            shutdown_called = True

        dispatcher = LifecycleDispatcher()
        monkeypatch.setattr(
            dispatcher,
            "_resolve_hook",
            lambda _name, path: startup if path == "hooks:on_startup" else shutdown,
        )
        app = _make_app_info(
            "still-starting-app",
            on_startup="hooks:on_startup",
            on_shutdown="hooks:on_shutdown",
        )

        assert await dispatcher.dispatch_startup([app]) == []
        try:
            assert await asyncio.wait_for(
                dispatcher.dispatch_shutdown([app]), timeout=0.25
            ) == []
            assert shutdown_called is False
        finally:
            release.set()
            await asyncio.wait_for(
                dispatcher.stop_detached_startup_hooks("still-starting-app"),
                timeout=1,
            )

    @pytest.mark.asyncio
    async def test_gateway_shutdown_sweeps_all_apps_under_one_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ownership checks include hookless apps and begin concurrently."""
        import kiro_crew.apps.lifecycle as lifecycle_mod

        monkeypatch.setattr(
            lifecycle_mod, "_GATEWAY_STARTUP_CLEANUP_TIMEOUT_SEC", 0.05
        )
        started: set[str] = set()
        all_started = asyncio.Event()
        invoked: list[str] = []

        dispatcher = LifecycleDispatcher()

        async def stop(
            app_name: str, *, bounded: bool = False, timeout: float | None = None
        ) -> bool:
            assert bounded is True
            assert timeout == 0.05
            started.add(app_name)
            if len(started) == 2:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=0.2)
            return True

        async def invoke(
            app_name: str, _hook_path: str, _ctx: Any, *, phase: str
        ) -> bool:
            assert phase == "shutdown"
            invoked.append(app_name)
            return True

        monkeypatch.setattr(dispatcher, "stop_detached_startup_hooks", stop)
        monkeypatch.setattr(dispatcher, "_invoke", invoke)
        hookless = _make_app_info("a-hookless")
        with_hook = _make_app_info("b-hook", on_shutdown="hooks:on_shutdown")

        assert await asyncio.wait_for(
            dispatcher.dispatch_shutdown([hookless, with_hook]), timeout=0.3
        ) == ["b-hook"]
        assert started == {"a-hookless", "b-hook"}
        assert invoked == ["b-hook"]

    @pytest.mark.asyncio
    async def test_hung_async_hook_times_out_and_is_reported_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A startup hook can outlive readiness but remains owned until completion."""
        import kiro_crew.apps.lifecycle as lifecycle_mod

        monkeypatch.setattr(lifecycle_mod, "_HOOK_TIMEOUT_SEC", 0.05)

        started = asyncio.Event()
        release = asyncio.Event()

        async def hangs(_ctx: Any) -> None:
            started.set()
            await release.wait()

        dispatcher = LifecycleDispatcher()
        monkeypatch.setattr(dispatcher, "_resolve_hook", lambda *_a, **_k: hangs)
        ctx = dispatcher._build_context(self._app_info("hang-app"))

        ok = await dispatcher._invoke("hang-app", "backend.hooks:on_startup", ctx, phase="startup")

        assert ok is False
        assert started.is_set(), "hook must actually have started before timing out"
        assert "hang-app" in lifecycle_mod._DETACHED_HOOK_TASKS
        assert ctx.health.status == "degraded"
        assert any("timed out" in issue for issue in ctx.health.to_dict()["issues"])

        release.set()
        assert await asyncio.wait_for(
            dispatcher.stop_detached_startup_hooks("hang-app"), timeout=1
        )
        await asyncio.sleep(0)
        assert not lifecycle_mod._DETACHED_HOOK_TASKS

    @pytest.mark.asyncio
    async def test_timeout_does_not_cancel_cooperative_hook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Timeout preserves ownership instead of asking the hook to cancel."""
        import kiro_crew.apps.lifecycle as lifecycle_mod

        monkeypatch.setattr(lifecycle_mod, "_HOOK_TIMEOUT_SEC", 0.05)

        release = asyncio.Event()
        finished = asyncio.Event()
        saw_cancel = False

        async def waits_for_release(_ctx: Any) -> None:
            nonlocal saw_cancel
            try:
                await release.wait()
            except asyncio.CancelledError:
                saw_cancel = True
                raise
            finally:
                finished.set()

        dispatcher = LifecycleDispatcher()
        monkeypatch.setattr(dispatcher, "_resolve_hook", lambda *_a, **_k: waits_for_release)
        ctx = dispatcher._build_context(self._app_info("cleanup-app"))

        ok = await dispatcher._invoke(
            "cleanup-app", "backend.hooks:on_startup", ctx, phase="startup"
        )

        assert ok is False
        assert saw_cancel is False
        assert "cleanup-app" in lifecycle_mod._DETACHED_HOOK_TASKS
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1)
        assert await asyncio.wait_for(
            dispatcher.stop_detached_startup_hooks("cleanup-app"), timeout=1
        )
        await asyncio.sleep(0)
        assert not lifecycle_mod._DETACHED_HOOK_TASKS

    @pytest.mark.asyncio
    async def test_retry_refuses_while_startup_hook_remains_owned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A timed-out startup task remains the sole invocation until it exits."""
        from unittest.mock import MagicMock

        import kiro_crew.apps.lifecycle as lifecycle_mod

        monkeypatch.setattr(lifecycle_mod, "_HOOK_TIMEOUT_SEC", 0.05)
        audit = MagicMock()
        monkeypatch.setattr(lifecycle_mod, "sel", lambda: audit)
        release = asyncio.Event()
        invocations = 0

        async def retained(_ctx: Any) -> None:
            nonlocal invocations
            invocations += 1
            await release.wait()

        dispatcher = LifecycleDispatcher()
        monkeypatch.setattr(dispatcher, "_resolve_hook", lambda *_a, **_k: retained)
        first_ctx = dispatcher._build_context(self._app_info("retry-app"))
        retry_ctx = dispatcher._build_context(self._app_info("retry-app"))

        try:
            assert await dispatcher._invoke(
                "retry-app", "backend.hooks:on_startup", first_ctx, phase="startup"
            ) is False
            assert invocations == 1
            assert len(lifecycle_mod._DETACHED_HOOK_TASKS["retry-app"]) == 1

            assert await dispatcher._invoke(
                "retry-app", "backend.hooks:on_startup", retry_ctx, phase="startup"
            ) is False
            assert invocations == 1
            assert len(lifecycle_mod._DETACHED_HOOK_TASKS["retry-app"]) == 1
            assert any(
                call.kwargs.get("outcome") == "failed"
                and call.kwargs.get("error") == "retained startup hook is already active"
                for call in audit.log_api_access.call_args_list
            )
        finally:
            release.set()
            await asyncio.wait_for(
                dispatcher.stop_detached_startup_hooks("retry-app"), timeout=1
            )
            await asyncio.sleep(0)

        assert "retry-app" not in lifecycle_mod._DETACHED_HOOK_TASKS

    @pytest.mark.asyncio
    async def test_to_thread_worker_remains_owned_until_worker_finishes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A terminal awaiter must never hide a still-running app worker thread."""
        import threading

        import kiro_crew.apps.lifecycle as lifecycle_mod

        monkeypatch.setattr(lifecycle_mod, "_HOOK_TIMEOUT_SEC", 0.05)
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def worker() -> None:
            started.set()
            release.wait()
            finished.set()

        async def waits_on_worker(_ctx: Any) -> None:
            await asyncio.to_thread(worker)

        dispatcher = LifecycleDispatcher()
        monkeypatch.setattr(dispatcher, "_resolve_hook", lambda *_a, **_k: waits_on_worker)
        ctx = dispatcher._build_context(self._app_info("thread-app"))

        try:
            ok = await dispatcher._invoke(
                "thread-app", "backend.hooks:on_startup", ctx, phase="startup"
            )

            assert ok is False
            assert started.is_set()
            assert not finished.is_set()
            assert "thread-app" in lifecycle_mod._DETACHED_HOOK_TASKS
            assert await dispatcher.stop_detached_startup_hooks(
                "thread-app", bounded=True, timeout=0.05
            ) is False
            assert "thread-app" in lifecycle_mod._DETACHED_HOOK_TASKS
        finally:
            release.set()
            if started.is_set():
                await asyncio.to_thread(finished.wait, 1)
            await asyncio.wait_for(
                dispatcher.stop_detached_startup_hooks("thread-app"), timeout=1
            )
            await asyncio.sleep(0)

        assert not lifecycle_mod._DETACHED_HOOK_TASKS

    @pytest.mark.asyncio
    async def test_pre_timeout_cancelled_thread_worker_leaves_residual_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Child cancellation before the deadline must remain fail-closed."""
        import threading

        import kiro_crew.apps.lifecycle as lifecycle_mod

        monkeypatch.setattr(lifecycle_mod, "_HOOK_TIMEOUT_SEC", 1.0)
        app_name = "early-cancel-app"
        lifecycle_mod._DETACHED_HOOK_RESIDUALS.discard(app_name)
        worker_started = threading.Event()
        worker_release = threading.Event()
        worker_finished = threading.Event()

        def worker() -> None:
            worker_started.set()
            worker_release.wait()
            worker_finished.set()

        async def self_cancelling_hook(_ctx: Any) -> None:
            task = asyncio.current_task()
            assert task is not None
            asyncio.get_running_loop().call_soon(task.cancel)
            await asyncio.to_thread(worker)

        dispatcher = LifecycleDispatcher()
        monkeypatch.setattr(
            dispatcher, "_resolve_hook", lambda *_a, **_k: self_cancelling_hook
        )
        ctx = dispatcher._build_context(self._app_info(app_name))

        try:
            assert await dispatcher._invoke(
                app_name, "backend.hooks:on_startup", ctx, phase="startup"
            ) is False
            assert await asyncio.to_thread(worker_started.wait, 1)
            await asyncio.sleep(0)
            assert app_name in lifecycle_mod._DETACHED_HOOK_RESIDUALS
            assert app_name not in lifecycle_mod._DETACHED_HOOK_TASKS
            assert worker_finished.is_set() is False
            assert await dispatcher.stop_detached_startup_hooks(
                app_name, bounded=True, timeout=0.01
            ) is False
        finally:
            worker_release.set()
            await asyncio.to_thread(worker_finished.wait, 1)
            lifecycle_mod._DETACHED_HOOK_TASKS.pop(app_name, None)
            lifecycle_mod._DETACHED_HOOK_RESIDUALS.discard(app_name)

    @pytest.mark.asyncio
    async def test_parent_cancellation_propagates_while_startup_remains_owned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancelling the dispatcher must not cancel or orphan its child hook."""
        import threading

        import kiro_crew.apps.lifecycle as lifecycle_mod

        monkeypatch.setattr(lifecycle_mod, "_HOOK_TIMEOUT_SEC", 1.0)
        app_name = "parent-cancel-app"
        worker_started = threading.Event()
        worker_release = threading.Event()
        worker_finished = threading.Event()

        def worker() -> None:
            worker_started.set()
            worker_release.wait()
            worker_finished.set()

        async def waits_on_worker(_ctx: Any) -> None:
            await asyncio.to_thread(worker)

        dispatcher = LifecycleDispatcher()
        monkeypatch.setattr(
            dispatcher, "_resolve_hook", lambda *_a, **_k: waits_on_worker
        )
        ctx = dispatcher._build_context(self._app_info(app_name))
        invoke_task = asyncio.create_task(
            dispatcher._invoke(
                app_name, "backend.hooks:on_startup", ctx, phase="startup"
            )
        )

        try:
            assert await asyncio.to_thread(worker_started.wait, 1)
            assert app_name in lifecycle_mod._DETACHED_HOOK_TASKS
            invoke_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await invoke_task
            assert app_name in lifecycle_mod._DETACHED_HOOK_TASKS
            assert app_name not in lifecycle_mod._DETACHED_HOOK_RESIDUALS
            assert await dispatcher.stop_detached_startup_hooks(
                app_name, bounded=True, timeout=0.01
            ) is False
        finally:
            worker_release.set()
            await asyncio.to_thread(worker_finished.wait, 1)
            await asyncio.wait_for(
                dispatcher.stop_detached_startup_hooks(app_name), timeout=1
            )
            lifecycle_mod._DETACHED_HOOK_TASKS.pop(app_name, None)
            lifecycle_mod._DETACHED_HOOK_RESIDUALS.discard(app_name)

    @pytest.mark.asyncio
    async def test_cancelled_retained_hook_leaves_fail_closed_residual_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Self-cancellation cannot make a live to_thread worker look stopped."""
        import threading

        import kiro_crew.apps.lifecycle as lifecycle_mod

        monkeypatch.setattr(lifecycle_mod, "_HOOK_TIMEOUT_SEC", 0.05)
        app_name = "cancel-app"
        lifecycle_mod._DETACHED_HOOK_RESIDUALS.discard(app_name)
        arm_cancel = asyncio.Event()
        worker_started = threading.Event()
        worker_release = threading.Event()
        worker_finished = threading.Event()

        def worker() -> None:
            worker_started.set()
            worker_release.wait()
            worker_finished.set()

        async def self_cancelling_hook(_ctx: Any) -> None:
            task = asyncio.current_task()
            assert task is not None
            await arm_cancel.wait()
            asyncio.get_running_loop().call_soon(task.cancel)
            await asyncio.to_thread(worker)

        dispatcher = LifecycleDispatcher()
        monkeypatch.setattr(
            dispatcher, "_resolve_hook", lambda *_a, **_k: self_cancelling_hook
        )
        ctx = dispatcher._build_context(self._app_info(app_name))

        try:
            assert await dispatcher._invoke(
                app_name, "backend.hooks:on_startup", ctx, phase="startup"
            ) is False
            assert app_name in lifecycle_mod._DETACHED_HOOK_TASKS

            arm_cancel.set()
            assert await asyncio.to_thread(worker_started.wait, 1)
            for _ in range(10):
                await asyncio.sleep(0)
                if app_name in lifecycle_mod._DETACHED_HOOK_RESIDUALS:
                    break

            assert app_name in lifecycle_mod._DETACHED_HOOK_RESIDUALS
            assert app_name not in lifecycle_mod._DETACHED_HOOK_TASKS
            assert worker_finished.is_set() is False
            assert await dispatcher.stop_detached_startup_hooks(
                app_name, bounded=True, timeout=0.01
            ) is False

            worker_release.set()
            assert await asyncio.to_thread(worker_finished.wait, 1)
            # Actual worker completion cannot be inferred from the cancelled
            # asyncio wrapper, so ownership remains fail-closed.
            assert await dispatcher.stop_detached_startup_hooks(app_name) is False
        finally:
            worker_release.set()
            await asyncio.to_thread(worker_finished.wait, 1)
            lifecycle_mod._DETACHED_HOOK_TASKS.pop(app_name, None)
            lifecycle_mod._DETACHED_HOOK_RESIDUALS.discard(app_name)

    @pytest.mark.asyncio
    async def test_hook_continuing_after_deadline_cannot_extend_readiness(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Readiness proceeds, while teardown still waits for true completion."""
        import kiro_crew.apps.lifecycle as lifecycle_mod

        monkeypatch.setattr(lifecycle_mod, "_HOOK_TIMEOUT_SEC", 0.05)
        release = asyncio.Event()
        finished = asyncio.Event()

        async def waits_for_release(_ctx: Any) -> None:
            await release.wait()
            finished.set()

        dispatcher = LifecycleDispatcher()
        monkeypatch.setattr(dispatcher, "_resolve_hook", lambda *_a, **_k: waits_for_release)
        ctx = dispatcher._build_context(self._app_info("stubborn-app"))

        ok = await asyncio.wait_for(
            dispatcher._invoke(
                "stubborn-app", "backend.hooks:on_startup", ctx, phase="startup"
            ),
            timeout=0.25,
        )

        assert ok is False
        assert "stubborn-app" in lifecycle_mod._DETACHED_HOOK_TASKS

        stopped = await dispatcher.stop_detached_startup_hooks(
            "stubborn-app", bounded=True, timeout=0.05
        )
        assert stopped is False
        assert "stubborn-app" in lifecycle_mod._DETACHED_HOOK_TASKS

        ordinary_stop = asyncio.create_task(
            dispatcher.stop_detached_startup_hooks("stubborn-app")
        )
        await asyncio.sleep(0.05)
        assert not ordinary_stop.done(), "ordinary disable must wait for terminal cleanup"

        release.set()
        assert await asyncio.wait_for(ordinary_stop, timeout=1) is True
        await asyncio.wait_for(finished.wait(), timeout=1)
        await asyncio.sleep(0)
        assert not lifecycle_mod._DETACHED_HOOK_TASKS

    @pytest.mark.asyncio
    async def test_shutdown_hook_cannot_outlive_teardown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shutdown stays pending until third-party hook code has actually stopped."""
        import kiro_crew.apps.lifecycle as lifecycle_mod

        monkeypatch.setattr(lifecycle_mod, "_HOOK_TIMEOUT_SEC", 0.01)
        started = asyncio.Event()
        release = asyncio.Event()

        async def shutdown_hook(_ctx: Any) -> None:
            started.set()
            await release.wait()

        dispatcher = LifecycleDispatcher()
        monkeypatch.setattr(dispatcher, "_resolve_hook", lambda *_a, **_k: shutdown_hook)
        ctx = dispatcher._build_context(self._app_info("shutdown-app"))
        invocation = asyncio.create_task(
            dispatcher._invoke(
                "shutdown-app", "backend.hooks:on_shutdown", ctx, phase="shutdown"
            )
        )

        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0.05)  # well beyond the startup-only deadline
        assert not invocation.done()
        assert not lifecycle_mod._DETACHED_HOOK_TASKS

        release.set()
        assert await asyncio.wait_for(invocation, timeout=1) is True
        assert not lifecycle_mod._DETACHED_HOOK_TASKS

    @pytest.mark.asyncio
    async def test_disable_reports_detached_startup_hook_that_did_not_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shared teardown receives a hard-failure marker for residual code."""
        import kiro_crew.apps.hooks_integration as integration

        class _Dispatcher:
            _cron_service = None

            async def stop_detached_startup_hooks(
                self, app_name: str, *, bounded: bool = False
            ) -> bool:
                assert app_name == "residual-app"
                assert bounded is True
                return False

        monkeypatch.setattr(integration, "_lifecycle_dispatcher", _Dispatcher())
        monkeypatch.setattr(integration, "_route_registry", None)

        result = await integration.on_app_disable(
            "residual-app",
            self._app_info("residual-app"),
            run_app_hooks=False,
            bounded_startup_cleanup=True,
        )

        assert result["startup_cleanup"].startswith("failed:")

    @pytest.mark.asyncio
    async def test_disable_skips_shutdown_hook_when_startup_cleanup_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A retryable teardown failure must not start unbounded app code."""
        import kiro_crew.apps.hooks_integration as integration

        invoked = False

        class _Dispatcher:
            _cron_service = None

            async def stop_detached_startup_hooks(
                self, app_name: str, *, bounded: bool = False
            ) -> bool:
                assert app_name == "residual-app"
                assert bounded is True
                return False

            async def _invoke(self, *_args: Any, **_kwargs: Any) -> bool:
                nonlocal invoked
                invoked = True
                return True

        monkeypatch.setattr(integration, "_lifecycle_dispatcher", _Dispatcher())
        monkeypatch.setattr(integration, "_route_registry", None)

        result = await integration.on_app_disable(
            "residual-app",
            _make_app_info("residual-app", on_shutdown="hooks:on_shutdown"),
            run_app_hooks=True,
            bounded_startup_cleanup=True,
        )

        assert result["startup_cleanup"].startswith("failed:")
        assert "hooks_shutdown" not in result
        assert invoked is False

    @pytest.mark.asyncio
    async def test_gateway_shutdown_forwards_hookless_enabled_apps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The integration layer must not filter ownership by hook presence."""
        import kiro_crew.apps.hooks_integration as integration

        seen: list[str] = []

        class _Dispatcher:
            async def dispatch_shutdown(self, apps: list[dict[str, Any]]) -> list[str]:
                seen.extend(app["name"] for app in apps)
                return []

        apps = [
            _make_app_info("hookless"),
            _make_app_info("with-hook", on_shutdown="hooks:on_shutdown"),
        ]
        monkeypatch.setattr(integration, "_lifecycle_dispatcher", _Dispatcher())
        monkeypatch.setattr(integration, "list_apps", lambda: apps)

        await integration.on_gateway_shutdown()

        assert seen == ["hookless", "with-hook"]

    @pytest.mark.asyncio
    async def test_synchronous_hook_is_never_subject_to_the_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sync hook returns before the iscoroutine check → runs to completion."""
        import kiro_crew.apps.lifecycle as lifecycle_mod

        # Even with a zero deadline, a sync hook is unaffected: it never awaits.
        monkeypatch.setattr(lifecycle_mod, "_HOOK_TIMEOUT_SEC", 0.0)

        ran = False

        def sync_hook(_ctx: Any) -> None:
            nonlocal ran
            ran = True

        dispatcher = LifecycleDispatcher()
        monkeypatch.setattr(dispatcher, "_resolve_hook", lambda *_a, **_k: sync_hook)
        ctx = dispatcher._build_context(self._app_info("sync-app"))

        ok = await dispatcher._invoke("sync-app", "backend.hooks:on_startup", ctx, phase="startup")

        assert ok is True
        assert ran is True
        assert ctx.health.status == "healthy"

    @pytest.mark.asyncio
    async def test_successful_async_hook_within_deadline_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An async hook that returns inside the deadline is unaffected → True."""
        ran = False

        async def quick_hook(_ctx: Any) -> None:
            nonlocal ran
            await asyncio.sleep(0)
            ran = True

        dispatcher = LifecycleDispatcher()
        monkeypatch.setattr(dispatcher, "_resolve_hook", lambda *_a, **_k: quick_hook)
        ctx = dispatcher._build_context(self._app_info("quick-app"))

        ok = await dispatcher._invoke(
            "quick-app", "backend.hooks:on_startup", ctx, phase="startup"
        )

        assert ok is True
        assert ran is True
        assert ctx.health.status == "healthy"

    @pytest.mark.asyncio
    async def test_one_apps_timeout_does_not_block_subsequent_apps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """dispatch_startup continues past a hung app; independent apps still start."""
        import kiro_crew.apps.lifecycle as lifecycle_mod

        monkeypatch.setattr(lifecycle_mod, "_HOOK_TIMEOUT_SEC", 0.05)

        invoked_names: list[str] = []
        release = asyncio.Event()

        async def hangs(_ctx: Any) -> None:
            await release.wait()

        async def quick(_ctx: Any) -> None:
            return None

        # Lexicographic order puts "a-hang" before "b-ok"; the hang must not
        # prevent "b-ok" from being invoked and reported successful.
        def resolve(app_name: str, _hook: str) -> Any:
            invoked_names.append(app_name)
            return hangs if app_name == "a-hang" else quick

        dispatcher = LifecycleDispatcher()
        monkeypatch.setattr(dispatcher, "_resolve_hook", resolve)

        apps = [self._app_info("b-ok"), self._app_info("a-hang")]
        succeeded = await dispatcher.dispatch_startup(apps)

        assert invoked_names == ["a-hang", "b-ok"], "both hooks were attempted, in order"
        assert succeeded == ["b-ok"], "the hung app is excluded; the healthy one starts"
        release.set()
        assert await asyncio.wait_for(
            dispatcher.stop_detached_startup_hooks("a-hang"), timeout=1
        )
        await asyncio.sleep(0)
        assert not lifecycle_mod._DETACHED_HOOK_TASKS

    @pytest.mark.asyncio
    async def test_timeout_preserves_sel_resource_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The timeout outcome keeps the established hook-path resource value."""
        import kiro_crew.apps.lifecycle as lifecycle_mod

        monkeypatch.setattr(lifecycle_mod, "_HOOK_TIMEOUT_SEC", 0.05)

        calls: list[dict[str, Any]] = []
        release = asyncio.Event()

        class _Sel:
            def log_api_access(self, **kwargs: Any) -> None:
                calls.append(kwargs)

        monkeypatch.setattr(lifecycle_mod, "sel", lambda: _Sel())

        async def hangs(_ctx: Any) -> None:
            await release.wait()

        dispatcher = LifecycleDispatcher()
        monkeypatch.setattr(dispatcher, "_resolve_hook", lambda *_a, **_k: hangs)
        ctx = dispatcher._build_context(self._app_info("sel-app"))

        await dispatcher._invoke("sel-app", "backend.hooks:on_startup", ctx, phase="startup")

        assert len(calls) == 1
        record = calls[0]
        assert record["outcome"] == "timeout"
        assert record["caller"] == "app:sel-app"
        assert record["resources"] == "backend.hooks:on_startup"
        release.set()
        assert await asyncio.wait_for(
            dispatcher.stop_detached_startup_hooks("sel-app"), timeout=1
        )
        await asyncio.sleep(0)
        assert not lifecycle_mod._DETACHED_HOOK_TASKS
