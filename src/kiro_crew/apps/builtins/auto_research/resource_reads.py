"""Exact campaign reads; a configured resource grant supplies the association.

Campaigns have no native workspace ownership field. This reader never infers
ownership from a question, a source URL, or a campaign's research directory.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from kiro_crew.dashboard.resource_readers import require_read, safe_projection

from .campaign import publication, storage
from .session_keys import is_campaign_id


async def read_resource(
    workspace_root: Path, resource_id: str, document: str, operation: str, body: dict
) -> dict:
    require_read(operation, body)
    if document not in {"state", "report"}:
        raise ValueError("invalid_document")
    if not is_campaign_id(resource_id):
        raise ValueError("resource_not_found")

    def read() -> dict:
        result: dict[str, Any]
        campaign = storage.get_campaign(resource_id)
        if campaign is None:
            raise ValueError("resource_not_found")
        if document == "report":
            result = {"report": publication._read_report(resource_id)}
        else:
            fields = (
                "id",
                "name",
                "question",
                "status",
                "created_at",
                "started_at",
                "finished_at",
                "total_cycles",
                "max_cycles",
                "findings",
                "pending_question",
                "error_message",
            )
            result = {"state": {key: campaign[key] for key in fields if key in campaign}}
        return safe_projection({"resource_id": resource_id, **result})

    return await asyncio.to_thread(read)
