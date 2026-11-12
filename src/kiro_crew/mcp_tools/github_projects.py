"""GitHub Projects (Projects v2) MCP tools for the kirocrew-core server.

Exposes tools to list, create, and complete tasks on a GitHub Project tied to
a repository or user account.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from kiro_crew import github_projects, mcp_core

logger = logging.getLogger(__name__)


def schemas() -> list[dict[str, Any]]:
    """Descriptors for GitHub Projects task management tools."""
    return [
        {
            "name": "github_project_tasks_list",
            "description": (
                "List and view tasks from a GitHub Project (Projects v2) tied to the current "
                "repository or a specified project. Returns each task's item ID, title, status, "
                "type, number, and URL."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": (
                            "GitHub owner (username or org). If omitted, automatically detected "
                            "from repository remotes or authenticated user."
                        ),
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository name. If omitted, detected from git remotes.",
                    },
                    "project_number": {
                        "type": "integer",
                        "description": "Optional specific project number (e.g. 1, 2, 3).",
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter tasks by status name (e.g. 'Todo', 'In Progress', 'Done').",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum tasks to return (default 50, max 100).",
                    },
                },
            },
        },
        {
            "name": "github_project_task_create",
            "description": (
                "Create a new task (draft issue) in the GitHub Project tied to the repository. "
                "Returns the created task item ID, title, and assigned status."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Title of the task to create.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Detailed description or checklist for the task.",
                    },
                    "owner": {
                        "type": "string",
                        "description": "GitHub owner (username or org).",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository name.",
                    },
                    "project_number": {
                        "type": "integer",
                        "description": "Specific project number.",
                    },
                    "status": {
                        "type": "string",
                        "description": "Initial status name to set (e.g. 'Todo', 'In Progress').",
                    },
                },
                "required": ["title"],
            },
        },
        {
            "name": "github_project_task_complete",
            "description": (
                "Mark a task in the GitHub Project as completed (sets its Status field to Done). "
                "Accepts the task's project item ID."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The ProjectV2Item ID of the task to complete.",
                    },
                    "owner": {
                        "type": "string",
                        "description": "GitHub owner.",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository name.",
                    },
                    "project_number": {
                        "type": "integer",
                        "description": "Project number.",
                    },
                    "status_name": {
                        "type": "string",
                        "description": "Status name for completion (defaults to 'Done').",
                    },
                },
                "required": ["task_id"],
            },
        },
    ]


def github_project_tasks_list(name: str, args: dict[str, Any]) -> str:
    """List tasks in a GitHub Project."""
    owner = str(args.get("owner") or "").strip()
    repo = str(args.get("repo") or "").strip()
    project_number = args.get("project_number")
    if project_number is not None:
        try:
            project_number = int(project_number)
        except (ValueError, TypeError):
            project_number = None

    status_filter = str(args.get("status") or "").strip() or None
    limit = int(args.get("limit") or github_projects.DEFAULT_TASK_LIMIT)

    try:
        tasks = github_projects.list_project_tasks(
            owner=owner,
            repo=repo,
            project_number=project_number,
            status_filter=status_filter,
            limit=limit,
        )
        res = json.dumps({"ok": True, "count": len(tasks), "tasks": tasks}, indent=2)
        cleaned, _ = mcp_core.redact_credentials(res)
        return cleaned
    except Exception as exc:
        err = json.dumps({"ok": False, "error": str(exc)})
        cleaned, _ = mcp_core.redact_credentials(err)
        return cleaned


def github_project_task_create(name: str, args: dict[str, Any]) -> str:
    """Create a task in a GitHub Project."""
    title = str(args.get("title") or "").strip()
    body = str(args.get("body") or "").strip()
    owner = str(args.get("owner") or "").strip()
    repo = str(args.get("repo") or "").strip()
    project_number = args.get("project_number")
    if project_number is not None:
        try:
            project_number = int(project_number)
        except (ValueError, TypeError):
            project_number = None

    status = str(args.get("status") or "").strip() or None

    try:
        task = github_projects.create_project_task(
            title=title,
            body=body,
            owner=owner,
            repo=repo,
            project_number=project_number,
            status=status,
        )
        res = json.dumps({"ok": True, "task": task}, indent=2)
        cleaned, _ = mcp_core.redact_credentials(res)
        return cleaned
    except Exception as exc:
        err = json.dumps({"ok": False, "error": str(exc)})
        cleaned, _ = mcp_core.redact_credentials(err)
        return cleaned


def github_project_task_complete(name: str, args: dict[str, Any]) -> str:
    """Mark a task in a GitHub Project as completed."""
    task_id = str(args.get("task_id") or "").strip()
    owner = str(args.get("owner") or "").strip()
    repo = str(args.get("repo") or "").strip()
    project_number = args.get("project_number")
    if project_number is not None:
        try:
            project_number = int(project_number)
        except (ValueError, TypeError):
            project_number = None

    status_name = str(args.get("status_name") or github_projects.DEFAULT_STATUS_DONE).strip()

    try:
        result = github_projects.complete_project_task(
            task_id=task_id,
            owner=owner,
            repo=repo,
            project_number=project_number,
            status_name=status_name,
        )
        res = json.dumps({"ok": True, "result": result}, indent=2)
        cleaned, _ = mcp_core.redact_credentials(res)
        return cleaned
    except Exception as exc:
        err = json.dumps({"ok": False, "error": str(exc)})
        cleaned, _ = mcp_core.redact_credentials(err)
        return cleaned


HANDLERS: dict[str, Callable[[str, dict[str, Any]], Any]] = {
    "github_project_tasks_list": github_project_tasks_list,
    "github_project_task_create": github_project_task_create,
    "github_project_task_complete": github_project_task_complete,
}
