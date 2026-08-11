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

        async def tracking_invoke(app_name: str, hook_path: str, ctx: Any) -> bool:
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

        async def tracking_invoke(app_name: str, hook_path: str, ctx: Any) -> bool:
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
