"""``crew_list``: the hub's read-only roster of connected crews (remote dispatch, audit D1).

``GET /api/session-control/crews`` answers one row per registered crew so a
conductor can see where it may dispatch before it calls ``session_create(crew=)``
or leaves the pick to placement. The route takes the ``crew=`` verbs' gates in their
order (switch, ``agent.session_control``, :func:`authorize_remote_dispatch`) and
makes only the peer reads two existing callers already make, per connected crew:

* placement's own probe, under its own bound: the version gate
  (:func:`remote_dispatch_routes.check_version`), then, only when the version
  matches, the headroom read placement ranks on (``GET api/chat/headroom``);
* the capability read the crew window's header uses
  (:func:`handlers_instances_peer.read_peer_capabilities`), for the version text and
  the agent and model names, run alongside so a slow model catalog costs only
  the names.

A row carries ids, names, a state word, numbers and the peer's agent and model
names, each already clamped and redacted by those helpers. No host, port, path,
profile or token from the registry row is read into it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes
from kiro_crew.dashboard.handlers.session_control import _counted, _require_internal

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: The audit verb: ``session_control_crew_list``.
VERB = "list"

#: Most crews one answer lists. A registry is the owner's own file; this bounds
#: the fan-out of peer reads one call can start.
MAX_CREWS = 32

#: Seconds the roster read may take: past the carrier's own models timeout
#: (``DEFAULT_MODELS_CAPABILITY_PROXY_TIMEOUT_SECS``, 20 s) and inside the MCP
#: body's 30 s wait (``mcp_crew_list.GET_TIMEOUT_S``).
ROSTER_TIMEOUT_S = 22.0

#: Largest ``open_workers`` shown; a peer reply past it reads as unknown.
_MAX_WORKERS = 10_000

#: Headroom fields shown, as placement and the peer gate read them.
_NUMBER_FIELDS = ("mem_free_gb", "disk_free_gb", "load_per_cpu", "measured_at")


def _headroom(payload: object) -> dict[str, Any] | None:
    """The peer snapshot re-typed to known fields, or ``None`` when it is not one."""
    from kiro_crew import resource_status
    from kiro_crew.dashboard.handlers.remote_placement import _number

    if not isinstance(payload, dict):
        return None
    out: dict[str, Any] = {key: _number(payload.get(key)) for key in _NUMBER_FIELDS}
    cpus = payload.get("cpu_count")
    out["cpu_count"] = cpus if isinstance(cpus, int) and not isinstance(cpus, bool) else None
    posture = payload.get("posture")
    known = (
        resource_status.POSTURE_AMPLE,
        resource_status.POSTURE_TIGHT,
        resource_status.POSTURE_CRITICAL,
    )
    out["posture"] = posture if posture in known else resource_status.POSTURE_UNKNOWN
    return out


def _open_workers(payload: object) -> int | None:
    """The peer's own count of open ``hub:`` workers (its ``crew_busy`` gate's figure)."""
    value = payload.get("open_workers") if isinstance(payload, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_WORKERS:
        return None
    return value


def _names(rows: object, key: str) -> list[str]:
    if not isinstance(rows, list):
        return []
    return [r[key] for r in rows if isinstance(r, dict) and isinstance(r.get(key), str) and r[key]]


async def _row(mgr: Any, row: Any, bound: dict[str, int]) -> dict[str, Any]:
    from kiro_crew.dashboard.handlers.remote_ingest import _crew_state

    instance_id = str(getattr(row, "id", "") or "")
    out: dict[str, Any] = {
        "id": instance_id,
        "name": str(getattr(row, "name", "") or instance_id),
        "crew_state": _crew_state(mgr, instance_id),
        "version": "",
        "version_match": None,
        "headroom": None,
        "open_workers": None,
        # Open bindings this hub holds on the crew: placement's reservations.
        "bound_here": int(bound.get(instance_id, 0)),
        "agents": [],
        "default_agent": "",
        "models": [],
        "unavailable": [],
    }
    if out["crew_state"] != "connected":
        return out
    await asyncio.gather(_placement_probe(mgr, instance_id, out), _roster(mgr, instance_id, out))
    return out


async def _placement_probe(mgr: Any, instance_id: str, out: dict[str, Any]) -> None:
    """Placement's own probe (version gate, then headroom), under its own bound."""
    from kiro_crew.dashboard.handlers.remote_placement import PROBE_TIMEOUT_S
    from kiro_crew.dashboard.remote_dispatch_headroom import NO_LOGINS_QUERY

    async def _probe() -> None:
        try:
            await routes.check_version(mgr, instance_id)
        except routes.CrewCallError as exc:
            if exc.code == "crew_version_mismatch":
                out["version_match"] = False
                return
            raise
        out["version_match"] = True
        status, payload = await routes.peer_call(
            mgr, instance_id, "GET", "api/chat/headroom", params=dict(NO_LOGINS_QUERY)
        )
        if 200 <= status < 300:
            out["headroom"] = _headroom(payload)
            out["open_workers"] = _open_workers(payload)
        else:
            out["unavailable"].append("headroom")

    try:
        await asyncio.wait_for(_probe(), PROBE_TIMEOUT_S)
    except routes.CrewCallError as exc:
        logger.info("crew_list: probe on %s refused (%s)", instance_id, exc.code)
        out["unavailable"].append("headroom")
    except asyncio.TimeoutError:
        logger.info("crew_list: probe on %s timed out", instance_id)
        out["unavailable"].append("headroom")
    except Exception:  # noqa: BLE001 - one crew's fault must not fail the list
        logger.warning("crew_list: probe on %s failed", instance_id, exc_info=True)
        out["unavailable"].append("headroom")


