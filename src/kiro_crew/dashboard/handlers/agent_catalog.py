"""Read-only execution choices, distinct from the configured member registry."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import agent_state
from kiro_crew.acp_backends import (
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_MARKDOWN_AGENT_SPECS,
    Routing,
    backend_to_stored,
    routing_for,
)
from kiro_crew.agent_discovery import AgentInfo, list_agents
from kiro_crew.agent_files import (
    AGENT_FILENAME,
    DASHBOARD_MANAGER_AGENT_FILENAME,
    GUEST_AGENT_FILENAME,
    LITE_AGENT_FILENAME,
    MATE_CONDUCTOR_AGENT_FILENAME,
    MATE_WORKER_AGENT_FILENAME,
)
from kiro_crew.config.loader import (
    KiroCrewConfig,
    default_template,
    dispatch_kiro_agent,
    refresh_materialized_agents,
)
from kiro_crew.dashboard.handlers._shared import _read_session_key, requesting_slot_project
from kiro_crew.dashboard.handlers.agents import (
    _agent_roster_row,
    _name_would_be_masked,
    _roster_mask,
)
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.slot_ownership import deny_app_slot_access, slot_not_found
from kiro_crew.executors import discovery_executor
from kiro_crew.members import is_member_session_key
from kiro_crew.platform import current_context, safe_context_call

logger = logging.getLogger(__name__)

# The managed specs that are not a sensible thing to run a chat AS. The bare
# cheap agent behind auto-titles and compaction (no prompt, no tools) is here,
# reached by the runtime itself and never picked by a person; so is the guest
# agent, which is a trust boundary. The primary ``kirocrew`` spec is deliberately
# NOT here: a chat session is a template choice, and the main managed agent is the
# default one. The other owned specs (conductor, worker, research, ...) are
# ordinary choices; hiding every owned file would drop them from a fresh install.
#
# The dashboard manager is here because of what it CANNOT do rather than what it
# is for. It holds the panel server and ``fs_read`` and nothing else: no shell, no
# file write, no session tools. A person who picked it for a chat would get an
# agent that can change one page and cannot answer anything, which reads as the
# product being broken rather than as a narrow agent doing its job. It is reached
# by a crewmate handing it page work, which is what the member base prompt's item
# 7 routes.
#
# The mate conductor is here because it is not a deliberate pick either: it is
# the default template under its own filename (rfc-mate-owned-agent-config.md,
# migration step 1), reached only through a crewmate's own ``kiro_agent``
# binding. Offering it as a "Built from" choice would read as a second,
# unexplained default beside the primary ``kirocrew`` spec it is byte-identical
# to today. The mate worker is here for the same reason: it is reached only
# through a crewmate's own ``session_create``, never picked.
_BACKGROUND_ONLY_FILES = frozenset(
    {
        LITE_AGENT_FILENAME,
        GUEST_AGENT_FILENAME,
        DASHBOARD_MANAGER_AGENT_FILENAME,
        MATE_CONDUCTOR_AGENT_FILENAME,
        MATE_WORKER_AGENT_FILENAME,
    }
)


def _is_background_only(agent: AgentInfo) -> bool:
    """A runtime-owned spec that exists for a background job, not a chat.

    Matched on the owned FILE, not the display name: a project checkout may
    declare its own ``kirocrew-lite.json`` under ``.kiro/agents`` and that is
    the user's file, an ordinary choice like any other project template.
    """
    return agent.kirocrew_owned and agent.filename in _BACKGROUND_ONLY_FILES


def _agent_runtime_policy(name: str, engine_identity: str) -> dict[str, Any] | None:
    """Return companion-owned UI policy without making it an enforcement boundary."""
    policy = safe_context_call(
        lambda: current_context().providers.agent_runtime_policy(engine_identity),
        fallback=None,
        log_message=f"Failed to read runtime policy for crew {name}",
    )
    return policy if isinstance(policy, dict) else None


def _templates(project_dir: Path | None) -> list[AgentInfo]:
    """Discover shared templates without enrolling members or allocating memory."""
    return _choosable_templates(list_agents(project_dir=project_dir))


def _choosable_templates(discovered: list[AgentInfo]) -> list[AgentInfo]:
    """The template choices out of one discovery scan (see :func:`_templates`)."""
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
        # A bool discovery computed, not record content, so it is not masked.
        "markdown_only": agent.markdown_only,
    }


def _session_backend(config: KiroCrewConfig, session_key: str) -> str:
    """The harness a session on *session_key* runs, in the config-file spelling.

    The same split the provider factory's selection makes
    (``members.select_provider_backend``): a crewmate's DM runs on
    ``agent.member_acp_backend``, every other chat and a picker with no chat on
    ``agent.acp_backend``. Both fields are already resolved runtime ids after
    ``KiroCrewConfig.load``, so this reads them rather than calling that gate,
    which logs every member routing. Display-only: no spawn reads this.
    """
    if is_member_session_key(session_key):
        return backend_to_stored(config.agent.member_acp_backend)
    return backend_to_stored(config.agent.acp_backend)


def _markdown_refused_backends() -> list[str]:
    """The hosts that refuse a markdown-only agent, in the config-file spelling.

    A host refuses one when its spawn names an agent (``Routing.AGENT_SPEC``, the
    routing table's answer to "this host has an agent to activate", which the
    runtime's activation guard confirms) and it cannot load the markdown form
    (``ACP_BACKENDS_MARKDOWN_AGENT_SPECS``). Every other host starts the session
    without the agent's spec either way, so a "won't start" badge there would be
    false.
    """
    return sorted(
        backend_to_stored(backend)
        for backend in ACP_BACKENDS_KNOWN
        if routing_for(backend) is Routing.AGENT_SPEC
        and backend not in ACP_BACKENDS_MARKDOWN_AGENT_SPECS
    )


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
        # One body for a missing slot and a refused one, so an app cannot tell them apart.
        if slot is None:
            return slot_not_found()
        denied = deny_app_slot_access(request.get("app", ""), slot, slot_name, "agents.catalog")
        if denied is not None:
            return denied
        # No single-project fallback: an unscoped chat must not acquire choices
        # that only a different conversation's working directory can resolve.
        project_dir = requesting_slot_project(state, session_key)

    # The reset below writes the owner's config, so only the owner's own fetch
    # may run it; anyone else's still gets a freshly scanned template list.
    owner = state is not None and is_owner_dashboard_request(request)
    try:
        # The picker's fetch is the one moment, after boot, that an agent removed
        # OUTSIDE the gateway (a package uninstall, a hand-deleted file) is
        # certain to be noticed: it already rescans the directory off the loop
        # for the template rows, so the snapshot refresh -- whose landed scan
        # resets a default agent whose template is gone -- rides the same
        # executor call, and the config is read AFTER it so the response names
        # the default the user will actually get.
        def _refreshed_templates() -> tuple[list[AgentInfo], frozenset[str]]:
            refresh_materialized_agents(heal_default=owner)
            discovered = list_agents(project_dir=project_dir)
            # From the WHOLE scan, not the template choices: a crewmate may be
            # bound to an agent the template list withholds.
            markdown_only = frozenset(a.name for a in discovered if a.markdown_only)
            return _choosable_templates(discovered), markdown_only

        templates, markdown_only_names = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), _refreshed_templates
        )
        config = await asyncio.to_thread(KiroCrewConfig.load)
    except Exception:
        logger.warning("Agent execution catalog could not be loaded", exc_info=True)
        return web.json_response(
            {
                "error": "Agent choices could not be loaded. Retry the catalog.",
                "code": "agent_catalog_unavailable",
            },
            status=503,
        )

    redact = not owner
    rows = []
    for name, member in config.agents.items():
        row = _agent_roster_row(name, "global", member, redact=redact)
        row["selection_kind"] = "member"
        # The agent that RUNS, which for a row that recorded a file name is the
        # agent that file declares.
        engine_identity = dispatch_kiro_agent(member.kiro_agent) or name
        row["markdown_only"] = engine_identity in markdown_only_names
        if not redact:
            # The companion keys its policy on that same agent.
            policy = _agent_runtime_policy(name, engine_identity)
            if policy is not None:
                row["runtime_policy"] = policy
        rows.append(row)
    rows.extend(_template_row(agent) for agent in templates)
    # The default the picker marks is the default for NEW SESSIONS: the template
    # a session created without a crewmate runs (`default_template`), not the
    # roster's default crewmate alias. The picker's "default for new sessions"
    # row reads and writes this one.
    template = default_template(config)
    return web.json_response(
        {
            "agents": rows,
            "default_agent": _roster_mask(template) if redact else template,
            # What a picker needs to badge a ``markdown_only`` row it cannot run:
            # the harness this chat (or, with no chat, a new session) runs, the
            # harnesses that refuse such an agent (the badge's condition), and
            # the ones that load it (the badge's remedy). None names a backend
            # here; all come from the registry and harness facts.
            "session_backend": _session_backend(config, session_key),
            "markdown_refused_backends": _markdown_refused_backends(),
            "markdown_spec_backends": sorted(
                backend_to_stored(b) for b in ACP_BACKENDS_MARKDOWN_AGENT_SPECS
            ),
        }
    )
