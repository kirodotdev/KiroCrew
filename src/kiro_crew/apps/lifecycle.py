"""Lifecycle Hook Dispatcher — invokes app Python hooks at gateway lifecycle events.

Hooks are loaded via the module_loader (same isolation as routes) and invoked
in deterministic order (lexicographic by app name for startup, reverse for shutdown).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any

from kiro_crew.apps.context import AppContext, build_app_context
from kiro_crew.apps.execution import shipped_builtin_app_root
from kiro_crew.apps.manager import app_dir
from kiro_crew.apps.module_loader import load_app_module
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


class LifecycleDispatcher:
    """Invokes app lifecycle hooks in deterministic order.

    Hooks are declared in ``backend.hooks.on_startup`` and
    ``backend.hooks.on_shutdown`` in the app manifest.
    """

    def __init__(
        self,
        *,
        cron_service: Any = None,
        broadcast_fn: Any = None,
        spawn_impl: Any = None,
    ) -> None:
        self._cron_service = cron_service
        self._broadcast_fn = broadcast_fn
        self._spawn_impl = spawn_impl

    async def dispatch_startup(self, enabled_apps: list[dict[str, Any]]) -> list[str]:
        """Call on_startup hooks for all enabled apps with hooks declared.

        Args:
            enabled_apps: List of app info dicts (from list_apps()).

        Returns:
            List of app names whose hooks were invoked successfully.
        """
        invoked: list[str] = []
        for app_info in sorted(enabled_apps, key=lambda a: a.get("name", "")):
            name = app_info.get("name", "")
            hook_path = self._get_hook(app_info, "on_startup")
            if not hook_path:
                continue
            ctx = self._build_context(app_info)
            success = await self._invoke(name, hook_path, ctx)
            if success:
                invoked.append(name)
        return invoked

    async def dispatch_shutdown(self, enabled_apps: list[dict[str, Any]]) -> list[str]:
        """Call on_shutdown hooks for all enabled apps (reverse order).

        Returns list of app names whose hooks were invoked successfully.
        """
        invoked: list[str] = []
        for app_info in sorted(enabled_apps, key=lambda a: a.get("name", ""), reverse=True):
            name = app_info.get("name", "")
            hook_path = self._get_hook(app_info, "on_shutdown")
            if not hook_path:
                continue
            ctx = self._build_context(app_info)
            success = await self._invoke(name, hook_path, ctx)
            if success:
                invoked.append(name)
        return invoked

    async def dispatch_enable(self, app_info: dict[str, Any]) -> bool:
        """Call on_startup hook for a single app being enabled.

        Returns True if hook was invoked successfully (or no hook declared).
        """
        name = app_info.get("name", "")
        hook_path = self._get_hook(app_info, "on_startup")
        if not hook_path:
            return True
        ctx = self._build_context(app_info)
        return await self._invoke(name, hook_path, ctx)

    async def dispatch_disable(self, app_info: dict[str, Any]) -> bool:
        """Call on_shutdown hook for a single app being disabled.

        Returns True if hook was invoked successfully (or no hook declared).
        """
        name = app_info.get("name", "")
        hook_path = self._get_hook(app_info, "on_shutdown")
        if not hook_path:
            return True
        ctx = self._build_context(app_info)
        return await self._invoke(name, hook_path, ctx)

    def _get_hook(self, app_info: dict[str, Any], hook_name: str) -> str:
        """Extract a hook path from app info."""
        manifest = app_info.get("manifest", {})
        backend = manifest.get("backend", {})
        hooks = backend.get("hooks", {})
        return hooks.get(hook_name, "")

    @staticmethod
    def _resolve_hook(app_name: str, hook_path: str):
        """Resolve ``module.path:callable`` to a callable.

        Installed apps: file-path load from the data-home app dir via
        ``load_app_module`` (unchanged — same isolation as routes).

        BUILTIN apps ship no code in the data-home dir (registration writes
        only app.json/installed.json), so the hook must import from the
        package instead. A normal dotted import — NOT a file-path load — is
        load-bearing here: routes and hooks must share ONE module instance
        (an app's routes may read module state its on_startup hook created;
        a file-path load would create a parallel instance and the routes
        would always see the empty one). Builtin package names use
        underscores where app names use hyphens.
        """
        shipped_root = shipped_builtin_app_root(app_name)
        if shipped_root is not None:
            # The package DIRECTORY NAME comes from the resolved root, not from
            # the app name: `shipped_builtin_app_root` matches on the shipped
            # manifest's own `name` field after resolve(strict=True) +
            # containment, so it already handles the hyphen/underscore convention
            # and nothing here has to build a module name out of the input.
            dotted, _, callable_name = hook_path.partition(":")
            mod = importlib.import_module(f"kiro_crew.apps.builtins.{shipped_root.name}.{dotted}")
            func = getattr(mod, callable_name, None)
            if func is None:
                raise ImportError(
                    f"Hook callable {callable_name!r} not found in builtin "
                    f"module {shipped_root.name}.{dotted} (app={app_name})"
                )
            return func
        return load_app_module(app_name, app_dir(app_name), hook_path)

    def _build_context(self, app_info: dict[str, Any]) -> AppContext:
        """Build an AppContext for the given app."""
        name = app_info.get("name", "")
        manifest = app_info.get("manifest", {})
        permissions = manifest.get("permissions", {})
        data_path = app_dir(name) / "data"
        data_path.mkdir(parents=True, exist_ok=True)

        return build_app_context(
            app_name=name,
            data_dir=data_path,
            permissions=permissions,
            cron_service=self._cron_service,
            broadcast_fn=self._broadcast_fn,
            spawn_impl=self._spawn_impl,
            app_config=manifest.get("extra", {}),
        )

    async def _invoke(self, app_name: str, hook_path: str, ctx: AppContext) -> bool:
        """Import and call a hook via module_loader (same isolation as routes).

        Returns True on success, False on failure.
        """

        try:
            # Through _resolve_hook, NOT load_app_module directly: for a shipped
            # builtin the latter does a file-path load and registers a SECOND
            # module object (`_kirocrew_app_<app>.<mod>`), so an on_startup hook
            # builds its runtime on a copy the app's routes never see — they keep
            # reading the empty original. _resolve_hook dotted-imports builtins
            # for exactly that reason and still file-path-loads third-party apps,
            # where the isolation is the point.
            func = self._resolve_hook(app_name, hook_path)
            if func is None:
                raise ImportError(f"hook {hook_path!r} did not resolve for app {app_name!r}")
            result = func(ctx)
            if asyncio.iscoroutine(result):
                await result
            logger.info("Lifecycle hook %s succeeded for app %s", hook_path, app_name)
            sel().log_api_access(
                caller=f"app:{app_name}",
                operation="lifecycle_hook_invoke",
                outcome="ok",
                resources=hook_path,
            )
            return True
        except Exception:
            logger.exception("Lifecycle hook %s failed for app %s", hook_path, app_name)
            ctx.health.mark_degraded(f"Lifecycle hook failed: {hook_path}")
            sel().log_api_access(
                caller=f"app:{app_name}",
                operation="lifecycle_hook_invoke",
                outcome="failed",
                resources=hook_path,
            )
            return False
