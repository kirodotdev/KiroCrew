"""``/ci-runs``: recent CI runs across the connected GitHub and GitLab repos.

Read on demand only: the CI runs dashboard calls it while its tab is open, and
nothing else does. One bounded page of runs per repo (GitHub Actions runs through
the user's ``gh`` session, GitLab pipelines through ``glab``); a repo whose read
fails comes back as a row carrying an error code instead of failing the whole
response, so one unreachable repo never hides the others. Azure DevOps repos are
not listed.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import github_client, provider, store

logger = logging.getLogger("kirocrew.app.issue-radar")

#: Connected GitHub and GitLab repos listed per request.
CI_RUNS_MAX_REPOS = 20

#: The forges whose recent runs the dashboard lists.
CI_RUNS_PROVIDERS = frozenset({provider.GITHUB, provider.GITLAB})

#: The setup-failure code per forge: the CLI that needs signing in.
_SETUP_ERROR = {provider.GITHUB: "gh_setup", provider.GITLAB: "glab_setup"}

#: Repo reads in flight at once.
_CONCURRENCY = 4


async def _handle_ci_runs(request: web.Request) -> web.Response:
    """GET /ci-runs — ``{"repos": [{owner, repo, provider, host, alerts, runs, error}]}``.

    ``alerts`` echoes the repo's ``notify_on_own_pr_ci_failure`` setting. ``error``
    is ``None`` or a fixed code (``provider_error``, ``gh_setup``, ``glab_setup``):
    the CLI's own stderr is logged, never returned. ``truncated`` is true when more
    GitHub and GitLab repos are connected than one response lists.
    """
    entries = await asyncio.to_thread(store.list_connected_repos)
    keys: list[provider.RepoKey] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("owner") or not entry.get("repo"):
            continue
        key = provider.key_from_parts(
            str(entry["owner"]), str(entry["repo"]), entry.get("provider"), entry.get("host")
        )
        if key.provider in CI_RUNS_PROVIDERS:
            keys.append(key)
    listed = keys[:CI_RUNS_MAX_REPOS]
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def _one(key: provider.RepoKey) -> dict[str, Any]:
        async with gate:
            return await _repo_runs(key)

    rows = await asyncio.gather(*(_one(k) for k in listed))
    return web.json_response({"repos": list(rows), "truncated": len(keys) > len(listed)})


async def _repo_runs(key: provider.RepoKey) -> dict[str, Any]:
    settings = await asyncio.to_thread(
        partial(store.read_repo_settings, key.owner, key.repo, provider=key.provider, host=key.host)
    )
    row: dict[str, Any] = {
        "owner": key.owner,
        "repo": key.repo,
        "provider": key.provider,
        "host": key.host,
        "alerts": bool(settings.get("notify_on_own_pr_ci_failure")),
        "runs": [],
        "error": None,
    }
    client = provider.client_for(key)
    try:
        row["runs"] = await asyncio.to_thread(
            partial(
                client.list_recent_workflow_runs,
                key.owner,
                key.repo,
                **provider.call_kwargs(key),
            )
        )
    # The clients' error classes are shared aliases (see errors.py), so these two
    # catch a glab failure exactly as they catch a gh one.
    except github_client.GhSetupError as exc:
        logger.warning(
            "issue-radar ci-runs: %s CLI unavailable for %s: %s", key.provider, key.slug, exc
        )
        row["error"] = _SETUP_ERROR.get(key.provider, "provider_error")
    except github_client.GhCliError as exc:
        logger.warning("issue-radar ci-runs provider error for %s: %s", key.slug, exc)
        row["error"] = "provider_error"
    except Exception:  # noqa: BLE001 — one repo's failure must not fail the response
        logger.warning("issue-radar ci-runs failed for %s", key.slug, exc_info=True)
        row["error"] = "provider_error"
    return row
