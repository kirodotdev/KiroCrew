"""The ``/`` menu's tail: what a chat's own live session advertised, then saved prompts.

:func:`kiro_crew.dashboard.handlers.agents.api_slash_commands` builds the list every
chat gets; :func:`menu_with_session_commands` appends the commands the ``?slot=``
chat's session listed in its last ``available_commands_update``, then the user's
opted-in saved prompts. Only a harness that runs its own commands keeps that list
(``LLMProvider.advertised_commands``), so on any other harness it adds nothing.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.dashboard.chat_utils import _DASHBOARD_SERVED_SLASH_COMMANDS
from kiro_crew.dashboard.chat_utils_slots import effective_session_key
from kiro_crew.dashboard.slot_ownership import app_owns_slot_session
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.quick_prompts import QUICK_PROMPTS, user_quick_prompt_menu


async def menu_with_session_commands(
    request: web.Request, rows: list[dict[str, str]], blocked: frozenset[str]
) -> list[dict[str, str]]:
    """*rows*, then what the requesting chat's session advertised, then the saved prompts.

    The saved prompts are expanded on every harness. An advertised name the dashboard
    lists, answers first, blocks, or expands as a quick or saved prompt is left out.
    """
    prompts = await asyncio.to_thread(user_quick_prompt_menu)
    taken = {row["name"] for row in rows + prompts} | blocked | set(QUICK_PROMPTS)
    return rows + _session_command_rows(request, taken | _DASHBOARD_SERVED_SLASH_COMMANDS) + prompts


def _session_command_rows(request: web.Request, taken: set[str]) -> list[dict[str, str]]:
    """Rows for the commands the ``?slot=`` chat's live session advertised, minus *taken*.

    Nothing for a request with no slot, a slot an app caller may not read as its own
    session, or a slot with no live session; the GET never starts one.
    """
    name = request.query.get("slot", "")
    if not name:
        return []
    state: DashboardState = request.app["state"]
    slot = state._slots.get(name)
    app = request.get("app", "")
    if slot is None or (app and not app_owns_slot_session(app, slot)):
        return []
    provider = state.sessions.get_provider(effective_session_key(slot))
    if provider is None:
        return []
    added = []
    for command in provider.advertised_commands():
        slash = "/" + command["name"]
        if slash not in taken:
            taken.add(slash)
            added.append({"name": slash, "description": command["description"]})
    return added
