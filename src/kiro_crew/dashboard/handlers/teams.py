"""Crewmate teams HTTP handlers -- ``/api/teams``.

A team is a name plus an ordered list of crewmates (``kiro_crew.crew_teams``). The
Crewmates page groups its roster by team and opens a team view per team; this
module is the list/create/rename/re-member/delete surface behind that.

Same posture as the members routes: dashboard-only (app tokens are denied with
404, deny-by-default), reads need the dashboard session, writes are OWNER
actions (``require_owner_dashboard_request``) because the document is the
owner's grouping and lives in a gateway-only masked directory
(``crew_teams.TEAMS_DIR_NAME``).

Members are validated against the registered crew names at write time so a
team can never be created around a crewmate that does not exist. That
validation and the write run under the crew registry's own config lock
(``agents._get_config_lock``), the lock the crew-delete route holds while it
removes a crew and drops it from its team (``crew_teams.drop_member``) -- so a
delete cannot slip between "the crew exists" and "the team is written" and
leave a team naming a crewmate that is gone -- and the lock the crew-create
route holds while it purges the new name from any team it was left on
(``crew_teams.release_name``) and registers it, so a recreated crew never
inherits a deleted one's membership.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew import crew_teams as teams_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.members import _deny_app_caller, _member_name_is_addressable

logger = logging.getLogger(__name__)

#: ``?team=`` ids are minted by ``teams._new_team_id`` (12 lowercase hex);
#: anything else in the path is refused before any file is touched -- the same
#: spelling the store itself accepts on read (``crew_teams._TEAM_ID_RE``), so a
#: malformed id is 400 ``invalid_team_id``, never a 404 that looks like a miss.


async def _require_owner_write(request: web.Request, operation: str) -> web.Response | None:
    """The shared owner gate, plus an audit of the ALLOW.

    ``require_owner_dashboard_request`` records only the denial. The owner's
    allow is a permission decision too, and every one of them is a SEL event,
    so the three writes record it here -- an enqueue, non-fatal like the
    denial's own, and the write proceeds either way.
    """
    denied = await require_owner_dashboard_request(request, operation)
    if denied is not None:
        return denied
    try:
        from kiro_crew.sel import sel as _sel

        _sel().log_api_access(
            caller=str(request.get("user") or "unknown"),
            operation=operation,
            outcome="allowed",
            source="dashboard",
            resources="crew_teams",
        )
    except Exception:
        logger.debug("SEL allow event for %s not recorded", operation, exc_info=True)
    return None


def _team_error(exc: teams_mod.TeamError) -> web.Response:
    status = 404 if exc.code == "team_not_found" else 400
    return web.json_response({"error": str(exc), "code": exc.code}, status=status)


def _unreadable() -> web.Response:
    return web.json_response(
        {"error": "the team list could not be read", "code": "teams_unreadable"},
        status=500,
    )


def _write_failed() -> web.Response:
    return web.json_response(
        {"error": "could not persist the team list", "code": "teams_write_failed"},
        status=500,
    )


def _valid_team_id(raw: str) -> bool:
    return teams_mod._TEAM_ID_RE.fullmatch(raw) is not None


def _config_lock():
    """The crew registry's lock, resolved late like ``members.py`` does: the
    agents handler module is heavy and imports this package's siblings, so a
    module-level import here would be a cycle."""
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    return _get_config_lock()


def _known_crews(cfg: KiroCrewConfig) -> set[str]:
    # The same filter the roster applies (``GET /api/members``): a config key
    # that is not a dispatchable Crew Member display name has no roster row and
    # cannot be on a team. Crew names are display text (``dr. eggbot`` is a
    # crew), so the identifier grammar is not the test; the store's own
    # ``MEMBER_NAME_MAX_CHARS`` -- the same display-name cap -- bounds the write.
    return {name for name in cfg.agents if _member_name_is_addressable(name)}


def _registered_crews() -> set[str]:
    """The registry as it is RIGHT NOW -- the store calls this inside its
    document lock (``crew_teams.KnownCrews``), which is what closes the window
    between "the crew exists" and "the team is written" against a removal in
    another process: ``kirocrew agents delete`` holds the same lock around its
    registry write, so it cannot land between the two."""
    return _known_crews(KiroCrewConfig.load())


async def api_teams_list(request: web.Request) -> web.Response:
    """GET /api/teams -- every team, in the user's order."""
    denied = await _deny_app_caller(request, "teams.list")
    if denied is not None:
        return denied
    try:
        teams = await asyncio.to_thread(teams_mod.read_teams)
    except teams_mod.TeamsUnreadable:
        logger.warning("teams read failed", exc_info=True)
        return _unreadable()
    # Reconciled against the live registry: a crew removed by a path that never
    # reached ``drop_member`` (the CLI, a package prune) is not answered as a
    # member. See ``crew_teams.prune_unknown``.
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    teams = teams_mod.prune_unknown(teams, _known_crews(cfg))
    return web.json_response({"teams": [t.to_dict() for t in teams]})


