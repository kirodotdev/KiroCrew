"""GitHub Projects (Projects v2) task management client and utilities.

Provides listing, creating, and completing tasks on a GitHub Project tied to a
repository or user/organization account via GitHub's GraphQL API.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import github_runner

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECS = 30.0
DEFAULT_TASK_LIMIT = 50
MAX_TASK_LIMIT = 100
DEFAULT_STATUS_DONE = "Done"
AUDIT_CALLER = "core:github-projects"

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_=-]+$")
_SSH_URL_PATTERN = re.compile(
    r"^(?:ssh://)?git@github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"
)
_HTTPS_URL_PATTERN = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"
)

GET_REPO_PROJECTS_QUERY = """
query($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    id
    name
    nameWithOwner
    projectsV2(first: 20) {
      nodes {
        id
        number
        title
        closed
      }
    }
    owner {
      ... on User {
        projectsV2(first: 20) {
          nodes {
            id
            number
            title
            closed
          }
        }
      }
      ... on Organization {
        projectsV2(first: 20) {
          nodes {
            id
            number
            title
            closed
          }
        }
      }
    }
  }
}
"""

GET_OWNER_PROJECT_QUERY = """
query($owner: String!, $number: Int!) {
  repositoryOwner(login: $owner) {
    ... on User {
      projectV2(number: $number) {
        id
        number
        title
        closed
      }
    }
    ... on Organization {
      projectV2(number: $number) {
        id
        number
        title
        closed
      }
    }
  }
}
"""

GET_PROJECT_FIELDS_QUERY = """
query($projectId: ID!) {
  node(id: $projectId) {
    ... on ProjectV2 {
      id
      title
      number
      fields(first: 50) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id
            name
            options {
              id
              name
            }
          }
          ... on ProjectV2Field {
            id
            name
            dataType
          }
        }
      }
    }
  }
}
"""

GET_PROJECT_ITEMS_QUERY = """
query($projectId: ID!, $first: Int!) {
  node(id: $projectId) {
    ... on ProjectV2 {
      id
      title
      number
      items(first: $first) {
        nodes {
          id
          type
          updatedAt
          createdAt
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                field {
                  ... on ProjectV2SingleSelectField {
                    name
                  }
                }
                name
                optionId
              }
              ... on ProjectV2ItemFieldTextValue {
                field {
                  ... on ProjectV2Field {
                    name
                  }
                }
                text
              }
            }
          }
          content {
            ... on DraftIssue {
              id
              title
              body
            }
            ... on Issue {
              id
              title
              number
              state
              url
              body
              repository {
                nameWithOwner
              }
            }
            ... on PullRequest {
              id
              title
              number
              state
              url
            }
          }
        }
      }
    }
  }
}
"""

GET_ITEM_PROJECT_QUERY = """
query($itemId: ID!) {
  node(id: $itemId) {
    ... on ProjectV2Item {
      id
      type
      project {
        id
        number
        title
      }
      content {
        ... on Issue {
          id
          number
          state
          url
        }
      }
    }
  }
}
"""

ADD_DRAFT_ISSUE_MUTATION = """
mutation($projectId: ID!, $title: String!, $body: String) {
  addProjectV2DraftIssue(input: {
    projectId: $projectId
    title: $title
    body: $body
  }) {
    projectItem {
      id
    }
  }
}
"""

UPDATE_ITEM_FIELD_VALUE_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $singleSelectOptionId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId
    itemId: $itemId
    fieldId: $fieldId
    value: {
      singleSelectOptionId: $singleSelectOptionId
    }
  }) {
    projectV2Item {
      id
    }
  }
}
"""


class GitHubProjectsError(RuntimeError):
    """Base exception for GitHub Projects operations."""


class GitHubAuthError(GitHubProjectsError):
    """Raised when authentication fails or required scope is missing."""


class GitHubNotFoundError(GitHubProjectsError):
    """Raised when a repository, project, item, or field is not found."""


@dataclass(frozen=True)
class GitHubProject:
    """GitHub Project (Projects v2) metadata."""

    id: str
    number: int
    title: str
    closed: bool = False


@dataclass(frozen=True)
class GitHubProjectTask:
    """Task item within a GitHub Project."""

    id: str
    title: str
    status: str
    type: str
    body: str = ""
    number: int | None = None
    url: str = ""
    repository: str = ""


