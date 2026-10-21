"""Bitbucket Cloud pull-request readiness provider."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.monitoring.models import ProviderErrorKind
from kiro_crew.monitoring.pull_request import (
    PullRequestCheck,
    PullRequestFacts,
    PullRequestProbeResult,
    build_pull_request_probe_result,
    opaque_provider_check_identity,
    pr_comment_body_digest,
    pr_comment_body_fingerprint,
    provider_error_result,
)
from kiro_crew.monitoring.targets import (
    BitbucketPullRequestTarget,
    parse_bitbucket_pull_request_target,
)
from kiro_crew.sel import sel

_API_ROOT = "https://api.bitbucket.org/2.0"
_TIMEOUT_SECS = 30.0
_MAX_RESPONSE_BYTES = 1024 * 1024
# PR-level comments are the surface a review bot's verdict comment lives on -- a
# body rewritten in place with a frozen created_at, which a count or a
# newest-timestamp probe cannot see and only a digest over the bodies can. Read
# with the same page size as statuses/tasks/conflicts, but FOLLOWED across pages
# up to this cap so a busy pull request whose oldest comment carries the verdict
# is read whole; a read still truncated at the cap is reported incomplete, which
# fails the digest closed. The per-comment body is reduced to a fixed-width
# fingerprint at the point of retention (``pr_comment_body_fingerprint``), so what
# is kept never scales with how much a reviewer wrote.
_COMMENT_PAGE_SIZE = 100
_COMMENT_MAX_PAGES = 10
_MAX_COMMENT_ITEMS = 1000
_CANONICAL_STATES = {
    "OPEN": "open",
    "MERGED": "merged",
    "DECLINED": "closed",
    "SUPERSEDED": "closed",
}
# Terminal means "no supplemental read can change the verdict", which is every
# mapped state except the open one. Deriving it keeps the two from drifting.
_TERMINAL_STATES = frozenset(
    raw for raw, canonical in _CANONICAL_STATES.items() if canonical != "open"
)

BitbucketFetch = Callable[[BitbucketPullRequestTarget, str], object]


class BitbucketPullRequestProvider:
    """Read Bitbucket Cloud over one host-pinned bounded HTTPS client."""

    def __init__(self, *, fetch: BitbucketFetch | None = None) -> None:
        self._fetch = fetch

    def probe(
        self,
        subjects: Sequence[str],
        *,
        previous_observations: Mapping[str, Mapping[str, object]] | None = None,
        use_owner_credentials: bool = True,
    ) -> Mapping[str, PullRequestProbeResult]:
        previous = previous_observations or {}
        return {
            subject: self._probe_one(
                subject,
                previous.get(subject),
                use_owner_credentials=use_owner_credentials,
            )
            for subject in subjects
        }

    def _probe_one(
        self,
        raw_target: str,
        previous_observation: Mapping[str, object] | None = None,
        use_owner_credentials: bool = True,
    ) -> PullRequestProbeResult:
        try:
            target = parse_bitbucket_pull_request_target(raw_target)
            credentials = (
                None
                if self._fetch is not None or not use_owner_credentials
                else KiroCrewConfig.load().load_credentials(propagate=False)
            )

            def fetch(resource: str) -> object:
                if self._fetch is not None:
                    return self._fetch(target, resource)
                return self._fetch_https(target, resource, credentials or {})

            pr = _object(fetch("pull_request"))
            if str(pr.get("state", "")).upper() in _TERMINAL_STATES:
                statuses: list[object] = []
                tasks: list[object] = []
                conflicts: list[object] = []
                comments: list[object] = []
                statuses_complete = tasks_complete = conflicts_complete = True
                comments_complete = True
            else:
                statuses, statuses_complete = _page(fetch("statuses"))
                tasks, tasks_complete = _page(fetch("tasks"))
                conflicts, conflicts_complete = _page(fetch("conflicts"))
                # The comment read is the ONE supplemental that must never fail
                # the probe: comment completeness does not gate readiness, so a
                # byte-limit, rate-limit, timeout or malformed comment page fails
                # only the digest closed (empty, no wake) and leaves the checks,
                # tasks and conflicts facts standing. The readiness reads above
                # stay in the outer try, where their failure IS a provider error.
                try:
                    comments, comments_complete = _page(fetch("comments"))
                except (HTTPError, OSError, KeyError, TypeError, ValueError):
                    comments, comments_complete = [], False
            facts = _facts(
                target,
                pr,
                statuses,
                tasks,
                conflicts,
                comments,
                statuses_complete=statuses_complete,
                tasks_complete=tasks_complete,
                conflicts_complete=conflicts_complete,
                comments_complete=comments_complete,
            )
        except PermissionError:
            return provider_error_result(
                ProviderErrorKind.AUTHENTICATION,
                "provider_authentication",
            )
        except FileNotFoundError:
            return provider_error_result(ProviderErrorKind.SETUP, "provider_setup")
        except HTTPError as exc:
            if exc.code == 401:
                return provider_error_result(
                    ProviderErrorKind.AUTHENTICATION,
                    "provider_authentication",
                )
            if exc.code == 403:
                return provider_error_result(
                    ProviderErrorKind.AUTHORIZATION,
                    "provider_authorization",
                )
            if exc.code == 404:
                return provider_error_result(
                    ProviderErrorKind.NOT_FOUND,
                    "provider_not_found",
                )
            if exc.code == 429 or exc.code >= 500:
                return provider_error_result(
                    ProviderErrorKind.RATE_LIMITED,
                    "provider_rate_limited",
                )
            return provider_error_result(ProviderErrorKind.TRANSIENT, "provider_transient")
        except OSError:
            # URLError and TimeoutError are both OSError subclasses, so this one
            # clause covers the whole network-failure class.
            return provider_error_result(ProviderErrorKind.TRANSIENT, "provider_transient")
        except (KeyError, TypeError, ValueError):
            return provider_error_result(
                ProviderErrorKind.TRANSIENT,
                "provider_malformed_response",
            )
        return build_pull_request_probe_result(
            facts,
            previous_observation=previous_observation,
        )

    @staticmethod
    def _fetch_https(
        target: BitbucketPullRequestTarget,
        resource: str,
        credentials: Mapping[str, str],
    ) -> object:
        workspace = quote(target.workspace, safe="")
        repository = quote(target.repository, safe="")
        root = (
            f"{_API_ROOT}/repositories/{workspace}/{repository}/"
            f"pullrequests/{target.pull_request_id}"
        )
        suffix = {
            "pull_request": "",
            "statuses": "/statuses?pagelen=100",
            "tasks": "/tasks?pagelen=100",
            "conflicts": "/conflicts?pagelen=100",
            "comments": f"/comments?pagelen={_COMMENT_PAGE_SIZE}",
        }[resource]
        headers = BitbucketPullRequestProvider._auth_headers(credentials)
        if resource == "comments":
            return BitbucketPullRequestProvider._fetch_paginated(f"{root}{suffix}", headers)
        return BitbucketPullRequestProvider._fetch_page(f"{root}{suffix}", headers)

    @staticmethod
    def _auth_headers(credentials: Mapping[str, str]) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        email = credentials.get("BITBUCKET_EMAIL", "")
        token = credentials.get("BITBUCKET_API_TOKEN", "")
        if bool(email) != bool(token):
            _audit_bitbucket("denied", reason="credentials_incomplete")
            raise PermissionError("Bitbucket credentials are incomplete")
        if email and token:
            encoded = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        return headers

    @staticmethod
    def _fetch_page(url: str, headers: Mapping[str, str]) -> Mapping[str, Any]:
        """Read one bounded page off the fixed API host."""
        request = Request(url, headers=dict(headers), method="GET")
        try:
            _audit_bitbucket("invoked", critical=True)
        except Exception as exc:
            raise FileNotFoundError("Bitbucket audit unavailable") from exc
        try:
            with build_opener(_PinnedBitbucketRedirect()).open(
                request,
                timeout=_TIMEOUT_SECS,
            ) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except Exception as exc:
            # urllib raises a socket timeout bare once connected and wraps one
            # raised while connecting in URLError.reason.
            timed_out = isinstance(exc, TimeoutError) or isinstance(
                getattr(exc, "reason", None), TimeoutError
            )
            _audit_bitbucket("failed", reason="timeout" if timed_out else "provider_error")
            raise
        if len(raw) > _MAX_RESPONSE_BYTES:
            _audit_bitbucket("failed", reason="output_too_large")
            raise ValueError("Bitbucket response exceeds the monitor bound")
        decoded = json.loads(raw.decode("utf-8"))
        _audit_bitbucket("completed")
        return _object(decoded)

    @staticmethod
    def _fetch_paginated(url: str, headers: Mapping[str, str]) -> Mapping[str, Any]:
        """Follow the provider's ``next`` link across a bounded number of pages.

        Returns the same ``{"values": [...], "next": ...}`` shape one page carries,
        so :func:`_page` reads it exactly like the single-page resources: the
        accumulated ``values`` and a ``next`` that is cleared ONLY when the whole
        collection was read inside the page and item caps. A read still advertising
        a ``next`` at the cap keeps it, so completeness fails closed and the digest
        is empty.

        Every page is pinned to the fixed API host BEFORE it is opened. The owner
        credential rides each request, so a provider-controlled ``next`` link is
        checked against the same host/scheme/userinfo/port rule a redirect is
        (:func:`_pinned_to_api_host`) rather than trusted -- the redirect handler
        guards only 3xx hops, not the initial request to each link. An off-host,
        non-string, or repeated link ends the walk and leaves the read incomplete
        (fail-closed, no exception), because comment completeness never gates
        readiness.
        """
        values: list[object] = []
        seen_links: set[str] = set()
        next_url: str | None = url
        complete = False
        for _ in range(_COMMENT_MAX_PAGES):
            if next_url is None:
                complete = True
                break
            if not _pinned_to_api_host(next_url):
                # Never attach the owner credential to an off-host link, whatever
                # the provider body claimed; stop and report the read incomplete.
                break
            page = BitbucketPullRequestProvider._fetch_page(next_url, headers)
            page_values, _page_complete = _page(page)
            values.extend(page_values)
            if len(values) >= _MAX_COMMENT_ITEMS:
                values = values[:_MAX_COMMENT_ITEMS]
                break
            raw_next = page.get("next")
            if not raw_next:
                complete = True
                break
            if not isinstance(raw_next, str) or raw_next in seen_links:
                # A non-string or a repeated cursor is a read that cannot be
                # advanced safely; stop and report it incomplete.
                break
            seen_links.add(raw_next)
            next_url = raw_next
        return {"values": values, "next": None if complete else "truncated"}


def _facts(
    target: BitbucketPullRequestTarget,
    pr: Mapping[str, Any],
    statuses: list[object],
    tasks: list[object],
    conflicts: list[object],
    comments: list[object],
    *,
    statuses_complete: bool,
    tasks_complete: bool,
    conflicts_complete: bool,
    comments_complete: bool,
) -> PullRequestFacts:
    raw_state = str(pr["state"]).upper()
    state = _CANONICAL_STATES.get(raw_state, "unknown")
    source_raw = pr.get("source")
    source = {} if source_raw is None else _object(source_raw)
    commit_raw = source.get("commit")
    commit = {} if commit_raw is None else _object(commit_raw)
    checks: list[PullRequestCheck] = []
    for index, raw in enumerate(statuses[:100]):
        item = _object(raw)
        status = str(item.get("state", "")).upper()
        if status == "SUCCESSFUL":
            normalized = "passed"
        elif status in {"FAILED", "STOPPED"}:
            normalized = "failed"
        elif status == "INPROGRESS":
            normalized = "pending"
        else:
            normalized = "unknown"
        raw_identity = item.get("key") or item.get("name") or item.get("uuid") or index
        identity = opaque_provider_check_identity("status", raw_identity)
        checks.append(PullRequestCheck(identity, normalized))
    participants = pr.get("participants", [])
    if not isinstance(participants, list):
        raise ValueError("Bitbucket participants are malformed")
    reviewers = [
        item
        for item in participants
        if isinstance(item, Mapping) and str(item.get("role", "")).casefold() == "reviewer"
    ]
    states = {str(item.get("state", "")).lower() for item in reviewers}
    if "changes_requested" in states:
        review_decision = "changes_requested"
    elif reviewers and all(item.get("approved") is True for item in reviewers):
        review_decision = "approved"
    elif reviewers:
        review_decision = "review_required"
    else:
        review_decision = "none"
    unresolved = sum(
        1
        for raw in tasks[:100]
        if str(_object(raw).get("state", "")).upper() not in {"RESOLVED", "CLOSED"}
    )
    if conflicts:
        mergeability = "conflicting"
    elif conflicts_complete:
        mergeability = "mergeable"
    else:
        mergeability = "pending"
    return PullRequestFacts(
        kind="bitbucket_pull_request",
        target=target.identity,
        state=state,
        draft=bool(pr.get("draft", False)),
        head_revision=str(commit.get("hash") or ""),
        mergeability=mergeability,
        review_decision=review_decision,
        checks=tuple(checks),
        checks_complete=statuses_complete,
        unresolved_review_threads=unresolved,
        review_threads_complete=tasks_complete,
        pr_comment_body_digest=_comment_body_digest(comments, comments_complete),
    )


def _comment_body_digest(comments: list[object], comments_complete: bool) -> str:
    """Digest the PR-level comment bodies, failing CLOSED on an incomplete read.

    Mirrors the GitHub adapter: a digest over ALL comment bodies, human and bot
    alike, because a human editing a comment in place is exactly as invisible to a
    count and as load-bearing as a bot rewriting a verdict. The per-comment body is
    reduced to a fixed-width fingerprint at the point of retention, so the probe's
    memory does not scale with comment prose. A malformed comment, or a read the
    pagination could not finish, yields "" so the shared condition logic emits no
    ``review_comment_bodies`` wake -- an empty/absent digest is the incomplete-read
    signal, so a comment page that keeps failing never wakes the owner forever.

    Bitbucket Cloud carries a comment body as ``{"content": {"raw": "..."}}``; a
    comment with no readable raw body contributes nothing, the same as GitHub's
    handling of an empty body.
    """
    if not comments_complete:
        return ""
    fingerprints: list[str] = []
    for raw in comments[:_MAX_COMMENT_ITEMS]:
        if not isinstance(raw, Mapping):
            # A malformed comment is treated as an incomplete read rather than a
            # provider error: comment completeness never gates readiness, so a
            # bad item fails the digest closed instead of failing the whole probe.
            return ""
        content = raw.get("content")
        if content is None:
            continue
        if not isinstance(content, Mapping):
            return ""
        body = content.get("raw")
        if not isinstance(body, str) or not body:
            continue
        fingerprints.append(pr_comment_body_fingerprint(body))
    return pr_comment_body_digest(fingerprints)


def _object(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("provider response must be an object")
    return value


def _page(value: object) -> tuple[list[object], bool]:
    page = _object(value)
    raw = page.get("values")
    if not isinstance(raw, list):
        raise ValueError("provider response values must be a list")
    return raw, not bool(page.get("next"))


def _audit_bitbucket(outcome: str, *, reason: str = "", critical: bool = False) -> None:
    """Record a credential-free fixed-host request lifecycle and a fixed reason word."""
    try:
        sel().log_api_access(
            caller="core:monitor",
            operation="monitor.bitbucket_probe",
            outcome=outcome,
            source="builtin-app",
            resources="bitbucket",
            error=reason,
            critical=critical,
        )
    except Exception:
        if critical:
            raise


def _pinned_to_api_host(url: str) -> bool:
    """True only for an HTTPS URL on the fixed API host with no embedded identity.

    One predicate for both the request path and the redirect path: a provider
    ``next`` link is opened with the owner credential attached, so it must clear
    the SAME host/scheme/userinfo/port check a redirect does before the request is
    sent, not only after a 3xx.
    """
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "api.bitbucket.org"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
    )


class _PinnedBitbucketRedirect(HTTPRedirectHandler):
    """Follow provider redirects only while they stay on the fixed API host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _pinned_to_api_host(newurl):
            raise ValueError("Bitbucket redirect left the fixed API host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)
