"""Lifecycle Hook Dispatcher — invokes app Python hooks at gateway lifecycle events.

Hooks are loaded via the module_loader (same isolation as routes) and invoked
in deterministic order (lexicographic by app name for startup, reverse for shutdown).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from functools import partial
from typing import Any

from kiro_crew.apps.context import AppContext, build_app_context
from kiro_crew.apps.execution import shipped_builtin_app_root
from kiro_crew.apps.manager import app_dir
from kiro_crew.apps.module_loader import load_app_module
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Per-hook wall-clock deadline (seconds) applied to async startup hooks.
#:
#: An ``on_startup`` hook awaiting a wedged resource would otherwise stall the
#: serial dispatch loop and keep the gateway from ever reaching readiness. The
#: timeout bounds that startup wait so independent apps still get their turn.
#: Shutdown is deliberately different: teardown must not report trust revoked
#: while third-party hook code still retains its ``AppContext`` capabilities, so
#: an ``on_shutdown`` task is awaited to completion and never detached. The
#: startup deadline is generous because legitimate setup may do real work; it is
#: a safety net against a hang, not a performance budget. Synchronous hooks run
#: to completion before the dispatcher awaits, so this deadline governs async
#: startup hooks only.
_HOOK_TIMEOUT_SEC = 30.0

#: Slot-history persistence may consume half of the gateway's cooperative
#: shutdown deadline before app cleanup begins. Ownership checks therefore only
#: observe tasks that are already terminal; active startup work is skipped
#: fail-closed so shutdown hooks for unaffected apps retain the remaining budget.
_GATEWAY_STARTUP_CLEANUP_TIMEOUT_SEC = 0.0

# Async startup hook tasks are owned from creation, before the first await.
# Cancelling an asyncio wrapper is not proof that execution stopped: a coroutine
# awaiting ``asyncio.to_thread`` becomes terminal while its worker thread keeps
# running. Hold a strong reference keyed by owning app so every destructive
# boundary sees active work even before the readiness deadline. Successfully
# completed tasks are removed by their observer; timed-out tasks remain owned
# until actual completion (or become a fail-closed residual on cancellation).
_DETACHED_HOOK_TASKS: dict[str, set[asyncio.Task[Any]]] = {}

# Cancellation makes an asyncio task terminal but does not prove work delegated
# through ``asyncio.to_thread`` stopped. Once a startup hook is observed
# cancelled, preserve an app-scoped residual marker for the rest of the process:
# no independent worker handle exists from which true completion can be proven.
# Destructive lifecycle operations therefore fail closed instead of withdrawing
# trust or replacing files while that worker may still hold AppContext powers.
_DETACHED_HOOK_RESIDUALS: set[str] = set()


def _mark_cancelled_startup_residual(app_name: str) -> None:
    """Record cancellation before releasing ownership of a startup task."""
    if app_name in _DETACHED_HOOK_RESIDUALS:
        return
    _DETACHED_HOOK_RESIDUALS.add(app_name)
    logger.error(
        "Lifecycle startup hook for app %s was cancelled; residual execution "
        "cannot be disproven",
        app_name,
    )


def _observe_detached_hook_task(
    app_name: str,
    observation: dict[str, bool],
    task: asyncio.Task[Any],
) -> None:
    """Observe terminal state without mistaking cancellation for completion."""
    try:
        task.result()
    except asyncio.CancelledError:
        # Set the residual marker BEFORE releasing the task reference so no
        # cleanup caller can observe a false ownership-free gap.
        _mark_cancelled_startup_residual(app_name)
    except Exception:
        # A pre-deadline failure is reported by the awaiting _invoke path. Once
        # readiness has moved on, this observer is the only remaining reporter.
        if observation["timed_out"]:
            logger.exception(
                "Lifecycle startup hook for app %s later failed after the deadline",
                app_name,
            )
    finally:
        owned = _DETACHED_HOOK_TASKS.get(app_name)
        if owned is not None:
            owned.discard(task)
            if not owned:
                _DETACHED_HOOK_TASKS.pop(app_name, None)


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
            success = await self._invoke(name, hook_path, ctx, phase="startup")
            if success:
                invoked.append(name)
        return invoked

    async def dispatch_shutdown(self, enabled_apps: list[dict[str, Any]]) -> list[str]:
        """Join startup ownership, then call shutdown hooks in reverse order.

        Every enabled app participates in one concurrent ownership sweep, even
        when it has no shutdown hook. Starting all waits before awaiting any one
        app keeps the sweep within a single gateway-shutdown deadline rather than
        multiplying that deadline by the number of apps.

        Returns list of app names whose hooks were invoked successfully.
        """
        ordered = sorted(enabled_apps, key=lambda a: a.get("name", ""), reverse=True)
        ownership = await asyncio.gather(
            *(
                self.stop_detached_startup_hooks(
                    app_info.get("name", ""),
                    bounded=True,
                    timeout=_GATEWAY_STARTUP_CLEANUP_TIMEOUT_SEC,
                )
                for app_info in ordered
            )
        )

        invoked: list[str] = []
        for app_info, startup_stopped in zip(ordered, ownership):
            name = app_info.get("name", "")
            hook_path = self._get_hook(app_info, "on_shutdown")
            if not startup_stopped:
                logger.error(
                    "Skipping shutdown hook for %s because retained startup "
                    "execution is still active",
                    name,
                )
                continue
            if not hook_path:
                continue
            ctx = self._build_context(app_info)
            success = await self._invoke(name, hook_path, ctx, phase="shutdown")
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
        return await self._invoke(name, hook_path, ctx, phase="startup")

    async def dispatch_disable(self, app_info: dict[str, Any]) -> bool:
        """Call on_shutdown hook for a single app being disabled.

        Returns True if hook was invoked successfully (or no hook declared).
        """
        name = app_info.get("name", "")
        hook_path = self._get_hook(app_info, "on_shutdown")
        if not hook_path:
            return True
        ctx = self._build_context(app_info)
        return await self._invoke(name, hook_path, ctx, phase="shutdown")

    async def stop_detached_startup_hooks(
        self,
        app_name: str,
        *,
        bounded: bool = False,
        timeout: float | None = None,
    ) -> bool:
        """Wait for retained startup hooks before teardown succeeds.

        Ordinary disable waits until tracked code terminates, so it cannot return
        success with a live AppContext. Trust withdrawal uses ``bounded=True``:
        if execution does not settle by the deadline, ``False`` tells teardown to
        retain trust and return a retryable failure. ``timeout`` is a test seam and
        overrides the normal bounded deadline when supplied.

        Tasks are not cancelled here. Cancellation can make an asyncio wrapper
        awaiting ``to_thread`` look terminal while the worker still executes app
        code. A cancelled retained task therefore leaves a process-lifetime
        residual marker, and every later cleanup attempt fails closed because no
        independent worker handle exists to prove completion.
        """
        if app_name in _DETACHED_HOOK_RESIDUALS:
            logger.error(
                "App %s has residual startup-hook execution after cancellation",
                app_name,
            )
            return False

        tasks = tuple(_DETACHED_HOOK_TASKS.get(app_name, ()))
        if not tasks:
            return True

        wait_timeout = timeout if timeout is not None else (_HOOK_TIMEOUT_SEC if bounded else None)
        done, pending = await asyncio.wait(tasks, timeout=wait_timeout)
        if done:
            # Do not rely on done-callback scheduling order: asyncio.wait may
            # resume before `_observe_detached_hook_task` runs.
            if any(task.cancelled() for task in done):
                _DETACHED_HOOK_RESIDUALS.add(app_name)
            await asyncio.gather(*done, return_exceptions=True)
        if app_name in _DETACHED_HOOK_RESIDUALS:
            logger.error(
                "App %s has residual startup-hook execution after cancellation",
                app_name,
            )
            return False
        if pending:
            logger.error(
                "App %s still has %d detached startup hook(s) after the cleanup wait",
                app_name,
                len(pending),
            )
            return False
        return True

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

    async def _invoke(self, app_name: str, hook_path: str, ctx: AppContext, *, phase: str) -> bool:
        """Import and call a hook via module_loader (same isolation as routes).

        ``phase`` is ``"startup"`` or ``"shutdown"``. Async startup hooks are
        bounded because one app must not hold gateway readiness forever. Async
        shutdown hooks are instead awaited to completion: returning from trust
        revocation while an in-process task still owns ``AppContext`` capabilities
        would leave third-party code running after teardown reports success.

        Startup uses ``asyncio.wait`` rather than ``wait_for``: ``wait_for``
        cancels on expiry and then waits for cancellation to finish, so a hook
        that catches ``CancelledError`` can still hold gateway startup forever.
        On expiry the dispatcher retains and observes the task in the background
        without cancelling it, records the timeout, and continues. Synchronous
        hooks return before the ``iscoroutine`` check and are unaffected.

        Returns True on success, False on failure.
        """

        try:
            # The readiness deadline releases the caller while retaining the live
            # task. A later enable/retry may hold the lifecycle lock correctly and
            # still arrive after that release, so caller-side serialization alone
            # cannot prevent duplicate startup work. Check and register without an
            # intervening await: event-loop atomicity makes this the final admission
            # point for boot, dashboard enable, and direct dispatcher callers.
            if phase == "startup":
                owned = tuple(_DETACHED_HOOK_TASKS.get(app_name, ()))
                for owned_task in owned:
                    if owned_task.cancelled():
                        _mark_cancelled_startup_residual(app_name)
                if app_name in _DETACHED_HOOK_RESIDUALS or any(
                    not owned_task.done() for owned_task in owned
                ):
                    detail = "retained startup hook is already active"
                    logger.error(
                        "Refusing duplicate lifecycle startup hook %s for app %s: %s",
                        hook_path,
                        app_name,
                        detail,
                    )
                    ctx.health.mark_degraded(f"Lifecycle hook refused during {phase}: {hook_path}")
                    sel().log_api_access(
                        caller=f"app:{app_name}",
                        operation="lifecycle_hook_invoke",
                        outcome="failed",
                        resources=hook_path,
                        error=detail,
                    )
                    return False

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
                task = asyncio.ensure_future(result)
                if phase == "startup":
                    observation = {"timed_out": False}
                    _DETACHED_HOOK_TASKS.setdefault(app_name, set()).add(task)
                    task.add_done_callback(
                        partial(
                            _observe_detached_hook_task,
                            app_name,
                            observation,
                        )
                    )
                    done, _pending = await asyncio.wait({task}, timeout=_HOOK_TIMEOUT_SEC)
                    if task not in done:
                        observation["timed_out"] = True
                        logger.error(
                            "Lifecycle hook %s for app %s timed out after %.0fs during %s; "
                            "task retained — continuing with remaining apps",
                            hook_path,
                            app_name,
                            _HOOK_TIMEOUT_SEC,
                            phase,
                        )
                        ctx.health.mark_degraded(
                            f"Lifecycle hook timed out during {phase}: {hook_path}"
                        )
                        sel().log_api_access(
                            caller=f"app:{app_name}",
                            operation="lifecycle_hook_invoke",
                            outcome="timeout",
                            resources=hook_path,
                        )
                        return False
                    if task.cancelled():
                        # The observer may not have run yet. Mark the residual
                        # synchronously so the caller cannot release its lifecycle
                        # lock through an ownership-free gap.
                        _mark_cancelled_startup_residual(app_name)
                        ctx.health.mark_degraded(
                            f"Lifecycle hook cancelled during {phase}: {hook_path}"
                        )
                        sel().log_api_access(
                            caller=f"app:{app_name}",
                            operation="lifecycle_hook_invoke",
                            outcome="failed",
                            resources=hook_path,
                        )
                        return False
                    # Retrieve a completed startup hook's result here so ordinary
                    # failures follow the shared exception path below.
                    task.result()
                else:
                    # A shutdown task still owns its AppContext capabilities.
                    # Teardown/trust revocation cannot complete while that code
                    # remains live, even if it ignores cancellation.
                    await task
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
                "Lifecycle hook %s failed for app %s during %s", hook_path, app_name, phase
            )
            ctx.health.mark_degraded(f"Lifecycle hook failed: {hook_path}")
            sel().log_api_access(
                caller=f"app:{app_name}",
                operation="lifecycle_hook_invoke",
                outcome="failed",
                resources=hook_path,
            )
            return False
