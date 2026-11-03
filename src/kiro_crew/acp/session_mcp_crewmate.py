"""A crewmate worker session's spec on the mirrored hosts' session MCP projection.

Split out of :mod:`kiro_crew.acp.session_mcp`, which imports it back under the
same name: a crewmate's worker runs a spec built per session from its crewmate's
file, and this is where the array-backed hosts read it.
"""

from __future__ import annotations

from typing import Any


def _crewmate_worker_spec(
    agent: str | None,
    spec: dict[str, Any] | None,
    session_key: str,
    *,
    tool_limits_honoured: bool = False,
    spec_policy_honoured: bool = False,
) -> dict[str, Any] | None:
    """*spec*, or for a crewmate's worker session the worker spec it runs on.

    The on-disk ``kirocrew-mate-worker`` is the base every crewmate's worker shares;
    the session's own worker is that base plus its crewmate's file
    (``mate_worker.worker_session_spec``, read from the session's execution record,
    the seam KAS's projection reads too). Every later read below is of THIS spec, so
    the array, the withhold set and the per-tool restrictions are the worker's own.
    Any other agent, or no session key, keeps *spec*. A worker session whose base
    is absent or unreadable raises :class:`~kiro_crew.mate_worker.WorkerKitUnreadable`
    (fail closed, as KAS's projection does) rather than run without its kit. On a
    host that does not honour single-tool limits (*tool_limits_honoured* False, the
    default), a crewmate file that sets any refuses with
    ``capability_tool_limits_unsupported``, judged on the read this spec is built from.
    On a host that applies no ``hooks``, ``permissions`` or ``toolsSettings``
    (*spec_policy_honoured* False, the default), a crewmate file that sets any
    refuses the worker, naming them.
    """
    from kiro_crew.mate_worker import MATE_WORKER_AGENT, WorkerKitUnreadable, worker_session_spec

    if agent != MATE_WORKER_AGENT or not session_key:
        return spec
    if not isinstance(spec, dict):
        raise WorkerKitUnreadable(
            f"worker session {session_key}: the worker base spec {MATE_WORKER_AGENT!r} is "
            f"missing or unreadable, so the worker would run without its kit"
        )
    return worker_session_spec(
        dict(spec),
        session_key,
        refuse_tool_limits=not tool_limits_honoured,
        refuse_spec_policy=not spec_policy_honoured,
    )
