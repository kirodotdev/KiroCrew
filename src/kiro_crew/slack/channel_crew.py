"""A Slack channel's Crew agent binding, resolved for the session it starts.

``slack.channels.<id>.agent`` may name a Kiro Crew agent record (a
``config.agents`` key) rather than a kiro-cli agent mode. kiro-cli only starts
a mode, so a chat turn that hands the record's NAME to ``get_or_create`` asks
for a mode that does not exist and runs in a per-session scratch directory
instead of the record's workspace. The dashboard and the cron path
(``gateway_runtime/cron_fire._resolve_cron_agent``) already collapse the name:
the record's ``kiro_agent`` goes to kiro-cli as ``agent``, the name itself goes
as ``crew_agent`` so ``prepare_runtime`` keeps the member identity it already
resolved by name, and the session runs in the record's workspace directory.
This module is that same collapse for the Slack chat turn.

Only a name that IS a ``config.agents`` key is collapsed. Anything else -- a
plain kiro agent mode such as ``kirocrew-lite`` -- returns ``None`` and the
caller passes it through unchanged, exactly as before. The binding itself is
not new: the channel already names the record and the session already resolved
its member identity from that name; this only stops the mode and the working
directory from disagreeing with it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelCrew:
    """What a channel-bound Crew agent record starts a session with."""

    #: The kiro-cli agent mode the record runs (``agents.<name>.kiro_agent``).
    kiro_agent: str
    #: The record's own name, passed as ``crew_agent`` so member identity holds.
    crew_agent: str
    #: The record's workspace directory, anchored by the one placement rule.
    cwd: str

    def allocation_kwargs(self) -> dict[str, Any]:
        """The ``get_or_create`` keyword arguments this binding supplies."""
        return {"agent": self.kiro_agent, "crew_agent": self.crew_agent, "cwd": self.cwd}


def resolve_channel_crew(name: str | None) -> ChannelCrew | None:
    """Resolve a channel's agent *name* to its Crew record's mode and workspace.

    Blocking (a config read when no live snapshot exists): call it off the loop.
    Returns ``None`` for an empty name, a name that is not a ``config.agents``
    key, or any failure to read the record, so the caller keeps its current
    behaviour rather than starting a session under a guess.
    """
    if not name:
        return None
    try:
        from kiro_crew.config import KiroCrewConfig, live
        from kiro_crew.config.loader import resolve_agent_bindings, workspace_dir_from_entry

        # The last APPLIED reload first, so a record created at runtime is seen
        # without a gateway restart (the cron resolver reads the same way).
        cfg = live.snapshot() or KiroCrewConfig.load()
        record = (getattr(cfg, "agents", None) or {}).get(name)
        if record is None:
            return None
        # validate_memory_files=False: only the mode is read here; the session's
        # own execution-context resolution validates its store later.
        bindings = resolve_agent_bindings(cfg, name, validate_memory_files=False)
        kiro_agent = bindings.kiro_agent or ""
        if not kiro_agent:
            return None
        # bindings.workspace_dir is the RAW configured value (the shipped default
        # is the relative "workspace"); as a provider cwd that would resolve
        # against the gateway process directory. workspace_dir_from_entry anchors
        # a relative dir under the data home and an unmapped name to the base
        # workspace directory, never another workspace's.
        ws_entry = (getattr(cfg, "workspaces", None) or {}).get(record.workspace or "")
        cwd = str(workspace_dir_from_entry(ws_entry))
        return ChannelCrew(kiro_agent=kiro_agent, crew_agent=name, cwd=cwd)
    except Exception:
        logger.debug("slack channel agent %r: crew record not resolvable", name, exc_info=True)
        return None
