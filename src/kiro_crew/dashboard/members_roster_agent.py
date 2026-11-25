"""Which crewmates on the roster are bound to an agent whose file is gone.

A crewmate row names its agent in ``kiro_agent``. A package uninstall, a
disabled app or a delete by hand removes that agent's file and leaves the row,
which still holds the crewmate's memory, chats and schedules. The roster marks
such a row so the person can see why it stopped answering and remove it
themselves. Nothing here changes or deletes a row.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.config import loader

logger = logging.getLogger(__name__)


def members_missing_agent(cfg: Any) -> frozenset[str]:
    """Names of the crewmates in *cfg* whose bound agent is positively gone.

    Blocking (directory reads), so the roster runs it in a thread. It rescans
    first, because an agent added or removed outside Kiro Crew since the last
    scan is not in the snapshot yet; the rescan reuses the snapshot while the
    agents directories have not moved.

    A row is named when a complete user-level scan leaves the installed
    snapshot without its template and its own crewmate file is absent on disk.
    The crewmate scan may skip an unreadable file or listing even when the
    user-level scan is complete; a direct stat rules out those false absences.
    That is absence, not the removal ``reset_dangling_default_agent`` waits for:
    a removal is known only to the process that saw the file go, and
    the mark must still show after a restart. The mark changes nothing, so
    absence is enough for it. Never named: a row on the managed ``kirocrew``
    template or on an agent kiro-cli ships inside its binary (neither has a
    file), and a row whose template the edition contributes or its own
    workspace declares (both still dispatch). Anything unsure -- a cold or empty
    snapshot, a partial user-level scan, an unreadable workspace, a stat error
    or a failed rescan -- names nobody.
    """
    try:
        loader.refresh_materialized_agents()
        names, _removed, complete = loader._removal_evidence()
        if not names or not complete:
            return frozenset()
        unbound: dict[str, str] = {}
        for name, row in cfg.agents.items():
            template = loader._dangling_template(row.kiro_agent, names)
            if template is not None and template not in loader.KIRO_CLI_BUILTIN_AGENTS:
                try:
                    (loader.mate_agents_dir() / f"{template}.json").stat()
                except FileNotFoundError:
                    unbound[name] = template
        if not unbound:
            return frozenset()
        edition = loader._edition_agent_names()
        return frozenset(
            name
            for name, template in unbound.items()
            if template not in edition
            and not loader._workspace_declares(
                template,
                loader._alias_workspace_dir(
                    cfg.workspaces, cfg.agents[name].workspace, cfg.default_workspace
                ),
            )
        )
    except Exception:  # noqa: BLE001 -- the mark is advisory; the roster still renders
        logger.debug("missing-agent check failed", exc_info=True)
        return frozenset()
