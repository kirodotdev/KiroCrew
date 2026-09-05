"""GitLab merge-request readiness provider for structured monitors."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.github_runner import SetupError, gitlab_ambient_token_allowed
from kiro_crew.monitoring.models import ProviderErrorKind
from kiro_crew.monitoring.provider_cli import audit_provider_cli_denied, run_provider_cli
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
    GitLabHostNotAllowed,
    GitLabMergeRequestTarget,
    parse_gitlab_merge_request_target,
)

_TIMEOUT_SECS = 30.0
_MAX_OUTPUT_BYTES = 1024 * 1024
_PAGE_SIZE = 100
_MAX_PROVIDER_ITEMS = _PAGE_SIZE * 2
_TERMINAL_STATES = {"merged", "closed"}

GitLabFetch = Callable[[GitLabMergeRequestTarget, str], object]


@dataclass(frozen=True)
class _Collection:
    items: list[object]
    complete: bool


class GitLabMergeRequestProvider:
    """Read GitLab.com or allowlisted self-managed merge requests through glab."""

    def __init__(
        self,
        *,
        gitlab_hosts: Sequence[str] | None = None,
        fetch: GitLabFetch | None = None,
    ) -> None:
        self._configured_hosts = tuple(gitlab_hosts) if gitlab_hosts is not None else None
        self._fetch = fetch

    def probe(
        self,
        raw_target: str,
        *,
        previous_observation: Mapping[str, object] | None = None,
    ) -> PullRequestProbeResult:
        try:
            target = parse_gitlab_merge_request_target(
                raw_target,
                gitlab_hosts=self._gitlab_hosts(),
            )

            def fetch(resource: str) -> object:
                if self._fetch is not None:
                    return self._fetch(target, resource)
                return self._fetch_with_glab(target, resource)

            mr = _object(fetch("merge_request"))
            if str(mr.get("state", "")).lower() in _TERMINAL_STATES:
                pipelines: list[object] = []
                discussions: list[object] = []
                discussions_complete = True
            else:
                head_pipeline = mr.get("head_pipeline")
                pipelines = [] if head_pipeline is None else [_object(head_pipeline)]
                discussions, discussions_complete = _collection(fetch("discussions"))
            facts = _facts(
                target,
                mr,
                pipelines,
                discussions,
                discussions_complete=discussions_complete,
            )
        except PullRequestProviderError as exc:
            return provider_failure_result(exc)
        except PermissionError:
            return provider_error_result(
                ProviderErrorKind.AUTHENTICATION,
                "provider_authentication",
            )
        except GitLabHostNotAllowed:
            audit_provider_cli_denied("glab")
            return provider_error_result(
                ProviderErrorKind.AUTHORIZATION,
                "provider_authorization",
            )
        except SetupError:
            return provider_error_result(ProviderErrorKind.SETUP, "provider_setup")
        except subprocess.TimeoutExpired:
            return provider_error_result(ProviderErrorKind.TRANSIENT, "provider_transient")
        except FileNotFoundError:
            return provider_error_result(ProviderErrorKind.SETUP, "provider_setup")
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

    def _gitlab_hosts(self) -> tuple[str, ...]:
        if self._configured_hosts is not None:
            return self._configured_hosts
        return tuple(KiroCrewConfig.load().dashboard.gitlab_hosts)

    @staticmethod
    def _fetch_with_glab(
        target: GitLabMergeRequestTarget,
        resource: str,
    ) -> object:
        project = quote(target.project_path, safe="")
        root = f"projects/{project}/merge_requests/{target.iid}"
        if resource == "merge_request":
            return GitLabMergeRequestProvider._get_glab_page(target, root)
        if resource == "discussions":
            collection = f"{root}/discussions"
        else:
            raise ValueError("unsupported GitLab monitor resource")
        first = _list(
            GitLabMergeRequestProvider._get_glab_page(
                target,
                (
                    f"{collection}&per_page={_PAGE_SIZE}&page=1"
                    if "?" in collection
                    else f"{collection}?per_page={_PAGE_SIZE}&page=1"
                ),
            )
        )
        if len(first) < _PAGE_SIZE:
            return _Collection(first, True)
        second = _list(
            GitLabMergeRequestProvider._get_glab_page(
                target,
                (
                    f"{collection}&per_page={_PAGE_SIZE}&page=2"
                    if "?" in collection
                    else f"{collection}?per_page={_PAGE_SIZE}&page=2"
                ),
            )
        )
        if len(second) < _PAGE_SIZE:
            return _Collection([*first, *second], True)
        third = _list(
            GitLabMergeRequestProvider._get_glab_page(
                target,
                f"{collection}?per_page={_PAGE_SIZE}&page=3",
            )
        )
        return _Collection([*first, *second], not third)

    @staticmethod
    def _get_glab_page(target: GitLabMergeRequestTarget, endpoint: str) -> object:
        proc = run_provider_cli(
            "glab",
            ["api", endpoint, "--hostname", target.host],
            timeout=_TIMEOUT_SECS,
            credentials=(
                None if gitlab_ambient_token_allowed(target.host) else {"GITLAB_TOKEN": ""}
            ),
        )
        if proc.returncode != 0:
            error = (proc.stderr or "").lower()
            raise PullRequestProviderError(classify_provider_error_text(error))
        raw = proc.stdout or ""
        if len(raw.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            raise ValueError("GitLab response exceeds the monitor bound")
        return json.loads(raw)


def _facts(
    target: GitLabMergeRequestTarget,
    mr: Mapping[str, Any],
    pipelines: list[object],
    discussions: list[object],
    *,
    discussions_complete: bool = True,
) -> PullRequestFacts:
    raw_state = str(mr["state"]).lower()
    state = {"opened": "open", "merged": "merged", "closed": "closed"}.get(
        raw_state,
        "unknown",
    )
    detailed_merge_status = str(mr.get("detailed_merge_status") or "").lower()
    legacy_merge_status = str(mr.get("merge_status") or "").lower()
    merge_status = detailed_merge_status or legacy_merge_status
    if detailed_merge_status == "mergeable":
        mergeability = "mergeable"
    elif merge_status in {"conflict", "conflicts", "cannot_be_merged"}:
        mergeability = "conflicting"
    elif merge_status == "need_rebase":
        mergeability = "behind"
    else:
        mergeability = "pending"
    checks: list[PullRequestCheck] = []
    current_head_pipeline = False
    for item in pipelines[:_MAX_PROVIDER_ITEMS]:
        pipeline = _object(item)
        pipeline_sha = pipeline.get("sha")
        if isinstance(pipeline_sha, str) and pipeline_sha and pipeline_sha != mr.get("sha"):
            continue
        current_head_pipeline = True
        status = str(pipeline.get("status", "")).lower()
        if status in {"failed", "canceled"}:
            normalized = "failed"
        elif status in {"success", "skipped"}:
            normalized = "passed"
        elif status in {
            "running",
            "pending",
            "created",
            "preparing",
            "waiting_for_resource",
            "manual",
        }:
            normalized = "pending"
        else:
            normalized = "unknown"
        identity = opaque_provider_check_identity("pipeline", "current_head")
        checks.append(PullRequestCheck(identity, normalized))
        break
    if not current_head_pipeline:
        checks.append(
            PullRequestCheck(
                opaque_provider_check_identity("pipeline", "current_head"),
                "unknown",
            )
        )
    approved_by = mr.get("approved_by")
    if merge_status == "requested_changes":
        review_decision = "changes_requested"
    elif merge_status == "not_approved":
        review_decision = "review_required"
    elif isinstance(approved_by, list) and approved_by:
        review_decision = "approved"
    else:
        review_decision = "none"
    unresolved = 0
    for discussion_raw in discussions[:_MAX_PROVIDER_ITEMS]:
        discussion = _object(discussion_raw)
        notes = _list(discussion.get("notes", []))
        if any(
            _object(note_raw).get("resolvable") is True
            and _object(note_raw).get("resolved") is not True
            for note_raw in notes
        ):
            unresolved += 1
    return PullRequestFacts(
        kind="gitlab_merge_request",
        target=target.identity,
        state=state,
        draft=bool(mr.get("draft", mr.get("work_in_progress", False))),
        head_revision=str(mr.get("sha") or ""),
        mergeability=mergeability,
        review_decision=review_decision,
        checks=tuple(checks),
        unresolved_review_threads=unresolved,
        review_threads_complete=discussions_complete,
        checks_complete=current_head_pipeline,
    )


def _object(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("provider response must be an object")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("provider response must be a list")
    return value


def _collection(value: object) -> tuple[list[object], bool]:
    if isinstance(value, _Collection):
        return value.items, value.complete
    return _list(value), True
