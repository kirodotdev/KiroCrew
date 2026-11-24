"""Raise an ``mcp.project_server`` card for each project server a new session held back.

The session held the servers (``agent_sdk.project_mcp_holds``); this asks the same question
for the chat's folder and host, and puts one card per held server in the chat, so the
person sees the server, its launch and the file, and starts it with one press. A card
already waiting for the same launch is not raised twice. A file the host reads itself
that Crew cannot read gets one notice row instead (``unreadable_notice``). Best-effort:
a failure here raises no card and never fails the turn.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_REASONS = {
    "new": "Held back: you haven't allowed this server in this folder yet.",
    "changed": "Held back: its command changed since you allowed it here.",
    "settings": "Held back: its settings changed since you allowed it here.",
}


def _reason(server: Any) -> str:
    """The card's reason line; a changed server names whether its command changed."""
    from kiro_crew.project_mcp_approvals import launch_digest

    if server.status != "changed":
        return _REASONS["new"]
    if (server.approved or {}).get("launch_digest") == launch_digest(server.entry):
        return _REASONS["settings"]
    return _REASONS["changed"]


async def offer_project_mcp_cards(
    state: Any, slot: Any, client: Any, session_key: str, agent: str = ""
) -> None:
    """Propose the cards for *client*'s new session in *slot*.

    *agent* is the agent the session runs, which can differ from the slot's own.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
    from kiro_crew.agent_sdk.capabilities import UNKNOWN_BACKEND_CAPABILITIES, capabilities_of
    from kiro_crew.agent_sdk.project_mcp_holds import (
        _READS_KIRO_PROJECT_FILE,
        held_server_element,
        project_hold,
        server_name_notice,
        unreadable_notice,
    )
    from kiro_crew.agent_sdk.spec_hooks import session_agent
    from kiro_crew.change_card_catalog import CardCatalogError
    from kiro_crew.change_card_project_mcp import KIND_PROJECT_MCP_SERVER
    from kiro_crew.dashboard.change_cards import CardError
    from kiro_crew.dashboard.handlers.change_cards_propose import propose_in_slot

    folder = getattr(client, "cwd", "") or getattr(slot, "project", "") or ""
    caps = capabilities_of(client)
    # A shape that carries no capabilities names no backend; "" would read as kiro-cli.
    if not folder or caps is UNKNOWN_BACKEND_CAPABILITIES:
        return
    backend = caps.backend
    runs = session_agent(client, agent)
    registered = (
        {"registered_agents": getattr(client, "kas_registered_agents", None)}
        if backend == ACP_BACKEND_KAS
        else {}
    )
    try:
        hold = await asyncio.to_thread(project_hold, backend, runs, folder, **registered)
    except Exception:  # noqa: BLE001 - a card offer never fails the turn
        logger.debug("project MCP cards: check failed", exc_info=True)
        return
    notice = unreadable_notice(backend, hold)
    notices = [notice] if notice else []
    for i, server in enumerate(hold.pending):
        notice = server_name_notice(server)
        if not notice and backend in _READS_KIRO_PROJECT_FILE:
            _, notice = await asyncio.to_thread(held_server_element, server, runs, folder)
        if notice:
            notices.append(notice)
            continue
        params = {
            "folder": server.folder,
            "file": server.file,
            "server": server.name,
            "fingerprint": server.fingerprint,
        }
        try:
            await propose_in_slot(
                state,
                slot.key,
                session_key,
                kind=KIND_PROJECT_MCP_SERVER,
                params=params,
                reason=_reason(server),
            )
        except (CardError, CardCatalogError) as exc:
            if exc.code == "too_many_cards":
                # The slot is at its live-card limit: each further try would only
                # re-read the file to be refused, so stop and name the rest once.
                rest: dict[str, int] = {}
                for held in hold.pending[i:]:
                    rest[held.path] = rest.get(held.path, 0) + 1
                notices += [
                    f"{count} more servers in {path} are held. Allow or dismiss the cards "
                    "above, then start a new chat to see the rest."
                    for path, count in rest.items()
                ]
                break
            if exc.code == "card_too_large":
                notices.append(
                    f"{server.name} in {server.path} is too large to show as a card. "
                    "Shorten its command, URL or settings in that file, then start a new chat."
                )
            elif exc.code == "card_hides_launch":
                notices.append(
                    f"{server.name} in {server.path} has a secret in its command or URL. "
                    "Move the secret into env or headers, then start a new chat."
                )
            logger.debug("project MCP card for %r not raised: %s", server.name, exc)
        except Exception:  # noqa: BLE001 - a card offer never fails the turn
            logger.debug("project MCP card for %r not raised", server.name, exc_info=True)
    for notice in dict.fromkeys(notices):
        from kiro_crew.dashboard.state import append_and_surface

        try:
            append_and_surface(state, slot, "notice", f"⚠ {notice}", "msg msg-info")
        except Exception:  # noqa: BLE001 - a notice never fails the turn
            logger.debug("project MCP notice not shown", exc_info=True)
