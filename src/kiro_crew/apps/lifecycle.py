"""Lifecycle Hook Dispatcher — invokes app Python hooks at gateway lifecycle events.

Hooks are loaded via the module_loader (same isolation as routes) and invoked
in deterministic order (lexicographic by app name for startup, reverse for shutdown).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.apps.context import AppContext, build_app_context
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
    ) -> None:
        self._cron_service = cron_service
        self._broadcast_fn = broadcast_fn

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
            app_config=manifest.get("extra", {}),
        )

    async def _invoke(self, app_name: str, hook_path: str, ctx: AppContext) -> bool:
        """Import and call a hook via module_loader (same isolation as routes).

        Returns True on success, False on failure.
        """

        try:
            app_root = app_dir(app_name)
            func = load_app_module(app_name, app_root, hook_path)
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
            logger.exception(
                "Lifecycle hook %s failed for app %s", hook_path, app_name
            )
            ctx.health.mark_degraded(f"Lifecycle hook failed: {hook_path}")
            sel().log_api_access(
                caller=f"app:{app_name}",
                operation="lifecycle_hook_invoke",
                outcome="failed",
                resources=hook_path,
            )
            return False
