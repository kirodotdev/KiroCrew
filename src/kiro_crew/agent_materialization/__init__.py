"""The owners behind :mod:`kiro_crew.agent`, the agent-spec materialization facade.

``kiro_crew.agent`` stays the one import path: callers, tests and documents reach every
name as ``kiro_crew.agent.<name>``, and a patch there reaches the owner that reads the
name. Nothing outside ``kiro_crew.agent`` and this package imports an owner, so the
layout below can change without touching a caller.

* :mod:`.kiro_hooks` -- the spec ``hooks`` field: validation, both spec shapes, the
  autoimport scan and the merge. The legacy-key repair of the specs Kiro Crew owns
  stays :func:`kiro_crew.agent.repair_agent_configs`.
* :mod:`.managed_mcp` -- policy over the managed MCP registry: emission eligibility,
  the gate snapshot, the data-home pin, the registry marker and field ownership.
* :mod:`.mcp_aliases` -- MCP server-key aliasing and the Connections tool aliases.
* :mod:`.auto_approve` -- what a spec may auto-approve under the governance ceiling,
  and the KAS ``permissions`` block derived from it.
* :mod:`.mcp_sources` -- projecting the app, global and dashboard-store MCP sources
  into the default spec during a rebuild.
* :mod:`.default_spec_commit` -- the rebuild's locked write of the default spec.
* :mod:`.fork_refresh` -- the governance refresh of crews' private template copies.
* :mod:`.service_agents` -- the lite, guest, knowledge and research agents.
* :mod:`.conductor_agents` -- the four conductor specs.
* :mod:`.worker_agent` -- the worker's mirror of the default spec and its spawn-time
  freshness gate.
* :mod:`.guide_platform` -- the one-time ``kirocrew-guide`` grant on an existing
  default spec.
* :mod:`.first_crewmate` -- the one-time first crewmate on the ordinary crewmate
  template.

Importing this package imports the facade first, and the facade imports every owner
once its own names are bound, so no owner can be observed half-built whichever module
a caller imports first.
"""

import kiro_crew.agent  # noqa: F401 -- the facade loads every owner, in order
