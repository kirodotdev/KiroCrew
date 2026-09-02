"""Live PR status + CI checks, and the watcher verdict derived from them.

Replaces the upstream app's review-service client, which spoke to an internal
service over ``curl`` with a cookie from disk (303 lines of auth plumbing, SSO
redirect handling, and a proprietary analyzer-status vocabulary). None of that survives:
Kiro Crew already ships a provider-neutral GitHub/GitLab PR reader at
``kiro_crew.dashboard.handlers.source_providers``, which is cached (30 s TTL),
coalesces concurrent fetches for one URL, redacts credentials out of provider
payloads, and resolves the ``gh``/``glab`` binaries through a validated
allowlist. This module is a thin, app-specific *interpreter* on top of it.

What it adds over the raw provider payload is the single question the watcher
loop needs answered: **is this PR done, does it need work, or is it blocked?**
That verdict was previously parsed out of an LLM's free-text reply, which made
the loop's control flow depend on prose. Here it is computed from structured
provider fields instead, and the agent is only asked to *act*, never to report
state.

Mergeability vocabulary (GitHub, via the provider's own normalization):
  ``mergeable``   — no conflicts; ``conflicting`` — needs a rebase;
  ``unknown`` — GitHub is still computing it (the provider already re-reads a
  few times before giving up, so ``unknown`` here means genuinely unsettled).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── verdicts the watcher loop switches on ────────────────────────────────────

#: Nothing left to do — the PR is ready for a human to publish/merge, or is
#: already merged. The watcher stops nudging.
VERDICT_READY = "READY"
#: The agent should keep working: failing checks, conflicts, or open review
#: comments that need addressing.
VERDICT_PROGRESS = "PROGRESS"
#: Something the agent cannot fix by editing code (permissions, a closed PR, a
#: provider error). The watcher stops and surfaces it.
VERDICT_BLOCKED = "BLOCKED"

#: Check conclusions that mean "this gate is red".
_FAILING_CONCLUSIONS = frozenset(
    {"FAILURE", "FAILED", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
)
#: Check conclusions that mean "still running" — not a failure, not yet a pass.
_PENDING_CONCLUSIONS = frozenset({"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", ""})
#: PR states that end the watcher regardless of checks.
_CLOSED_STATES = frozenset({"CLOSED", "MERGED"})


def _conclusion_of(check: dict[str, Any]) -> str:
    """Normalized upper-case conclusion for one check row."""
    return str(check.get("conclusion") or check.get("state") or "").upper()


def _is_required(check: dict[str, Any]) -> bool:
    """Whether a red result on this check should block.

    ``allow_failure`` is GitLab's explicit opt-out; a check that allows failure
    is advisory and must not drive the verdict, or a flaky optional job would
    keep the loop nudging forever.
    """
    return not bool(check.get("allow_failure"))


def summarize_checks(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll a list of check rows into counts + a one-line label."""
    failing: list[str] = []
    pending = 0
    passed = 0
    # Red-but-advisory checks are counted separately: they must not drive the
    # verdict, but reporting "no checks" when a check demonstrably ran would
    # misdescribe the PR in the Changes table.
    advisory_failing = 0
    for check in checks or []:
        conclusion = _conclusion_of(check)
        name = str(check.get("name") or check.get("context") or "check")
        if conclusion in _FAILING_CONCLUSIONS:
            if _is_required(check):
                failing.append(name)
            else:
                advisory_failing += 1
            continue
        if conclusion in _PENDING_CONCLUSIONS:
            pending += 1
            continue
        passed += 1
    if failing:
        label = f"{len(failing)} failing"
    elif pending:
        label = f"{pending} running"
    elif advisory_failing and not passed:
        label = f"{advisory_failing} failing (advisory)"
    elif passed:
        label = "all passed"
    else:
        label = "no checks"
    return {
        "failing": failing,
        "failingCount": len(failing),
        "advisoryFailing": advisory_failing,
        "pending": pending,
        "passed": passed,
        # How many checks this summary actually saw. The auto-publish gate requires
        # `total > 0` to prove a PR is green rather than merely un-red — a PR with no
        # checks at all must not read as passing. Without this key that gate could never
        # be satisfied, so every green draft was refused with "no checks ran".
        # Advisory failures count: they ran, they just cannot drive the verdict.
        "total": len(failing) + advisory_failing + pending + passed,
        "label": label,
    }


