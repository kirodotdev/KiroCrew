"""Bounded projections for explicitly authorized resource reads.

Application-specific readers live with their applications. These helpers neither
discover permissions nor instantiate a runner, a worker, or a preview server.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

MAX_ROWS = 100
MAX_TEXT = 8000
_SECRET_KEYS = {
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "api_key",
    "access_token",
    "refresh_token",
    "client_secret",
}


def require_read(operation: str, body: dict) -> None:
    if operation != "read":
        raise ValueError("unsupported_operation")
    if body != {}:
        raise ValueError("invalid_body")


def contained_path(workspace_root: Path, value: str) -> Path:
    """Require an absolute native path resolving inside the configured root."""
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("resource_scope_mismatch")
    root = workspace_root.resolve()
    path = Path(value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("resource_scope_mismatch")
    return path


def safe_projection(value: Any, *, depth: int = 0, _budget: list[int] | None = None) -> Any:
    """Redact user/provider text and cap nested payloads before serialization."""
    budget = _budget if _budget is not None else [48000, 2000]
    budget[1] -= 1
    if depth > 8 or budget[1] < 0:
        return None
    if isinstance(value, str):
        text = redact_credentials(redact_exfiltration_urls(value)[0])[0]
        text = text[: min(MAX_TEXT, budget[0])]
        budget[0] -= len(text)
        return text
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {
            str(key)[:100]: (
                "[REDACTED]"
                if str(key).lower() in _SECRET_KEYS
                else safe_projection(item, depth=depth + 1, _budget=budget)
            )
            for key, item in list(value.items())[:MAX_ROWS]
        }
    if isinstance(value, (tuple, list)):
        return [safe_projection(item, depth=depth + 1, _budget=budget) for item in value[:MAX_ROWS]]
    return None


def taskrunner_reader(runner):
    """Bind the existing runner; expose one authorized run's state or results."""
    from kiro_crew.task_reporter import build_status

    async def read_resource(
        workspace_root: Path,
        resource_id: str,
        document: str,
        operation: str,
        body: dict,
    ) -> dict:
        require_read(operation, body)
        if document not in {"state", "result"}:
            raise ValueError("invalid_document")
        run = runner._runs.get(resource_id) if runner is not None else None
        if run is None:
            raise ValueError("resource_not_found")
        contained_path(workspace_root, run.work_dir)
        # Pass only this run to the native projection, never aggregate globally.
        tasks = {resource_id: runner._tasks[resource_id]} if resource_id in runner._tasks else {}
        row = build_status({resource_id: run}, tasks)["runs"][0]
        fields = (
            "task_id",
            "name",
            "running",
            "status",
            "started_at",
            "finished_at",
            "tasks",
            "current_task",
            "completed",
            "failed",
            "skipped",
            "error",
            "tokens_used",
        )
        result = {key: row[key] for key in fields}
        if document == "result":
            result["steps"] = row["task_details"][:MAX_ROWS]
            result["lessons_learned"] = row["lessons_learned"]
            result["truncated"] = len(row["task_details"]) > MAX_ROWS
        return safe_projection({"resource_id": resource_id, document: result})

    return read_resource
