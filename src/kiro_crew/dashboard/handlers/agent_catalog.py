"""Read-only execution choices, distinct from the configured member registry."""

from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path

from aiohttp import web

from kiro_crew import agent_state
from kiro_crew.agent_discovery import AgentInfo, list_agents
from kiro_crew.agent_files import AGENT_FILENAME, LITE_AGENT_FILENAME
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import _read_session_key, requesting_slot_project
from kiro_crew.dashboard.handlers.agents import (
    _agent_roster_row,
    _name_would_be_masked,
    _roster_mask,
)
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.executors import discovery_executor

logger = logging.getLogger(__name__)

# The managed specs that are not a sensible thing to run a chat AS. Only the
# bare cheap agent behind auto-titles and compaction (no prompt, no tools) is
# here; it is reached by the runtime itself, never picked by a person. The
# primary ``kirocrew`` spec is deliberately NOT here: a chat session is a
# template choice, and the main managed agent is the default one. The other
# owned specs (conductor, worker, research, ...) are ordinary choices; hiding
# every owned file would drop them from a fresh install.
_BACKGROUND_ONLY_FILES = frozenset({LITE_AGENT_FILENAME})


def _is_background_only(agent: AgentInfo) -> bool:
    """A runtime-owned spec that exists for a background job, not a chat.

    Matched on the owned FILE, not the display name: a project checkout may
    declare its own ``kirocrew-lite.json`` under ``.kiro/agents`` and that is
    the user's file, an ordinary choice like any other project template.
    """
    return agent.kirocrew_owned and agent.filename in _BACKGROUND_ONLY_FILES


def _templates(project_dir: Path | None) -> list[AgentInfo]:
    """Discover shared templates without enrolling members or allocating memory."""
    discovered = list_agents(project_dir=project_dir)
    # Discovery's display enrichment can omit unreadable lineage. A selectable
    # catalog cannot interpret that omission as proof a private copy is shared.
    forks = agent_state.all_fork_info()
    # Narrower than the sync route's ``source != "kirocrew"``: that route decides
    # which specs become ENROLLED crew members, and the runtime's own files are
    # rightly not members. A chat session, by contrast, is a template choice, so
    # the primary ``kirocrew`` spec must be offered; only the background-only
    # managed spec is withheld here.
    chosen = [
        agent
        for agent in discovered
        if not agent.private_to
        and agent.name not in forks
        and Path(agent.filename).stem not in forks
        and not _is_background_only(agent)
        and not _name_would_be_masked(agent.name)
    ]
    # The primary managed agent leads the list; discovery's stem order is kept
    # for everything else (``sorted`` is stable), so nothing else moves.
    return sorted(chosen, key=lambda agent: agent.filename != AGENT_FILENAME)


def _template_row(agent: AgentInfo) -> dict[str, object]:
    """Only execution-choice metadata leaves the discovery boundary."""
    return {
        "name": agent.name,
        "selection_kind": "template",
        "scope": agent.scope,
        "kiro_agent": agent.name,
        "description": _roster_mask(agent.description),
        "source": _roster_mask(agent.source),
    }


async def api_agent_catalog(request: web.Request) -> web.Response:
    """GET /api/agents/catalog — members and templates in separate namespaces.

    The member-management endpoint remains unchanged. No name-based deduplication
    crosses namespaces: a shared template and a member can have the same name.
    """
    state = request.app.get("state")
    session_key = _read_session_key(request)
    # The browser sends this placeholder when a page has no chat slot to name.
    # It is a transport identity, not a conversation whose project can be scoped.
    if session_key == "dashboard:ui":
        session_key = ""
    project_dir = None
    if state is not None and session_key:
        slot_name = session_key.split(":", 1)[-1]
        slot = state._slots.get(slot_name)
        if slot is None:
            return web.json_response(
                {"error": "Conversation not found", "code": "slot_not_found"}, status=404
            )
        from kiro_crew.dashboard.chat_handlers import _deny_cross_app_slot_access

        denied = _deny_cross_app_slot_access(request, slot, slot_name, "agents.catalog")
        if denied is not None:
            return denied
        # No single-project fallback: an unscoped chat must not acquire choices
        # that only a different conversation's working directory can resolve.
        project_dir = requesting_slot_project(state, session_key)

    try:
        config = await asyncio.to_thread(KiroCrewConfig.load)
        templates = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), functools.partial(_templates, project_dir)
        )
    except Exception:
        logger.warning("Agent execution catalog could not be loaded", exc_info=True)
        return web.json_response(
            {
                "error": "Agent choices could not be loaded. Retry the catalog.",
                "code": "agent_catalog_unavailable",
            },
            status=503,
        )

    redact = state is None or not is_owner_dashboard_request(request)
    rows = []
    for name, member in config.agents.items():
        row = _agent_roster_row(name, "global", member, redact=redact)
        row["selection_kind"] = "member"
        rows.append(row)
    rows.extend(_template_row(agent) for agent in templates)
    return web.json_response(
        {
            "agents": rows,
            "default_agent": _roster_mask(config.default_agent) if redact else config.default_agent,
        }
    )
