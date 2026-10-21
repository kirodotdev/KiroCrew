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
    comment_body_fingerprint,
    opaque_provider_check_identity,
    pr_comment_body_digest,
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
_CANONICAL_STATES = {"opened": "open", "merged": "merged", "closed": "closed"}
# Terminal means "no supplemental read can change the verdict", which is every
# mapped state except the open one. Deriving it keeps the two from drifting.
_TERMINAL_STATES = frozenset(
    raw for raw, canonical in _CANONICAL_STATES.items() if canonical != "open"
)

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
            target = parse_gitlab_merge_request_target(
                raw_target,
                gitlab_hosts=self._gitlab_hosts(),
            )
            if self._fetch is None and not use_owner_credentials:
                audit_provider_cli_denied("glab")
                return provider_error_result(
                    ProviderErrorKind.AUTHORIZATION,
                    "provider_authorization",
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
                notes: list[object] = []
                notes_complete = True
            else:
                head_pipeline = mr.get("head_pipeline")
                pipelines = [] if head_pipeline is None else [_object(head_pipeline)]
                discussions, discussions_complete = _collection(fetch("discussions"))
                notes, notes_complete = _read_comments(fetch)
            facts = _facts(
                target,
                mr,
                pipelines,
                discussions,
                notes,
                discussions_complete=discussions_complete,
                notes_complete=notes_complete,
            )
        except PullRequestProviderError as exc:
            return provider_failure_result(exc)
        except PermissionError:
            return provider_error_result(
                ProviderErrorKind.AUTHENTICATION,
                "provider_authentication",
            )
        except GitLabHostNotAllowed:
            audit_provider_cli_denied("glab", reason="host_not_allowlisted")
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
        elif resource == "comments":
            # MR notes are the surface a review bot's verdict comment lives on,
            # the GitLab analogue of GitHub's PR-level issue comments. Paginated
            # with the exact same page-size and three-page bound as discussions.
            collection = f"{root}/notes"
        else:
            raise ValueError("unsupported GitLab monitor resource")
        return GitLabMergeRequestProvider._fetch_collection_pages(target, collection)

    @staticmethod
    def _fetch_collection_pages(
        target: GitLabMergeRequestTarget,
        collection: str,
    ) -> _Collection:
        """Walk one list endpoint under the shared page-size and page bound.

        Up to three pages: a short page ends the walk complete, and a third page
        is read only to learn whether anything is left beyond the second -- the
        items past the second page are never retained, so the retained set is
        bounded at ``_MAX_PROVIDER_ITEMS`` whatever the collection's true size.
        One walk for discussions and notes alike keeps their paging from
        drifting apart.
        """

        def page(number: int) -> list[object]:
            separator = "&" if "?" in collection else "?"
            return _list(
                GitLabMergeRequestProvider._get_glab_page(
                    target,
                    f"{collection}{separator}per_page={_PAGE_SIZE}&page={number}",
                )
            )

        first = page(1)
        if len(first) < _PAGE_SIZE:
            return _Collection(first, True)
        second = page(2)
        if len(second) < _PAGE_SIZE:
            return _Collection([*first, *second], True)
        third = page(3)
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
    notes: list[object],
    *,
    discussions_complete: bool = True,
    notes_complete: bool = True,
) -> PullRequestFacts:
    raw_state = str(mr["state"]).lower()
    state = _CANONICAL_STATES.get(raw_state, "unknown")
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
        discussion_notes = _list(discussion.get("notes", []))
        if any(
            _object(note_raw).get("resolvable") is True
            and _object(note_raw).get("resolved") is not True
            for note_raw in discussion_notes
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
        pr_comment_body_digest=_comment_body_digest(notes, notes_complete),
    )


def _comment_body_digest(notes: list[object], notes_complete: bool) -> str:
    """Digest the MR's human note bodies, or "" when the read was incomplete.

    MR notes are GitLab's PR-level comments -- the surface a review bot rewrites
    its verdict on in place, with ``created_at`` frozen, so only a digest over
    the bodies sees that change. Mirrors the GitHub adapter: each body is reduced
    to a fixed-width fingerprint at the point of retention through the shared
    helper, and the aggregate digest is built the same way, so the two providers'
    digests cannot drift.

    ``system: true`` notes are EXCLUDED. GitLab emits a system note for its own
    activity -- "approved this merge request", "added N commits", "changed the
    description", label and milestone edits -- none of which is a review remark a
    human or bot wrote, and all of which move on routine churn a comment-body
    wake must not fire on. The resolvable review remarks that carry a verdict are
    user notes (``system: false``), so dropping system notes keeps exactly the
    bodies this wake exists for.

    Fail-closed: an incomplete read yields "" so no condition is emitted and a
    page that keeps failing cannot wake the owner forever. A malformed note is
    treated as an incomplete read for the same reason, mirroring the GitHub page
    reader that marks a page incomplete on a malformed comment.
    """
    if not notes_complete:
        return ""
    fingerprints: list[str] = []
    for note_raw in notes[:_MAX_PROVIDER_ITEMS]:
        if not isinstance(note_raw, Mapping):
            return ""
        if note_raw.get("system") is True:
            continue
        body = note_raw.get("body")
        if not isinstance(body, str):
            return ""
        if not body:
            continue
        fingerprints.append(comment_body_fingerprint(body))
    return pr_comment_body_digest(fingerprints)


def _read_comments(fetch: Callable[[str], object]) -> tuple[list[object], bool]:
    """Read the MR notes, isolating any failure to the comment digest alone.

    A failed, timed-out or malformed comment read must NOT fail the whole probe:
    the comment-body digest is a per-condition wake signal with no bearing on
    review-readiness, so a refused read drops the digest and nothing else. This
    mirrors the GitHub adapter, where a comment-read failure yields "" and never a
    provider error. Every failure class ``_get_glab_page`` or ``_collection`` can
    raise is caught here and reported as an INCOMPLETE read, which ``_facts`` turns
    into an empty digest (fail-closed), so a page that keeps failing cannot wake
    the owner forever.
    """
    try:
        return _collection(fetch("comments"))
    except (
        PullRequestProviderError,
        subprocess.TimeoutExpired,
        OSError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return [], False


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