async def _read_body(request: web.Request) -> dict | web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    return body


async def api_teams_create(request: web.Request) -> web.Response:
    """POST /api/teams ``{name, members}`` -- append a team.

    The listed crewmates leave whichever team held them (one team per crewmate,
    enforced by the store in the same write).
    """
    denied = await _deny_app_caller(request, "teams.create")
    if denied is not None:
        return denied
    # Owner gate BEFORE input validation, like the rules write: the non-owner
    # answer stays a uniform 401/403, never a 400 that leaks what validates.
    owner_denied = await _require_owner_write(request, "teams.write")
    if owner_denied is not None:
        return owner_denied
    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body
    try:
        # Two locks. The in-process registry lock: the dashboard's crew-delete
        # route holds it from removing the crew through dropping it from its
        # team, and the crew-create route from purging the name through
        # registering it. The store's own document lock, inside which
        # `_registered_crews` is evaluated, keeps the write's registry snapshot
        # as fresh as the store can make it against `kirocrew agent delete` in
        # another process.
        async with _config_lock():
            team = await asyncio.to_thread(
                teams_mod.create_team,
                body.get("name"),
                body.get("members", []),
                known=_registered_crews,
            )
    except teams_mod.TeamError as exc:
        return _team_error(exc)
    except teams_mod.TeamsUnreadable:
        logger.warning("teams read failed", exc_info=True)
        return _unreadable()
    except OSError:
        logger.warning("teams write failed", exc_info=True)
        return _write_failed()
    return web.json_response({"team": team.to_dict()}, status=201)


async def api_teams_update(request: web.Request) -> web.Response:
    """PUT /api/teams/{id} ``{name?, members?, add?, remove?}``.

    ``members`` replaces the list; ``add`` / ``remove`` change only the named
    crewmates against the list as it stands (what the dialog sends, so two
    tabs editing one team never overwrite each other's membership).
    """
    denied = await _deny_app_caller(request, "teams.update")
    if denied is not None:
        return denied
    owner_denied = await _require_owner_write(request, "teams.write")
    if owner_denied is not None:
        return owner_denied
    team_id = request.match_info["id"]
    if not _valid_team_id(team_id):
        return web.json_response(
            {"error": "invalid team id", "code": "invalid_team_id"}, status=400
        )
    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body
    try:
        async with _config_lock():  # see api_teams_create
            team = await asyncio.to_thread(
                teams_mod.update_team,
                team_id,
                name=body.get("name"),
                members=body.get("members"),
                add=body.get("add"),
                remove=body.get("remove"),
                known=_registered_crews,
            )
    except teams_mod.TeamError as exc:
        return _team_error(exc)
    except teams_mod.TeamsUnreadable:
        logger.warning("teams read failed", exc_info=True)
        return _unreadable()
    except OSError:
        logger.warning("teams write failed", exc_info=True)
        return _write_failed()
    return web.json_response({"team": team.to_dict()})


async def api_teams_delete(request: web.Request) -> web.Response:
    """DELETE /api/teams/{id} -- remove a team; its crewmates keep their chats."""
    denied = await _deny_app_caller(request, "teams.delete")
    if denied is not None:
        return denied
    owner_denied = await _require_owner_write(request, "teams.write")
    if owner_denied is not None:
        return owner_denied
    team_id = request.match_info["id"]
    if not _valid_team_id(team_id):
        return web.json_response(
            {"error": "invalid team id", "code": "invalid_team_id"}, status=400
        )
    try:
        await asyncio.to_thread(teams_mod.delete_team, team_id)
    except teams_mod.TeamError as exc:
        return _team_error(exc)
    except teams_mod.TeamsUnreadable:
        logger.warning("teams read failed", exc_info=True)
        return _unreadable()
    except OSError:
        logger.warning("teams write failed", exc_info=True)
        return _write_failed()
    return web.json_response({"ok": True})
