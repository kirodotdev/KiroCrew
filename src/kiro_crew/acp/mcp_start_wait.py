"""Which MCP servers a session's first prompt waits for.

THE RULE, stated here once; the spec and the call site
(``runtime_session_load._wait_managed_mcp``) point back to it.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.acp.mcp_mount_check import stub_server
from kiro_crew.acp.mcp_report_bound import keep
from kiro_crew.acp.mcp_session_report import STATUS_CAP, active_custom_agent, roster_names
from kiro_crew.start_priority import StartPriority

logger = logging.getLogger(__name__)


def person_waits(start_priority: StartPriority, *, focus_resume: bool = False) -> bool:
    """Whether a person is waiting on this session start, or is about to be.

    THE predicate for what a start does for a person, read here and by the
    re-open in ``runtime_session_load``. WHO IS WAITING decides it, not the
    queue the start waited in:

    * a start that claimed ``FOREGROUND`` (``kiro_crew.start_priority``), or
    * *focus_resume*: the dashboard owner came back to a chat whose session is
      saved but not running, and the gateway re-opens it ahead of their message
      (``ws._handle_slot_focused``). That start stays ``BACKGROUND`` in the start
      queues, so it never takes a place kept for a typed message, yet the person
      it is for is about to type, and a message they send joins it.

    Every other start is one nobody waits on.
    """
    return start_priority is StartPriority.FOREGROUND or focus_resume


def first_prompt_brief_wait(
    params: dict[str, Any],
    agent: str,
    required: tuple[str, ...],
    start_priority: StartPriority,
    *,
    focus_resume: bool = False,
) -> tuple[str, ...]:
    """The servers a session start waits for briefly, after its *required* ones.

    * *required*: Kiro Crew's own managed servers that were sent
      (``mcp_session_report.required_managed_servers``, read by the caller). The
      prompt waits for each for the whole start budget and the start fails
      without them: the agent's contract, its memory and its messaging tools
      live there.
    * The answer: waited for at most ``session_handle_mcp.MOUNTED_MCP_WAIT_SECS``,
      never required.

      - The servers Kiro Crew sent ON THE SESSION (a Connection's bridge), always
        -- except a gateway-shared stub (``-m`` the stub module)
        when a person is waiting: that is one of the agent's own servers moved
        onto the session to be shared, so it goes to the first turn like them.
      - The agent's OWN servers (the user's, which stay in the agent's block),
        only when nobody is waiting on this start (a sub-agent child, an
        unattended run, a warm start, a re-sent server set). Such a run may have
        one turn only, so a server that is late for it is a server it never
        gets. Never for the background helper ``kirocrew-lite``, which mounts
        nothing (``session_parts.policy.BACKGROUND_AGENT``).

    * Not waited for by the START: the agent's own servers when a person is
      waiting on it (:func:`person_waits`). Kiro CLI does not wait for them
      either. The host reads its server
      list when a tool is loaded, so a server that connects during the turn
      serves it. They are left to the first turn (:func:`left_to_first_turn`),
      and what that turn does turns on one fact, never on who is waiting alone:

      - Its prompt carries a block that spells out every one of them still
        unreported (``context_assembly/mcp_servers.py``), so the model can wait
        for the one it needs (``mcp_server_wait``): the prompt goes out.
      - Its prompt does not (``kirocrew chat``, a side question, a one-shot, a
        command: no ``[MCP SERVERS]`` block reaches those): it holds for them as
        the start did before, at most ``MOUNTED_MCP_WAIT_SECS``
        (``session_handle_mcp.hold_untold_first_turn``).

      Waiting for all of them at every start held every first prompt up to five
      seconds for tools most first prompts never use.

    Names are the ones frames carry, in wire order, as many as the session's
    report keeps a status for (``STATUS_CAP``): a server past that has no state
    a wait could read. The agent's own past it are counted where they are
    declared (``McpSessionReport.declared_omitted``) and named as not ready.
    """
    servers = params.get("mcpServers")
    sent = list(servers) if isinstance(servers, list) else []
    if not person_waits(start_priority, focus_resume=focus_resume):
        # The background helper's template mounts nothing, so a server in its block
        # is one another tool wrote into the spec, and a background turn on it is
        # closed right after such a start (``llm_helpers_parts.background_turns``).
        # Waiting for that server only delays the refusal.
        # shortcut: matched by name; give lite a real tool and this must go.
        from kiro_crew.session_parts.policy import BACKGROUND_AGENT  # circular import

        if agent != BACKGROUND_AGENT:
            sent += _own_servers(params, agent)
    else:
        # A person is waiting: a server the gateway shares is one of the agent's own
        # moved onto the session, so it is left to the first turn as they are.
        sent = [element for element in sent if not _is_shared_stub(element)]
    return tuple(name for name in roster_names(sent, STATUS_CAP) if name not in required)


def left_to_first_turn(
    params: dict[str, Any],
    agent: str,
    required: tuple[str, ...],
    start_priority: StartPriority,
    *,
    focus_resume: bool = False,
) -> tuple[tuple[str, ...], int]:
    """The agent's own servers this start did not wait for (rule: :func:`first_prompt_brief_wait`).

    Nothing for a start nobody waits on: it waited for them itself. The names
    are as many as the session's report keeps a status for (``STATUS_CAP``), so
    each is either named to the first turn or waited for by it. The count is how
    many more of them there are, the agent's own and the shared ones alike: past
    that bound no turn can be told of them all, so the first prompt holds as the
    start did.
    """
    if not person_waits(start_priority, focus_resume=focus_resume):
        return (), 0
    waited = {
        *required,
        *first_prompt_brief_wait(
            params, agent, required, start_priority, focus_resume=focus_resume
        ),
    }
    own = _own_servers(params, agent)
    servers = params.get("mcpServers")
    shared = [
        {"name": element.get("name")}
        for element in (servers if isinstance(servers, list) else [])
        if _is_shared_stub(element)
    ]
    deferred = [*own, *shared]
    every = roster_names(deferred, len(deferred))
    return keep((name for name in every if name not in waited), STATUS_CAP)


def policy_hidden_servers(agent: str, required: tuple[str, ...]) -> frozenset[str]:
    """The *required* servers that list no tool at all for *agent*'s sessions.

    A managed server filters its own tool list through the agent's
    ``managedToolPolicy``, as the gateway answers it to that server. An app agent
    whose manifest declares only its own tools gets every managed tool excluded
    (``apps.permissions.managed_tool_exclusion``), so ``kirocrew-cron`` connects
    with an empty catalog and no tool tag. That is the agent's own restriction,
    and the readiness barrier must not wait for tools it will never be shown
    (``KasMcpReadiness.policy_hidden``).

    Read through the same policy reader and list filter the gateway and the
    servers use, so a server counts here only when its listing really is empty.
    A server with one tool left still has to show it. A policy that cannot be
    read, a server whose tools cannot be named, or an install the sandbox does
    not seal counts nothing, so the barrier waits as it always did. Blocking
    file I/O: call it off the loop.
    """
    if not required:
        return frozenset()
    try:
        # Function-local: the policy reader sits in the dashboard layer, which
        # imports the ACP stack.
        from kiro_crew.config.paths import kiro_agents_dir
        from kiro_crew.dashboard.handlers.sessions import _read_managed_tool_policy_sync
        from kiro_crew.mcp_discovery_managed import managed_tool_names_for_policy
        from kiro_crew.mcp_listing_gateway import _package_tree_is_sealed, _policy_of
        from kiro_crew.mcp_shared_listing import within_policy

        # Naming a server's tools imports its module into this process, outside the
        # sandbox, so it is done only where an agent cannot have written that code,
        # as the gateway's own listing build does.
        if not _package_tree_is_sealed():
            return frozenset()
        policy = _policy_of(_read_managed_tool_policy_sync(kiro_agents_dir(), agent))
        if policy is None:
            return frozenset()
        hidden = set()
        for name in required:
            tools = managed_tool_names_for_policy(name)
            if tools and not within_policy(name, [{"name": tool} for tool in tools], policy):
                hidden.add(name)
        return frozenset(hidden)
    except Exception:  # noqa: BLE001 - an unread policy only keeps the full wait
        logger.debug(
            "managed tool policy of %r unreadable; waiting for every tool", agent, exc_info=True
        )
        return frozenset()


def _is_shared_stub(element: Any) -> bool:
    """Whether a session-level *element* starts the gateway's shared-server stub."""
    return isinstance(element, dict) and bool(stub_server(element))


def _own_servers(params: dict[str, Any], agent: str) -> list[dict[str, str]]:
    active = active_custom_agent(params, agent) or {}
    return [{"name": name} for name in active.get("mcpServers", {})]
