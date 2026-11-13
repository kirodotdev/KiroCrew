"""The server-less agent a KAS session starts in, so Kiro Crew's servers connect first.

KAS connects one process's MCP servers through a queue four wide, in the order
a session declares them, and it declares the running agent's own ``mcpServers``
block before the session-level array. Kiro Crew's servers travel in that array
(``kas_agents.hoist_servers_for_session``), so a session that starts in its own
agent queues them behind every server the agent declares, and session start,
which waits for Kiro Crew's servers (``wait_mcp_ready``), then waits for all of
them: measured, 35 s with 60 MCP servers against 2 s with none.

The order cannot be changed in one declaration without changing what a server
means. Sent in the array, the agent's servers would reach every sub-agent of
the session; sent in the block, Kiro Crew's would lose the declaration site
that proves a connected server is this session's own.

So the session starts in :data:`START_AGENT_ID`, an agent that declares no
server and mounts no tool. The engine then queues the array alone. The
``session/set_mode`` every KAS start already sends switches to the session's
own agent, and that switch is what declares the agent's block, behind Kiro
Crew's servers in the same queue. No prompt ever runs in the start agent: a
failed ``set_mode`` ends the session (``create_session``).

``session/load`` cannot name a mode: the engine resumes a session in the mode it
stored for it, which is the session's own agent, so that agent's servers queue
first again. It does take the definition re-sent with the load over the one it
kept, though. A re-open a person is waiting on
(``mcp_start_wait.person_waits``) therefore first loads the session with
:func:`parking_load`, in which the own agent declares no server and the session
sends none, so nothing starts; switches it to the start agent, which makes that
the stored mode; and frees it again (``runtime_session_load._park_in_start_agent``).
The load that follows then runs as a new session's start does: the engine
resumes in the start agent and queues the array alone, and ``set_mode`` queues
the agent's servers behind it. That ``set_mode`` also puts the session's own
agent back as the stored mode.

Every ``session/load`` registers the start agent (:func:`with_start_agent`, the
same refusals), so a session whose re-open was cut off between those two steps
still comes back in an agent the engine knows; it would start in the engine's
own ``vibe`` mode otherwise. ``load_session`` switches such a session to its
own agent whoever resumes it.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp.kas_agents import KAS_MAX_CUSTOM_AGENTS
from kiro_crew.acp.kas_subagent_targets import mounts_subagent_tool

#: The wire id of the start agent. Lower-case and hyphenated, the shape measured
#: to register as a client agent (``agent_files.KAS_WIRE_ID_SUFFIX``).
START_AGENT_ID = "kirocrew-session-start"

_START_AGENT_NOTE = (
    "Kiro Crew starts a session here before it switches to the session's own "
    "agent. It has no tools and is not an agent to select or delegate to."
)


def _start_agent() -> dict[str, Any]:
    """A fresh start-agent entry: no server, no tool, no ``mcp.json`` reach.

    The prompt is never sent to a model. It is not empty because an engine that
    drops a prompt-less entry would start the session in its own ``vibe`` mode.
    """
    return {
        "id": START_AGENT_ID,
        "description": _START_AGENT_NOTE,
        "prompt": _START_AGENT_NOTE,
        "tools": [],
        "includeMcpJson": False,
    }


def with_start_agent(batch: list[Any]) -> list[Any] | None:
    """*batch* with the start agent appended, or ``None`` to start in the own agent.

    *batch* is the wire ``customAgents`` list, the session's own agent first.
    ``None`` when starting in the own agent costs nothing or the start agent
    cannot be added safely:

    * the own agent declares no server, so nothing would queue ahead;
    * the own agent mounts the engine's sub-agent tool, whose roster is the
      session's registered agents: the start agent must not become a name the
      model can delegate to;
    * an entry already carries :data:`START_AGENT_ID` (the engine keeps the first
      entry of an id), or the batch is at the engine's cap
      (``kas_agents.KAS_MAX_CUSTOM_AGENTS``): one entry more fails the whole
      ``_meta.kiro`` validation, so a full batch is sent as built.

    The caller's list is not mutated.
    """
    own = batch[0] if batch else None
    if not isinstance(own, dict):
        return None
    servers = own.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        return None
    if mounts_subagent_tool(own):
        return None
    if len(batch) >= KAS_MAX_CUSTOM_AGENTS:
        return None
    if any(isinstance(entry, dict) and entry.get("id") == START_AGENT_ID for entry in batch):
        return None
    return [*batch, _start_agent()]


def parking_load(params: dict[str, Any]) -> dict[str, Any] | None:
    """The ``session/load`` that brings a stored session back with nothing to start.

    *params* is the load as it will be sent, its batch already carrying the
    start agent (:func:`with_start_agent`). The answer differs in two places:
    the session's own agent, the batch's first entry, declares no server and
    reads no ``mcp.json``, and the session-level array is empty. ``None`` when
    the batch does not end in the start agent this module appends, since there
    is then nothing to park the session in: an entry that only carries the id
    (a sub-agent target the user named that way) declares what its author gave
    it, servers included. The caller's dicts are not mutated.
    """
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    kiro = meta.get("kiro")
    if not isinstance(kiro, dict):
        return None
    batch = kiro.get("customAgents")
    if not isinstance(batch, list) or len(batch) < 2 or not isinstance(batch[0], dict):
        return None
    if batch[-1] != _start_agent():
        return None
    own = {key: value for key, value in batch[0].items() if key != "mcpServers"}
    own["includeMcpJson"] = False
    return {
        **params,
        "mcpServers": [],
        "_meta": {**meta, "kiro": {**kiro, "customAgents": [own, *batch[1:]]}},
    }
