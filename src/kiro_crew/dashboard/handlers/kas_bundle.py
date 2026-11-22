"""Dashboard handlers for Kiro Crew's own KAS engine bundle (Settings > Agent Harness).

Two routes, both thin over :mod:`kiro_crew.agent_sdk.kas_bundle`:

* ``GET /api/kas-bundle`` -- the pinned version, whether it is installed, whether
  ``agent.kas_bundle`` is on, the state of an install this gateway is running, and
  why this host cannot run the engine when it cannot.
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
    from kiro_crew.config.loader_document import kas_bundle_declared

    cfg = KiroCrewConfig.load().agent
    saved = kas_bundle_declared()
    # Unsaved, the choice is the engine the next KAS spawn really takes
    # (``kas_route``: the build's preference, the sign-in, cooldowns and pins all
    # decided), so the card never shows the engine chosen while chats run on kiro-cli.
    on_engine = bool(cfg.kas_bundle) if saved else bool(kas_bundle.kas_route().bundle)
    installed = kas_bundle.installed_bundle()
    state = _install_state(app)
    # The gateway's one installer also runs installs a KAS spawn started; this
    # app's own task is one of its callers, so either counts as installing.
    shared = kas_bundle.engine_install_snapshot()
    own = _installing(state)
    if own or not shared["running"]:
        done, total = int(state.get("done") or 0), int(state.get("total") or 0)
    else:
        done, total = shared["done"], shared["total"]
    host_unsupported = (
        kas_bundle.host_unsupported_reason() or shared["host_unsupported"]
        if installed is None
        else ""
    )
    error = (str(state.get("error") or "") or shared["error"]) if installed is None else ""
    return {
        "enabled": bool(cfg.kas_bundle),
        # The Settings engine choice: a saved ``agent.kas_bundle``, else the route the
        # next KAS spawn takes. ``choice_saved`` says which.
        "engine_choice": "engine" if on_engine else "kiro_cli",
        "choice_saved": saved,
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
        "installing": own or shared["running"],
        "progress": {"done": done, "total": total},
        # A failed install, this app's own or a spawn's, is reported until the
        # engine is installed; once it is, no earlier failure is current. When the
        # host cannot run the engine, that reason is the error: an older failure
        # (an offline install, say) would only give stale advice.
        "error": (host_unsupported or error) if error else "",
        # Why this host cannot run the engine (glibc, OS or architecture, or no
        # build for it in the manifest the last install fetched), '' when it can:
        # every install here is refused, so the panel offers none.
        "host_unsupported": host_unsupported,
        # The route the last KAS spawn took (``acp/kas_route.py``), so the panel
        # can say when Crew's own engine was chosen because kiro-cli cannot run.
        # ``None`` before the first KAS spawn in this gateway.
        "route": kas_bundle.last_kas_route(),
    }


def _chat_needs_sign_in() -> bool:
    """Whether a new chat would fail for lack of Kiro Crew's own sign-in.

    True only with KAS as the main harness and the next spawn on the bundle
    route with no usable Crew sign-in. The gateway decides, so the dashboard
    never guesses the route. Blocking; False when it cannot be worked out.
    """
    from kiro_crew.agent_sdk import kas_bundle
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        kas = KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS
    except Exception:  # noqa: BLE001 - unreadable config: no prompt
        return False
    return kas and kas_bundle.bundle_route_lacks_sign_in()


def _sign_in_moves_to_engine() -> bool:
    """Whether signing in to Kiro Crew would move new chats onto its engine.

    True with KAS as the main harness, kiro-cli signed in here, Kiro Crew not, and
    a build that prefers the engine (``kas_route.crew_sign_in_moves_to_bundle``).
    Blocking; False when it cannot be worked out.
    """
    from kiro_crew.agent_sdk import kas_bundle
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        kas = KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS
    except Exception:  # noqa: BLE001 - unreadable config: no prompt
        return False
    return kas and kas_bundle.crew_sign_in_moves_to_bundle()


async def _reply_payload(request: web.Request) -> dict[str, Any]:
    """The status both routes answer with, so every reply carries every field.

    The dashboard stores either reply in one cache entry: an install reply that
    left ``chat_needs_sign_in`` out would hide the chat's sign-in mid-flow.
    """
    payload = await asyncio.to_thread(_status_payload, request.app)
    # Owner only: signing in is an owner action (``kas_login._require_owner``),
    # so a prompt to sign in is no use to anyone else.
    owner = is_owner_dashboard_request(request)
    payload["chat_needs_sign_in"] = owner and (await asyncio.to_thread(_chat_needs_sign_in))
    payload["sign_in_moves_to_engine"] = (
        owner
        and not payload["chat_needs_sign_in"]
        and (await asyncio.to_thread(_sign_in_moves_to_engine))
    )
    return payload


async def api_kas_bundle_status(request: web.Request) -> web.Response:
    return web.json_response(await _reply_payload(request))


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

    # Through the gateway's one installer, so a click while a KAS spawn is
    # installing waits for that install instead of racing it.
    kas_bundle.install_pinned_engine(
        KiroCrewConfig.load().agent.kas_bundle_url, caller="dashboard", on_progress=_on_progress
    )


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
    return web.json_response(await _reply_payload(request), status=202)
