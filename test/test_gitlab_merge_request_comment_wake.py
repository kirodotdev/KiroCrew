"""GitLab MR review-comment-body wake.

The GitLab adapter digests MR note bodies so that a review bot's verdict comment
rewritten IN PLACE -- `created_at` frozen, body changed -- still wakes the owner,
mirroring the GitHub PR-level comment digest. These tests cover the correctness
bar: a body change flips the digest and wakes, an in-place edit flips it too, an
incomplete read fails closed, unchanged notes do not re-wake, GitLab `system`
activity notes are excluded, and the digest is independent of note page order.
"""

import json
import subprocess

from kiro_crew.monitoring.gitlab_merge_request import GitLabMergeRequestProvider
from kiro_crew.monitoring.models import MonitorObservationStatus

_SUBJECT = "https://gitlab.com/acme/widgets/-/merge_requests/8"


def _probe(provider, target=_SUBJECT, **kwargs):
    return provider.probe((target,), **kwargs)[target]


def _comment_condition_key(result):
    """The one PR-level-comment-body condition key on a GitLab result, or None."""
    keys = [
        condition.key
        for condition in result.observation.conditions
        if condition.key.startswith("review_comment_bodies:")
    ]
    assert len(keys) <= 1, "at most one comment-digest condition per subject"
    return keys[0] if keys else None


def _comment_payloads(notes):
    """A live, ACTIONABLE GitLab MR carrying the given MR notes.

    The pipeline fails so the subject reaches ACTIONABLE and its conditions are
    carried -- the comment-body digest rides alongside, exactly as it does on a
    GitHub subject made actionable by a failing check.
    """
    return {
        "merge_request": {
            "state": "opened",
            "draft": False,
            "sha": "c0ffee",
            "detailed_merge_status": "checking",
            "head_pipeline": {"id": 2, "sha": "c0ffee", "status": "failed"},
        },
        "discussions": [],
        "comments": notes,
    }


def _probe_notes(notes):
    provider = GitLabMergeRequestProvider(
        gitlab_hosts=[],
        fetch=lambda _target, resource: _comment_payloads(notes)[resource],
    )
    return _probe(provider)


def test_gitlab_comment_body_change_flips_the_digest_and_wakes():
    """(a) A note body change flips the digest and produces a wake condition."""
    before = _probe_notes([{"system": False, "body": "no blocking findings"}])
    after = _probe_notes([{"system": False, "body": "blocking: found an issue"}])

    key_before = _comment_condition_key(before)
    key_after = _comment_condition_key(after)
    assert key_before is not None and key_after is not None
    assert key_before != key_after
    assert before.canonical["pr_comment_body_digest"] != after.canonical["pr_comment_body_digest"]


def test_gitlab_in_place_edit_with_unchanged_created_at_still_flips_the_digest():
    """(b) An in-place edit (created_at frozen) still flips the digest: only a
    digest over the bodies, not a timestamp, can see it."""
    frozen = "2026-01-01T00:00:00Z"
    before = _probe_notes([{"system": False, "body": "verdict: pending", "created_at": frozen}])
    after = _probe_notes([{"system": False, "body": "verdict: approved", "created_at": frozen}])
    assert _comment_condition_key(before) != _comment_condition_key(after)


def test_gitlab_incomplete_notes_read_yields_empty_digest_and_no_wake(monkeypatch):
    """(c) A failed/partial notes read digests nothing: fail-closed. The refused
    comment read must not abort the whole probe, only drop the digest."""
    endpoints: list[str] = []

    def fetch_fails_notes(_executable, argv, **_kwargs):
        endpoint = argv[1]
        endpoints.append(endpoint)
        if endpoint.endswith("merge_requests/8"):
            payload = {
                "state": "opened",
                "draft": False,
                "sha": "c0ffee",
                "detailed_merge_status": "checking",
                "head_pipeline": {"id": 2, "sha": "c0ffee", "status": "failed"},
            }
            return subprocess.CompletedProcess(["glab"], 0, stdout=json.dumps(payload), stderr="")
        if "/notes?" in endpoint:
            return subprocess.CompletedProcess(["glab"], 1, stdout="", stderr="HTTP 403 forbidden")
        return subprocess.CompletedProcess(["glab"], 0, stdout=json.dumps([]), stderr="")

    monkeypatch.setattr(
        "kiro_crew.monitoring.gitlab_merge_request.run_provider_cli",
        fetch_fails_notes,
    )
    result = _probe(GitLabMergeRequestProvider(gitlab_hosts=[]))

    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert "pr_comment_body_digest" not in result.canonical
    assert _comment_condition_key(result) is None
    assert sum("/notes?" in endpoint for endpoint in endpoints) >= 1


