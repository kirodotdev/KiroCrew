"""Typed public-GitHub pull-request observations for structured monitors."""

from __future__ import annotations

import errno
import hashlib
import json
import re
import subprocess
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from kiro_crew.github_runner import SetupError, resolve_gh, run_gh
from kiro_crew.monitoring.models import (
    MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET,
    MAX_MONITOR_CHECK_IDENTITY_CHARS,
    ProviderErrorKind,
)
from kiro_crew.monitoring.pull_request import (
    PullRequestCheck,
    PullRequestFacts,
    PullRequestProbeResult,
    build_pull_request_probe_result,
    opaque_provider_check_identity,
    provider_error_result,
)
from kiro_crew.security import redact

_GITHUB_HOST = "github.com"
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_URL_IN_CHECK_IDENTITY_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_RAW_URL_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_HTTP_STATUS_RE = re.compile(r"\bhttp\s+(\d{3})\b", re.IGNORECASE)
_HEAD_REVISION_RE = re.compile(r"^[0-9a-fA-F]{1,128}$")
_PROBE_TIMEOUT_SECS = 30.0
_REVIEW_THREAD_PAGE_SIZE = 100
_REVIEW_THREAD_MAX_PAGES = 10
_MERGEABLE_SETTLED_STATES = frozenset({"CLEAN", "HAS_HOOKS", "UNSTABLE"})
_PR_FIELDS = "number,state,isDraft,headRefOid,mergeable,mergeStateStatus,reviewDecision"
_CHECK_FIELDS = "statusCheckRollup,headRefOid"
_MAX_PULL_REQUEST_NUMBER = 2_147_483_647
_MAX_CHECK_ROWS = MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET * 4
_REVIEW_THREADS_QUERY = """
query($owner:String!,$repo:String!,$number:Int!,$cursor:String){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$number){
      reviewThreads(first:PAGE_SIZE,after:$cursor){
        pageInfo{hasNextPage endCursor}
        nodes{isResolved isOutdated}
      }
    }
  }
}
""".replace("PAGE_SIZE", str(_REVIEW_THREAD_PAGE_SIZE)).strip()

GitHubResolver = Callable[[], str]
GitHubRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class GitHubPullRequestTarget:
    """Validated identity of one public GitHub pull request."""

    host: str
    owner: str
    repo: str
    number: int

    def __post_init__(self) -> None:
        if self.host != _GITHUB_HOST:
            raise ValueError("target must be a public GitHub pull request")
        if any(
            segment in {".", ".."} or _SEGMENT_RE.fullmatch(segment) is None
            for segment in (self.owner, self.repo)
        ):
            raise ValueError("target must be a public GitHub pull request")
        if isinstance(self.number, bool) or not isinstance(self.number, int) or self.number <= 0:
            raise ValueError("target must be a public GitHub pull request")

    @property
    def identity(self) -> str:
        return f"{self.host}/{self.owner}/{self.repo}#{self.number}"

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.repo}/pull/{self.number}"


GitHubCheck = PullRequestCheck


@dataclass(frozen=True)
class GitHubPullRequestResponse:
    """Allowlisted provider facts with no raw response attached."""

    target: GitHubPullRequestTarget
    state: str
    draft: bool
    head_revision: str
    mergeability: str
    review_decision: str
    checks: tuple[GitHubCheck, ...]
    checks_complete: bool
    unresolved_review_threads: int
    review_threads_complete: bool


GitHubPullRequestProbeResult = PullRequestProbeResult


