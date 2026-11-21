"""``GET /v1/models``: the agents and Bots a Crew API caller can name as ``model``."""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from kiro_crew import members as members_mod
from kiro_crew.agent_discovery import list_agents
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.crew_api_auth import OWNER_ONLY_PUBLIC_ROUTES
from kiro_crew.dashboard.crew_api_owner import owner_route
from kiro_crew.executors import discovery_executor


def _row(model_id: str, kind: str, description: str) -> dict[str, Any]:
    crew = {"kind": kind, "name": model_id}
    if description:
        crew["description"] = description
    return {"id": model_id, "object": "model", "owned_by": "crew", "crew": crew}


def usable_models() -> list[dict[str, Any]]:
    """Every ``model`` id the owner can run a turn on. Reads disk: call off the loop.

    A Bot is listed by its name and an agent template by its spec name, the two
    spellings a turn already accepts. A Bot shadows a template of the same name, as
    it does when a turn resolves the name.
    """
    rows = {
        name: _row(name, "bot", "")
        for name in KiroCrewConfig.load().agents
        if members_mod.is_dispatchable_member_name(name)
    }
    for info in list_agents():
        rows.setdefault(info.name, _row(info.name, "agent", info.description))
    return list(rows.values())


async def usable_models_off_loop() -> list[dict[str, Any]]:
    return await asyncio.get_running_loop().run_in_executor(discovery_executor(), usable_models)


async def _list_models(request: web.Request) -> web.Response:
    return web.json_response({"object": "list", "data": await usable_models_off_loop()})


#: GET /v1/models. Served by the owner gate today, like the Owner tier: the gate
#: refuses every other caller before the catalog is read.
api_models = owner_route(_list_models, OWNER_ONLY_PUBLIC_ROUTES["/v1/models"])
