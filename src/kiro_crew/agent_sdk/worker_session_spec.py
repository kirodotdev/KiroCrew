"""The spec a worker session would run, read for a before-dispatch capability lookup.

``worker_capabilities`` (``dashboard/handlers/worker_capabilities.py``) answers which
MCP servers a conductor's next worker can call. That needs the spec the worker's
session reads and each backend's spawn projection, both below the agent-SDK boundary,
so the lookup reaches them here, as ``kirocrew doctor`` reaches the same reads
through ``agent_sdk.drivers.acp.agent_spec_mcp_refs``.

Read-only and blocking; nothing here records or caches anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def worker_session_spec(
    agent: str, backend: str, work_dir: str | Path | None = None
) -> dict[str, Any] | None:
    """*agent*'s spec as a session on *backend*, started in *work_dir*, reads it.

    A backend whose session gets its servers from a mirrored ``session/new`` array
    reads exactly what that array is built from
    (``acp.session_mcp._session_spec_and_snapshot_for``: a checkout's own spec keeps
    only its switch-off keys, approved template servers are merged, and Crew's
    default servers are added to a user-level spec only, on the backends in
    ``ACP_BACKENDS_DEFAULT_CREW_SERVERS_IN_ARRAY``). Every other backend reads the
    checkout spec first, then the user's (``acp.session_mcp.agent_spec_snapshot``),
    then that spawn's own additions (``acp.default_crew_servers.session_spec_on``).
    ``None`` when no spec resolves.
    """
    from kiro_crew.acp.default_crew_servers import session_spec_on
    from kiro_crew.acp.session_mcp import _session_spec_and_snapshot_for, agent_spec_snapshot
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_DEFAULT_CREW_SERVERS_IN_ARRAY
    from kiro_crew.providers.mirrors import has_mirror

    if has_mirror(backend):
        spec, _snapshot = _session_spec_and_snapshot_for(
            agent,
            work_dir,
            default_crew_servers=backend in ACP_BACKENDS_DEFAULT_CREW_SERVERS_IN_ARRAY,
        )
        return spec if isinstance(spec, dict) else None
    spec = agent_spec_snapshot(agent, work_dir=work_dir)
    return session_spec_on(backend, agent, spec) if isinstance(spec, dict) else None
