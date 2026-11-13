"""Which MCP servers a session's first prompt waits for.

THE RULE, stated here once; the spec and the call site
(``runtime_session_load._wait_managed_mcp``) point back to it.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp.mcp_session_report import STATUS_CAP, active_custom_agent, roster_names
from kiro_crew.start_priority import StartPriority


def first_prompt_brief_wait(
    params: dict[str, Any],
    agent: str,
    required: tuple[str, ...],
    start_priority: StartPriority,
) -> tuple[str, ...]:
    """The servers a session start waits for briefly, after its *required* ones.

    * *required*: Kiro Crew's own managed servers that were sent
      (``mcp_session_report.required_managed_servers``, read by the caller). The
      prompt waits for each for the whole start budget and the start fails
      without them: the agent's contract, its memory and its messaging tools
      live there.
    * The answer: waited for at most ``session_handle_mcp.MOUNTED_MCP_WAIT_SECS``,
      never required.

      - The servers Kiro Crew sent ON THE SESSION (a Connection's bridge, a routed
        stub), always. Kiro Crew mounted them for this conversation and they are
        few.
      - The agent's OWN servers (the user's, which stay in the agent's block),
        only when nobody is waiting on this start (``BACKGROUND``: a sub-agent
        child, an unattended run, a warm start, a re-sent server set). Such a run
        may have one turn only, so a server that is late for it is a server it
        never gets.

    * Not waited for by the START: the agent's own servers when a person is
      waiting on it (``FOREGROUND``, the claim ``kiro_crew.start_priority``
      defines). Kiro CLI does not wait for them either. The host reads its server
      list when a tool is loaded, so a server that connects during the turn
      serves it. They are left to the first turn (:func:`left_to_first_turn`),
      and what that turn does turns on one fact, never on the priority alone:

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
    if start_priority is not StartPriority.FOREGROUND:
        sent += _own_servers(params, agent)
    return tuple(name for name in roster_names(sent, STATUS_CAP) if name not in required)


def left_to_first_turn(
    params: dict[str, Any],
    agent: str,
    required: tuple[str, ...],
    start_priority: StartPriority,
) -> tuple[tuple[str, ...], int]:
    """The agent's own servers this start did not wait for (rule: :func:`first_prompt_brief_wait`).

    Nothing for a start nobody waits on: it waited for them itself. The names
    are as many as the session's report keeps a status for (``STATUS_CAP``), so
    each is either named to the first turn or waited for by it. The count is how
    many more the agent has: past that bound no turn can be told of them all, so
    the first prompt holds as the start did.
    """
    if start_priority is not StartPriority.FOREGROUND:
        return (), 0
    waited = {*required, *first_prompt_brief_wait(params, agent, required, start_priority)}
    own = _own_servers(params, agent)
    names = tuple(name for name in roster_names(own, STATUS_CAP) if name not in waited)
    return names, max(0, len(own) - STATUS_CAP)


def _own_servers(params: dict[str, Any], agent: str) -> list[dict[str, str]]:
    active = active_custom_agent(params, agent) or {}
    return [{"name": name} for name in active.get("mcpServers", {})]
