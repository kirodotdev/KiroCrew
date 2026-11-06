"""Whether the agent a message cron job dispatches still exists.

A job stores its agent by name. When that agent is renamed or uninstalled the
name resolves to nothing. Dispatched anyway, the backend runs some other agent
without the missing one's tool permissions, so an unattended run stops on
permission prompts nobody answers. The chat path refuses the same miss
(``turn_admission``); this is the cron twin, so the run fails with a reason the
user can act on instead.
"""

from __future__ import annotations

from typing import Any


def unavailable_cron_agent_message(agent_name: str) -> str:
    """The run error a job whose agent is gone records and alerts with."""
    return (
        f"Agent '{agent_name}' is no longer available. "
        "Edit this schedule to pick another agent, or delete it."
    )


def cron_agent_missing(cfg: Any, agent_name: str | None, project_dir: str | None) -> bool:
    """True only when *agent_name* is positively known to resolve to no agent.

    Blocking (the project scope is a directory read), so an async caller runs it
    in a thread. A blank name is the default agent, never missing. An incomplete
    installed-agent snapshot cannot prove a miss (a spec it skipped may still be
    on disk), so it answers False and the fire dispatches as before.
    """
    from kiro_crew.config.loader_agents import _resolve_agent_selection
    from kiro_crew.config.loader_snapshots import (
        materialized_snapshot_complete,
        refresh_materialized_agents,
    )

    name = (agent_name or "").strip()
    if not name or cfg is None:
        return False
    if _resolve_agent_selection(cfg, name, project_dir)[3]:
        return False
    # One rescan before failing a run on a miss: an agent added outside Kiro
    # Crew since the last scan is not in the snapshot yet. Fires are rare, so
    # the directory read is cheap here, unlike on every chat turn.
    refresh_materialized_agents()
    if not materialized_snapshot_complete():
        return False
    return not _resolve_agent_selection(cfg, name, project_dir)[3]
