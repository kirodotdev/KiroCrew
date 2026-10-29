"""``GET /api/shared-crew/sessions``, ``/people`` and ``/mode``.

All three answer only the owner's dashboard session or a tailnet person the
access set admitted (``request["is_dashboard_user"] is True``). An app token or
an internal caller is refused with 403: these routes are new, and admitting a
caller the existing session list confines (an app sees only its own sessions
there) would widen what that caller can read. A non-owner dashboard session (a
channel allowlist link) is refused too, as the existing session list refuses it.

Neither route reads an identity from the request. The viewer is the actor the
auth middleware published.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.shared_crew.attribution import creator_of_public
from kiro_crew.shared_crew.codec import ContractError
from kiro_crew.shared_crew.contracts import Principal, SessionPage, SessionQuery
from kiro_crew.shared_crew.identity import viewer_principal
from kiro_crew.shared_crew.people import people_view
from kiro_crew.shared_crew.people import registry as people_registry
from kiro_crew.shared_crew.session_query import query_sessions

logger = logging.getLogger(__name__)

SESSIONS_PATH = "/api/shared-crew/sessions"
PEOPLE_PATH = "/api/shared-crew/people"
MODE_PATH = "/api/shared-crew/mode"


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


def _tailnet_person(request: web.Request) -> bool:
    """A person the direct listener admitted with no token, after checking the access set.

    Only that path checks the set before it names the person. A token-holder
    whose request merely resolves to a tailnet peer (attribution only) is not
    one, and goes through the owner gate with its token subject.
    """
    from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY, PrincipalKind
    from kiro_crew.shared_crew.identity import PROVIDER_TAILSCALE

    if request.get("auth_tailnet_direct") is not True:
        return False
    principal = getattr(request.get(ACTOR_REQUEST_KEY), "principal", None)
    return (
        principal is not None
        and principal.provider == PROVIDER_TAILSCALE
        and principal.kind is PrincipalKind.HUMAN
    )


async def _refusal(request: web.Request) -> web.Response | None:
    if request.get("is_dashboard_user") is not True or request.get("app"):
        return _error(403, "forbidden", "dashboard user required")
    if _tailnet_person(request):
        return None
    from kiro_crew.dashboard.handlers._shared_access import require_owner_for_dashboard_caller

    return await require_owner_for_dashboard_caller(request, "shared_crew_read")


async def _catalog_rows(request: web.Request) -> list[dict[str, Any]]:
    state = request.app.get("state")
    log = getattr(state, "conversation_log", None) if state is not None else None
    if log is None:
        return []
    return await asyncio.to_thread(log.list_sessions)


def _machine_only(key: str) -> bool:
    from kiro_crew.dashboard.handlers.sessions import _is_machine_only_session

    return _is_machine_only_session(key)


async def api_shared_crew_sessions(request: web.Request) -> web.Response:
    """The creator-filtered, paged session list (``SessionPage`` plus ``viewer``)."""
    refused = await _refusal(request)
    if refused is not None:
        return refused
    try:
        query = SessionQuery.from_query(request.query)
    except ContractError as exc:
        return _error(400, exc.code, str(exc))
    viewer = viewer_principal(request)
    rows = await _catalog_rows(request)
    try:
        page: SessionPage = query_sessions(rows, query, viewer, exclude=_machine_only)
    except ContractError as exc:
        return _error(400, exc.code, str(exc))
    body = page.to_dict()
    body["viewer"] = viewer.to_public_dict()
    body["creator"] = query.creator
    return web.json_response(body)


async def api_shared_crew_people(request: web.Request) -> web.Response:
    """Observed collaborators, labelled by source, and the directory state."""
    refused = await _refusal(request)
    if refused is not None:
        return refused
    from kiro_crew.shared_crew.routes_invites import decorate_people

    rows = [r for r in await _catalog_rows(request) if not _machine_only(str(r.get("key") or ""))]
    body = people_view(viewer_principal(request), rows)
    return web.json_response(await asyncio.to_thread(decorate_people, body))


def _observed_principals() -> list[Principal]:
    """People this process saw, each with the provenance the server derived."""
    return [obs.principal for obs in people_registry().snapshot()]


def _session_creators(rows: list[dict[str, Any]]) -> list[Principal]:
    """Session creators read back from public attribution, which has no provenance."""
    return [creator_of_public(row.get("attribution")) for row in rows]


def _owner_id(request: web.Request) -> str:
    state = request.app.get("state")
    return str(getattr(state, "owner_id", "") or "") if state is not None else ""


async def api_shared_crew_mode(request: web.Request) -> web.Response:
    """The Crew's People/setup mode (``crew_mode.mode_view``)."""
    refused = await _refusal(request)
    if refused is not None:
        return refused
    from kiro_crew.gateway_identity import gateway_id
    from kiro_crew.shared_crew.access import setup_paths_dict
    from kiro_crew.shared_crew.crew_mode import mode_view

    rows = [r for r in await _catalog_rows(request) if not _machine_only(str(r.get("key") or ""))]
    crew_id = await asyncio.to_thread(gateway_id)
    body = mode_view(
        viewer_principal(request),
        _observed_principals(),
        setup_paths=setup_paths_dict(),
        crew_id=crew_id,
        creators=_session_creators(rows),
        owner_id=_owner_id(request),
    )
    return web.json_response(body)


def register(app: web.Application) -> None:
    """Register the identity routes on *app*."""
    app.router.add_get(SESSIONS_PATH, api_shared_crew_sessions)
    app.router.add_get(PEOPLE_PATH, api_shared_crew_people)
    app.router.add_get(MODE_PATH, api_shared_crew_mode)