def parse_repo_string(repo_str: str) -> tuple[str, str]:
    """Parse 'owner/repo' or GitHub URL into (owner, repo)."""
    text = (repo_str or "").strip()
    if not text:
        return "", ""
    m_ssh = _SSH_URL_PATTERN.match(text)
    if m_ssh:
        return m_ssh.group("owner"), m_ssh.group("repo")
    m_http = _HTTPS_URL_PATTERN.match(text)
    if m_http:
        return m_http.group("owner"), m_http.group("repo")
    if "/" in text and not text.startswith("http"):
        parts = text.split("/", 1)
        return parts[0].strip(), parts[1].strip().removesuffix(".git")
    return "", ""


def detect_repo_context(cwd: str | Path | None = None) -> tuple[str, str]:
    """Detect (owner, repo) from git remotes in current or target directory."""
    work_dir = Path(cwd) if cwd else Path.cwd()
    for remote_name in ("upstream", "origin"):
        try:
            proc = subprocess.run(
                ["git", "config", "--get", f"remote.{remote_name}.url"],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                owner, repo = parse_repo_string(proc.stdout.strip())
                if owner and repo:
                    return owner, repo
        except Exception as exc:
            logger.debug("Failed reading git remote %s: %s", remote_name, exc)
    return "", ""


def run_graphql_query(
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECS,
) -> dict[str, Any]:
    """Execute a GraphQL query or mutation via GitHub CLI (gh api graphql)."""
    try:
        gh_bin = github_runner.resolve_gh()
    except Exception as exc:
        raise GitHubAuthError(f"GitHub CLI is not available: {exc}") from exc

    argv = [gh_bin, "api", "graphql", "-f", f"query={query.strip()}"]
    if variables:
        for key, val in variables.items():
            if val is not None:
                if isinstance(val, (int, float, bool)):
                    argv.extend(["-F", f"{key}={val}"])
                else:
                    argv.extend(["-f", f"{key}={val}"])

    try:
        proc = github_runner.run_gh(argv, timeout=timeout, audit_caller=AUDIT_CALLER)
    except subprocess.TimeoutExpired as exc:
        raise GitHubProjectsError(f"GitHub API query timed out after {timeout}s") from exc
    except Exception as exc:
        raise GitHubProjectsError(f"Failed invoking gh CLI: {exc}") from exc

    stdout_text = proc.stdout or ""
    stderr_text = proc.stderr or ""
    combined = f"{stdout_text}\n{stderr_text}".strip()

    if proc.returncode != 0:
        lowered = combined.lower()
        if (
            "http 401" in lowered
            or "http 403" in lowered
            or "bad credentials" in lowered
            or "authentication" in lowered
            or "requires the 'project' scope" in lowered
            or "resource not accessible" in lowered
        ):
            raise GitHubAuthError(
                f"GitHub API authentication/permission error (exit {proc.returncode}): {combined}"
            )
        raise GitHubProjectsError(f"GitHub API query failed (exit {proc.returncode}): {combined}")

    try:
        payload = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise GitHubProjectsError("Malformed JSON response from GitHub API") from exc

    if not isinstance(payload, dict):
        raise GitHubProjectsError("Unexpected response shape from GitHub API")

    errors = payload.get("errors")
    if errors and isinstance(errors, list):
        msg = "; ".join(
            str(err.get("message", err)) if isinstance(err, dict) else str(err) for err in errors
        )
        lowered_msg = msg.lower()
        if "not found" in lowered_msg or "could not resolve" in lowered_msg:
            raise GitHubNotFoundError(f"GitHub resource not found: {msg}")
        if "forbidden" in lowered_msg or "scope" in lowered_msg or "permission" in lowered_msg:
            raise GitHubAuthError(f"GitHub permission error: {msg}")
        raise GitHubProjectsError(f"GitHub GraphQL error: {msg}")

    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    return data


def resolve_project(
    owner: str = "",
    repo: str = "",
    project_number: int | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECS,
) -> GitHubProject:
    """Resolve a GitHub Project from owner/repo or project number."""
    if not owner and not repo:
        detected_owner, detected_repo = detect_repo_context()
        owner = detected_owner or owner
        repo = detected_repo or repo

    if not owner:
        # Fall back to authenticated user
        try:
            gh_bin = github_runner.resolve_gh()
            proc = github_runner.run_gh(
                [gh_bin, "api", "user", "--jq", ".login"],
                timeout=timeout,
                audit_caller=AUDIT_CALLER,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                owner = proc.stdout.strip()
        except Exception:
            pass

    if not owner:
        raise GitHubNotFoundError(
            "GitHub owner or repository could not be determined. "
            "Please provide --owner or --repo."
        )

    # 1. If explicit project_number is specified, query owner's project by number
    if project_number is not None:
        data = run_graphql_query(
            GET_OWNER_PROJECT_QUERY,
            {"owner": owner, "number": project_number},
            timeout=timeout,
        )
        owner_data = data.get("repositoryOwner") or {}
        proj = owner_data.get("projectV2")
        if proj and isinstance(proj, dict) and proj.get("id"):
            return GitHubProject(
                id=str(proj["id"]),
                number=int(proj.get("number") or project_number),
                title=str(proj.get("title") or ""),
                closed=bool(proj.get("closed")),
            )
        raise GitHubNotFoundError(
            f"GitHub Project #{project_number} not found for owner '{owner}'."
        )

    # 2. Query repository's projectsV2 and owner fallback
    if repo:
        data = run_graphql_query(
            GET_REPO_PROJECTS_QUERY,
            {"owner": owner, "repo": repo},
            timeout=timeout,
        )
        repo_data = data.get("repository") or {}
        repo_projs = (repo_data.get("projectsV2") or {}).get("nodes") or []
        open_repo_projs = [p for p in repo_projs if isinstance(p, dict) and not p.get("closed")]
        if open_repo_projs:
            first = open_repo_projs[0]
            return GitHubProject(
                id=str(first["id"]),
                number=int(first["number"]),
                title=str(first.get("title") or ""),
                closed=bool(first.get("closed")),
            )

        # Fallback to owner's projects
        owner_data = repo_data.get("owner") or {}
        owner_projs = (owner_data.get("projectsV2") or {}).get("nodes") or []
        open_owner_projs = [p for p in owner_projs if isinstance(p, dict) and not p.get("closed")]
        if open_owner_projs:
            first = open_owner_projs[0]
            return GitHubProject(
                id=str(first["id"]),
                number=int(first["number"]),
                title=str(first.get("title") or ""),
                closed=bool(first.get("closed")),
            )

    raise GitHubNotFoundError(
        f"No open GitHub Project found linked to repository '{owner}/{repo}'. "
        "Specify a project number with --project."
    )


def get_project_fields(
    project_id: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECS,
) -> list[dict[str, Any]]:
    """Retrieve field definitions for a ProjectV2 (including single-select options)."""
    if not project_id or not _ID_PATTERN.match(project_id):
        raise ValueError(f"Invalid project_id: {project_id!r}")

    data = run_graphql_query(
        GET_PROJECT_FIELDS_QUERY,
        {"projectId": project_id},
        timeout=timeout,
    )
    node = data.get("node") or {}
    fields_data = (node.get("fields") or {}).get("nodes") or []
    return [f for f in fields_data if isinstance(f, dict)]


def list_project_tasks(
    owner: str = "",
    repo: str = "",
    project_number: int | None = None,
    project_id: str = "",
    status_filter: str | None = None,
    limit: int = DEFAULT_TASK_LIMIT,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECS,
) -> list[dict[str, Any]]:
    """List tasks in a GitHub Project, optionally filtered by status name."""
    bounded_limit = max(1, min(limit, MAX_TASK_LIMIT))

    if not project_id:
        proj = resolve_project(
            owner=owner, repo=repo, project_number=project_number, timeout=timeout
        )
        project_id = proj.id

    data = run_graphql_query(
        GET_PROJECT_ITEMS_QUERY,
        {"projectId": project_id, "first": bounded_limit},
        timeout=timeout,
    )
    node = data.get("node") or {}
    items = (node.get("items") or {}).get("nodes") or []

    tasks: list[dict[str, Any]] = []
    filter_norm = status_filter.strip().lower() if status_filter else None

    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        item_type = str(item.get("type") or "DRAFT_ISSUE")

        # Extract status from fieldValues
        status_name = ""
        field_values = (item.get("fieldValues") or {}).get("nodes") or []
        for fv in field_values:
            if not isinstance(fv, dict):
                continue
            fld = fv.get("field") or {}
            if str(fld.get("name") or "").lower() == "status":
                status_name = str(fv.get("name") or "")
                break

        if filter_norm and status_name.lower() != filter_norm:
            continue

        content = item.get("content") or {}
        title = str(content.get("title") or "")
        body = str(content.get("body") or "")
        number = content.get("number")
        url = str(content.get("url") or "")
        repo_name = ""
        if isinstance(content.get("repository"), dict):
            repo_name = str(content["repository"].get("nameWithOwner") or "")

        tasks.append(
            {
                "id": item_id,
                "title": title,
                "status": status_name or "No Status",
                "type": item_type,
                "number": number,
                "url": url,
                "body": body,
                "repository": repo_name,
            }
        )

    return tasks


def create_project_task(
    title: str,
    body: str = "",
    owner: str = "",
    repo: str = "",
    project_number: int | None = None,
    project_id: str = "",
    status: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECS,
) -> dict[str, Any]:
    """Create a new task (draft issue) in the GitHub Project."""
    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("Task title cannot be empty.")
    if len(clean_title) > 1024:
        raise ValueError("Task title cannot exceed 1024 characters.")

    if not project_id:
        proj = resolve_project(
            owner=owner, repo=repo, project_number=project_number, timeout=timeout
        )
        project_id = proj.id

    data = run_graphql_query(
        ADD_DRAFT_ISSUE_MUTATION,
        {
            "projectId": project_id,
            "title": clean_title,
            "body": (body or "").strip(),
        },
        timeout=timeout,
    )

    created_info = (data.get("addProjectV2DraftIssue") or {}).get("projectItem") or {}
    item_id = str(created_info.get("id") or "")
    if not item_id:
        raise GitHubProjectsError("GitHub API did not return a project item ID after creation.")

    current_status = ""
    if status:
        fields = get_project_fields(project_id, timeout=timeout)
        status_field = next(
            (f for f in fields if str(f.get("name") or "").lower() == "status"),
            None,
        )
        if status_field:
            options = status_field.get("options") or []
            target_opt = next(
                (
                    opt
                    for opt in options
                    if str(opt.get("name") or "").lower() == status.strip().lower()
                ),
                None,
            )
            if target_opt and target_opt.get("id"):
                run_graphql_query(
                    UPDATE_ITEM_FIELD_VALUE_MUTATION,
                    {
                        "projectId": project_id,
                        "itemId": item_id,
                        "fieldId": str(status_field["id"]),
                        "singleSelectOptionId": str(target_opt["id"]),
                    },
                    timeout=timeout,
                )
                current_status = str(target_opt.get("name") or status)

    return {
        "id": item_id,
        "title": clean_title,
        "body": body,
        "status": current_status or "Todo",
        "type": "DRAFT_ISSUE",
        "projectId": project_id,
    }


def complete_project_task(
    task_id: str,
    owner: str = "",
    repo: str = "",
    project_number: int | None = None,
    project_id: str = "",
    status_name: str = DEFAULT_STATUS_DONE,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECS,
) -> dict[str, Any]:
    """Mark a project task as completed (updating its Status field to Done)."""
    clean_id = (task_id or "").strip()
    if not clean_id or not _ID_PATTERN.match(clean_id):
        raise ValueError(f"Invalid task_id: {task_id!r}")

    # Discover project if not explicitly supplied
    if not project_id:
        item_data = run_graphql_query(GET_ITEM_PROJECT_QUERY, {"itemId": clean_id}, timeout=timeout)
        node = item_data.get("node") or {}
        proj_node = node.get("project") or {}
        if proj_node.get("id"):
            project_id = str(proj_node["id"])

    if not project_id:
        proj = resolve_project(
            owner=owner, repo=repo, project_number=project_number, timeout=timeout
        )
        project_id = proj.id

    fields = get_project_fields(project_id, timeout=timeout)
    status_field = next(
        (f for f in fields if str(f.get("name") or "").lower() == "status"),
        None,
    )
    if not status_field:
        raise GitHubNotFoundError(f"Status field not found on project {project_id}.")

    options = status_field.get("options") or []
    target_status = status_name.strip().lower()
    matching_opt = next(
        (opt for opt in options if str(opt.get("name") or "").lower() == target_status),
        None,
    )

    if not matching_opt:
        # Fallback to option containing "done", "complete", or "closed"
        for opt in options:
            name_low = str(opt.get("name") or "").lower()
            if any(term in name_low for term in ("done", "complete", "closed")):
                matching_opt = opt
                break

    if not matching_opt and options:
        # Pick last option (typically the terminal column)
        matching_opt = options[-1]

    if not matching_opt or not matching_opt.get("id"):
        raise GitHubNotFoundError(
            f"Could not find a completion status option in project {project_id}."
        )

    run_graphql_query(
        UPDATE_ITEM_FIELD_VALUE_MUTATION,
        {
            "projectId": project_id,
            "itemId": clean_id,
            "fieldId": str(status_field["id"]),
            "singleSelectOptionId": str(matching_opt["id"]),
        },
        timeout=timeout,
    )

    return {
        "id": clean_id,
        "status": str(matching_opt.get("name") or status_name),
        "projectId": project_id,
        "completed": True,
    }
