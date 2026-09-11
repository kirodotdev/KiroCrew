"""Typed public-GitHub Actions workflow-run observations for structured monitors.

This module is the second monitored kind, and it exists to test one claim the
monitoring substrate makes: that a genuinely different subject can be added as a
probe plus a registry entry, without editing the shared decision engine, the
shared result type, or the shared probe protocol.

A workflow run is a different subject from a pull request, deliberately. It shares
the credential and the CLI, so there is no authentication work here to distract
from that question. What differs is the substance: a run's terminal states are
``success`` / ``failure`` / ``cancelled`` / ``timed_out`` rather than merged or
closed, its objective is that the run REACHED a conclusion rather than that a human
may review it, and it cannot be inferred from a message by URL the way a pull
request can. The last property is why the kind is registered as not publicly
armable: it has to speak the registry's vocabulary by construction.

The engine reads only what :class:`MonitorProbeResult` declares. Everything a
workflow run knows that a pull request does not -- the run's conclusion, the
workflow name -- stays on this module's own response type and its own probe-result
subclass, never on the shared type. That is the line every kind draws for itself.
"""

from __future__ import annotations

import errno
import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from kiro_crew.github_runner import SetupError, resolve_gh, run_gh
from kiro_crew.monitoring.models import (
    MonitorObservation,
    MonitorObservationStatus,
    MonitorProbeResult,
    ProviderErrorKind,
)

_GITHUB_HOST = "github.com"
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_RAW_URL_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_HTTP_STATUS_RE = re.compile(r"\bhttp\s+(\d{3})\b", re.IGNORECASE)
_HEAD_REVISION_RE = re.compile(r"^[0-9a-fA-F]{1,128}$")
_PROBE_TIMEOUT_SECS = 30.0
_RUN_FIELDS = "databaseId,status,conclusion,headSha,workflowName,event"
_MAX_WORKFLOW_RUN_ID = 9_223_372_036_854_775_807

GitHubResolver = Callable[[], str]
GitHubRunner = Callable[..., subprocess.CompletedProcess[str]]

#: Actions statuses that are not yet a conclusion. Any status outside this set that
#: is also not ``completed`` is treated as pending too, so a status vocabulary the
#: CLI adds later degrades to "still running" rather than to a wrong verdict.
_IN_PROGRESS_STATUSES = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})

#: A run's conclusion mapped to this kind's OWN terminal vocabulary. Not merged or
#: closed: those are pull-request words. A run either reached a good end, a bad end,
#: or was stopped. ``neutral`` / ``skipped`` / ``stale`` are non-failing ends and
#: read as success, matching how the pull-request check normalizer treats the same
#: conclusions on an individual check.
_SUCCESS_CONCLUSIONS = frozenset({"success", "neutral", "skipped", "stale"})
_CANCELLED_CONCLUSIONS = frozenset({"cancelled"})
_FAILURE_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required", "startup_failure"})


@dataclass(frozen=True)
class GitHubWorkflowRunTarget:
    """Validated identity of one public GitHub Actions workflow run."""

    host: str
    owner: str
    repo: str
    run_id: int

    def __post_init__(self) -> None:
        if self.host != _GITHUB_HOST:
            raise ValueError("target must be a public GitHub workflow run")
        if any(
            segment in {".", ".."} or _SEGMENT_RE.fullmatch(segment) is None
            for segment in (self.owner, self.repo)
        ):
            raise ValueError("target must be a public GitHub workflow run")
        if isinstance(self.run_id, bool) or not isinstance(self.run_id, int) or self.run_id <= 0:
            raise ValueError("target must be a public GitHub workflow run")

    @property
    def identity(self) -> str:
        return f"{self.host}/{self.owner}/{self.repo}/actions/runs/{self.run_id}"

    @property
    def repo_slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.repo}/actions/runs/{self.run_id}"


@dataclass(frozen=True)
class GitHubWorkflowRunResponse:
    """Allowlisted provider facts with no raw response attached."""

    target: GitHubWorkflowRunTarget
    status: str
    conclusion: str
    head_revision: str
    workflow_name: str
    event: str


