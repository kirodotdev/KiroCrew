"""The last step of a KAS session's ``customAgents`` batch: who its sub-agent tool may run.

``KasHarness.session_extras`` hands this the session agent's projected entries and
the targets ``kas_subagent_targets.project_subagent_targets`` registered. In a
session ``kas_engine_subagents.engine_subagents_on`` names, the default agent's
entry mounts the engine's sub-agent tool with only the shipped helper reachable.
Then Kiro Crew's own agents are denied to every entry that mounts the tool,
listed or not (``kas_subagent_targets.deny_own_agents``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from kiro_crew.acp.kas_subagent_targets import deny_own_agents

logger = logging.getLogger(__name__)


def native_batch(
    custom_agents: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    *,
    agent: str = "",
    session_key: str = "",
    member_dispatch: bool = False,
    crew_panel: bool = False,
    agents_dir: Path | None = None,
    work_dir: str | Path | None = None,
    stub_server_names: frozenset[str] = frozenset(),
    settings_dir: str | Path | None = None,
    notices: list[str] | None = None,
    parent_spec: dict[str, Any] | None = None,
    remote_workspace: bool = False,
) -> list[dict[str, Any]]:
    """The session's batch: its own entries, then its targets, own agents denied.

    Blocking (a config read): runs in the harness's projection thread. A config
    that cannot be read mounts nothing, and neither does a session in a remote
    workspace, whose built-ins (the engine's sub-agent tool among them) are
    replaced by the workspace's own tools.
    """
    batch = [*custom_agents, *targets]
    if agents_dir is not None and not remote_workspace:
        from kiro_crew.acp import kas_engine_subagents as engine
        from kiro_crew.config.loader import KiroCrewConfig

        try:
            cfg = KiroCrewConfig.load()
        except Exception:  # noqa: BLE001 - an unreadable config keeps Kiro Crew's own
            logger.warning("config unreadable; KAS sub-agents stay off", exc_info=True)
            cfg = None
        if cfg is not None and engine.engine_subagents_on(
            cfg,
            agent,
            session_key,
            member_dispatch=member_dispatch,
            crew_panel=crew_panel,
        ):
            batch = engine.with_engine_subagents(
                batch,
                agent,
                agents_dir=agents_dir,
                work_dir=work_dir,
                stub_server_names=stub_server_names,
                session_key=session_key,
                settings_dir=settings_dir,
                notices=[] if notices is None else notices,
                parent_spec=parent_spec,
            )
    return deny_own_agents(batch)


__all__ = ["native_batch"]
