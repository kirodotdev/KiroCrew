"""A GitLab project's recent pipelines, for the CI runs dashboard.

Split out of ``gitlab_client`` to keep that module under the repository's
file-line cap; ``gitlab_client.list_recent_workflow_runs`` stays the import path
callers and tests use (it re-exports this function). The client's ``_glab_api`` /
``_rows`` are resolved through the module AT CALL TIME, not bound at import, so a
test patching ``gitlab_client._glab_api`` reaches this read too, and the late
import avoids an import cycle with ``gitlab_client``.

Each row has exactly the shape ``github_client.list_recent_workflow_runs`` returns,
in GitHub's status and conclusion vocabulary, because the dashboard renders both
forges with one component.
"""

from __future__ import annotations

import re

from . import gitlab_normalization as _normalization
from .github_client_ci import (
    _CI_NAME_MAX_CHARS,
    _CI_TOKEN_MAX_CHARS,
    _CI_URL_MAX_CHARS,
    CI_RECENT_RUNS_PER_REPO,
    _bounded_str,
)
from .github_normalization import parse_gh_timestamp

# A merge-request pipeline runs on GitLab's own MR ref rather than a branch, and the
# ref names the MR, so its iid comes free with the list row: no per-row call.
_MR_REF_RE = re.compile(r"^refs/merge-requests/([1-9][0-9]{0,9})/(?:head|merge|train)$")

# GitLab's in-flight pipeline statuses in GitHub's vocabulary. A running pipeline, and
# one still stopping its jobs (``canceling``), is GitHub's ``in_progress``; every
# other unfinished status (created, pending, preparing, waiting_for_resource,
# scheduled, manual) is still waiting to run.
_IN_FLIGHT_STATUS = {"running": "in_progress", "canceling": "in_progress"}


def list_recent_workflow_runs(
    owner: str, repo: str, *, host: str = "", timeout: float | None = None
) -> list[dict]:
    """The most recent pipelines in a project, newest first.

    One page of :data:`CI_RECENT_RUNS_PER_REPO` pipelines
    (``GET projects/:id/pipelines``). ``host`` is re-checked against the allowlist
    at the spawn boundary, like every ``glab`` call. Status and conclusion come
    from the same normalization the PR pane's run list uses, so a failed pipeline
    reads as conclusion ``failure``. ``pull_requests`` holds the MR iid of a
    merge-request pipeline and is empty otherwise.
    """
    from . import gitlab_client as client  # late: seam patching + no import cycle

    path = (
        f"projects/{client.project_path(owner, repo)}/pipelines"
        f"?per_page={CI_RECENT_RUNS_PER_REPO}&order_by=id&sort=desc"
    )
    effective_timeout = client.GL_TIMEOUT_SEC if timeout is None else timeout
    rows = client._rows(
        client._glab_api(path, host=host, timeout=effective_timeout, paginate=False)
    )
    out: list[dict] = []
    for row in rows:
        run_id = row.get("id")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            continue
        norm = _normalization._norm_pipeline_run(row)
        if norm is None:
            continue
        ref = row.get("ref") if isinstance(row.get("ref"), str) else None
        mr = _MR_REF_RE.match(ref or "")
        status = norm["status"]
        if status != "completed":
            status = _IN_FLIGHT_STATUS.get(status, "queued")
        out.append(
            {
                "id": run_id,
                "name": _bounded_str(norm["name"], _CI_NAME_MAX_CHARS) or "pipeline",
                "status": status,
                "conclusion": _bounded_str(norm["conclusion"], _CI_TOKEN_MAX_CHARS),
                "url": _bounded_str(norm["url"], _CI_URL_MAX_CHARS),
                "branch": None if mr else _bounded_str(ref, _CI_NAME_MAX_CHARS),
                "pull_requests": [int(mr.group(1))] if mr else [],
                "created_at": _bounded_str(row.get("created_at"), _CI_TOKEN_MAX_CHARS),
                "started_at": _bounded_str(row.get("started_at"), _CI_TOKEN_MAX_CHARS),
                "updated_at": _bounded_str(row.get("updated_at"), _CI_TOKEN_MAX_CHARS),
                "duration_sec": _duration(row),
            }
        )
    return out[:CI_RECENT_RUNS_PER_REPO]


def _duration(row: dict) -> int | None:
    """GitLab's own ``duration`` when the row carries one, else start to last update.

    The list endpoint usually omits both ``duration`` and ``started_at``, so the
    fallback measures from ``created_at``, as the GitHub read does for a run with
    no start stamp.
    """
    own = row.get("duration")
    if isinstance(own, (int, float)) and not isinstance(own, bool) and own >= 0:
        return int(own)
    started = parse_gh_timestamp(row.get("started_at") or row.get("created_at"))
    updated = parse_gh_timestamp(row.get("updated_at"))
    if started is None or updated is None or updated < started:
        return None
    return int((updated - started).total_seconds())
