"""Find the pull request for a channel's branch, with its CI rollup.

Both reads go through the dashboard's source-provider runner, which is already
the one hardened path from the gateway to ``gh``: an allowlisted executable, a
strict env allowlist that pins ``GH_HOST``, an output cap, a timeout, OS
isolation and an SEL record per call. ``gh`` therefore never runs inside the
worktree and never reads its config: the repo and branch are passed explicitly.

The PR is found with ``gh pr list --head`` (an empty list is a clean "no PR",
where ``gh pr view <branch>`` would exit 1 with prose to parse). Its state and CI
come from the same read the dashboard's PR chip uses (``_fetch_check_status``),
so the channel and the dashboard cannot disagree about a PR — and that read
fetches ``statusCheckRollup`` separately, so a token without Checks access
still gets the PR's number and state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kiro_crew.slack.code_channels.workspace.remote import RepoRef
from kiro_crew.validation import is_valid_followup_branch

logger = logging.getLogger(__name__)

_PUBLIC_GITHUB = frozenset({"github.com", "www.github.com"})

#: Chip-vocabulary states this module reports.
PR_STATES = frozenset({"open", "draft", "merged", "closed"})
#: Chip-vocabulary CI values.
CI_STATES = frozenset({"passed", "failed", "running"})


@dataclass(frozen=True)
class PullRequest:
    """One PR: ``state`` and ``ci`` use the dashboard chip's vocabulary (``ci`` may be "")."""

    number: int
    url: str
    state: str
    ci: str = ""


@dataclass(frozen=True)
class PrLookup:
    """``status`` is ``found``, ``none`` (no PR for the branch) or ``unavailable``.

    ``unavailable`` covers gh missing, gh signed out, an unlisted Enterprise host
    and a forge that is not GitHub; the caller keeps what it last knew.
    """

    status: str
    pr: PullRequest | None = None
    reason: str = ""


def _github_host_allowed(host: str) -> bool:
    if host in _PUBLIC_GITHUB:
        return True
    try:
        from kiro_crew.dashboard.source_providers import hosts

        return host in hosts._allowed_github_hosts()
    except Exception:  # pragma: no cover - defensive: no dashboard in this process
        return False


async def _load_enterprise_hosts() -> None:
    """Warm the Enterprise-host allowlist the check above reads (empty while cold)."""
    try:
        from kiro_crew.dashboard.source_providers import hosts

        await hosts.ensure_gitlab_hosts_loaded()
    except Exception:
        logger.debug("forge host allowlist did not load", exc_info=True)


def _pick(rows: object, repo: RepoRef) -> PullRequest | None:
    """The open PR when there is one, else the newest; ``None`` for no usable row."""
    if not isinstance(rows, list):
        return None
    prefix = f"https://{repo.host}/{repo.owner}/{repo.name}/pull/".lower()
    candidates: list[PullRequest] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        number, url = row.get("number"), str(row.get("url") or "")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            continue
        if url.lower() != f"{prefix}{number}":
            continue
        raw = str(row.get("state") or "").lower()
        state = "draft" if raw == "open" and row.get("isDraft") else raw
        if state in PR_STATES:
            candidates.append(PullRequest(number, url, state))
    for pr in candidates:
        if pr.state in {"open", "draft"}:
            return pr
    return candidates[0] if candidates else None


async def find_pull_request(repo: RepoRef, branch: str) -> PrLookup:
    """The PR whose head is ``branch`` on ``repo`` (any state), with CI when known."""
    if not branch or not is_valid_followup_branch(branch):
        return PrLookup("unavailable", reason="branch")
    if repo.host not in _PUBLIC_GITHUB:
        await _load_enterprise_hosts()
    if not _github_host_allowed(repo.host):
        return PrLookup("unavailable", reason="host")
    from kiro_crew.dashboard.source_providers import chip_refresh, runner
    from kiro_crew.dashboard.source_providers.contract import SourceProviderError

    try:
        rows = await runner._run_json(
            "gh",
            "pr",
            "list",
            "--repo",
            repo.slug,
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "number,url,state,isDraft",
            "--limit",
            "5",
            host=repo.host,
        )
    except SourceProviderError as exc:
        logger.info("code channel PR lookup unavailable for %s: %s", repo.slug, exc)
        return PrLookup("unavailable", reason=exc.reason or "gh")
    except Exception:
        logger.warning("code channel PR lookup failed for %s", repo.slug, exc_info=True)
        return PrLookup("unavailable", reason="error")
    pr = _pick(rows, repo)
    if pr is None:
        return PrLookup("none")
    try:
        status = await chip_refresh._fetch_check_status(pr.url)
    except Exception as exc:  # gh signed out for checks, rate limit, outage
        logger.info("code channel CI read unavailable for %s: %s", pr.url, exc)
        status = None
    if isinstance(status, dict):
        state = status.get("state") if status.get("state") in PR_STATES else pr.state
        ci = status.get("ci") if status.get("ci") in CI_STATES else ""
        pr = PullRequest(pr.number, pr.url, str(state), str(ci or ""))
    return PrLookup("found", pr)