@dataclass(frozen=True)
class GitHubWorkflowRunProbeResult(MonitorProbeResult):
    """One run's canonical facts, plus the typed response behind them.

    The engine reads only what :class:`MonitorProbeResult` declares. ``response``
    is this kind's own detail and stays here rather than on the shared type -- the
    same line the pull-request kind draws with its own subclass.
    """

    response: GitHubWorkflowRunResponse | None


class GitHubWorkflowRunProvider:
    """Read public workflow-run state through the authenticated hardened gh runner."""

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
    ) -> Mapping[str, GitHubWorkflowRunProbeResult]:
        """Return one canonical run-completion observation per subject.

        The GitHub CLI answers for one run per call, so this loops. The loop is an
        implementation detail of this kind: the boundary is plural so a host that
        answers for many subjects at once needs no signature change, and keyed by
        the subject string as passed so a caller can look up what it asked for.

        ``previous_observations`` is accepted to satisfy the shared boundary; a
        workflow run has no head-change semantics, so it is unused here.
        """
        return {subject: self._probe_one(subject) for subject in subjects}

    def _probe_one(self, raw_target: str) -> GitHubWorkflowRunProbeResult:
        """Return one canonical run-completion observation."""
        try:
            target = parse_github_workflow_run_target(raw_target)
            gh = self._resolver()
            proc = self._runner(
                [
                    gh,
                    "run",
                    "view",
                    str(target.run_id),
                    "--repo",
                    target.repo_slug,
                    "--json",
                    _RUN_FIELDS,
                ],
                timeout=_PROBE_TIMEOUT_SECS,
                audit_caller="core:monitor",
                pin_host=_GITHUB_HOST,
            )
            failure = _process_failure(proc)
            if failure is not None:
                return failure
            response = _normalize_response(target, _json_object(proc.stdout))
        except (SetupError, FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            return _provider_exception_error(exc)
        except (TypeError, ValueError, KeyError):
            return _provider_error(ProviderErrorKind.TRANSIENT, "provider_malformed_response")
        canonical = _canonical_response(response)
        status, reason_code = _classify_response(response)
        return GitHubWorkflowRunProbeResult(
            response=response,
            canonical=canonical,
            observation=MonitorObservation(
                _fingerprint(canonical),
                status,
                reason_code=reason_code,
            ),
        )


def parse_github_workflow_run_target(raw: str) -> GitHubWorkflowRunTarget:
    """Parse one exact public GitHub Actions run URL into a typed identity.

    A workflow run URL is ``/owner/repo/actions/runs/<id>``. An attempt-scoped URL
    ``.../actions/runs/<id>/attempts/<n>`` and the job-scoped ``/job/<id>`` form are
    both refused: the run id is the durable identity a probe watches, and admitting
    a sub-view would make two URLs for one subject key the same watch differently.
    """
    if not isinstance(raw, str) or not raw or _RAW_URL_CONTROL_RE.search(raw):
        raise ValueError("target must be a GitHub workflow run URL")
    parsed = urlparse(raw)
    try:
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise ValueError("target must be a public GitHub workflow run URL") from exc
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
        raise ValueError("target must be a public GitHub workflow run URL")
    parts = PurePosixPath(parsed.path).parts
    if len(parts) != 6 or parts[0] != "/" or parts[3] != "actions" or parts[4] != "runs":
        raise ValueError("target must be a GitHub workflow run URL")
    owner, repo, raw_id = parts[1], parts[2], parts[5]
    if parsed.path != f"/{owner}/{repo}/actions/runs/{raw_id}":
        raise ValueError("target must be a canonical GitHub workflow run URL")
    if (
        not raw_id.isascii()
        or not raw_id.isdecimal()
        or raw_id.startswith("0")
        or int(raw_id, 10) > _MAX_WORKFLOW_RUN_ID
    ):
        raise ValueError("target must be a GitHub workflow run with a positive id")
    try:
        return GitHubWorkflowRunTarget(_GITHUB_HOST, owner, repo, int(raw_id, 10))
    except ValueError as exc:
        raise ValueError("target must be a valid GitHub workflow run") from exc


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
    target: GitHubWorkflowRunTarget,
    raw: Mapping[str, Any],
) -> GitHubWorkflowRunResponse:
    required = {"databaseId", "status", "conclusion", "headSha", "workflowName", "event"}
    if not required.issubset(raw):
        raise ValueError("GitHub workflow run response is malformed")
    run_id = raw["databaseId"]
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id != target.run_id:
        raise ValueError("GitHub workflow run response is malformed")
    status = raw["status"]
    if not isinstance(status, str) or not status:
        raise ValueError("GitHub workflow run response is malformed")
    conclusion = raw["conclusion"]
    # A run that has not concluded reports an empty-string or null conclusion.
    if conclusion is None:
        conclusion = ""
    if not isinstance(conclusion, str):
        raise ValueError("GitHub workflow run response is malformed")
    head_revision = raw["headSha"]
    if not isinstance(head_revision, str) or (
        head_revision and _HEAD_REVISION_RE.fullmatch(head_revision) is None
    ):
        raise ValueError("GitHub workflow run response is malformed")
    workflow_name = raw["workflowName"]
    event = raw["event"]
    if not isinstance(workflow_name, str) or not isinstance(event, str):
        raise ValueError("GitHub workflow run response is malformed")
    return GitHubWorkflowRunResponse(
        target=target,
        status=status.lower(),
        conclusion=conclusion.lower(),
        head_revision=head_revision,
        workflow_name=workflow_name,
        event=event,
    )