async def _roster(mgr: Any, instance_id: str, out: dict[str, Any]) -> None:
    """The crew window header's capability read, for the version and the names.

    Not under placement's bound: a cold model catalog may take the carrier's own
    models timeout, and must not cost the version gate or the headroom above.
    """
    from kiro_crew.dashboard.handlers_instances_peer import read_peer_capabilities

    try:
        caps = await asyncio.wait_for(read_peer_capabilities(mgr, instance_id), ROSTER_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.info("crew_list: roster read on %s timed out", instance_id)
        out["unavailable"].extend(["agents", "models"])
        return
    except Exception:  # noqa: BLE001 - one crew's fault must not fail the list
        logger.warning("crew_list: roster read on %s failed", instance_id, exc_info=True)
        out["unavailable"].extend(["agents", "models"])
        return
    out["version"] = caps.get("version") or ""
    out["agents"] = _names(caps.get("agents"), "name")
    out["default_agent"] = caps.get("default_agent") or ""
    out["models"] = _names(caps.get("models"), "model_name")
    missing = caps.get("unavailable")
    if isinstance(missing, dict):
        out["unavailable"].extend(k for k in ("agents", "models") if k in missing)


async def list_crews(state: "DashboardState") -> tuple[list[dict[str, Any]], int]:
    """``(rows, omitted)``: one row per registered crew up to :data:`MAX_CREWS`,
    probed at once, and how many more are registered. Raises :class:`CrewCallError`."""
    from kiro_crew.dashboard.handlers.remote_ingest import open_remote_bindings

    mgr = routes._manager(state)
    try:
        rows = await asyncio.to_thread(routes._instances, state)
    except Exception:
        logger.debug("crew registry unreadable", exc_info=True)
        rows = []
    try:
        bound = await asyncio.to_thread(open_remote_bindings)
    except Exception:
        logger.info("crew_list: open remote bindings unreadable", exc_info=True)
        bound = {}
    rows = sorted(rows, key=lambda r: str(getattr(r, "id", "") or ""))
    shown = rows[:MAX_CREWS]
    listed = list(await asyncio.gather(*(_row(mgr, r, bound) for r in shown)))
    return listed, len(rows) - len(shown)


@_counted("other")
async def api_session_control_crews(request: web.Request) -> web.Response:
    """GET /api/session-control/crews — the connected-crew roster, conductor only."""
    from kiro_crew.dashboard import session_control as sc
    from kiro_crew.dashboard.handlers._shared import _read_session_key

    refused = await _require_internal(request)
    if refused is not None:
        return refused
    declared = _read_session_key(request)
    disabled = await routes.refuse_if_disabled(declared, VERB)
    if disabled is not None:
        return disabled
    if not await asyncio.to_thread(sc.session_control_enabled):
        routes._audit(declared, VERB, "denied", error="session_control_disabled")
        return web.json_response(
            {
                "error": "session control is disabled (agent.session_control)",
                "code": "session_control_disabled",
            },
            status=403,
        )
    try:
        await routes.authorize_remote_dispatch(request, VERB)
        crews, omitted = await list_crews(request.app["state"])
    except routes.CrewCallError as exc:
        routes._audit(declared, VERB, "denied" if exc.status < 500 else "failure", error=exc.code)
        return routes._response(exc)
    routes._audit(declared, VERB, "success")
    return web.json_response({"crews": crews, "omitted": omitted})
