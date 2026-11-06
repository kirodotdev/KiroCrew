"""GET /api/skills/-/audit — read-only overlap report across pending and live skills.

The audit reports only. Staging, approving and dismissing a candidate stay with the
pending-queue handlers in ``prompts.py``, which are owner-gated; this endpoint writes
nothing.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor

from ._shared import _get_skills


def _sel():
    """Late-binding sel() — allows monkeypatching at parent package level."""
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


# Bound the audit response even when one pending candidate links a large skill
# library into a single component. The dashboard applies a smaller render cap;
# these limits protect transport and cache size before the payload reaches it.
_SKILL_AUDIT_MAX_CLUSTERS = 50
_SKILL_AUDIT_MAX_MEMBERS = 20
_SKILL_AUDIT_MAX_RELATIONS = 8


def _bound_skill_audit_clusters(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cap cluster fan-out, keeping pending members and the relations among kept members."""
    bounded: list[dict[str, Any]] = []
    for cluster in clusters[:_SKILL_AUDIT_MAX_CLUSTERS]:
        all_members = [
            member for member in cluster.get("members") or [] if isinstance(member, dict)
        ]
        # Pending members first: a plain slice would drop a pending member sorted
        # after 20 live names, and with it the relations the Skills tab shows on
        # that candidate's row.
        all_members.sort(key=lambda member: 0 if member.get("kind") == "pending" else 1)
        members = all_members[:_SKILL_AUDIT_MAX_MEMBERS]
        member_ids = {str(member.get("id")) for member in members}
        all_relations = list(cluster.get("relations") or [])
        relations = [
            relation
            for relation in all_relations
            if isinstance(relation, dict)
            and set(map(str, relation.get("members") or [])) <= member_ids
        ][:_SKILL_AUDIT_MAX_RELATIONS]
        bounded.append(
            {
                **cluster,
                "members": members,
                "relations": relations,
                "omitted_members": len(all_members) - len(members),
                "omitted_relations": len(all_relations) - len(relations),
            }
        )
    return bounded


async def api_skills_audit(request: web.Request) -> web.Response:
    """GET /api/skills/-/audit — report overlap between pending and live skills.

    Read-only: nothing is staged, approved or dismissed from here.
    """
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)
    try:
        audit = await asyncio.get_running_loop().run_in_executor(discovery_executor(), skills.audit)
    except Exception:
        _sel().log_tool_invocation(
            session_key="",
            agent="api",
            source="dashboard",
            tool_name="api_skills_audit",
            tool_kind="skill",
            outcome="error",
            metadata={},
        )
        return web.json_response({"error": "internal error", "code": "internal_error"}, status=500)
    clusters = list(audit.get("clusters") or [])
    total_clusters = len(clusters)
    bounded_clusters = _bound_skill_audit_clusters(clusters)
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_skills_audit",
        tool_kind="skill",
        outcome="ok",
        metadata={
            "count": total_clusters,
            "returned": len(bounded_clusters),
            "omitted_entries": int(audit.get("omitted_entries") or 0),
            "omitted_relations": int(audit.get("omitted_relations") or 0),
        },
    )
    return web.json_response(
        {
            "clusters": bounded_clusters,
            "total_clusters": total_clusters,
            "omitted_entries": int(audit.get("omitted_entries") or 0),
            "omitted_relations": int(audit.get("omitted_relations") or 0),
        }
    )