def derive_verdict(pr: dict[str, Any], checks_summary: dict[str, Any]) -> tuple[str, str]:
    """Return ``(verdict, reason)`` for a PR from structured fields only.

    Deliberately fail-safe toward PROGRESS rather than READY: declaring a PR
    ready when it is not would end the watcher on an unfinished change, which is
    the expensive mistake. An extra nudge cycle is cheap.
    """
    state = str(pr.get("state") or "").upper()
    if state == "MERGED" or pr.get("mergedAt"):
        return VERDICT_READY, "merged"
    if state == "CLOSED":
        return VERDICT_BLOCKED, "pull request was closed without merging"
    if state and state not in {"OPEN"} and state in _CLOSED_STATES:
        return VERDICT_BLOCKED, f"pull request state is {state.lower()}"

    if checks_summary.get("failingCount"):
        names = ", ".join(checks_summary.get("failing", [])[:3])
        return VERDICT_PROGRESS, f"failing checks: {names}"

    mergeable = str(pr.get("mergeable") or "").lower()
    if mergeable == "conflicting":
        return VERDICT_PROGRESS, "merge conflicts — needs a rebase onto the base branch"

    unresolved = int(pr.get("unresolvedThreads") or 0)
    if unresolved:
        return VERDICT_PROGRESS, f"{unresolved} unresolved review thread(s)"

    if checks_summary.get("pending"):
        return VERDICT_PROGRESS, f"{checks_summary['pending']} check(s) still running"

    if mergeable == "unknown":
        return VERDICT_PROGRESS, "mergeability still being computed"

    return VERDICT_READY, "checks green, no conflicts, no open threads"


def _count_unresolved(pr: dict[str, Any]) -> int:
    """Unresolved review threads, when the provider payload carries them.

    Reads ``resolvable`` + ``resolved``, which is what the dashboard's provider normalization
    actually writes onto a comment (``source_providers.py``). An earlier version tested
    ``isResolved is False`` — a key the provider never emits — so this returned 0 for every
    pull request, which in turn made ``unresolvedThreads`` always 0 and left BOTH open-thread
    guards dead: this counter feeds ``derive_verdict`` and the ``autoPublish`` gate.
    ``resolvable`` is required because a plain issue comment is not a thread and can never be
    "resolved"; counting those would block publish on ordinary discussion. Raised by the
    Opus 5 review.
    """
    total = 0
    for comment in pr.get("comments") or []:
        if not isinstance(comment, dict):
            continue
        if comment.get("resolvable") and comment.get("resolved") is False:
            total += 1
    return total


async def fetch_pr_status(url: str, *, refresh: bool = False) -> dict[str, Any]:
    """Fetch a PR and reduce it to the app's status record.

    Returns a dict with ``ok`` False and an ``error`` string rather than raising:
    a provider hiccup must degrade one row in the Changes table, never fail a
    whole run. This mirrors how the upstream original degraded on a stale auth
    cookie — minus the cookie.
    """
    # In-function ON PURPOSE, and one of `top-level-imports`' three documented exceptions:
    # `try: import … except ImportError:` for an optional dependency. The app degrades to
    # "provider unavailable" rather than failing to import, so a core layout change cannot
    # take the whole builtin offline at gateway startup. Hoisting this to module scope was
    # suggested by review; doing so would delete the guard, which is the point of it.
    try:
        from kiro_crew.dashboard.handlers import source_providers as sp
    except ImportError as exc:  # pragma: no cover - core always ships this
        return {"ok": False, "error": f"source provider unavailable: {exc}", "url": url}

    try:
        pr = await sp.fetch_pull_request(url, refresh=refresh)
    except Exception as exc:  # noqa: BLE001 - provider raises several error types
        logger.info("PR fetch failed for %s: %s", url, exc)
        return {"ok": False, "error": str(exc)[:300], "url": url}

    checks = list(pr.get("checks") or [])
    if refresh:
        # The full payload embeds a checks rollup, but an explicit refresh wants
        # the live check state, which has its own lighter endpoint.
        try:
            checks = await sp.fetch_pull_request_checks(url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("checks refresh failed for %s: %s", url, exc)

    summary = summarize_checks(checks)
    enriched = {**pr, "unresolvedThreads": _count_unresolved(pr)}
    verdict, reason = derive_verdict(enriched, summary)
    return {
        "ok": True,
        "url": url,
        "number": pr.get("number"),
        "title": pr.get("title") or "",
        "state": pr.get("state") or "",
        "draft": bool(pr.get("draft")),
        "mergeable": pr.get("mergeable") or "",
        "mergeStateStatus": pr.get("mergeStateStatus") or "",
        "headBranch": pr.get("headBranch") or "",
        "baseBranch": pr.get("baseBranch") or "",
        "author": pr.get("author") or "",
        "additions": pr.get("additions") or 0,
        "deletions": pr.get("deletions") or 0,
        "changedFiles": pr.get("changedFiles") or 0,
        "updatedAt": pr.get("updatedAt") or "",
        "unresolvedThreads": enriched["unresolvedThreads"],
        "checks": summary,
        "verdict": verdict,
        "verdictReason": reason,
    }