def _canonical_response(response: GitHubWorkflowRunResponse) -> dict[str, object]:
    return {
        "conclusion": response.conclusion,
        "event": response.event,
        "head_revision": response.head_revision,
        "kind": "github_workflow_run",
        "status": response.status,
        "target": response.target.identity,
        "workflow_name": response.workflow_name,
    }


def _fingerprint(canonical: Mapping[str, object]) -> str:
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _classify_response(
    response: GitHubWorkflowRunResponse,
) -> tuple[MonitorObservationStatus, str]:
    """Map a run to the engine's generic status plus this kind's own reason code.

    The objective is ``run_complete``: a run is done when it reaches a conclusion.
    A completed run's conclusion decides success or blocked; anything not completed
    is pending. This is the terminal-state mapping the acceptance test pins.
    """
    if response.status in _IN_PROGRESS_STATUSES or response.status != "completed":
        return MonitorObservationStatus.PENDING, "run_in_progress"
    if response.conclusion in _SUCCESS_CONCLUSIONS:
        return MonitorObservationStatus.SUCCESS, "run_succeeded"
    if response.conclusion in _CANCELLED_CONCLUSIONS:
        return MonitorObservationStatus.BLOCKED, "run_cancelled"
    if response.conclusion in _FAILURE_CONCLUSIONS:
        return MonitorObservationStatus.BLOCKED, "run_failed"
    # Completed with a conclusion this kind does not recognize. Treated as blocked
    # rather than pending: a completed run is terminal, and calling an unrecognized
    # terminal conclusion "still running" would keep a finished watch alive forever.
    return MonitorObservationStatus.BLOCKED, "run_conclusion_unknown"


def _process_failure(
    proc: subprocess.CompletedProcess[str],
) -> GitHubWorkflowRunProbeResult | None:
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
    if "http 429" in lowered:
        return ProviderErrorKind.RATE_LIMITED
    if any(
        marker in lowered
        for marker in ("bad credentials", "authentication", "not logged into", "gh auth login")
    ):
        return ProviderErrorKind.AUTHENTICATION
    if any(
        marker in lowered for marker in ("not found", "could not resolve to a", "no runs found")
    ):
        return ProviderErrorKind.NOT_FOUND
    if any(
        marker in lowered
        for marker in ("forbidden", "permission", "resource not accessible", "saml")
    ):
        return ProviderErrorKind.AUTHORIZATION
    return ProviderErrorKind.TRANSIENT


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


def _provider_exception_error(error: BaseException) -> GitHubWorkflowRunProbeResult:
    kind = _provider_exception_kind(error)
    reason = "provider_transient" if kind is ProviderErrorKind.TRANSIENT else "provider_setup"
    return _provider_error(kind, reason)


def _provider_error(kind: ProviderErrorKind, reason_code: str) -> GitHubWorkflowRunProbeResult:
    return GitHubWorkflowRunProbeResult(
        response=None,
        canonical={},
        observation=MonitorObservation(
            "",
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error=kind,
            reason_code=reason_code,
        ),
    )
