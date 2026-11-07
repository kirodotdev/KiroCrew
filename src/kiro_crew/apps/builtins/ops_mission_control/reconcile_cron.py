from __future__ import annotations

import json
from typing import Any

from kiro_crew.cron_script import Report, Skip


def run(ctx: Any) -> None:
    response = ctx.call_tool(
        "kirocrew-core",
        "ops_mission_control_api",
        {"method": "POST", "path": "/reconcile"},
    )
    result = json.loads(response)
    if not isinstance(result, dict):
        raise ValueError("Ops Mission Control reconcile returned a non-object response")
    changed = result.get("changed")
    resolved = result.get("resolved")
    if not isinstance(changed, bool) or not isinstance(resolved, list):
        raise ValueError("Ops Mission Control reconcile returned an invalid result")
    if not changed:
        raise Skip()
    ids = [item.get("id") for item in resolved if isinstance(item, dict)]
    if not ids or any(not isinstance(incident_id, str) for incident_id in ids):
        raise ValueError("Ops Mission Control reconcile returned invalid incident IDs")
    raise Report("Resolved incidents: " + ", ".join(ids))
