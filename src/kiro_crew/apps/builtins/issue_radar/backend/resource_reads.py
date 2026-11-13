"""Cache-only reads for an exactly granted connected repository.

No provider client or AI-summary handler is called. A missing cache remains
missing; it never triggers credential-backed refresh or model execution.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kiro_crew.dashboard.resource_readers import MAX_ROWS, require_read, safe_projection

from . import store

_ITEM = re.compile(r"(issue|pull|investigation)-([1-9][0-9]{0,8})\Z")


def resource_id_for_repo(record: dict) -> str:
    identity = [
        record.get("provider", "github"),
        record.get("host", "github.com"),
        record.get("owner", ""),
        record.get("repo", ""),
    ]
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


async def read_resource(
    workspace_root: Path, resource_id: str, document: str, operation: str, body: dict
) -> dict:
    # The exact owner-approved workspace/resource grant is the association.
    # A connected repository does not itself carry a local workspace path.
    require_read(operation, body)
    if document != "snapshot" and _ITEM.fullmatch(document) is None:
        raise ValueError("invalid_document")

    def read() -> dict:
        records = store.list_connected_repos()
        record = next((row for row in records if resource_id_for_repo(row) == resource_id), None)
        if record is None:
            raise ValueError("resource_not_found")
        owner, repo = record["owner"], record["repo"]
        provider, host = record.get("provider", "github"), record.get("host", "github.com")
        root = store.provider_root(provider=provider, host=host)
        result = {"resource_id": resource_id, "cache_only": True}
        if document == "snapshot":
            issues = store.read_issues_cache(owner, repo, root=root)
            pulls = store.read_pulls_cache(owner, repo, root=root)
            result.update(
                issues=None if issues is None else issues[:MAX_ROWS],
                pulls=None if pulls is None else pulls[:MAX_ROWS],
                available={"issues": issues is not None, "pulls": pulls is not None},
                truncated=len(issues or []) > MAX_ROWS or len(pulls or []) > MAX_ROWS,
            )
        else:
            match = _ITEM.fullmatch(document)
            assert match is not None
            kind, number = match.group(1), int(match.group(2))
            readers: dict[str, Callable[..., Any]] = {
                "issue": store.read_issue_detail_cache,
                "pull": store.read_pr_detail_cache,
                "investigation": store.read_investigation,
            }
            result[document] = readers[kind](owner, repo, number, root=root)
        return safe_projection(result)

    return await asyncio.to_thread(read)