class GitHubPullRequestProvider:
    """Read public pull-request state through the authenticated hardened gh runner."""

    def __init__(
        self,
        *,
        resolver: GitHubResolver = resolve_gh,
        runner: GitHubRunner = run_gh,
    ) -> None:
        self._resolver = resolver
        self._runner = runner

    def probe(
        self,
        raw_target: str,
        *,
        previous_observation: Mapping[str, object] | None = None,
    ) -> GitHubPullRequestProbeResult:
        """Return one canonical review-ready observation."""
        try:
            target = parse_github_pull_request_target(raw_target)
            gh = self._resolver()
            primary = self._runner(
                [gh, "pr", "view", target.url, "--json", _PR_FIELDS],
                timeout=_PROBE_TIMEOUT_SECS,
                audit_caller="core:monitor",
                pin_host=_GITHUB_HOST,
            )
            failure = _process_failure(primary)
            if failure is not None:
                return failure
            raw_primary = _json_object(primary.stdout)
            response = _normalize_response(
                target,
                raw_primary,
                checks=(),
                checks_complete=True,
                unresolved_review_threads=0,
                review_threads_complete=True,
            )
            supplemental_error: ProviderErrorKind | None = None
            if response.state not in {"merged", "closed"}:
                checks, checks_complete, checks_error = self._checks(
                    gh,
                    target,
                    response.head_revision,
                )
                unresolved, complete, review_error = self._review_threads(gh, target)
                supplemental_error = _combine_provider_errors(checks_error, review_error)
                response = replace(
                    response,
                    checks=checks,
                    checks_complete=checks_complete,
                    unresolved_review_threads=unresolved,
                    review_threads_complete=complete,
                )
            return build_pull_request_probe_result(
                PullRequestFacts(
                    kind="github_pull_request",
                    target=response.target.identity,
                    state=response.state,
                    draft=response.draft,
                    head_revision=response.head_revision,
                    mergeability=response.mergeability,
                    review_decision=response.review_decision,
                    checks=response.checks,
                    checks_complete=response.checks_complete,
                    unresolved_review_threads=response.unresolved_review_threads,
                    review_threads_complete=response.review_threads_complete,
                ),
                previous_observation=previous_observation,
                response=response,
                supplemental_provider_error=supplemental_error,
            )
        except SetupError as exc:
            if _transient_os_error(exc.__cause__):
                return _provider_error(ProviderErrorKind.TRANSIENT, "provider_transient")
            return _provider_error(ProviderErrorKind.SETUP, "provider_setup")
        except FileNotFoundError:
            return _provider_error(ProviderErrorKind.SETUP, "provider_setup")
        except subprocess.TimeoutExpired:
            return _provider_error(ProviderErrorKind.TRANSIENT, "provider_transient")
        except OSError as exc:
            if _transient_os_error(exc):
                return _provider_error(ProviderErrorKind.TRANSIENT, "provider_transient")
            return _provider_error(ProviderErrorKind.SETUP, "provider_setup")
        except (TypeError, ValueError, KeyError):
            return _provider_error(
                ProviderErrorKind.TRANSIENT,
                "provider_malformed_response",
            )

    def _checks(
        self,
        gh: str,
        target: GitHubPullRequestTarget,
        expected_head: str,
    ) -> tuple[tuple[GitHubCheck, ...], bool, ProviderErrorKind | None]:
        try:
            proc = self._runner(
                [gh, "pr", "view", target.url, "--json", _CHECK_FIELDS],
                timeout=_PROBE_TIMEOUT_SECS,
                audit_caller="core:monitor",
                pin_host=_GITHUB_HOST,
            )
        except (SetupError, FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            return (), False, _provider_exception_kind(exc)
        failure = _process_failure(proc)
        if failure is not None:
            return (), False, failure.observation.provider_error
        try:
            raw = _json_object(proc.stdout)
            head = raw.get("headRefOid")
            if not isinstance(head, str) or head != expected_head:
                return (), False, ProviderErrorKind.TRANSIENT
            rollup = raw.get("statusCheckRollup")
            if rollup is None:
                return (), True, None
            if not isinstance(rollup, list):
                raise ValueError("GitHub check rollup is malformed")
            checks = _normalize_checks(rollup[:_MAX_CHECK_ROWS])
            checks, buckets_complete = _bounded_checks(checks)
            complete = len(rollup) <= _MAX_CHECK_ROWS and buckets_complete
            return checks, complete, None
        except (KeyError, TypeError, ValueError):
            return (), False, ProviderErrorKind.TRANSIENT

    def _review_threads(
        self,
        gh: str,
        target: GitHubPullRequestTarget,
    ) -> tuple[int, bool, ProviderErrorKind | None]:
        unresolved = 0
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for page in range(_REVIEW_THREAD_MAX_PAGES):
            argv = [
                gh,
                "api",
                "graphql",
                "-f",
                f"query={_REVIEW_THREADS_QUERY}",
                "-f",
                f"owner={target.owner}",
                "-f",
                f"repo={target.repo}",
                "-F",
                f"number={target.number}",
            ]
            if cursor is not None:
                argv.extend(("-f", f"cursor={cursor}"))
            try:
                proc = self._runner(
                    argv,
                    timeout=_PROBE_TIMEOUT_SECS,
                    audit_caller="core:monitor",
                    pin_host=_GITHUB_HOST,
                )
            except (SetupError, FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
                return unresolved, False, _provider_exception_kind(exc)
            failure = _process_failure(proc)
            failure_kind = failure.observation.provider_error if failure is not None else None
            try:
                raw = _json_object(proc.stdout)
            except ValueError:
                if failure_kind is not None:
                    return unresolved, False, failure_kind
                raise
            has_errors = bool(raw.get("errors"))
            try:
                threads = raw["data"]["repository"]["pullRequest"]["reviewThreads"]
                nodes = threads["nodes"]
                page_info = threads["pageInfo"]
            except (KeyError, TypeError):
                return unresolved, False, failure_kind or ProviderErrorKind.TRANSIENT
            if not isinstance(nodes, list) or not isinstance(page_info, Mapping):
                return unresolved, False, failure_kind or ProviderErrorKind.TRANSIENT
            nodes_complete = True
            for node in nodes:
                if (
                    not isinstance(node, Mapping)
                    or not isinstance(node.get("isResolved"), bool)
                    or not isinstance(node.get("isOutdated"), bool)
                ):
                    nodes_complete = False
                    continue
                unresolved += int(not node["isResolved"] and not node["isOutdated"])
            if failure_kind is not None or has_errors or not nodes_complete:
                return unresolved, False, failure_kind or ProviderErrorKind.TRANSIENT
            has_next = page_info.get("hasNextPage")
            if has_next is False:
                return unresolved, True, None
            if has_next is not True:
                return unresolved, False, ProviderErrorKind.TRANSIENT
            next_cursor = page_info.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return unresolved, False, ProviderErrorKind.TRANSIENT
            if next_cursor == cursor or next_cursor in seen_cursors:
                return unresolved, False, ProviderErrorKind.TRANSIENT
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            if page + 1 == _REVIEW_THREAD_MAX_PAGES:
                return unresolved, False, None
        return unresolved, False, None


def parse_github_pull_request_target(raw: str) -> GitHubPullRequestTarget:
    """Parse one exact public GitHub pull-request URL into a typed identity."""
    if not isinstance(raw, str) or not raw or _RAW_URL_CONTROL_RE.search(raw):
        raise ValueError("target must be a GitHub pull request URL")
    parsed = urlparse(raw)
    try:
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise ValueError("target must be a public GitHub pull request URL") from exc
    if (
        parsed.scheme != "https"
        or host not in {_GITHUB_HOST, f"www.{_GITHUB_HOST}"}
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("target must be a public GitHub pull request URL")
    parts = PurePosixPath(parsed.path).parts
    if len(parts) != 5 or parts[0] != "/" or parts[3] != "pull":
        raise ValueError("target must be a GitHub pull request URL")
    owner, repo, raw_number = parts[1], parts[2], parts[4]
    if parsed.path != f"/{owner}/{repo}/pull/{raw_number}":
        raise ValueError("target must be a canonical GitHub pull request URL")
    if (
        not raw_number.isascii()
        or not raw_number.isdecimal()
        or raw_number.startswith("0")
        or int(raw_number, 10) > _MAX_PULL_REQUEST_NUMBER
    ):
        raise ValueError("target must be a GitHub pull request with a positive number")
    try:
        return GitHubPullRequestTarget(_GITHUB_HOST, owner, repo, int(raw_number, 10))
    except ValueError as exc:
        raise ValueError("target must be a valid GitHub pull request") from exc


def _json_object(raw: str | None) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ValueError("GitHub response is malformed")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("GitHub response is malformed") from exc
    if not isinstance(payload, dict):
        raise ValueError("GitHub response is malformed")
    return payload


def _normalize_response(
    target: GitHubPullRequestTarget,
    raw: Mapping[str, Any],
    checks: tuple[GitHubCheck, ...],
    checks_complete: bool,
    unresolved_review_threads: int,
    review_threads_complete: bool,
) -> GitHubPullRequestResponse:
    required = {
        "number",
        "state",
        "isDraft",
        "headRefOid",
        "mergeable",
        "mergeStateStatus",
        "reviewDecision",
    }
    if not required.issubset(raw):
        raise ValueError("GitHub pull request response is malformed")
    number = raw["number"]
    if (
        isinstance(number, bool)
        or not isinstance(number, int)
        or number != target.number
        or not isinstance(raw["isDraft"], bool)
    ):
        raise ValueError("GitHub pull request response is malformed")
    for name in ("state", "mergeable", "mergeStateStatus"):
        if not isinstance(raw[name], str):
            raise ValueError("GitHub pull request response is malformed")
    head_revision = raw["headRefOid"]
    if not isinstance(head_revision, str) or (
        head_revision and _HEAD_REVISION_RE.fullmatch(head_revision) is None
    ):
        raise ValueError("GitHub pull request response is malformed")
    review_decision = raw["reviewDecision"]
    if review_decision is not None and not isinstance(review_decision, str):
        raise ValueError("GitHub pull request response is malformed")
    return GitHubPullRequestResponse(
        target=target,
        state=_normalize_pr_state(raw["state"]),
        draft=raw["isDraft"],
        head_revision=head_revision,
        mergeability=_normalize_mergeability(raw["mergeable"], raw["mergeStateStatus"]),
        review_decision=_normalize_review_decision(review_decision),
        checks=checks,
        checks_complete=checks_complete,
        unresolved_review_threads=unresolved_review_threads,
        review_threads_complete=review_threads_complete,
    )


def _normalize_checks(raw: object) -> tuple[GitHubCheck, ...]:
    if not isinstance(raw, list):
        raise ValueError("GitHub check rollup is malformed")
    grouped: dict[tuple[str, ...], tuple[str, list[str]]] = {}
    for row_index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValueError("GitHub check rollup is malformed")
        identity, state, group_key = _normalize_check(item)
        if group_key is None:
            group_key = ("independent_check_run", str(row_index))
        _, candidates = grouped.setdefault(group_key, (identity, []))
        candidates.append(state)
    normalized: list[GitHubCheck] = []
    for identity, candidates in grouped.values():
        state = min(candidates, key=("failed", "pending", "unknown", "passed").index)
        try:
            check = GitHubCheck(_sanitize_check_identity(identity), state)
        except ValueError:
            check = GitHubCheck(
                opaque_provider_check_identity("github_check", identity),
                "unknown",
            )
        normalized.append(check)
    return tuple(sorted(normalized, key=lambda item: item.identity))


def _bounded_checks(checks: tuple[GitHubCheck, ...]) -> tuple[tuple[GitHubCheck, ...], bool]:
    """Bound each durable state bucket without letting one state consume the others."""
    bounded: list[GitHubCheck] = []
    complete = True
    for state in ("failed", "passed", "pending", "unknown"):
        matching = [check for check in checks if check.state == state]
        if len(matching) > MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET:
            complete = False
        bounded.extend(matching[:MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET])
    return tuple(sorted(bounded, key=lambda item: (item.identity, item.state))), complete


def _normalize_check(
    raw: Mapping[str, object],
) -> tuple[str, str, tuple[str, ...] | None]:
    typename = raw.get("__typename")
    if typename == "CheckRun":
        name = raw.get("name")
        workflow = raw.get("workflowName")
        if not isinstance(name, str) or not name:
            raise ValueError("GitHub check rollup is malformed")
        identity = f"{workflow} / {name}" if isinstance(workflow, str) and workflow else name
        status = raw.get("status")
        conclusion = raw.get("conclusion")
        if status != "COMPLETED":
            state = "pending" if isinstance(status, str) else "unknown"
        elif conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED", "STALE"}:
            state = "passed"
        elif conclusion in {
            "FAILURE",
            "CANCELLED",
            "TIMED_OUT",
            "ACTION_REQUIRED",
            "STARTUP_FAILURE",
        }:
            state = "failed"
        else:
            state = "unknown"
        return identity, state, None
    if typename == "StatusContext":
        context = raw.get("context")
        if not isinstance(context, str) or not context:
            raise ValueError("GitHub check rollup is malformed")
        raw_state = raw.get("state")
        if isinstance(raw_state, str):
            state = {
                "SUCCESS": "passed",
                "PENDING": "pending",
                "EXPECTED": "pending",
                "FAILURE": "failed",
                "ERROR": "failed",
            }.get(raw_state, "unknown")
        else:
            state = "unknown"
        return context, state, ("status_context", context)
    raise ValueError("GitHub check rollup is malformed")


def _normalize_pr_state(raw: str) -> str:
    return {"OPEN": "open", "CLOSED": "closed", "MERGED": "merged"}.get(raw.upper(), "unknown")


def _sanitize_check_identity(identity: str) -> str:
    normalized = "".join(
        (
            " "
            if unicodedata.category(character).startswith("C")
            or unicodedata.category(character) in {"Zl", "Zp"}
            else character
        )
        for character in identity
    ).strip()
    redacted = redact(_URL_IN_CHECK_IDENTITY_RE.sub("[provider-url]", normalized)).strip()
    if not redacted:
        raise ValueError("GitHub check identity is empty after sanitization")
    if len(redacted) <= MAX_MONITOR_CHECK_IDENTITY_CHARS:
        return redacted
    digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()[:16]
    prefix_length = MAX_MONITOR_CHECK_IDENTITY_CHARS - len(digest) - 1
    return f"{redacted[:prefix_length]}#{digest}"


def _normalize_review_decision(raw: str | None) -> str:
    if raw is None or raw == "":
        return "none"
    return {
        "APPROVED": "approved",
        "CHANGES_REQUESTED": "changes_requested",
        "REVIEW_REQUIRED": "review_required",
    }.get(raw.upper(), "unknown")


def _normalize_mergeability(mergeable: str, merge_state: str) -> str:
    normalized_mergeable = mergeable.upper()
    normalized_state = merge_state.upper()
    if normalized_mergeable == "CONFLICTING" or normalized_state == "DIRTY":
        return "conflicting"
    if normalized_state == "BEHIND":
        return "behind"
    if normalized_state == "BLOCKED":
        return "blocked"
    if normalized_mergeable != "MERGEABLE" or normalized_state not in _MERGEABLE_SETTLED_STATES:
        return "pending"
    return "mergeable"


def _process_failure(
    proc: subprocess.CompletedProcess[str],
) -> GitHubPullRequestProbeResult | None:
    if proc.returncode == 0:
        return None
    kind = _classify_cli_error(proc.stderr if isinstance(proc.stderr, str) else "")
    reasons = {
        ProviderErrorKind.RATE_LIMITED: "provider_rate_limited",
        ProviderErrorKind.AUTHENTICATION: "provider_authentication",
        ProviderErrorKind.AUTHORIZATION: "provider_authorization",
        ProviderErrorKind.NOT_FOUND: "provider_not_found",
        ProviderErrorKind.TRANSIENT: "provider_transient",
    }
    return _provider_error(kind, reasons[kind])


def _classify_cli_error(raw: str) -> ProviderErrorKind:
    lowered = raw.lower()
    if any(marker in lowered for marker in ("rate limit", "abuse detection", "too many requests")):
        return ProviderErrorKind.RATE_LIMITED
    status_match = _HTTP_STATUS_RE.search(raw)
    if status_match is not None:
        status = int(status_match.group(1), 10)
        if status == 429:
            return ProviderErrorKind.RATE_LIMITED
        if status == 401:
            return ProviderErrorKind.AUTHENTICATION
        if status == 403:
            return ProviderErrorKind.AUTHORIZATION
        if status == 404:
            return ProviderErrorKind.NOT_FOUND
        if status >= 500:
            return ProviderErrorKind.TRANSIENT
    if "could not resolve host" in lowered:
        return ProviderErrorKind.TRANSIENT
    if any(
        marker in lowered
        for marker in ("http 429", "rate limit", "abuse detection", "too many requests")
    ):
        return ProviderErrorKind.RATE_LIMITED
    if any(
        marker in lowered
        for marker in (
            "bad credentials",
            "authentication",
            "not logged into",
            "gh auth login",
        )
    ):
        return ProviderErrorKind.AUTHENTICATION
    if any(
        marker in lowered
        for marker in (
            "not found",
            "could not resolve to a repository",
            "could not resolve to a pullrequest",
        )
    ):
        return ProviderErrorKind.NOT_FOUND
    if any(
        marker in lowered
        for marker in (
            "forbidden",
            "permission",
            "resource not accessible",
            "saml",
        )
    ):
        return ProviderErrorKind.AUTHORIZATION
    return ProviderErrorKind.TRANSIENT


def _combine_provider_errors(
    first: ProviderErrorKind | None,
    second: ProviderErrorKind | None,
) -> ProviderErrorKind | None:
    """Keep the most specific supplemental failure when both evidence calls fail."""
    priority = {
        ProviderErrorKind.AUTHENTICATION: 0,
        ProviderErrorKind.AUTHORIZATION: 1,
        ProviderErrorKind.NOT_FOUND: 2,
        ProviderErrorKind.RATE_LIMITED: 3,
        ProviderErrorKind.TRANSIENT: 4,
        ProviderErrorKind.SETUP: 5,
    }
    present = [error for error in (first, second) if error is not None]
    return min(present, key=priority.__getitem__) if present else None


def _transient_os_error(error: BaseException | None) -> bool:
    """Classify bounded host-pressure and connection failures as retryable."""
    return isinstance(error, OSError) and error.errno in {
        errno.EAGAIN,
        errno.EMFILE,
        errno.ENFILE,
        errno.ENOMEM,
        errno.ECONNRESET,
        errno.ETIMEDOUT,
    }


def _provider_exception_kind(error: BaseException) -> ProviderErrorKind:
    """Map runner exceptions without letting supplemental reads erase primary facts."""
    if isinstance(error, subprocess.TimeoutExpired):
        return ProviderErrorKind.TRANSIENT
    if isinstance(error, FileNotFoundError):
        return ProviderErrorKind.SETUP
    if isinstance(error, SetupError):
        return (
            ProviderErrorKind.TRANSIENT
            if _transient_os_error(error.__cause__)
            else ProviderErrorKind.SETUP
        )
    if isinstance(error, OSError) and _transient_os_error(error):
        return ProviderErrorKind.TRANSIENT
    return ProviderErrorKind.SETUP


def _provider_error(kind: ProviderErrorKind, reason_code: str) -> GitHubPullRequestProbeResult:
    return provider_error_result(kind, reason_code)
