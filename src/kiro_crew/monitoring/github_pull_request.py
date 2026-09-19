"""Typed public-GitHub pull-request observations for structured monitors."""

from __future__ import annotations

import errno
import hashlib
import json
import re
import subprocess
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from kiro_crew.github_runner import SetupError, resolve_gh, run_gh
from kiro_crew.monitoring.github_provider_errors import (
    REASON_SHARED_COOLDOWN,
    classify_cli_error,
    shared_cooldown,
    shared_cooldown_summary,
)
from kiro_crew.monitoring.models import (
    MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET,
    MAX_MONITOR_CHECK_IDENTITY_CHARS,
    MonitorObservation,
    MonitorObservationStatus,
    ProviderErrorKind,
)
from kiro_crew.monitoring.provider_cli import audit_provider_cli_denied
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
_MAX_PULL_REQUEST_NUMBER = 2_147_483_647
# GitHub bounds one connection page at 100 nodes, so the row budget this adapter
# has always enforced is spent as pages rather than as one oversized request.
_ROLLUP_PAGE_SIZE = 100
_ROLLUP_MAX_PAGES = 4
_MAX_CHECK_ROWS = MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET * 4
# One document holds many subjects, and a document that grows without a bound is
# a request that times out rather than a batch. A call with more subjects than
# this is spent as consecutive queries, the same way a paginated read is.
_MAX_SUBJECTS_PER_QUERY = 25
# Aliases and variable names are built from a subject's INDEX in the batch, never
# from any part of the subject, so no owner, repository, number or cursor is ever
# interpolated into a query document. Every one of those travels as a typed
# GraphQL variable instead.
_ALIAS_RE = re.compile(r"^s(\d+)$")
_PR_CORE_SELECTION = "number state isDraft headRefOid mergeable mergeStateStatus reviewDecision"
_ROLLUP_SELECTION = """
headRefOid
commits(last:1){nodes{commit{oid statusCheckRollup{contexts(first:PAGE_SIZE,after:$CURSOR){
  totalCount pageInfo{hasNextPage endCursor}
  nodes{
    __typename
    ... on CheckRun{name status conclusion checkSuite{workflowRun{workflow{name}}}}
    ... on StatusContext{context state}
  }
}}}}}
""".replace("PAGE_SIZE", str(_ROLLUP_PAGE_SIZE)).strip()
_REVIEW_THREADS_SELECTION = """
reviewThreads(first:PAGE_SIZE,after:$CURSOR){
  pageInfo{hasNextPage endCursor}
  nodes{isResolved isOutdated}
}
""".replace("PAGE_SIZE", str(_REVIEW_THREAD_PAGE_SIZE)).strip()
# GitHub meters GraphQL in points and REST in requests, and the two budgets are
# SEPARATE. An exhausted point budget therefore refuses every read above while
# these two paths keep answering, which is the whole reason the fallback exists.
#
# Both are built only from a validated target: `GitHubPullRequestTarget` admits
# an owner and repository matching `_SEGMENT_RE` and a positive integer number,
# and a revision reaching the second path has already passed
# `_HEAD_REVISION_RE`. So no provider-controlled text is interpolated into a
# request path.
_REST_PULL_REQUEST_PATH = "repos/{owner}/{repo}/pulls/{number}"
_REST_COMMIT_STATUS_PATH = "repos/{owner}/{repo}/commits/{revision}/status?per_page={page_size}"
_REST_STATUS_PAGE_SIZE = 100
#: GraphQL's ``mergeable`` enum, keyed by the REST boolean carrying the same fact.
_REST_MERGEABLE = {True: "MERGEABLE", False: "CONFLICTING"}
#: Reported for the one primary fact REST does not carry at all.
#: ``_normalize_review_decision`` maps it to ``"unknown"``, which
#: ``classify_pull_request_facts`` answers with PENDING -- so a REST-sourced
#: observation can report a FAILING board but never a ready one. This is the
#: fail-closed half of the fallback, and the test suite asserts it directly
#: rather than leaving it to follow from the mapping.
_REST_ABSENT_REVIEW_DECISION = "UNKNOWN"
# GraphQL error types that name a cause this adapter's taxonomy already has. An
# unlisted or absent type is not guessed at: it falls through to the message
# classifier and then to TRANSIENT, so an unclassified failure still leaves this
# layer classified.
_GRAPHQL_ERROR_KINDS = {
    "NOT_FOUND": ProviderErrorKind.NOT_FOUND,
    "FORBIDDEN": ProviderErrorKind.AUTHORIZATION,
    "INSUFFICIENT_SCOPES": ProviderErrorKind.AUTHORIZATION,
    "UNAUTHORIZED": ProviderErrorKind.AUTHENTICATION,
    "RATE_LIMITED": ProviderErrorKind.RATE_LIMITED,
    "SERVICE_UNAVAILABLE": ProviderErrorKind.TRANSIENT,
    "INTERNAL": ProviderErrorKind.TRANSIENT,
}
_PROVIDER_ERROR_REASONS = {
    ProviderErrorKind.RATE_LIMITED: "provider_rate_limited",
    ProviderErrorKind.AUTHENTICATION: "provider_authentication",
    ProviderErrorKind.AUTHORIZATION: "provider_authorization",
    ProviderErrorKind.NOT_FOUND: "provider_not_found",
    ProviderErrorKind.TRANSIENT: "provider_transient",
    ProviderErrorKind.SETUP: "provider_setup",
}
# How specific each failure is, lowest first. One subject can be named by several
# reported errors and two supplemental reads can fail differently, so both
# reductions rank them here rather than each keeping its own table.
_PROVIDER_ERROR_PRIORITY = {
    ProviderErrorKind.AUTHENTICATION: 0,
    ProviderErrorKind.AUTHORIZATION: 1,
    ProviderErrorKind.NOT_FOUND: 2,
    ProviderErrorKind.RATE_LIMITED: 3,
    ProviderErrorKind.TRANSIENT: 4,
    ProviderErrorKind.SETUP: 5,
}

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

