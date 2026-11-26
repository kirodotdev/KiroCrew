"""``GET /v1/crew/bots``: the Bots a caller may use, with their status.

An app credential lists only the Bots of its own grant: the Bots the owner gave
when approving it, read from the grant the verified credential names
(:data:`crew_api_scopes.CREDENTIAL_KEY`). Nothing in the request chooses the grant,
and no row of any other Bot is built, so an app cannot learn from this route which
other Bots exist: a name its grant holds that no Bot has today is left out, and an
``after`` cursor is compared only with the ids the caller itself can list.

Which Bots those are is decided in one place, ``LocalAuthorization.bots_in_range``:
the Bot must still hold the member id it held when the owner gave it. A Bot made
later under the same name has a new one and is not listed, unless it was made
from the deleted Bot's kept memory store, which keeps the member id. A Bot that
has no member id is not listed; the default agent of an ordinary install has none.

The machine owner lists every Bot a turn can name, in the same shape.

Each row is the contract's ``Bot`` and nothing else, built by the function that
builds the installed apps' rows (``handlers.members_app``): the free text leaves
through the roster mask, ``avatar_url`` is null because the gateway has no image
address for a Bot, and ``status`` is ``busy`` while a turn runs in the Bot's own
thread or a turn noted as the Bot's runs (``_list_bots`` says which turns those
are). ``id`` is the Bot's name, the value ``model`` takes, where an installed app is
given the slug.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from aiohttp import web

from kiro_crew import member_goals
from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG
from kiro_crew.dashboard import local_authorization, running_bot_turns
from kiro_crew.dashboard.crew_api_auth import OWNER_ONLY_PUBLIC_ROUTES
from kiro_crew.dashboard.crew_api_errors import contract_error, request_id_for
from kiro_crew.dashboard.crew_api_grant_requests import _host_owner_refusal
from kiro_crew.dashboard.crew_api_owner import owner_route
from kiro_crew.dashboard.crew_api_scopes import CREDENTIAL_KEY

# The installed apps' Bot row, thread key and status value, through the members
# handlers' facade: one copy for both lists.
from kiro_crew.dashboard.handlers.members import STATUS_BUSY, _bot_row, _thread_key
from kiro_crew.members import MemberSlugError

logger = logging.getLogger(__name__)

PATH: Final = "/v1/crew/bots"
ROUTE_KEY: Final = ("GET", PATH)

#: The contract's ``Limit`` parameter.
LIMIT_DEFAULT: Final = 20
LIMIT_MAX: Final = 100


class _Unavailable(Exception):
    """The Bot roster or the grant record cannot be read now."""


class _Invalid(Exception):
    """A query value the contract does not allow."""


def _limit(request: web.Request) -> int:
    raw = request.query.get("limit")
    if raw is None:
        return LIMIT_DEFAULT
    # At most three digits: a longer run of digits is out of range whatever its value,
    # and ``int`` refuses a very long one.
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 3):
        raise _Invalid("limit must be a whole number from 1 to 100")
    value = int(raw)
    if not 1 <= value <= LIMIT_MAX:
        raise _Invalid("limit is out of range")
    return value


def _page(ids: list[str], after: str | None, limit: int) -> tuple[list[str], bool]:
    """The ids after *after*, at most *limit*, and whether more follow.

    *after* is looked up only in *ids*, the caller's own list, so the answer to a
    cursor outside it is the same whether or not a Bot of that name exists.
    """
    start = 0
    if after is not None:
        if after not in ids:
            raise _Invalid("after is not an id of this list")
        start = ids.index(after) + 1
    return ids[start : start + limit], len(ids) > start + limit


def _bot_identities(cfg: KiroCrewConfig) -> dict[str, str | None]:
    """Every Bot a turn can name, in config order, and which Bot each is."""
    if cfg.degraded_sections & {DEGRADED_WHOLE_CONFIG, "agents"}:
        raise _Unavailable("the Bot roster is degraded")
    return local_authorization.bot_identities(cfg)


def _row(cfg: KiroCrewConfig, name: str, member_id: str | None) -> tuple[dict[str, Any], str, str]:
    """One ``Bot`` (status still idle), its thread key and its member id (empty when it
    has none). Reads that Bot's files.

    The row is the installed apps' own (``members_app._bot_row``) with the Bot's name
    as its ``id``."""
    agent_cfg = cfg.agents[name]
    has_goal, thread_key = False, ""
    try:
        slug = members_mod.member_slug(name, cfg)
    except MemberSlugError:
        slug = ""
    if slug:
        try:
            has_goal = member_goals.has_goal(member_goals.read_goals(slug, name))
        except member_goals.GoalsUnreadable:
            has_goal = False
        thread_key = _thread_key(slug, name)
    return _bot_row(slug, name, agent_cfg, has_goal, bot_id=name), thread_key, member_id or ""


def _read(
    grant_id: str | None, after: str | None, limit: int
) -> tuple[list[tuple[dict[str, Any], str, str]], bool]:
    """The page's rows with their thread keys and member ids, and whether more follow.
    File IO.

    *grant_id* is the verified app credential's grant, or ``None`` for the machine
    owner. Only the Bots on the page are read from disk.
    """
    cfg = KiroCrewConfig.load()
    now_is = _bot_identities(cfg)
    if grant_id is None:
        ids = list(now_is)
    else:
        # The grant's own order, so the order says nothing of any other Bot. A grant
        # revoked or run out between token auth's check and this read has no Bot.
        ids = local_authorization.gateway_local_authorization().bots_in_range(grant_id, now_is)
    page, has_more = _page(ids, after, limit)
    return [_row(cfg, name, now_is.get(name)) for name in page], has_more


async def _list_bots(request: web.Request) -> web.Response:
    request_id = request_id_for(request)
    credential = request.get(CREDENTIAL_KEY)
    if credential is None:
        # Not a verified app credential: only the machine owner lists every Bot.
        denied = await _host_owner_refusal(request, OWNER_ONLY_PUBLIC_ROUTES[PATH])
        if denied is not None:
            return denied
    grant_id = credential.grant_id if credential is not None else None
    try:
        limit = _limit(request)
        rows, has_more = await asyncio.to_thread(_read, grant_id, request.query.get("after"), limit)
    except _Invalid as invalid:
        return contract_error(400, "invalid_request", str(invalid), request_id)
    except Exception:
        # Neither the roster's nor the grant record's text is logged.
        logger.warning("Crew API Bot list read failed")
        return contract_error(
            503, "instance_unavailable", "the Bot roster is unavailable", request_id
        )
    state = request.app.get("state")
    # Live state is read on the loop, as the owner's roster reads it. A Bot is busy
    # only where the turn is known to run as this Bot; a turn that cannot be tied to
    # it is not counted, so one Bot's work is never shown as another's. Two turns
    # are known: one in the Bot's own thread, found by the thread's binding as the
    # installed apps' list reads it, and a turn noted under this Bot's member id
    # (``running_bot_turns``): a dashboard turn (a chat, a ``POST /v1/responses`` or
    # ``/v1/chat/completions`` turn, a task review), a scheduled run of one agent,
    # a webhook turn that runs the registered agent.
    # shortcut: a channel turn reads idle, since it picks its agent by name and
    # holds no member id known to be the one it runs as. So does a helper that
    # carries the Bot's member id (a subagent, a workflow or task step, a Bot's
    # worker session outside its own schedule), an agent sequence, and a webhook
    # request that names another agent.
    slots = state._slots if state is not None else {}
    running = running_bot_turns.running_member_ids()
    data: list[dict[str, Any]] = []
    for row, thread_key, member_id in rows:
        slot = slots.get(thread_key) if thread_key else None
        if (slot is not None and slot.running) or (member_id and member_id in running):
            row["status"] = STATUS_BUSY
        data.append(row)
    return web.json_response(
        {
            "object": "list",
            "data": data,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
            "has_more": has_more,
        }
    )


#: GET /v1/crew/bots serves a verified app credential that holds ``bots:read`` or
#: ``bots:chat`` with the Bots of its grant. Every other caller passes the owner gate,
#: and the handler then asks for the machine owner.
api_bots = owner_route(_list_bots, OWNER_ONLY_PUBLIC_ROUTES[PATH], app_route=ROUTE_KEY)
