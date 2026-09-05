"""Azure DevOps Services pull-request readiness provider."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from typing import Any

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.github_runner import SetupError
from kiro_crew.monitoring.models import ProviderErrorKind
from kiro_crew.monitoring.provider_cli import run_provider_cli
from kiro_crew.monitoring.pull_request import (
    PullRequestCheck,
    PullRequestFacts,
    PullRequestProbeResult,
    PullRequestProviderError,
    build_pull_request_probe_result,
    classify_provider_error_text,
    opaque_provider_check_identity,
    provider_error_result,
    provider_failure_result,
)
from kiro_crew.monitoring.targets import (
    AzureDevOpsPullRequestTarget,
    parse_azure_devops_pull_request_target,
)

_TIMEOUT_SECS = 30.0
_MAX_OUTPUT_BYTES = 1024 * 1024
_MAX_EVIDENCE_ITEMS = 100
_TERMINAL_STATUSES = {"completed", "abandoned"}

AzureFetch = Callable[[AzureDevOpsPullRequestTarget, str], object]


class AzureDevOpsPullRequestProvider:
    """Read Azure DevOps Services through a preinstalled Azure CLI extension."""

    def __init__(self, *, fetch: AzureFetch | None = None) -> None:
        self._fetch = fetch

    def probe(
        self,
        raw_target: str,
        *,
        previous_observation: Mapping[str, object] | None = None,
    ) -> PullRequestProbeResult:
        try:
            target = parse_azure_devops_pull_request_target(raw_target)
            credentials = (
                None
                if self._fetch is not None
                else KiroCrewConfig.load().load_credentials(propagate=False)
            )

            def fetch(resource: str) -> object:
                if self._fetch is not None:
                    return self._fetch(target, resource)
                return self._fetch_with_az(target, resource, credentials or {})

            pull_request = _object(fetch("pull_request"))
            if str(pull_request.get("status", "")).lower() in _TERMINAL_STATUSES:
                statuses: list[object] = []
                threads: list[object] = []
                policies: list[object] = []
                statuses_complete = threads_complete = policies_complete = True
            else:
                statuses, statuses_complete = _values(fetch("statuses"))
                threads, threads_complete = _values(fetch("threads"))
                policies, policies_complete = _values(fetch("policies"))
            facts = _facts(
                target,
                pull_request,
                statuses,
                threads,
                policies,
                statuses_complete=statuses_complete,
                threads_complete=threads_complete,
                policies_complete=policies_complete,
            )
        except PullRequestProviderError as exc:
            return provider_failure_result(exc)
        except PermissionError:
            return provider_error_result(
                ProviderErrorKind.AUTHENTICATION,
                "provider_authentication",
            )
        except (SetupError, FileNotFoundError):
            return provider_error_result(ProviderErrorKind.SETUP, "provider_setup")
        except subprocess.TimeoutExpired:
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
    def _fetch_with_az(
        target: AzureDevOpsPullRequestTarget,
        resource: str,
        credentials: Mapping[str, str],
    ) -> object:
        organization_url = f"https://dev.azure.com/{target.organization}"
        common = [
            "--organization",
            organization_url,
            "--output",
            "json",
        ]
        if resource == "pull_request":
            args = [
                "repos",
                "pr",
                "show",
                "--id",
                str(target.pull_request_id),
                *common,
            ]
        elif resource == "policies":
            args = [
                "repos",
                "pr",
                "policy",
                "list",
                "--id",
                str(target.pull_request_id),
                *common,
            ]
        else:
            resource_names = {
                "statuses": "pullRequestStatuses",
                "threads": "pullRequestThreads",
            }
            args = [
                "devops",
                "invoke",
                *common,
                "--area",
                "git",
                "--resource",
                resource_names[resource],
                "--route-parameters",
                f"project={target.project}",
                f"repositoryId={target.repository}",
                f"pullRequestId={target.pull_request_id}",
                "--api-version",
                "7.1",
            ]
        proc = run_provider_cli(
            "az",
            args,
            timeout=_TIMEOUT_SECS,
            credentials=credentials,
        )
        if proc.returncode != 0:
            error = (proc.stderr or "").lower()
            if "azure-devops extension" in error or "az extension add" in error:
                raise SetupError("Azure DevOps extension is unavailable")
            raise PullRequestProviderError(classify_provider_error_text(error))
        raw = proc.stdout or ""
        if len(raw.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            raise ValueError("Azure DevOps response exceeds the monitor bound")
        return json.loads(raw)


def _facts(
    target: AzureDevOpsPullRequestTarget,
    pr: Mapping[str, Any],
    statuses: list[object],
    threads: list[object],
    policies: list[object],
    *,
    statuses_complete: bool,
    threads_complete: bool,
    policies_complete: bool,
) -> PullRequestFacts:
    repository = _object(pr["repository"])
    project = _object(repository["project"])
    if (
        str(repository["name"]).casefold() != target.repository.casefold()
        or str(project["name"]).casefold() != target.project.casefold()
    ):
        raise ValueError("Azure DevOps response identity does not match the target")
    raw_state = str(pr["status"]).lower()
    state = {"active": "open", "completed": "merged", "abandoned": "closed"}.get(
        raw_state,
        "unknown",
    )
    raw_merge = str(pr.get("mergeStatus", "")).lower()
    if raw_merge == "succeeded":
        mergeability = "mergeable"
    elif raw_merge in {"conflicts", "failure"}:
        mergeability = "conflicting" if raw_merge == "conflicts" else "blocked"
    elif raw_merge in {"queued", "notset"}:
        mergeability = "pending"
    else:
        mergeability = "pending"
    checks: list[PullRequestCheck] = []
    for index, raw in enumerate(statuses[:_MAX_EVIDENCE_ITEMS]):
        item = _object(raw)
        context = item.get("context")
        context_name = _object(context).get("name") if context else None
        raw_identity = item.get("id") or context_name or index
        identity = opaque_provider_check_identity("status", raw_identity)
        checks.append(PullRequestCheck(identity, _status_state(item.get("state"))))
    for index, raw in enumerate(policies[:_MAX_EVIDENCE_ITEMS]):
        item = _object(raw)
        configuration = item.get("configuration")
        policy_raw_identity: object = index
        if isinstance(configuration, Mapping):
            policy_raw_identity = configuration.get("id") or index
        identity = opaque_provider_check_identity("policy", policy_raw_identity)
        checks.append(PullRequestCheck(identity, _status_state(item.get("status"))))
    reviewers = pr.get("reviewers", [])
    if not isinstance(reviewers, list):
        raise ValueError("Azure DevOps reviewers are malformed")
    reviewer_records = [item for item in reviewers if isinstance(item, Mapping)]
    votes = [item.get("vote") for item in reviewer_records]
    required_votes = [item.get("vote") for item in reviewer_records if item.get("isRequired")]
    if any(isinstance(vote, int) and vote < 0 for vote in votes):
        review_decision = "changes_requested"
    elif required_votes and all(isinstance(vote, int) and vote > 0 for vote in required_votes):
        review_decision = "approved"
    elif required_votes:
        review_decision = "review_required"
    elif any(isinstance(vote, int) and vote > 0 for vote in votes):
        review_decision = "approved"
    else:
        review_decision = "none"
    unresolved = sum(
        1
        for raw in threads[:_MAX_EVIDENCE_ITEMS]
        if str(_object(raw).get("status", "")).lower() in {"active", "pending"}
    )
    raw_commit = pr.get("lastMergeSourceCommit")
    commit = {} if raw_commit is None else _object(raw_commit)
    return PullRequestFacts(
        kind="azure_devops_pull_request",
        target=target.identity,
        state=state,
        draft=bool(pr.get("isDraft", False)),
        head_revision=str(commit.get("commitId", "")),
        mergeability=mergeability,
        review_decision=review_decision,
        checks=tuple(checks),
        unresolved_review_threads=unresolved,
        review_threads_complete=(threads_complete and len(threads) <= _MAX_EVIDENCE_ITEMS),
        checks_complete=(
            statuses_complete
            and policies_complete
            and len(statuses) <= _MAX_EVIDENCE_ITEMS
            and len(policies) <= _MAX_EVIDENCE_ITEMS
        ),
    )


def _status_state(value: object) -> str:
    status = str(value or "").lower()
    if status in {"succeeded", "approved", "notapplicable"}:
        return "passed"
    if status in {"failed", "error", "rejected", "broken", "canceled"}:
        return "failed"
    if status in {"pending", "queued", "running", "notset"}:
        return "pending"
    return "unknown"


def _object(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("provider response must be an object")
    return value


def _values(value: object) -> tuple[list[object], bool]:
    if isinstance(value, list):
        return value, True
    payload = _object(value)
    raw = payload.get("value")
    if not isinstance(raw, list):
        raise ValueError("provider response values must be a list")
    continuation = payload.get("continuationToken", payload.get("continuation_token"))
    return raw, not bool(continuation)
