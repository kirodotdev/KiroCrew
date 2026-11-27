"""Whether a KAS chat's own process may host its ``spawn_run`` sub-agents as sessions.

KAS is in ``ACP_BACKENDS_SESSION_SHARING``: a template sub-agent of a KAS chat
opens a session on the chat's own process (``subagent_manager/run_start.py``),
where the engine dedupes MCP servers across sessions by server config and
resolved cwd. So a sub-agent in the pool cwd joins the chat's user servers
instead of starting its own fleet, and only its own per-session Crew servers start.

Membership says the harness can; this says the one process in hand can. A KAS
process may take sub-agents only when all hold, and otherwise every sub-agent
gets a dedicated process, as before:

- its handshake advertised ``sessionCapabilities.close``
  (``AcpRuntime.can_release``): the sub-agent's teardown closes its session and
  keeps the record that ``spawn_continue`` loads, instead of deleting it;
- the engine that process runs, as recorded at its spawn, is at or above
  ``KAS_CHAT_SHARING_MIN_ENGINE``: it runs turns of different sessions in
  parallel and names the session on every ``session/update``;
- it was not spawned in KAS machine mode, whose token and cell a sub-agent's own
  start would choose afresh;
- it was not spawned under a looser ``agent.sandbox`` tier than a dedicated
  start would get now (``sandbox_tier_tightened``);
- it does not serve other chats (``chat_runtime_sharing``), so a sub-agent never
  lands on a process another chat holds the lease to.

Each answer reads fields set at spawn and the cached config, so it is safe on the
event loop. Any doubt answers no, which is the dedicated process.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def runtime_hosts_subagents(runtime: Any, *, chat_shared: bool = False) -> bool:
    """Whether *runtime*, a KAS chat's own process, may host its sub-agents' sessions.

    *chat_shared* is True when the parent's provider holds a lease on a process
    that serves several chats. Never raises; see the module docstring for the terms.
    """
    try:
        from kiro_crew.acp.chat_runtime_sharing import engine_shares_chats
        from kiro_crew.acp_backends import ACP_BACKEND_KAS

        is_kas = runtime is not None and getattr(runtime, "acp_backend", None) == ACP_BACKEND_KAS
        if not is_kas or chat_shared or runtime.can_release() is not True:
            return False
        harness = runtime._harness
        if getattr(harness, "spawn_machine_auth", None) is not None:
            return False
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.session_allocation_parts.companions import sandbox_tier_tightened

        if sandbox_tier_tightened(runtime.sandbox_mode, KiroCrewConfig.load().agent.sandbox):
            return False
        engine = getattr(harness, "bundle_engine_version", "") or getattr(
            harness, "relay_engine_version", ""
        )
        if not engine_shares_chats(engine if isinstance(engine, str) else None):
            logger.info(
                "kas-subagent-sharing: parent engine %s is below the sharing floor; "
                "sub-agents keep their own process",
                engine or "unknown",
            )
            return False
        return True
    except Exception:  # noqa: BLE001 - any doubt keeps the dedicated process
        logger.debug("kas-subagent-sharing: host check failed", exc_info=True)
        return False
