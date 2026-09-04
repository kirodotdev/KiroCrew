"""Property tests for Lifecycle Hook Dispatcher.

Feature: app-sdk-gateway-hooks
Properties 9, 14: Deterministic ordering and shell-before-Python.
"""
from __future__ import annotations

import asyncio
import re
import threading
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


class TestGatewayShutdownBackendSweep:
    """Gateway shutdown must stop the backends it spawned, not just run hooks.

    Spawned backends are gateway children: without an explicit stop they
    reparent to PID 1 when the gateway exits and keep listening on their ports.
    """

    @pytest.mark.asyncio
    async def test_shutdown_stops_spawned_backends_after_hooks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hooks are dispatched FIRST (an app's on_shutdown still has its
        backend alive), then every gateway-spawned backend is stopped, off the
        event loop thread."""
        import kiro_crew.apps.hooks_integration as integration

        order: list[str] = []
        stop_threads: list[threading.Thread] = []

        class _Dispatcher:
            async def dispatch_shutdown(self, apps: list[dict[str, Any]]) -> list[str]:
                order.append("hooks")
                return []

        def fake_stop(name: str) -> bool:
            order.append(f"stop:{name}")
            stop_threads.append(threading.current_thread())
            return True

        monkeypatch.setattr(integration, "_lifecycle_dispatcher", _Dispatcher())
        monkeypatch.setattr(integration, "list_apps", lambda: [_make_app_info("app-a")])
        monkeypatch.setattr(
            integration, "spawned_backend_names", lambda: ["app-a", "app-b"]
        )
        monkeypatch.setattr(integration, "stop_app_backend", fake_stop)

        loop_thread = threading.current_thread()
        await integration.on_gateway_shutdown()

        # Hooks first; the stops run concurrently, so only membership is
        # asserted — a total order over them would pin an accident.
        assert order[0] == "hooks"
        assert set(order[1:]) == {"stop:app-a", "stop:app-b"}
        # The stop blocks in the kernel (process-group signal + wait) — it must
        # run on an executor thread, never on the event loop thread.
        assert all(t is not loop_thread for t in stop_threads)

    @pytest.mark.asyncio
    async def test_stop_targets_come_from_the_tracking_table_not_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sweep is driven by what the gateway actually spawned, so a child
        whose app was disabled cross-process (metadata-only) is still stopped,
        and an enabled app with nothing running is never passed to
        stop_app_backend (whose pidfile erasure would destroy the stale-reap's
        record of a prior-generation orphan)."""
        import kiro_crew.apps.hooks_integration as integration

        stopped: list[str] = []

        def fake_stop(name: str) -> bool:
            stopped.append(name)
            return True

        # Metadata says "enabled-idle" is enabled; the tracking table says only
        # "disabled-but-running" has a spawned backend.
        monkeypatch.setattr(integration, "_lifecycle_dispatcher", None)
        monkeypatch.setattr(
            integration, "list_apps", lambda: [_make_app_info("enabled-idle")]
        )
        monkeypatch.setattr(
            integration, "spawned_backend_names", lambda: ["disabled-but-running"]
        )
        monkeypatch.setattr(integration, "stop_app_backend", fake_stop)

        await integration.on_gateway_shutdown()

        assert stopped == ["disabled-but-running"]

    def test_spawned_backend_names_excludes_adopted_backends(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adopted records (proc=None) are externally managed instances whose
        contract is to SURVIVE gateway exit and be re-adopted on the next
        start; the shutdown sweep must never signal them."""
        import kiro_crew.apps.backend as backend

        spawned = backend.AppProcess(app_name="spawned-app", proc=object())  # type: ignore[arg-type]
        adopted = backend.AppProcess(
            app_name="adopted-app", proc=None, port=4242, adopted_pids=[123]
        )
        monkeypatch.setattr(
            backend, "_processes", {"spawned-app": spawned, "adopted-app": adopted}
        )

        assert backend.spawned_backend_names() == ["spawned-app"]

    @pytest.mark.asyncio
    async def test_one_failing_stop_does_not_skip_the_rest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One app's failing backend stop must not leave the others running."""
        import kiro_crew.apps.hooks_integration as integration

        stopped: list[str] = []

        def fake_stop(name: str) -> bool:
            if name == "app-a":
                raise RuntimeError("stop failed")
            stopped.append(name)
            return True

        monkeypatch.setattr(integration, "_lifecycle_dispatcher", None)
        monkeypatch.setattr(integration, "list_apps", lambda: [])
        monkeypatch.setattr(
            integration, "spawned_backend_names", lambda: ["app-a", "app-b"]
        )
        monkeypatch.setattr(integration, "stop_app_backend", fake_stop)

        await integration.on_gateway_shutdown()

        assert stopped == ["app-b"]

    @pytest.mark.asyncio
    async def test_the_sweep_shares_one_deadline_instead_of_stacking_waits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wedged backend's stop cannot hold the sweep past its shared
        budget: the sweep returns (logging a warning) so the rest of gateway
        cleanup still gets its share of the 10s cooperative window, rather
        than multiplying the per-app SIGTERM grace by the number of apps."""
        import kiro_crew.apps.hooks_integration as integration

        release = threading.Event()

        def wedged_stop(name: str) -> bool:
            release.wait(timeout=5.0)
            return True

        monkeypatch.setattr(integration, "_BACKEND_STOP_BUDGET_SECS", 0.05)
        monkeypatch.setattr(integration, "_lifecycle_dispatcher", None)
        monkeypatch.setattr(integration, "list_apps", lambda: [])
        monkeypatch.setattr(integration, "spawned_backend_names", lambda: ["wedged"])
        monkeypatch.setattr(integration, "stop_app_backend", wedged_stop)

        try:
            await asyncio.wait_for(integration.on_gateway_shutdown(), timeout=2.0)
        finally:
            # Unblock the executor thread so it does not outlive the test.
            release.set()

    @pytest.mark.asyncio
    async def test_sweep_runs_even_when_hook_dispatch_fails_or_is_cancelled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hook dispatch awaits third-party on_shutdown hooks; a wedged hook is
        cancelled by the gateway's shutdown deadline and a broken dispatcher can
        raise. Neither may skip the backend sweep, or every spawned backend is
        orphaned — the defect this path exists to fix."""
        import kiro_crew.apps.hooks_integration as integration

        stopped: list[str] = []

        def fake_stop(name: str) -> bool:
            stopped.append(name)
            return True

        class _RaisingDispatcher:
            async def dispatch_shutdown(self, apps: list[dict[str, Any]]) -> list[str]:
                raise RuntimeError("dispatcher broke")

        monkeypatch.setattr(integration, "_lifecycle_dispatcher", _RaisingDispatcher())
        monkeypatch.setattr(integration, "list_apps", lambda: [_make_app_info("app-a")])
        monkeypatch.setattr(integration, "spawned_backend_names", lambda: ["app-a"])
        monkeypatch.setattr(integration, "stop_app_backend", fake_stop)

        with pytest.raises(RuntimeError, match="dispatcher broke"):
            await integration.on_gateway_shutdown()
        assert stopped == ["app-a"]

        # Cancellation (the shutdown deadline cutting off a hanging hook) also
        # reaches the sweep on its way out.
        stopped.clear()
        hook_started = asyncio.Event()

        class _HangingDispatcher:
            async def dispatch_shutdown(self, apps: list[dict[str, Any]]) -> list[str]:
                hook_started.set()
                await asyncio.Event().wait()  # never returns
                return []

        monkeypatch.setattr(integration, "_lifecycle_dispatcher", _HangingDispatcher())
        task = asyncio.ensure_future(integration.on_gateway_shutdown())
        await asyncio.wait_for(hook_started.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)
        assert stopped == ["app-a"]

    @pytest.mark.asyncio
    async def test_budget_overrun_does_not_cancel_queued_stops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stop futures share the executor with the rest of shutdown, so a
        stop can still be QUEUED when the budget fires. The timeout must
        release the sweep without cancelling that queued stop — a cancelled
        queued future never runs stop_app_backend and its backend is never
        signalled at all."""
        import concurrent.futures

        import kiro_crew.apps.hooks_integration as integration

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        release = threading.Event()
        queued_ran = threading.Event()
        ran: list[str] = []

        def fake_stop(name: str) -> bool:
            if name == "a-wedged":
                release.wait(timeout=5.0)
            ran.append(name)
            if name == "b-queued":
                queued_ran.set()
            return True

        monkeypatch.setattr(integration, "subprocess_executor", lambda: pool)
        monkeypatch.setattr(integration, "_BACKEND_STOP_BUDGET_SECS", 0.05)
        monkeypatch.setattr(integration, "_lifecycle_dispatcher", None)
        monkeypatch.setattr(integration, "list_apps", lambda: [])
        monkeypatch.setattr(
            integration, "spawned_backend_names", lambda: ["a-wedged", "b-queued"]
        )
        monkeypatch.setattr(integration, "stop_app_backend", fake_stop)

        try:
            # The single-worker pool holds "a-wedged" running and "b-queued"
            # queued when the 0.05s budget fires; the sweep must return anyway.
            await asyncio.wait_for(integration.on_gateway_shutdown(), timeout=2.0)
            release.set()
            assert queued_ran.wait(timeout=2.0), (
                "the sweep timeout cancelled a queued stop; that backend would "
                "never be signalled"
            )
            assert ran == ["a-wedged", "b-queued"]
        finally:
            release.set()
            pool.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Shutdown resolves the LOADED code, not disk (issue #7880 reconciler teardown)
# ---------------------------------------------------------------------------


class TestShutdownResolvesLoadedCode:
    @pytest.mark.asyncio
    async def test_shutdown_prefers_cached_module_over_disk(self, monkeypatch) -> None:
        """GPT [BLOCKING]: on_shutdown must stop the code that is actually
        running. A same-path v2 reinstall leaves v2 on disk while v1's task is
        still live; resolving from disk would run v2's on_shutdown and orphan v1.
        _invoke(phase='shutdown') resolves the ALREADY-LOADED (cached) callable
        first, falling back to the disk loader only when nothing is cached."""
        import sys
        from types import SimpleNamespace

        import kiro_crew.apps.module_loader as ml
        from kiro_crew.apps.lifecycle import LifecycleDispatcher

        app_name = "reload-app"
        hook_path = "backend.hooks:on_shutdown"
        ran: list[str] = []

        # v1 is the LOADED module: register it in sys.modules under the app's key.
        v1 = SimpleNamespace(on_shutdown=lambda ctx: ran.append("v1"))
        key = ml._module_namespace(app_name, "backend.hooks")
        sys.modules[key] = v1  # type: ignore[assignment]

        dispatcher = LifecycleDispatcher()
        # The disk loader would return v2 (the replacement) -- it must NOT be used.
        monkeypatch.setattr(
            dispatcher, "_resolve_hook", lambda a, h: (lambda ctx: ran.append("v2"))
        )
        ctx = SimpleNamespace(health=SimpleNamespace(mark_degraded=lambda *a, **k: None))
        try:
            ok = await dispatcher._invoke(app_name, hook_path, ctx, phase="shutdown")
        finally:
            sys.modules.pop(key, None)

        assert ok is True
        assert ran == ["v1"], "shutdown must run the loaded (cached) code, not disk v2"

    @pytest.mark.asyncio
    async def test_shutdown_falls_back_to_disk_when_nothing_cached(self, monkeypatch) -> None:
        """When no module is cached (e.g. a builtin, or never imported), shutdown
        falls back to the normal disk/dotted resolver."""
        from types import SimpleNamespace

        from kiro_crew.apps.lifecycle import LifecycleDispatcher

        ran: list[str] = []
        dispatcher = LifecycleDispatcher()
        monkeypatch.setattr(
            dispatcher, "_resolve_hook", lambda a, h: (lambda ctx: ran.append("disk"))
        )
        ctx = SimpleNamespace(health=SimpleNamespace(mark_degraded=lambda *a, **k: None))
        ok = await dispatcher._invoke(
            "uncached-app", "backend.hooks:on_shutdown", ctx, phase="shutdown"
        )
        assert ok is True
        assert ran == ["disk"]


class TestCacheShutdownForNeverLoadsOnTheEventLoop:
    """GPT rounds 8-9 (lifecycle.py shutdown cache): enable-time caching must not
    BLOCK the event loop (r8 F2) nor re-import a same-module app (r8 F1), and must
    still RETAIN a separate shutdown module startup never imported (r9). The
    resolution: resolve an already-loaded module in-memory (no disk, no re-import),
    and load a genuinely-separate module OFF the loop via asyncio.to_thread."""

    @pytest.mark.asyncio
    async def test_loaded_module_is_resolved_in_memory_without_reimport(self, monkeypatch):
        import sys
        from types import ModuleType

        import kiro_crew.apps.lifecycle as lc
        import kiro_crew.apps.module_loader as ml

        ml._shutdown_callables.pop("loaded-app", None)
        key = ml._module_namespace("loaded-app", "backend.hooks")
        mod = ModuleType(key)

        def _on_shutdown(ctx):
            return None

        mod.on_shutdown = _on_shutdown
        sys.modules[key] = mod

        # A same/loaded module must be resolved via sys.modules ONLY: no disk load
        # (would block the loop) and no re-import (would detach the running state).
        def _boom(*a, **kw):
            raise AssertionError("a loaded module must not be disk-loaded / re-imported")

        monkeypatch.setattr(lc, "load_app_module", _boom, raising=False)

        try:
            disp = lc.LifecycleDispatcher()
            await disp.cache_shutdown_for(
                {
                    "name": "loaded-app",
                    "manifest": {"backend": {"hooks": {"on_shutdown": "backend.hooks:on_shutdown"}}},
                }
            )
            # Snapshotted from sys.modules (pure getattr), generation-tagged.
            assert ml.resolve_loaded_callable("loaded-app", "backend.hooks:on_shutdown") is _on_shutdown
        finally:
            sys.modules.pop(key, None)
            ml._shutdown_callables.pop("loaded-app", None)

    @pytest.mark.asyncio
    async def test_separate_module_is_retained_via_off_loop_load(self, monkeypatch):
        import kiro_crew.apps.lifecycle as lc
        import kiro_crew.apps.module_loader as ml

        ml._shutdown_callables.pop("sep-app", None)

        def _sep_shutdown(ctx):
            return None

        # _resolve_hook does the disk load for a separate module; it MUST be reached
        # only through asyncio.to_thread (off the event loop), never called inline.
        offloaded = {"via_thread": False}
        monkeypatch.setattr(lc.LifecycleDispatcher, "_resolve_hook", lambda self, n, p: _sep_shutdown)

        real_to_thread = asyncio.to_thread

        async def _tracking_to_thread(fn, *a, **kw):
            offloaded["via_thread"] = True
            return await real_to_thread(fn, *a, **kw)

        monkeypatch.setattr(lc.asyncio, "to_thread", _tracking_to_thread)

        disp = lc.LifecycleDispatcher()
        # on_shutdown lives in a module startup never imported -> not in sys.modules,
        # so it is loaded off-loop and RETAINED (r9: separate modules must be kept).
        await disp.cache_shutdown_for(
            {
                "name": "sep-app",
                "manifest": {"backend": {"hooks": {"on_shutdown": "backend.never_loaded:on_shutdown"}}},
            }
        )
        assert offloaded["via_thread"] is True, "separate-module load must go off-loop via to_thread"
        assert ml._shutdown_callables.get("sep-app") == (ml._current_generation("sep-app"), _sep_shutdown)
        ml._shutdown_callables.pop("sep-app", None)

    @pytest.mark.asyncio
    async def test_stale_generation_callable_is_not_used(self, monkeypatch):
        import kiro_crew.apps.module_loader as ml

        ml._shutdown_callables.pop("gen-app", None)
        ml._app_load_generation.pop("gen-app", None)

        def _v1(ctx):
            return None

        # Cache a v1 callable at generation 0.
        ml.cache_shutdown_callable("gen-app", _v1)
        assert ml.resolve_loaded_callable("gen-app", "backend.hooks:on_shutdown") is _v1

        # A reload (unload bumps the generation) invalidates the stale v1 entry.
        ml._app_load_generation["gen-app"] = ml._current_generation("gen-app") + 1
        assert ml.resolve_loaded_callable("gen-app", "backend.hooks:on_shutdown") is None
        ml._shutdown_callables.pop("gen-app", None)
        ml._app_load_generation.pop("gen-app", None)
