"""Azure DevOps review-comment-body digest: the wake on an edited verdict.

The Azure DevOps adapter nests comments inside PR threads, which it already
fetches for the unresolved-thread count, so the comment-body digest is read from
that same list rather than through a second request that could observe a
different head. The digest is over BODIES, so a verdict comment edited in place
-- same id, frozen ``publishedDate`` -- still flips it, which a count or a
newest-date probe cannot see. These tests pin that wake and its fail-closed
edges. Kept in its own module so the main provider-monitor suite stays under the
2,000-line file cap.
"""

import pytest

from kiro_crew.monitoring.azure_devops_pull_request import AzureDevOpsPullRequestProvider
from kiro_crew.monitoring.models import MonitorObservationStatus

_TARGET = "https://dev.azure.com/acme/project/_git/widgets/pullrequest/9"


@pytest.fixture(autouse=True)
def _isolate_azure_cli_config_dirs(_floor_monkeypatch):
    """Provider tests must not inherit Azure CLI paths from the CI host.

    Patches through ``_floor_monkeypatch`` (D11), not the shared ``monkeypatch``,
    so a sibling test's ``monkeypatch.undo()`` cannot lift this autouse isolation.
    """
    monkeypatch = _floor_monkeypatch
    monkeypatch.delenv("AZURE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("AZURE_EXTENSION_DIR", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)


def _comment_condition_key(result):
    """The one review_comment_bodies condition key on a result, or None."""
    keys = [
        condition.key
        for condition in result.observation.conditions
        if condition.key.startswith("review_comment_bodies:")
    ]
    assert len(keys) <= 1, "at most one comment-digest condition per subject"
    return keys[0] if keys else None


def _payloads(threads_value, *, threads_complete=True):
    threads_payload = {"value": threads_value}
    if not threads_complete:
        threads_payload["continuationToken"] = "more"
    return {
        "pull_request": {
            "status": "active",
            "isDraft": False,
            "repository": {"name": "widgets", "project": {"name": "project"}},
            "lastMergeSourceCommit": {"commitId": "def"},
            "mergeStatus": "succeeded",
            "reviewers": [],
        },
        # A failing status drives the subject ACTIONABLE, so its conditions are
        # carried and the comment digest rides alongside -- the same shape the
        # GitHub suite uses, where conditions ride only on an ACTIONABLE subject.
        "statuses": {"value": [{"context": {"name": "build"}, "state": "failed"}]},
        "threads": threads_payload,
        "policies": {"value": []},
    }


def _probe_threads(threads_value, *, threads_complete=True):
    payloads = _payloads(threads_value, threads_complete=threads_complete)
    provider = AzureDevOpsPullRequestProvider(fetch=lambda _target, resource: payloads[resource])
    return provider.probe((_TARGET,))[_TARGET]


def test_a_comment_body_change_flips_the_digest_and_wakes():
    before = _probe_threads(
        [{"status": "closed", "comments": [{"content": "no blocking findings"}]}]
    )
    after = _probe_threads(
        [{"status": "closed", "comments": [{"content": "blocking: found an issue"}]}]
    )
    assert before.observation.status is MonitorObservationStatus.ACTIONABLE
    assert before.canonical["pr_comment_body_digest"]
    assert before.canonical["pr_comment_body_digest"] != after.canonical["pr_comment_body_digest"]
    key_before = _comment_condition_key(before)
    key_after = _comment_condition_key(after)
    assert key_before is not None and key_after is not None
    assert key_before != key_after


def test_an_in_place_edit_with_unchanged_dates_still_flips_the_digest():
    """Same comment id and publishedDate, body rewritten: a count or a
    newest-date probe is blind to it, only the body digest sees it."""
    frozen = {"id": 7, "publishedDate": "2026-01-01T00:00:00Z"}
    before = _probe_threads([{"status": "closed", "comments": [{**frozen, "content": "LGTM"}]}])
    after = _probe_threads(
        [{"status": "closed", "comments": [{**frozen, "content": "changes requested: fix it"}]}]
    )
    assert _comment_condition_key(before) != _comment_condition_key(after)


def test_unchanged_comments_keep_the_same_digest_and_do_not_wake_again():
    threads = [{"status": "closed", "comments": [{"content": "ship it"}]}]
    first = _probe_threads(threads)
    second = _probe_threads(threads)
    assert first.canonical["pr_comment_body_digest"] == second.canonical["pr_comment_body_digest"]
    assert _comment_condition_key(first) == _comment_condition_key(second)


def test_an_incomplete_threads_read_yields_an_empty_digest_and_no_wake():
    """Fail closed: a continuation token means the thread list was truncated, so
    the comment view is partial and a page that keeps failing cannot wake the
    owner forever."""
    result = _probe_threads(
        [{"status": "closed", "comments": [{"content": "partial view"}]}],
        threads_complete=False,
    )
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert "pr_comment_body_digest" not in result.canonical
    assert _comment_condition_key(result) is None


def test_a_failed_threads_read_yields_an_empty_digest_and_no_wake():
    """A supplemental read that raises is typed as a provider error with no
    facts, so no digest and no comment condition reach the owner."""
    payloads = _payloads([{"status": "closed", "comments": [{"content": "x"}]}])

    def fetch(_target, resource):
        if resource == "threads":
            raise PermissionError("threads denied")
        return payloads[resource]

    provider = AzureDevOpsPullRequestProvider(fetch=fetch)
    result = provider.probe((_TARGET,))[_TARGET]
    assert result.observation.status is MonitorObservationStatus.PROVIDER_ERROR
    assert "pr_comment_body_digest" not in result.canonical
    assert _comment_condition_key(result) is None


def test_comments_across_multiple_threads_are_all_digested():
    """Every thread's comments feed one digest; adding a comment in any thread
    flips it."""
    before = _probe_threads(
        [
            {"status": "closed", "comments": [{"content": "alpha"}]},
            {"status": "closed", "comments": [{"content": "beta"}]},
        ]
    )
    after = _probe_threads(
        [
            {"status": "closed", "comments": [{"content": "alpha"}]},
            {"status": "closed", "comments": [{"content": "beta"}, {"content": "gamma"}]},
        ]
    )
    assert _comment_condition_key(before) != _comment_condition_key(after)


def test_the_digest_is_independent_of_thread_and_comment_order():
    """Fingerprints are sorted before hashing, so the order threads and comments
    arrive in cannot move the digest."""
    first = _probe_threads(
        [
            {"status": "closed", "comments": [{"content": "a"}, {"content": "b"}]},
            {"status": "closed", "comments": [{"content": "c"}]},
        ]
    )
    second = _probe_threads(
        [
            {"status": "closed", "comments": [{"content": "c"}, {"content": "b"}]},
            {"status": "closed", "comments": [{"content": "a"}]},
        ]
    )
    assert first.canonical["pr_comment_body_digest"] == second.canonical["pr_comment_body_digest"]


def test_system_comments_are_excluded_from_the_digest():
    """Azure's own lifecycle narration ('voted', 'updated') is commentType
    system; it turns over on its own and must not wake the watch."""
    with_text = _probe_threads(
        [{"status": "closed", "comments": [{"content": "real review", "commentType": "text"}]}]
    )
    plus_system = _probe_threads(
        [
            {
                "status": "closed",
                "comments": [
                    {"content": "real review", "commentType": "text"},
                    {"content": "Kyle voted 10", "commentType": "system"},
                ],
            }
        ]
    )
    assert (
        with_text.canonical["pr_comment_body_digest"]
        == plus_system.canonical["pr_comment_body_digest"]
    )


def test_empty_bodies_and_no_comments_emit_no_condition():
    """A thread with no readable prose contributes nothing, so a pull request
    with only empty or commentless threads emits no comment condition."""
    result = _probe_threads(
        [
            {"status": "closed", "comments": [{"content": ""}]},
            {"status": "closed"},
        ]
    )
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert "pr_comment_body_digest" not in result.canonical
    assert _comment_condition_key(result) is None


def test_a_malformed_comment_fails_closed_to_no_digest():
    """A non-string body means the view cannot be trusted whole, so the whole
    digest is withheld rather than built from a partial read."""
    result = _probe_threads([{"status": "closed", "comments": [{"content": 12345}]}])
    assert "pr_comment_body_digest" not in result.canonical
    assert _comment_condition_key(result) is None


def test_a_huge_comment_body_costs_a_fixed_width_fingerprint():
    """Retention is bounded at the comment: a megabyte body and a tiny one both
    reduce to one 64-char fingerprint, and the body never reaches the condition
    key."""
    marker = "UNIQUE-AZURE-BODY-MARKER-9c1d"
    result = _probe_threads(
        [{"status": "closed", "comments": [{"content": marker + "y" * 1_000_000}]}]
    )
    key = _comment_condition_key(result)
    assert key is not None
    assert marker not in key
    assert len(key) < 200