def test_gitlab_unchanged_notes_keep_the_same_digest_and_do_not_re_wake():
    """(d) Unchanged notes yield the same digest, so no new condition key."""
    notes = [
        {"system": False, "body": "first remark"},
        {"system": False, "body": "second remark"},
    ]
    first = _probe_notes(list(notes))
    second = _probe_notes(list(notes))
    assert _comment_condition_key(first) is not None
    assert _comment_condition_key(first) == _comment_condition_key(second)
    assert first.canonical["pr_comment_body_digest"] == second.canonical["pr_comment_body_digest"]


def test_gitlab_system_notes_are_excluded_from_the_comment_digest():
    """GitLab emits a system note for its own activity (approvals, commit adds,
    description edits); those are not review remarks and must not wake. A board
    with ONLY system notes carries no comment digest at all."""
    only_system = _probe_notes(
        [
            {"system": True, "body": "approved this merge request"},
            {"system": True, "body": "added 3 commits"},
        ]
    )
    assert "pr_comment_body_digest" not in only_system.canonical
    assert _comment_condition_key(only_system) is None

    # Adding a system note beside a human note does not move the digest: the
    # system note is dropped before the body is fingerprinted.
    human_only = _probe_notes([{"system": False, "body": "please rebase"}])
    human_plus_system = _probe_notes(
        [
            {"system": False, "body": "please rebase"},
            {"system": True, "body": "added 1 commit"},
        ]
    )
    assert (
        human_only.canonical["pr_comment_body_digest"]
        == human_plus_system.canonical["pr_comment_body_digest"]
    )


def test_gitlab_comment_digest_is_independent_of_note_page_order(monkeypatch):
    """(e) Pagination across >1 page: the digest sorts bodies before hashing, so
    the order the two pages arrive in cannot move it."""

    def make_fetch(first_body, second_body):
        def fetch(_executable, argv, **_kwargs):
            endpoint = argv[1]
            if endpoint.endswith("merge_requests/8"):
                payload: object = {
                    "state": "opened",
                    "draft": False,
                    "sha": "c0ffee",
                    "detailed_merge_status": "checking",
                    "head_pipeline": {"id": 2, "sha": "c0ffee", "status": "failed"},
                }
            elif "/discussions?" in endpoint:
                payload = []
            elif "/notes?" in endpoint and endpoint.endswith("page=1"):
                payload = [{"system": False, "body": first_body}] + [
                    {"system": False, "body": f"filler {index}"} for index in range(99)
                ]
            elif "/notes?" in endpoint and endpoint.endswith("page=2"):
                payload = [{"system": False, "body": second_body}]
            else:
                payload = []
            return subprocess.CompletedProcess(["glab"], 0, stdout=json.dumps(payload), stderr="")

        return fetch

    monkeypatch.setattr(
        "kiro_crew.monitoring.gitlab_merge_request.run_provider_cli",
        make_fetch("alpha", "omega"),
    )
    first = _probe(GitLabMergeRequestProvider(gitlab_hosts=[]))
    monkeypatch.setattr(
        "kiro_crew.monitoring.gitlab_merge_request.run_provider_cli",
        make_fetch("omega", "alpha"),
    )
    second = _probe(GitLabMergeRequestProvider(gitlab_hosts=[]))
    assert _comment_condition_key(first) is not None
    assert _comment_condition_key(first) == _comment_condition_key(second)


def test_gitlab_terminal_mr_reads_no_comments():
    """A merged or closed MR issues no supplemental read, comments included."""
    calls: list[str] = []

    def fetch(_target, resource):
        calls.append(resource)
        if resource != "merge_request":
            raise AssertionError("terminal MR must not read supplemental endpoints")
        return {
            "state": "merged",
            "draft": False,
            "sha": "c0ffee",
            "detailed_merge_status": "mergeable",
        }

    provider = GitLabMergeRequestProvider(gitlab_hosts=[], fetch=fetch)
    result = _probe(provider)

    assert result.observation.status is MonitorObservationStatus.SUCCESS
    assert calls == ["merge_request"]
    assert "pr_comment_body_digest" not in result.canonical
