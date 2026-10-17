"""``/ci-runs``: recent GitHub Actions runs across the connected GitHub repos.

Read on demand only: the CI runs dashboard calls it while its tab is open, and
nothing else does. One bounded page of runs per repo through the user's existing
``gh`` session; a repo whose read fails comes back as a row carrying an error
code instead of failing the whole response, so one unreachable repo never hides
the others.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import github_client, provider, store

logger = logging.getLogger("kirocrew.app.issue-radar")

#: Connected GitHub repos listed per request.
CI_RUNS_MAX_REPOS = 20

#: Repo reads in flight at once.
_CONCURRENCY = 4


async def _handle_ci_runs(request: web.Request) -> web.Response:
    """GET /ci-runs — ``{"repos": [{owner, repo, provider, host, alerts, runs, error}]}``.

    ``alerts`` echoes the repo's ``notify_on_own_pr_ci_failure`` setting. ``error``
    is ``None`` or a fixed code (``provider_error``, ``gh_setup``): ``gh``'s own
    stderr is logged, never returned. ``truncated`` is true when more GitHub repos
    are connected than one response lists.
    """
    entries = await asyncio.to_thread(store.list_connected_repos)
    keys: list[provider.RepoKey] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("owner") or not entry.get("repo"):
            continue
        key = provider.key_from_parts(
            str(entry["owner"]), str(entry["repo"]), entry.get("provider"), entry.get("host")
        )
        if key.provider == provider.GITHUB:
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
    try:
        row["runs"] = await asyncio.to_thread(
            partial(github_client.list_recent_workflow_runs, key.owner, key.repo)
        )
    except github_client.GhSetupError as exc:
        logger.warning(
            "issue-radar ci-runs: gh unavailable for %s/%s: %s", key.owner, key.repo, exc
        )
        row["error"] = "gh_setup"
    except github_client.GhCliError as exc:
        logger.warning("issue-radar ci-runs provider error for %s/%s: %s", key.owner, key.repo, exc)
        row["error"] = "provider_error"
    except Exception:  # noqa: BLE001 — one repo's failure must not fail the response
        logger.warning("issue-radar ci-runs failed for %s/%s", key.owner, key.repo, exc_info=True)
        row["error"] = "provider_error"
    return row
