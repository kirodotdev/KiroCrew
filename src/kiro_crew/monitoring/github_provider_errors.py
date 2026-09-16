"""The GitHub monitors' shared provider-error vocabulary.

``github_pull_request`` and ``github_workflow_run`` probe the same host with
the same ``gh`` binary and token, so they read ``gh`` stderr the same way and
honour the same process-wide ``github:api`` cooldown. Both facts live here
once; each monitor keeps only its own result type.

Parsing itself is ``taskq.adapters.github.parse_gh_stderr`` (the dependency
coordinator's adapter): this module maps the adapter's category onto the
monitors' :class:`ProviderErrorKind` and reason codes, and reads the
coordinator's shared ``retry_at`` for the scope.
"""

from __future__ import annotations

from kiro_crew.monitoring.models import ProviderErrorKind

#: ``parse_gh_stderr().category`` -> monitor error kind.
PROVIDER_KIND_BY_CATEGORY: dict[str, ProviderErrorKind] = {
    "rate_limited": ProviderErrorKind.RATE_LIMITED,
    "authentication": ProviderErrorKind.AUTHENTICATION,
    "authorization": ProviderErrorKind.AUTHORIZATION,
    "not_found": ProviderErrorKind.NOT_FOUND,
    "transient": ProviderErrorKind.TRANSIENT,
}

#: Reason code recorded on a probe that failed with the given kind.
PROVIDER_REASON_BY_KIND: dict[ProviderErrorKind, str] = {
    ProviderErrorKind.RATE_LIMITED: "provider_rate_limited",
    ProviderErrorKind.AUTHENTICATION: "provider_authentication",
    ProviderErrorKind.AUTHORIZATION: "provider_authorization",
    ProviderErrorKind.NOT_FOUND: "provider_not_found",
    ProviderErrorKind.TRANSIENT: "provider_transient",
}

#: Reason code for a probe skipped because the process-wide ``github:api``
#: schedule is still cooling down. Distinct from ``provider_rate_limited`` so
#: a state history tells "GitHub refused this probe" from "this probe waited
#: out a refusal something else already took".
REASON_SHARED_COOLDOWN = "provider_rate_limited_shared"


def classify_cli_error(raw: str) -> ProviderErrorKind:
    """``gh`` stderr -> provider error kind, via the one shared GitHub adapter."""
    from kiro_crew.taskq.adapters.github import parse_gh_stderr

    return PROVIDER_KIND_BY_CATEGORY[parse_gh_stderr(raw).category]


def shared_cooldown(now: float) -> float | None:
    """The ``github:api`` scope's coordinated ``retry_at`` while it is ahead of *now*.

    Tasks that hit GitHub's rate limit park on one per-scope schedule in the
    dependency coordinator (``taskq.dependency``). A monitor probe is another
    GitHub call from the same host with the same token, so it spends nothing
    while that schedule says the scope is down: the probe answers
    ``RATE_LIMITED`` locally and is retried by the monitor's own cadence. No
    coordinator (a process that never opened a task store) reads as no
    cooldown. Read-only: a monitor has no task row and never joins the schedule.
    """
    from kiro_crew.taskq.adapters.github import SCOPE_API
    from kiro_crew.taskq.dependency import shared_retry_at

    retry_at = shared_retry_at(SCOPE_API)
    if retry_at is None or retry_at <= now:
        return None
    return retry_at


def shared_cooldown_summary(retry_at: float) -> str:
    return f"github:api is rate limited until {retry_at:.0f}; probe skipped"
