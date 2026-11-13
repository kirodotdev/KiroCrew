"""Read registered-project metadata and proposals without starting a preview.

The backend server module is deliberately not imported: its import-time runtime
state and on-demand preview helpers are not a passive resource-read API.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from kiro_crew.apps.manager import app_data_dir
from kiro_crew.dashboard.resource_readers import (
    MAX_ROWS,
    contained_path,
    require_read,
    safe_projection,
)

from . import request_state


def _reader_runtime(data: Path) -> SimpleNamespace:
    runtime = SimpleNamespace(
        DATA_DIR=data,
        QUEUE_DIR=data / "queue",
        HANDLED_DIR=data / "handled",
        json=json,
        os=os,
        MAX_RECORD_BYTES=2 * 1024 * 1024,
        _PathEscape=request_state.PathEscape,
        _contained=request_state.contain_path,
    )
    runtime._request_status = request_state.request_status
    runtime._el_name = request_state.element_name
    runtime._redact_text = lambda value: safe_projection(str(value or ""))
    runtime._redact_thread = lambda thread: request_state.redact_thread(runtime, thread)
    runtime._summarize_comment = lambda comment: request_state.summarize_comment(runtime, comment)
    return runtime


async def read_resource(
    workspace_root: Path, resource_id: str, document: str, operation: str, body: dict
) -> dict:
    require_read(operation, body)
    if document not in {"preview", "proposals"}:
        raise ValueError("invalid_document")

    def read() -> dict:
        result: dict[str, Any]
        runtime = _reader_runtime(app_data_dir("design-tweak"))
        config = request_state.load_config(runtime)
        project = next((row for row in config["projects"] if str(row["id"]) == resource_id), None)
        if project is None:
            raise ValueError("resource_not_found")
        root = contained_path(workspace_root, project["path"])
        if document == "preview":
            # A persisted URL is not evidence of a still-running listener. No
            # ensure/start, liveness probe, URL fetch, or dev-server scan here.
            result = {
                "preview": {
                    "project_id": resource_id,
                    "name": project.get("name", ""),
                    "project_root": str(root),
                    "started": False,
                    "live_preview_available": None,
                }
            }
        else:
            proposals: list[dict] = []
            truncated = False
            for directory in (runtime.QUEUE_DIR, runtime.HANDLED_DIR):
                for path in sorted(directory.glob("*.json")):
                    record = request_state.read_request(runtime, path)
                    if record is None or str(record.get("projectId", "")) != resource_id:
                        continue
                    # Conflicting root evidence may not be hidden by a valid ID.
                    record_root = contained_path(workspace_root, record.get("projectRoot", ""))
                    if record_root != root:
                        raise ValueError("resource_scope_mismatch")
                    if len(proposals) == MAX_ROWS:
                        truncated = True
                        break
                    summary = request_state.summarize_request(runtime, record)
                    # Source maps and preview URLs are outside the proposal
                    # state contract; do not relay arbitrary stored file paths.
                    for comment in summary["comments"]:
                        for field in ("sourceFile", "source", "previewUrl"):
                            comment.pop(field, None)
                    proposals.append(summary)
                if truncated:
                    break
            result = {"proposals": proposals, "truncated": truncated}
        return safe_projection({"resource_id": resource_id, **result})

    return await asyncio.to_thread(read)


def register_routes(ctx):
    """Register passive resources through the App Kit route-hook grant."""
    from kiro_crew.apps.manager import is_app_enabled
    from kiro_crew.apps.resource_access import register_provider

    if ctx.http_app is None:
        return []
    register_provider(
        ctx.http_app, "design-tweak", read_resource, enabled=lambda: is_app_enabled("design-tweak")
    )
    return []
