"""Dashboard handlers for Kiro Crew's own KAS engine bundle (Settings > Agent Harness).

Two routes, both thin over :mod:`kiro_crew.agent_sdk.kas_bundle`:

* ``GET /api/kas-bundle`` -- the pinned version, whether it is installed, whether
  ``agent.kas_bundle`` is on, and the state of an install this gateway is running.
* ``POST /api/kas-bundle/install`` -- install this release's pinned engine in the
  background. Owner-only and SEL-audited: it writes the gateway-owned ``engines``
  leaf and fetches ~55 MB, so a non-owner allowed user gets a coded 403.

The switch itself is NOT here. It is ``agent.kas_bundle`` and goes through the
ordinary config PATCH, so it carries the same validation, audit and
restart-required marking as every other setting.

The install runs in the GATEWAY process (outside every agent sandbox), which is
the only place the ``engines`` leaf is writable. One install per gateway at a
time: a second POST while one is running answers the running one's state rather
than starting another, so a double click cannot race two writers into the same
staging directory. The in-flight state lives on the aiohttp app, not in a module
global, so two apps in one process (tests) do not see each other's install.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_STATE_KEY = "kas_bundle_install"


def _install_state(app: web.Application) -> dict[str, Any]:
    state = app.get(_STATE_KEY)
    if not isinstance(state, dict):
        state = {"task": None, "error": "", "done": 0, "total": 0}
        app[_STATE_KEY] = state
    return state


def _installing(state: dict[str, Any]) -> bool:
    task = state.get("task")
    return isinstance(task, asyncio.Task) and not task.done()


def _status_payload(app: web.Application) -> dict[str, Any]:
    """Blocking (reads config and the engines directory); call off the loop."""
    from kiro_crew.agent_sdk import kas_bundle
    from kiro_crew.agent_sdk.kas_bundle import KAS_PIN_VERSION, pinned_version
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig.load().agent
    installed = kas_bundle.installed_bundle()
    state = _install_state(app)
    return {
        "enabled": bool(cfg.kas_bundle),
        "pinned_version": KAS_PIN_VERSION,
        "runs_version": pinned_version(),
        "installed": (
            {
                "version": installed.version,
                "node_version": installed.node_version,
                "bundled": installed.bundled,
            }
            if installed is not None
            else None
        ),
        "installing": _installing(state),
        "progress": {"done": int(state.get("done") or 0), "total": int(state.get("total") or 0)},
        "error": str(state.get("error") or ""),
    }


async def api_kas_bundle_status(request: web.Request) -> web.Response:
    return web.json_response(await asyncio.to_thread(_status_payload, request.app))


async def _audit(request: web.Request, outcome: str, error: str = "") -> None:
    user = request.get("user", "")

    def _log() -> None:
        sel().log_api_access(
            caller=str(user) if user else "dashboard-user",
            operation="kas_bundle_install",
            outcome=outcome,
            source="dashboard",
            resources=request.path,
            error=error,
        )

    try:
        await asyncio.to_thread(_log)
    except Exception:
        logger.debug("Could not audit KAS bundle install", exc_info=True)


def _run_install(state: dict[str, Any]) -> None:
    """The blocking install. Progress is summed across the two artifacts."""
    from kiro_crew.agent_sdk import kas_bundle
    from kiro_crew.config.loader import KiroCrewConfig

    totals: dict[str, tuple[int, int]] = {}

    def _on_progress(label: str, done: int, total: int) -> None:
        totals[label] = (done, total)
        state["done"] = sum(d for d, _ in totals.values())
        state["total"] = sum(t for _, t in totals.values())

    kas_bundle.install_pinned(KiroCrewConfig.load().agent.kas_bundle_url, on_progress=_on_progress)


async def _install_task(state: dict[str, Any]) -> None:
    from kiro_crew.agent_sdk.backend_install import forget_probe
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
    from kiro_crew.agent_sdk.kas_bundle import KasBundleError

    try:
        await asyncio.to_thread(_run_install, state)
        # The Agent Harness panel's KAS row answers from the install probe, which
        # on the bundle route reads the engine this just laid down; its cached
        # "missing" verdict would otherwise outlive the install for the TTL.
        forget_probe(ACP_BACKEND_KAS)
    except KasBundleError as exc:
        # KasBundleError's text is the remedy and names no credential.
        state["error"] = str(exc)
        logger.warning("KAS bundle install failed: %s", exc)
    except Exception:
        state["error"] = "The KAS engine install failed; see the gateway log."
        logger.exception("KAS bundle install failed")


async def api_kas_bundle_install(request: web.Request) -> web.Response:
    if not is_owner_dashboard_request(request):
        await _audit(request, "denied", error="non-owner caller")
        return web.json_response(
            {"error": "Only the dashboard owner can install the KAS engine.", "code": "owner_only"},
            status=403,
        )
    state = _install_state(request.app)
    if not _installing(state):
        state.update(error="", done=0, total=0)
        state["task"] = asyncio.create_task(_install_task(state))
        await _audit(request, "started")
    payload = await asyncio.to_thread(_status_payload, request.app)
    return web.json_response(payload, status=202)