# A classified failure and the reason code it is reported under. The pair travels
# together because two failures of the same kind can still name different reasons
# -- a malformed response and a transport fault are both retryable -- and a caller
# reading only the kind would report them identically.
_Failure = tuple[ProviderErrorKind, str]


@dataclass(frozen=True)
class _BatchSubject:
    """One subject as the caller spelled it, beside its validated identity."""

    raw: str
    target: GitHubPullRequestTarget


def _chunked(
    members: Sequence[_BatchSubject],
    size: int,
) -> list[Sequence[_BatchSubject]]:
    """Split the subjects into documents small enough to answer inside the timeout."""
    return [members[start : start + size] for start in range(0, len(members), size)]


def _shared_host(members: Sequence[_BatchSubject]) -> str | None:
    """The one host this chunk may be pinned to, or ``None`` if it names two.

    One query carries one credential, so a query spanning two hosts would read the
    second host's subjects with the first host's token. That is what may not
    happen, and this is the check that makes it true rather than assumed: the
    caller refuses the whole query when it does not hold, because the query is the
    unit that carries the identity.

    Nothing can reach the ``None`` branch today -- ``parse_github_pull_request_target``
    accepts only ``github.com`` -- which is why the check exists instead of a
    grouping pass keyed on the host. A pass that sorts subjects into per-host
    queries would be machinery for a case no test can construct; a check is
    exercised by construction and fails closed the day the target gate admits a
    second host.
    """
    hosts = {member.target.host for member in members}
    if len(hosts) != 1:
        return None
    return hosts.pop()


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
        subjects: Sequence[str],
        *,
        previous_observations: Mapping[str, Mapping[str, object]] | None = None,
        use_owner_credentials: bool = True,
    ) -> Mapping[str, GitHubPullRequestProbeResult]:
        """Return one canonical review-ready observation per subject.

        GitHub's GraphQL API answers for many pull requests in one document, so
        this BATCHES rather than looping: the subjects of one call share each
        read, and each read costs one request per chunk of at most
        ``_MAX_SUBJECTS_PER_QUERY`` subjects instead of one per subject. A tick of
        any size up to that bound is three requests, and each further chunk adds
        three. Fifty subjects were roughly one hundred and fifty ``gh``
        invocations and are now six, plus one document for each further page a
        subject's rollup or thread list advertises.

        One query carries one identity, and that is checked rather than assumed.
        The credential is this call's argument, so it cannot differ between two of
        its subjects; the host is per subject, so every chunk is checked to name
        one host and the query is refused if it names two, which is what keeps a
        document from reading one host's subjects with another host's token.

        The three reads stay separate because their failures are separate. The
        load-bearing primary read selects no check rollup, so a missing Checks
        permission cannot erase authorized lifecycle facts; each supplemental
        read carries the head revision, so a push mid-tick becomes typed
        incomplete evidence rather than another commit's checks; and a merged or
        closed primary state issues neither supplemental request.

        A subject that cannot be read degrades ALONE. GitHub answers a partial
        failure with the readable subjects populated under ``data`` and the rest
        named in ``errors[].path``, and ``gh`` exits non-zero while still writing
        that ``data`` -- so the exit code is not what decides, and one unreadable
        subject costs the other subjects nothing.

        Keyed by the subject string as passed, so a caller can always look up
        what it asked for.
        """
        previous = previous_observations or {}
        results: dict[str, GitHubPullRequestProbeResult] = {}
        members: list[_BatchSubject] = []
        seen: set[str] = set()
        for raw_target in subjects:
            if raw_target in seen:
                continue
            seen.add(raw_target)
            try:
                target = parse_github_pull_request_target(raw_target)
            except (TypeError, ValueError):
                results[raw_target] = _provider_error(
                    ProviderErrorKind.TRANSIENT,
                    "provider_malformed_response",
                )
                continue
            members.append(_BatchSubject(raw_target, target))
        if not members:
            return results
        if not use_owner_credentials:
            # One refusal per call, not per subject: the queries that were not
            # allowed to run are what the audit records, and this call is those
            # queries. The credential arrives here, so it cannot differ between
            # two subjects of one call the way a host could.
            audit_provider_cli_denied("gh")
            results.update(
                _group_error(members, _classified_failure(ProviderErrorKind.AUTHORIZATION)),
            )
            return results
        cooldown = _shared_cooldown(time.time())
        if cooldown is not None:
            # One cooldown per CALL, charged to every subject: the shared
            # `github:api` scope is a property of the host's rate limit, not of a
            # subject, and none of these queries ran.
            results.update({member.raw: _shared_cooldown_result(cooldown) for member in members})
            return results
        try:
            gh = self._resolver()
        except (SetupError, FileNotFoundError, OSError) as exc:
            results.update(_group_error(members, _exception_failure(exc)))
            return results
        for chunk in _chunked(members, _MAX_SUBJECTS_PER_QUERY):
            host = _shared_host(chunk)
            if host is None:
                # Refuse the query rather than pin it to one of two hosts, which
                # would read the other host's subjects with this host's token.
                results.update(_group_error(chunk, _classified_failure(ProviderErrorKind.SETUP)))
                continue
            results.update(self._probe_batch(gh, host, chunk, previous))
        return results

    def _probe_batch(
        self,
        gh: str,
        host: str,
        members: Sequence[_BatchSubject],
        previous: Mapping[str, Mapping[str, object]],
    ) -> dict[str, GitHubPullRequestProbeResult]:
        """Read one chunk of subjects with one request per evidence kind."""
        results: dict[str, GitHubPullRequestProbeResult] = {}
        facts, primary_errors = self._primary(gh, host, members)
        degraded = self._degrade_primary_to_rest(gh, host, members, facts, primary_errors)
        for raw_target, (kind, reason) in primary_errors.items():
            results[raw_target] = _provider_error(kind, reason)
        live: list[_BatchSubject] = []
        for member in members:
            response = facts.get(member.raw)
            if response is None:
                continue
            if response.state in {"merged", "closed"}:
                results[member.raw] = _build_result(response, previous.get(member.raw), None)
                continue
            live.append(member)
        heads = {member.raw: facts[member.raw].head_revision for member in live}
        # A subject whose primary facts came from REST is one the GraphQL bucket
        # has already refused, so its supplemental documents are not spent: the
        # same budget would refuse them in the same tick.
        graphql_live = [member for member in live if member.raw not in degraded]
        checks = self._checks(gh, host, graphql_live, heads)
        threads = self._review_threads(gh, host, graphql_live)
        rest_checks = self._checks_rest(
            gh,
            host,
            [
                member
                for member in live
                if member.raw in degraded or checks[member.raw][2] is ProviderErrorKind.RATE_LIMITED
            ],
            heads,
        )
        for member in live:
            if member.raw in rest_checks:
                check_rows, checks_complete, checks_error = rest_checks[member.raw]
            else:
                check_rows, checks_complete, checks_error = checks[member.raw]
            unresolved, threads_complete, threads_error = (
                (0, False, None) if member.raw in degraded else threads[member.raw]
            )
            if threads_error is ProviderErrorKind.RATE_LIMITED:
                # Thread resolution is the ONE signal REST cannot express, so a
                # refused thread read is reported as an incomplete COUNT rather
                # than as a provider error. Incompleteness already holds the
                # subject at PENDING, and it does not spend the retirement budget
                # that an exhausted point bucket would otherwise drain to zero.
                threads_error = None
            response = replace(
                facts[member.raw],
                checks=check_rows,
                checks_complete=checks_complete,
                unresolved_review_threads=unresolved,
                review_threads_complete=threads_complete,
            )
            results[member.raw] = _build_result(
                response,
                previous.get(member.raw),
                _combine_provider_errors(checks_error, threads_error),
            )
        return results

    def _degrade_primary_to_rest(
        self,
        gh: str,
        host: str,
        members: Sequence[_BatchSubject],
        facts: dict[str, GitHubPullRequestResponse],
        primary_errors: dict[str, _Failure],
    ) -> set[str]:
        """Re-read the rate-limited subjects on REST and name the ones that recovered.

        A watch reading only GraphQL retires on ``max_provider_errors`` whenever
        the account's point budget is spent, with the REST bucket untouched and
        answering. This is the one place that asymmetry is spent.

        ONLY a rate limit is retried. Authentication, authorization, not-found and
        transient failures each say something about the credential or the subject
        that a second transport would answer identically, so they are charged
        exactly as before.

        A subject the fallback cannot read either KEEPS the rate limit it was
        already charged. The REST failure is a second diagnosis of a subject
        already known to be refused, and letting it replace the first could
        substitute a terminal kind for a retryable one -- a REST ``404`` would
        retire a watch that the GraphQL-only code would have retried. Bounding it
        this way is what makes the fallback never worse than no fallback.

        ``facts`` and ``primary_errors`` are corrected in place, because this is
        the same answer they already hold rather than a third one beside it.
        """
        retry = [
            member
            for member in members
            if primary_errors.get(member.raw, (None, ""))[0] is ProviderErrorKind.RATE_LIMITED
        ]
        if not retry:
            return set()
        recovered = self._primary_rest(gh, host, retry)
        for raw_target, response in recovered.items():
            facts[raw_target] = response
            primary_errors.pop(raw_target, None)
        return set(recovered)

    def _primary_rest(
        self,
        gh: str,
        host: str,
        members: Sequence[_BatchSubject],
    ) -> dict[str, GitHubPullRequestResponse]:
        """Re-read the load-bearing facts on the REST bucket, one request per subject.

        REST has no batching, so a chunk costs one request per subject instead of
        one per chunk. The chunk is already bounded by ``_MAX_SUBJECTS_PER_QUERY``
        and this runs only while the cheaper transport is refusing, so the bound is
        the one the batched path already carries.
        """
        facts: dict[str, GitHubPullRequestResponse] = {}
        for member in members:
            target = member.target
            payload, _ = self._rest(
                gh,
                host,
                _REST_PULL_REQUEST_PATH.format(
                    owner=target.owner,
                    repo=target.repo,
                    number=target.number,
                ),
            )
            if payload is None:
                continue
            try:
                facts[member.raw] = _normalize_response(
                    target,
                    _rest_primary_node(payload),
                    checks=(),
                    checks_complete=False,
                    unresolved_review_threads=0,
                    review_threads_complete=False,
                )
            except (KeyError, TypeError, ValueError):
                continue
        return facts

    def _checks_rest(
        self,
        gh: str,
        host: str,
        members: Sequence[_BatchSubject],
        expected_heads: Mapping[str, str],
    ) -> dict[str, tuple[tuple[GitHubCheck, ...], bool, ProviderErrorKind | None]]:
        """Read the REST-expressible half of each subject's board, one request each.

        Commit statuses ONLY, and that is a measured bound rather than a choice.
        REST names no workflow for a check run -- neither the run's own payload nor
        its check suite carries one -- while the GraphQL rollup builds a check
        run's identity as ``"<workflow> / <name>"``. A check run read here would
        therefore carry a DIFFERENT identity for the same check, and the watch
        would report one failure twice, once per transport. A commit status
        carries ``context``, which IS its GraphQL identity, so the status half is
        exactly the half that can be read without that drift.

        The missing half is why every result here is incomplete. Incomplete
        already means PENDING unless a check failed, so the degraded board still
        wakes the session on a red status and still cannot call the subject
        review-ready.

        The read is PINNED to the revision the primary read reported, so the
        mid-tick push that the GraphQL path detects by comparing heads cannot
        arise: a status page for another commit is not reachable from here.
        """
        resolved: dict[str, tuple[tuple[GitHubCheck, ...], bool, ProviderErrorKind | None]] = {}
        for member in members:
            revision = expected_heads.get(member.raw, "")
            if not revision:
                # No revision to pin the read to. The primary facts stand and the
                # board is reported unread, which classifies as PENDING.
                resolved[member.raw] = ((), False, None)
                continue
            target = member.target
            payload, failure = self._rest(
                gh,
                host,
                _REST_COMMIT_STATUS_PATH.format(
                    owner=target.owner,
                    repo=target.repo,
                    revision=revision,
                    page_size=_REST_STATUS_PAGE_SIZE,
                ),
            )
            if payload is None:
                # Both buckets refused this subject, so there is no third
                # transport to degrade to and the failure is charged as a failure.
                resolved[member.raw] = (
                    (),
                    False,
                    failure[0] if failure else ProviderErrorKind.TRANSIENT,
                )
                continue
            try:
                normalized = _normalize_checks(_rest_status_rows(payload))
            except (KeyError, TypeError, ValueError):
                resolved[member.raw] = ((), False, ProviderErrorKind.TRANSIENT)
                continue
            bounded, _ = _bounded_checks(normalized)
            resolved[member.raw] = (bounded, False, None)
        return resolved

    def _rest(
        self,
        gh: str,
        host: str,
        path: str,
    ) -> tuple[Mapping[str, Any] | None, _Failure | None]:
        """Run one REST read on the bucket the GraphQL point budget does not share.

        One request answers for one subject, so unlike :meth:`_graphql` there is no
        partially-readable answer to preserve: a non-zero exit is about this
        request and nothing else. The exit code is therefore read FIRST -- the
        error body GitHub writes to stdout is itself a valid JSON object, so a
        parse-led order would read a refusal as a response.
        """
        try:
            proc = self._runner(
                [gh, "api", path],
                timeout=_PROBE_TIMEOUT_SECS,
                audit_caller="core:monitor",
                pin_host=host,
            )
        except (SetupError, FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            return None, _exception_failure(exc)
        if proc.returncode != 0:
            stderr = proc.stderr if isinstance(proc.stderr, str) else ""
            return None, _classified_failure(_classify_cli_error(stderr))
        try:
            return _json_object(proc.stdout), None
        except ValueError:
            return None, (ProviderErrorKind.TRANSIENT, "provider_malformed_response")

    def _primary(
        self,
        gh: str,
        host: str,
        members: Sequence[_BatchSubject],
    ) -> tuple[dict[str, GitHubPullRequestResponse], dict[str, _Failure]]:
        """Read every subject's load-bearing facts in one document."""
        if not members:
            return {}, {}
        document, argv_tail = _batch_document(members, _PR_CORE_SELECTION)
        payload, group_failure = self._graphql(gh, host, document, argv_tail)
        if payload is None:
            failure = group_failure or _classified_failure(ProviderErrorKind.TRANSIENT)
            return {}, dict.fromkeys((member.raw for member in members), failure)
        facts: dict[str, GitHubPullRequestResponse] = {}
        reported = _subject_errors(payload, members)
        failures: dict[str, _Failure] = {
            raw: _classified_failure(kind) for raw, kind in reported.items()
        }
        for index, member in enumerate(members):
            if member.raw in failures:
                continue
            node = _alias_pull_request(payload, index)
            if node is None:
                # A null pull request with no error naming it is GitHub saying the
                # subject is not there, which is the same answer as NOT_FOUND.
                failures[member.raw] = _classified_failure(ProviderErrorKind.NOT_FOUND)
                continue
            try:
                facts[member.raw] = _normalize_response(
                    member.target,
                    node,
                    checks=(),
                    checks_complete=True,
                    unresolved_review_threads=0,
                    review_threads_complete=True,
                )
            except (KeyError, TypeError, ValueError):
                failures[member.raw] = (
                    ProviderErrorKind.TRANSIENT,
                    "provider_malformed_response",
                )
        return facts, failures

    def _checks(
        self,
        gh: str,
        host: str,
        members: Sequence[_BatchSubject],
        expected_heads: Mapping[str, str],
    ) -> dict[str, tuple[tuple[GitHubCheck, ...], bool, ProviderErrorKind | None]]:
        """Read every subject's check rollup, bounded in pages rather than rows."""
        rows: dict[str, list[Mapping[str, Any]]] = {member.raw: [] for member in members}
        complete: dict[str, bool] = dict.fromkeys((m.raw for m in members), True)
        errors: dict[str, ProviderErrorKind | None] = dict.fromkeys((m.raw for m in members), None)
        pending = list(members)
        cursors: dict[str, str] = {}
        seen_cursors: dict[str, set[str]] = {member.raw: set() for member in members}
        for _ in range(_ROLLUP_MAX_PAGES):
            if not pending:
                break
            document, argv_tail = _batch_document(pending, _ROLLUP_SELECTION, cursors=cursors)
            payload, group_failure = self._graphql(gh, host, document, argv_tail)
            if payload is None:
                for member in pending:
                    errors[member.raw] = (
                        group_failure[0] if group_failure else ProviderErrorKind.TRANSIENT
                    )
                    complete[member.raw] = False
                break
            round_errors = _subject_errors(payload, pending)
            advancing: list[_BatchSubject] = []
            for index, member in enumerate(pending):
                if member.raw in round_errors:
                    errors[member.raw] = round_errors[member.raw]
                    complete[member.raw] = False
                    continue
                node = _alias_pull_request(payload, index)
                if node is None:
                    errors[member.raw] = ProviderErrorKind.NOT_FOUND
                    complete[member.raw] = False
                    continue
                try:
                    page, commit_revision, total, has_next, cursor = _rollup_page(node)
                except (KeyError, TypeError, ValueError):
                    errors[member.raw] = ProviderErrorKind.TRANSIENT
                    complete[member.raw] = False
                    continue
                # The head is re-read beside the rollup, and the rollup's own
                # commit is read too, so a push that lands mid-tick is typed
                # incomplete evidence instead of another commit's checks. Both
                # comparisons are the same guard: this page does not describe the
                # revision the primary read reported. The commit comparison needs
                # both values to say anything, so a subject whose head GitHub did
                # not report is judged on the head field alone.
                head = node.get("headRefOid")
                expected = expected_heads.get(member.raw, "")
                mismatched_commit = bool(
                    commit_revision and expected and commit_revision != expected
                )
                if not isinstance(head, str) or head != expected or mismatched_commit:
                    rows[member.raw] = []
                    errors[member.raw] = ProviderErrorKind.TRANSIENT
                    complete[member.raw] = False
                    continue
                rows[member.raw].extend(page)
                if total > _MAX_CHECK_ROWS:
                    complete[member.raw] = False
                if not has_next or cursor is None:
                    continue
                if cursor in seen_cursors[member.raw]:
                    errors[member.raw] = ProviderErrorKind.TRANSIENT
                    complete[member.raw] = False
                    continue
                seen_cursors[member.raw].add(cursor)
                cursors[member.raw] = cursor
                advancing.append(member)
            pending = advancing
        for member in pending:
            # The page cap was reached with more pages still advertised: the rows
            # so far are real but incomplete, and that is not a provider failure.
            complete[member.raw] = False
        resolved: dict[str, tuple[tuple[GitHubCheck, ...], bool, ProviderErrorKind | None]] = {}
        for member in members:
            error = errors[member.raw]
            if error is not None:
                resolved[member.raw] = ((), False, error)
                continue
            try:
                normalized = _normalize_checks(rows[member.raw][:_MAX_CHECK_ROWS])
            except (KeyError, TypeError, ValueError):
                resolved[member.raw] = ((), False, ProviderErrorKind.TRANSIENT)
                continue
            bounded, buckets_complete = _bounded_checks(normalized)
            resolved[member.raw] = (bounded, complete[member.raw] and buckets_complete, None)
        return resolved

    def _review_threads(
        self,
        gh: str,
        host: str,
        members: Sequence[_BatchSubject],
    ) -> dict[str, tuple[int, bool, ProviderErrorKind | None]]:
        """Count every subject's unresolved review threads in shared pages."""
        unresolved: dict[str, int] = dict.fromkeys((m.raw for m in members), 0)
        complete: dict[str, bool] = dict.fromkeys((m.raw for m in members), True)
        errors: dict[str, ProviderErrorKind | None] = dict.fromkeys((m.raw for m in members), None)
        pending = list(members)
        cursors: dict[str, str] = {}
        seen_cursors: dict[str, set[str]] = {member.raw: set() for member in members}
        for _ in range(_REVIEW_THREAD_MAX_PAGES):
            if not pending:
                break
            document, argv_tail = _batch_document(
                pending,
                _REVIEW_THREADS_SELECTION,
                cursors=cursors,
            )
            payload, group_failure = self._graphql(gh, host, document, argv_tail)
            if payload is None:
                for member in pending:
                    errors[member.raw] = (
                        group_failure[0] if group_failure else ProviderErrorKind.TRANSIENT
                    )
                    complete[member.raw] = False
                break
            round_errors = _subject_errors(payload, pending)
            advancing: list[_BatchSubject] = []
            for index, member in enumerate(pending):
                node = _alias_pull_request(payload, index)
                if node is None:
                    errors[member.raw] = round_errors.get(member.raw, ProviderErrorKind.NOT_FOUND)
                    complete[member.raw] = False
                    continue
                try:
                    counted, nodes_complete, has_next, cursor = _review_thread_page(node)
                except (KeyError, TypeError, ValueError):
                    errors[member.raw] = round_errors.get(member.raw, ProviderErrorKind.TRANSIENT)
                    complete[member.raw] = False
                    continue
                # A usable node still counts, whether its page also reported an
                # error or a malformed sibling. An unresolved thread GitHub did
                # return is a real blocker, and dropping the count because the page
                # was partial would report the subject as having none; only the
                # count's completeness is lost.
                unresolved[member.raw] += counted
                if member.raw in round_errors:
                    errors[member.raw] = round_errors[member.raw]
                    complete[member.raw] = False
                    continue
                if not nodes_complete:
                    errors[member.raw] = ProviderErrorKind.TRANSIENT
                    complete[member.raw] = False
                    continue
                if not has_next or cursor is None:
                    continue
                if cursor in seen_cursors[member.raw]:
                    errors[member.raw] = ProviderErrorKind.TRANSIENT
                    complete[member.raw] = False
                    continue
                seen_cursors[member.raw].add(cursor)
                cursors[member.raw] = cursor
                advancing.append(member)
            pending = advancing
        for member in pending:
            # The page cap was reached with more pages still advertised: the count
            # so far is real but incomplete, and that is not a provider failure.
            complete[member.raw] = False
        return {
            member.raw: (unresolved[member.raw], complete[member.raw], errors[member.raw])
            for member in members
        }

    def _graphql(
        self,
        gh: str,
        host: str,
        document: str,
        argv_tail: Sequence[str],
    ) -> tuple[Mapping[str, Any] | None, _Failure | None]:
        """Run one document, keeping a partially-readable answer usable.

        ``gh`` exits non-zero whenever the response carries ANY error, including
        one scoped to a single alias, so the exit code is read as evidence about
        that alias rather than about the request. It decides the outcome only when
        there is no payload to read instead -- which is what lets one unreadable
        subject leave its siblings' verdicts intact.
        """
        try:
            proc = self._runner(
                [gh, "api", "graphql", "-f", f"query={document}", *argv_tail],
                timeout=_PROBE_TIMEOUT_SECS,
                audit_caller="core:monitor",
                pin_host=host,
            )
        except (SetupError, FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            return None, _exception_failure(exc)
        stderr = proc.stderr if isinstance(proc.stderr, str) else ""
        try:
            payload = _json_object(proc.stdout)
        except ValueError:
            if proc.returncode != 0:
                return None, _classified_failure(_classify_cli_error(stderr))
            return None, (ProviderErrorKind.TRANSIENT, "provider_malformed_response")
        if not isinstance(payload.get("data"), Mapping):
            reported = _reported_error_kinds(payload.get("errors"))
            if reported:
                return None, _classified_failure(_reduce_provider_errors(reported))
            if proc.returncode != 0:
                return None, _classified_failure(_classify_cli_error(stderr))
            # Parseable JSON carrying no ``data`` and reporting no error is a
            # response this adapter cannot read, which is malformed rather than a
            # transport failure.
            return None, (ProviderErrorKind.TRANSIENT, "provider_malformed_response")
        return payload, None


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
                state,
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
        if not isinstance(name, str):
            raise ValueError("GitHub check rollup is malformed")
        identity = (
            f"{workflow} / {name}"
            if name and isinstance(workflow, str) and workflow
            else name
            or opaque_provider_check_identity(
                "github_check",
                (typename, workflow, status, conclusion),
            )
        )
        return identity, state, None
    if typename == "StatusContext":
        context = raw.get("context")
        if not isinstance(context, str):
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
        if context:
            return context, state, ("status_context", context)
        return (
            opaque_provider_check_identity("github_check", (typename, raw_state)),
            state,
            None,
        )
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


def _rest_enum(value: object) -> object:
    """Spell one REST enum the way GraphQL spells it, leaving a non-string alone.

    The two transports use the same vocabulary in different case --
    ``mergeable_state: "blocked"`` against ``mergeStateStatus: BLOCKED``, ``state:
    "failure"`` against ``FAILURE`` -- so case is the whole translation.

    A non-string is passed through so that ``_normalize_response`` and
    ``_normalize_check`` reject it as malformed, which is the same answer the
    GraphQL path gives for the same shape. Coercing it here would turn a response
    this adapter cannot read into a confident verdict about the subject.
    """
    return value.upper() if isinstance(value, str) else value


def _rest_primary_node(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Translate one REST pull request into the primary read's own node shape.

    A translation, not a second normalizer. Every validation, enum mapping and
    bound stays in ``_normalize_response`` and ``_normalize_mergeability``, so one
    fact cannot come to mean two things depending on which bucket answered. What
    REST genuinely spells differently is what this maps: ``merged`` is a boolean
    beside ``state`` rather than a third state, ``mergeable`` is a boolean rather
    than an enum, ``head.sha`` carries the revision, and ``mergeable_state`` is
    the lowercase of ``mergeStateStatus``.

    ``reviewDecision`` is the one primary fact REST does not carry AT ALL, so it
    is reported as unknown rather than guessed at. That is the fail-closed half of
    the fallback: an unknown review decision classifies as PENDING, so a
    REST-sourced observation can report a failing board but never a ready one.
    """
    head = raw.get("head")
    mergeable = raw.get("mergeable")
    return {
        "number": raw.get("number"),
        "state": "MERGED" if raw.get("merged") is True else _rest_enum(raw.get("state")),
        "isDraft": raw.get("draft"),
        "headRefOid": head.get("sha") if isinstance(head, Mapping) else None,
        "mergeable": _REST_MERGEABLE[mergeable] if isinstance(mergeable, bool) else "UNKNOWN",
        "mergeStateStatus": _rest_enum(raw.get("mergeable_state")),
        "reviewDecision": _REST_ABSENT_REVIEW_DECISION,
    }


def _rest_status_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Translate one combined-status response into the rollup's own row shape.

    REST's combined status reports the latest status per context, which is the
    same projection the GraphQL rollup reports, so each row becomes the
    ``StatusContext`` row ``_normalize_check`` already reads and no second check
    normalizer exists. Rows past the adapter's long-standing row budget are
    dropped here for the same reason the paginated GraphQL path drops them.
    """
    statuses = payload.get("statuses")
    if not isinstance(statuses, list):
        raise ValueError("GitHub commit status is malformed")
    rows: list[dict[str, Any]] = []
    for row in statuses[:_MAX_CHECK_ROWS]:
        if not isinstance(row, Mapping):
            raise ValueError("GitHub commit status is malformed")
        rows.append(
            {
                "__typename": "StatusContext",
                "context": row.get("context"),
                "state": _rest_enum(row.get("state")),
            }
        )
    return rows


def _batch_document(
    members: Sequence[_BatchSubject],
    selection: str,
    cursors: Mapping[str, str] | None = None,
) -> tuple[str, list[str]]:
    """Build one document over many subjects, passing every value as a variable.

    Nothing from a subject reaches the document TEXT: each alias and its variable
    names are built from the subject's index in the batch, while owner,
    repository, number and cursor travel as typed GraphQL variables. So a hostile
    repository name is a value the server binds, never syntax this function emits.

    A selection carrying the ``$CURSOR`` placeholder is paginated, and each
    subject gets its own cursor variable -- one document therefore advances
    subjects that are on different pages of their own connections.
    """
    paginated = "$CURSOR" in selection
    declarations: list[str] = []
    blocks: list[str] = []
    argv: list[str] = []
    for index, member in enumerate(members):
        declarations.extend((f"$o{index}:String!", f"$r{index}:String!", f"$n{index}:Int!"))
        body = selection
        if paginated:
            declarations.append(f"$c{index}:String")
            body = selection.replace("$CURSOR", f"$c{index}")
            cursor = (cursors or {}).get(member.raw)
            if cursor is not None:
                argv.extend(("-f", f"c{index}={cursor}"))
        blocks.append(
            f"s{index}:repository(owner:$o{index},name:$r{index})"
            f"{{pullRequest(number:$n{index}){{{body}}}}}"
        )
        argv.extend(
            (
                "-f",
                f"o{index}={member.target.owner}",
                "-f",
                f"r{index}={member.target.repo}",
                "-F",
                f"n{index}={member.target.number}",
            )
        )
    return "query(" + ",".join(declarations) + "){" + " ".join(blocks) + "}", argv


def _alias_index(path: object) -> int | None:
    """Read which subject an error's path names, or nothing if it names none."""
    if not isinstance(path, list) or not path or not isinstance(path[0], str):
        return None
    match = _ALIAS_RE.fullmatch(path[0])
    return int(match.group(1), 10) if match is not None else None


def _alias_pull_request(payload: Mapping[str, Any], index: int) -> Mapping[str, Any] | None:
    """Take one subject's node out of a batched response."""
    data = payload.get("data")
    node = data.get(f"s{index}") if isinstance(data, Mapping) else None
    pull_request = node.get("pullRequest") if isinstance(node, Mapping) else None
    return pull_request if isinstance(pull_request, Mapping) else None


def _classify_graphql_error(entry: Mapping[str, Any]) -> ProviderErrorKind:
    """Classify one reported error before it leaves this layer.

    The reported ``type`` is preferred because it is the host's own vocabulary. An
    unlisted or absent one is not guessed at: the message goes through the same
    classifier the CLI's stderr does, which ends at TRANSIENT, so an unclassified
    failure is still a classified provider error rather than a silent pass.
    """
    reported = entry.get("type")
    if isinstance(reported, str):
        kind = _GRAPHQL_ERROR_KINDS.get(reported.upper())
        if kind is not None:
            return kind
    message = entry.get("message")
    return _classify_cli_error(message if isinstance(message, str) else "")


def _reported_error_kinds(errors: object) -> list[ProviderErrorKind]:
    """Classify every error a response reported, ignoring which subject it named."""
    if not isinstance(errors, list):
        return []
    return [_classify_graphql_error(entry) for entry in errors if isinstance(entry, Mapping)]


def _subject_errors(
    payload: Mapping[str, Any],
    members: Sequence[_BatchSubject],
) -> dict[str, ProviderErrorKind]:
    """Attribute each reported error to the subject its alias names.

    An error whose path names no alias in this batch is not attributable to one
    subject, so it is charged to EVERY subject in the batch: a document-level
    failure means none of them was read, and dropping it would report a verdict
    from a response that carried none. A subject named more than once keeps its
    most specific error, the same ranking supplemental failures use.
    """
    reported: dict[str, list[ProviderErrorKind]] = {}
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return {}
    for entry in errors:
        if not isinstance(entry, Mapping):
            continue
        kind = _classify_graphql_error(entry)
        index = _alias_index(entry.get("path"))
        named = (
            [members[index]] if index is not None and 0 <= index < len(members) else list(members)
        )
        for member in named:
            reported.setdefault(member.raw, []).append(kind)
    return {raw: _reduce_provider_errors(kinds) for raw, kinds in reported.items()}


def _group_error(
    members: Sequence[_BatchSubject],
    failure: _Failure,
) -> dict[str, GitHubPullRequestProbeResult]:
    """Charge one failure that preceded the query to every subject it covered."""
    kind, reason = failure
    return {member.raw: _provider_error(kind, reason) for member in members}


def _build_result(
    response: GitHubPullRequestResponse,
    previous_observation: Mapping[str, object] | None,
    supplemental_provider_error: ProviderErrorKind | None,
) -> GitHubPullRequestProbeResult:
    """Reduce one subject's allowlisted facts to its canonical result."""
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
        supplemental_provider_error=supplemental_provider_error,
    )


def _page_cursor(page_info: object) -> tuple[bool, str | None]:
    """Read one page's continuation, refusing a shape that cannot be trusted."""
    if not isinstance(page_info, Mapping):
        raise ValueError("GitHub pagination is malformed")
    has_next = page_info.get("hasNextPage")
    if has_next is False:
        return False, None
    if has_next is not True:
        raise ValueError("GitHub pagination is malformed")
    cursor = page_info.get("endCursor")
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("GitHub pagination is malformed")
    return True, cursor


def _flat_check_row(raw: object) -> dict[str, Any]:
    """Present one rollup node in the flat shape the check normalizer reads.

    ``workflowName`` is GitHub's own workflow name, reached through the check
    suite's run; every other field is passed through untouched, so
    ``_normalize_check`` reads exactly the keys it always has.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("GitHub check rollup is malformed")
    row = {key: value for key, value in raw.items() if key != "checkSuite"}
    suite = raw.get("checkSuite")
    run = suite.get("workflowRun") if isinstance(suite, Mapping) else None
    workflow = run.get("workflow") if isinstance(run, Mapping) else None
    name = workflow.get("name") if isinstance(workflow, Mapping) else None
    if isinstance(name, str):
        row["workflowName"] = name
    return row


def _rollup_page(
    node: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], str, int, bool, str | None]:
    """Flatten one rollup page and name the commit it describes."""
    commits = node["commits"]
    entries = commits.get("nodes") if isinstance(commits, Mapping) else None
    if not isinstance(entries, list):
        raise ValueError("GitHub check rollup is malformed")
    if not entries:
        # A pull request carrying no commit has no rollup to read, which is an
        # empty complete check set rather than a failure.
        return [], "", 0, False, None
    entry = entries[0]
    commit = entry.get("commit") if isinstance(entry, Mapping) else None
    if not isinstance(commit, Mapping) or not isinstance(commit.get("oid"), str):
        raise ValueError("GitHub check rollup is malformed")
    revision = commit["oid"]
    rollup = commit.get("statusCheckRollup")
    if rollup is None:
        return [], revision, 0, False, None
    contexts = rollup.get("contexts") if isinstance(rollup, Mapping) else None
    if not isinstance(contexts, Mapping):
        raise ValueError("GitHub check rollup is malformed")
    rows = contexts.get("nodes")
    total = contexts.get("totalCount")
    if not isinstance(rows, list) or isinstance(total, bool) or not isinstance(total, int):
        raise ValueError("GitHub check rollup is malformed")
    has_next, cursor = _page_cursor(contexts.get("pageInfo"))
    return [_flat_check_row(row) for row in rows], revision, total, has_next, cursor


def _review_thread_page(node: Mapping[str, Any]) -> tuple[int, bool, bool, str | None]:
    """Count one page's unresolved threads, ignoring outdated ones."""
    threads = node["reviewThreads"]
    if not isinstance(threads, Mapping):
        raise ValueError("GitHub review threads are malformed")
    nodes = threads.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("GitHub review threads are malformed")
    unresolved = 0
    nodes_complete = True
    for thread in nodes:
        if (
            not isinstance(thread, Mapping)
            or not isinstance(thread.get("isResolved"), bool)
            or not isinstance(thread.get("isOutdated"), bool)
        ):
            nodes_complete = False
            continue
        unresolved += int(not thread["isResolved"] and not thread["isOutdated"])
    has_next, cursor = _page_cursor(threads.get("pageInfo"))
    return unresolved, nodes_complete, has_next, cursor


# ``gh`` stderr classification and the process-wide ``github:api`` cooldown are
# shared with the sibling monitor (``monitoring.github_provider_errors``); the
# module-level names stay so tests and callers address them per monitor.
_classify_cli_error = classify_cli_error
_shared_cooldown = shared_cooldown


def _shared_cooldown_result(retry_at: float) -> GitHubPullRequestProbeResult:
    return _provider_error(
        ProviderErrorKind.RATE_LIMITED,
        REASON_SHARED_COOLDOWN,
        summary=shared_cooldown_summary(retry_at),
    )


def _reduce_provider_errors(kinds: Sequence[ProviderErrorKind]) -> ProviderErrorKind:
    """Keep the most specific failure out of several reported for one subject."""
    if not kinds:
        return ProviderErrorKind.TRANSIENT
    return min(kinds, key=_PROVIDER_ERROR_PRIORITY.__getitem__)


def _combine_provider_errors(
    first: ProviderErrorKind | None,
    second: ProviderErrorKind | None,
) -> ProviderErrorKind | None:
    """Keep the most specific supplemental failure when both evidence reads fail."""
    present = [error for error in (first, second) if error is not None]
    return _reduce_provider_errors(present) if present else None


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


def _classified_failure(kind: ProviderErrorKind) -> _Failure:
    """Pair a classified kind with the reason code it is reported under."""
    return kind, _PROVIDER_ERROR_REASONS[kind]


def _exception_failure(error: BaseException) -> _Failure:
    """Classify a runner exception without letting it erase primary facts.

    ``_provider_exception_kind`` returns only TRANSIENT or SETUP, so the reason is
    derivable from the kind. The split is not cosmetic: TRANSIENT becomes
    RETRY_PROVIDER, but only until ``max_provider_errors`` (3 by default) is
    reached, after which it too retires the monitor; SETUP is not retryable at all
    and retires it on the FIRST occurrence (STOP_BLOCKED, outcome BLOCKED). So a
    host fault mislabelled SETUP costs the whole watch immediately rather than
    after the budget. If that classifier ever gains a third kind, this must gain
    the matching reason rather than stamping ``"provider_setup"`` on it.
    """
    return _classified_failure(_provider_exception_kind(error))


def _provider_error(
    kind: ProviderErrorKind, reason_code: str, *, summary: str = ""
) -> GitHubPullRequestProbeResult:
    result = provider_error_result(kind, reason_code)
    if not summary:
        return result
    return GitHubPullRequestProbeResult(
        response=None,
        canonical={},
        observation=MonitorObservation(
            "",
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error=kind,
            reason_code=reason_code,
            summary=summary,
        ),
    )
