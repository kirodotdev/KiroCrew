"""Bitbucket Cloud pull-request readiness provider."""

from __future__ import annotations

import base64
import json
import socket
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
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
_TERMINAL_STATES = {"MERGED", "DECLINED", "SUPERSEDED"}

BitbucketFetch = Callable[[BitbucketPullRequestTarget, str], object]


class BitbucketPullRequestProvider:
    """Read Bitbucket Cloud over one host-pinned bounded HTTPS client."""

    def __init__(self, *, fetch: BitbucketFetch | None = None) -> None:
        self._fetch = fetch

    def probe(
        self,
        raw_target: str,
        *,
        previous_observation: Mapping[str, object] | None = None,
    ) -> PullRequestProbeResult:
        try:
            target = parse_bitbucket_pull_request_target(raw_target)
            credentials = (
                None
                if self._fetch is not None
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
                statuses_complete = tasks_complete = conflicts_complete = True
            else:
                statuses, statuses_complete = _page(fetch("statuses"))
                tasks, tasks_complete = _page(fetch("tasks"))
                conflicts, conflicts_complete = _page(fetch("conflicts"))
            facts = _facts(
                target,
                pr,
                statuses,
                tasks,
                conflicts,
                statuses_complete=statuses_complete,
                tasks_complete=tasks_complete,
                conflicts_complete=conflicts_complete,
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
        except (TimeoutError, socket.timeout, URLError):
            return provider_error_result(ProviderErrorKind.TRANSIENT, "provider_transient")
        except OSError:
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
        }[resource]
        headers = {"Accept": "application/json"}
        email = credentials.get("BITBUCKET_EMAIL", "")
        token = credentials.get("BITBUCKET_API_TOKEN", "")
        if bool(email) != bool(token):
            _audit_bitbucket("denied")
            raise PermissionError("Bitbucket credentials are incomplete")
        if email and token:
            encoded = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        request = Request(f"{root}{suffix}", headers=headers, method="GET")
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
        except Exception:
            _audit_bitbucket("failed")
            raise
        if len(raw) > _MAX_RESPONSE_BYTES:
            _audit_bitbucket("failed")
            raise ValueError("Bitbucket response exceeds the monitor bound")
        decoded = json.loads(raw.decode("utf-8"))
        _audit_bitbucket("completed")
        return decoded


def _facts(
    target: BitbucketPullRequestTarget,
    pr: Mapping[str, Any],
    statuses: list[object],
    tasks: list[object],
    conflicts: list[object],
    *,
    statuses_complete: bool,
    tasks_complete: bool,
    conflicts_complete: bool,
) -> PullRequestFacts:
    raw_state = str(pr["state"]).upper()
    state = {"OPEN": "open", "MERGED": "merged", "DECLINED": "closed"}.get(
        raw_state,
        "closed" if raw_state == "SUPERSEDED" else "unknown",
    )
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
    return PullRequestFacts(
        kind="bitbucket_pull_request",
        target=target.identity,
        state=state,
        draft=bool(pr.get("draft", False)),
        head_revision=str(commit.get("hash") or ""),
        mergeability=(
            "conflicting" if conflicts else "mergeable" if conflicts_complete else "pending"
        ),
        review_decision=review_decision,
        checks=tuple(checks),
        checks_complete=statuses_complete,
        unresolved_review_threads=unresolved,
        review_threads_complete=tasks_complete,
    )


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


def _audit_bitbucket(outcome: str, *, critical: bool = False) -> None:
    """Record a credential-free fixed-host request lifecycle."""
    try:
        sel().log_api_access(
            caller="core:monitor",
            operation="monitor.bitbucket_probe",
            outcome=outcome,
            source="builtin-app",
            resources="bitbucket",
            critical=critical,
        )
    except Exception:
        if critical:
            raise


class _PinnedBitbucketRedirect(HTTPRedirectHandler):
    """Follow provider redirects only while they stay on the fixed API host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.bitbucket.org"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            raise ValueError("Bitbucket redirect left the fixed API host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)
