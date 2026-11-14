"""Positive KiroCrew Mate service-agent evidence for diagnostic Run records.

KiroCrew 0.11.0 defines the two service agents in ``mate_worker.py``. The
present agent projection can tell us *which spec was selected*, never which
Crew Ticket or Assignment owns it. No role hint is trusted as a permission,
model route, or scheduling signal. Unrecognized/legacy agents stay unclassified.
"""
from __future__ import annotations

from typing import Any, Mapping

# Exact Mate 2.0 service-agent IDs from KiroCrew v0.11.0 source. No fuzzy
# matching on prompts, names, member display labels or filesystem paths.
_MATE_ROLE_BY_AGENT = {
    "kirocrew-mate-worker": "worker",
    "kirocrew-mate-conductor": "conductor",
}


def projected_crew_role(agent_id: Any) -> str:
    """Classify a known Crew-projected service agent without task inference."""
    return _MATE_ROLE_BY_AGENT.get(agent_id, "") if isinstance(agent_id, str) else ""


def crew_role_from_run_metadata(metadata: Any) -> str:
    """Only recognize our allowlisted *stored* agent identity; ignore role text."""
    if not isinstance(metadata, Mapping):
        return ""
    return projected_crew_role(metadata.get("crew_projected_agent_id"))


def projected_crew_agent_id(agent: Any) -> str:
    """Bounded positive agent profile identity at a Run's start boundary."""
    agent_id = getattr(agent, "agent_id", None)
    return agent_id if projected_crew_role(agent_id) else ""
