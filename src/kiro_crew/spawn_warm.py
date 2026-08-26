"""Agent-cache warming for spawn-shaped requests.

Lives here rather than in ``dashboard/handlers/messaging.py``, where it grew,
because both callers of it are not dashboard handlers: the ``POST /api/spawn``
endpoint and Crew dispatch. Keeping it in the handlers package forced Crew's
module to choose between a function-local import (against the repo's
``top-level-imports`` rule) and pulling the ENTIRE handler tree into every
process that imports ``crew_chat`` — including the Slack gateway, which imports
it at module scope. This module depends on config, cwd validation and agent
discovery only, so neither caller pays for the other's surface.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kiro_crew.agent_discovery import warm_project_agent_names
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.executors import discovery_executor
from kiro_crew.subagent import validate_cwd


async def warm_project_agents_for_spawn(state: Any, cwd: str) -> None:
    """Warm the project agent-name cache for a spawn-shaped request, safely.

    ``_validate_agent`` runs on the loop and therefore reads ONLY
    ``cached_project_agent_names()``; without this warm, a spawn that names a
    project agent is refused ("not found") until some unrelated session happens
    to warm that project's cache. Best-effort and never raises.

    A caller-supplied cwd MUST pass the same ``validate_cwd()`` gate ``spawn()``
    itself applies BEFORE any discovery read touches it — warming first would
    read ``<cwd>/.kiro`` from a path the allowlist rejects. That applies to a
    STORED cwd on retry as much as a fresh one: the allowlist can have changed
    since the original spawn (a removed root must not stay warm-able forever),
    so the check is against the CURRENT config on every call. On rejection the
    cwd is simply not warmed and ``spawn()`` refuses it with the real error.
    The pool cwd is Kiro Crew's own default project dir and needs no allowlist.
    Config load + ``validate_cwd`` (realpath/isdir) are blocking filesystem
    work, so the whole check runs on the discovery pool.
    """
    warm_dir = ""
    if cwd:

        def _validated_warm_dir() -> str:
            try:
                allowed_roots = KiroCrewConfig.load().agent.subagent_cwd_allowed_roots
            except Exception:
                allowed_roots = []  # fail closed, mirroring spawn()
            resolved, _err = validate_cwd(cwd, allowed_roots)
            return resolved

        warm_dir = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), _validated_warm_dir
        )
    else:
        warm_dir = str(getattr(state.sessions, "_pool_cwd", "") or "")
    if warm_dir:
        await warm_project_agent_names(warm_dir)
