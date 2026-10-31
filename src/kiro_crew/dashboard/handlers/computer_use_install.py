"""agent-desktop install API -- Settings > Computer Use, driver setup.

``GET  /api/computer-use/agent-desktop/status``             open read
``POST /api/computer-use/agent-desktop/install``            dashboard owner only
``POST /api/computer-use/agent-desktop/permissions-guide``  dashboard owner only

Modeled on the browser installer (``messaging_api/browser_routes.py``): the
install runs in the background and the page polls the status, the gateway owns
ONE job record so a refreshed page or a second tab sees the same install, and
a gateway shutdown kills the npm child through the job's
:class:`~kiro_crew.computer_use.install_runner.InstallScope`. The record is in-memory:
a restart starts with no job, so a stale ``running`` cannot survive one.

Install and guide are owner-only for the browser installer's reason: installing
a desktop-automation binary adds an executable host capability, and the guide
drives System Settings. Both decisions are SEL-audited whichever way they go.
A status read never spawns npm; it runs only the local ``permissions`` probe.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from kiro_crew.computer_use.install_runner import InstallScope, run_in_scope
from kiro_crew.computer_use import agent_desktop_install

logger = logging.getLogger(__name__)

JOB_IDLE = "idle"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"


@dataclass
class _Job:
    """The one install this gateway owns. Mutated on the event loop only."""

    state: str = JOB_IDLE
    log: list[str] = field(default_factory=list)
    error: str | None = None
    task: asyncio.Task | None = None  # type: ignore[type-arg]
    scope: InstallScope | None = None

    def log_tail(self) -> str:
        return "\n".join(self.log)[-agent_desktop_install.LOG_TAIL_CAP :]

    def append(self, text: str) -> None:
        self.log.append(text)
        # Bound memory: the tail is all anyone reads.
        while (
            len(self.log) > 1
            and sum(len(x) for x in self.log) > 4 * agent_desktop_install.LOG_TAIL_CAP
        ):
            self.log.pop(0)

    def snapshot(self) -> dict[str, Any]:
        return {"state": self.state, "log_tail": self.log_tail(), "error": self.error}


_job = _Job()


def _reset_for_tests() -> None:
    global _job
    _job = _Job()


def _sel():
    from kiro_crew.dashboard.handlers.computer_use import _sel as cu_sel

    return cu_sel()


def _deny_non_owner(request: web.Request, operation: str) -> web.Response | None:
    """403 unless the caller is the dashboard owner; audit the decision either way."""
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if is_owner_dashboard_request(request):
        _sel().log_api_access(
            caller=str(request.get("user") or "owner"),
            operation=operation,
            outcome="allowed",
            source="computer_use_api",
            resources=request.path,
        )
        return None
    app_name = request.get("app", "")
    caller = f"app:{app_name}" if app_name else str(request.get("user") or "anonymous")
    _sel().log_api_access(
        caller=caller,
        operation=operation,
        outcome="denied",
        source="computer_use_api",
        resources=request.path,
        error="agent-desktop setup requires the dashboard owner",
    )
    return _owner_denial_response(request, "dashboard user required", "dashboard_user_required")


def _driver_active() -> str:
    """``agent-desktop`` / ``native`` / ``unsupported`` from the shared backend's class.

    Read by class name so this module does not import the drivers, and
    defensively: a backend that fails to build reports ``unsupported``.
    """
    try:
        from kiro_crew.computer_use.backend import get_shared_backend

        name = type(get_shared_backend()).__name__
    except Exception:  # noqa: BLE001 - status must answer
        logger.debug("computer-use backend unavailable for status", exc_info=True)
        return "unsupported"
    lowered = name.lower()
    if "agentdesktop" in lowered:
        return "agent-desktop"
    if "unsupported" in lowered or lowered.startswith("linux"):
        return "unsupported"
    return "native"


async def _status_payload() -> dict[str, Any]:
    ready = await asyncio.to_thread(agent_desktop_install.readiness)
    driver = await asyncio.to_thread(_driver_active)
    return {
        "installed": bool(ready.get("installed")),
        "version": ready.get("version"),
        "pinned": ready.get("pinned"),
        "path": ready.get("path"),
        "driver_active": driver,
        "permissions": ready.get("permissions") or agent_desktop_install._unknown_permissions(),
        "job": _job.snapshot(),
    }


async def api_agent_desktop_status(request: web.Request) -> web.Response:
    """GET status: readiness plus the job. Never spawns npm."""
    return web.json_response(await _status_payload())


def _start_job() -> None:
    loop = asyncio.get_running_loop()
    job = _Job(state=JOB_RUNNING, scope=InstallScope())
    global _job
    _job = job

    def _on_log(text: str) -> None:
        loop.call_soon_threadsafe(job.append, text)

    async def _run() -> None:
        assert job.scope is not None
        try:
            result = await asyncio.to_thread(
                run_in_scope, job.scope, agent_desktop_install.install, _on_log
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(job.scope.terminate)
            job.state, job.error = JOB_FAILED, "interrupted: the gateway stopped this install"
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            job.state = JOB_FAILED
            job.error = agent_desktop_install.redact_install_output(str(exc))[-2000:]
            return
        if result.get("ok") and not job.scope.terminated:
            job.state, job.error = JOB_DONE, None
        else:
            job.state = JOB_FAILED
            step = result.get("step") or "install"
            job.error = f"{step}: {result.get('error') or 'failed'}"

    job.task = asyncio.create_task(_run())


async def api_agent_desktop_install(request: web.Request) -> web.Response:
    """POST install: start (or join) the background npm install; answers the status."""
    denied = _deny_non_owner(request, "computer_use_agent_desktop_install")
    if denied is not None:
        return denied
    # A second click joins the running job: npm is not safe twice over one prefix.
    if _job.state != JOB_RUNNING:
        _start_job()
    return web.json_response(await _status_payload(), status=202)


async def api_agent_desktop_permissions_guide(request: web.Request) -> web.Response:
    """POST permissions-guide: open the macOS grant flow via ``permissions --guide``."""
    denied = _deny_non_owner(request, "computer_use_agent_desktop_permissions_guide")
    if denied is not None:
        return denied
    result = await asyncio.to_thread(agent_desktop_install.open_permissions_guide)
    if not result.get("ok"):
        return web.json_response(
            {"ok": False, "error": result.get("error"), "code": "permissions_guide_failed"},
            status=409,
        )
    return web.json_response({"ok": True, "error": None})


async def stop_agent_desktop_install() -> None:
    """Kill a running install's npm child and wait for its task. Never raises."""
    job = _job
    if job.scope is not None:
        try:
            await asyncio.to_thread(job.scope.terminate)
        except RuntimeError:
            job.scope.terminate()
    task = job.task
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
        await asyncio.wait({task})
        if not task.cancelled():
            task.exception()


def register_routes(app: web.Application) -> None:
    """Add the three endpoints and the shutdown hook. Call before the app freezes."""
    app.router.add_get("/api/computer-use/agent-desktop/status", api_agent_desktop_status)
    app.router.add_post("/api/computer-use/agent-desktop/install", api_agent_desktop_install)
    app.router.add_post(
        "/api/computer-use/agent-desktop/permissions-guide",
        api_agent_desktop_permissions_guide,
    )

    async def _cleanup(_app: web.Application) -> None:
        try:
            await stop_agent_desktop_install()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.debug("agent-desktop install stop failed during shutdown", exc_info=True)

    app.on_cleanup.append(_cleanup)
