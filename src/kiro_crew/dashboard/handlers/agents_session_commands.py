"""The ``/`` menu's tail: what a chat's own live session advertised, then saved prompts.

:func:`kiro_crew.dashboard.handlers.agents.api_slash_commands` builds the list every
chat gets; :func:`menu_with_session_commands` appends the commands the ``?slot=``
chat's session listed in its last ``available_commands_update``, then the user's
opted-in saved prompts. Only a harness that runs its own commands keeps that list
(``LLMProvider.advertised_commands``), so on any other harness it adds nothing.
:func:`menu_backend` names the backend whose base list that chat gets.
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


async def menu_backend(request: web.Request, default: str) -> str:
    """The backend whose commands the ``?slot=`` chat's base menu lists.

    It is the backend the slash gate resolves for that chat's session, so the menu
    offers the commands the gate runs there. A request with no slot, or with a slot
    an app caller may not read as its own session, gets *default*, the configured
    backend. The lookup never starts a session.
    """
    slot = _requested_slot(request)
    if slot is None:
        return default
    from kiro_crew.dashboard.chat_runner import _slash_command_backend

    return await _slash_command_backend(request.app["state"], effective_session_key(slot))


def _requested_slot(request: web.Request):
    """The ``?slot=`` chat, or None when the request names no slot the caller may read."""
    name = request.query.get("slot", "")
    if not name:
        return None
    state: DashboardState = request.app["state"]
    slot = state._slots.get(name)
    app = request.get("app", "")
    if slot is None or (app and not app_owns_slot_session(app, slot)):
        return None
    return slot


def _session_command_rows(request: web.Request, taken: set[str]) -> list[dict[str, str]]:
    """Rows for the commands the ``?slot=`` chat's live session advertised, minus *taken*.

    Nothing for a request with no slot, a slot an app caller may not read as its own
    session, or a slot with no live session; the GET never starts one.
    """
    slot = _requested_slot(request)
    if slot is None:
        return []
    state: DashboardState = request.app["state"]
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
