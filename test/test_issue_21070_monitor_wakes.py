"""Regression tests for review-only GitHub PR monitor wakeups."""

from kiro_crew.monitoring.decision import decide_monitor, stamp_monitor_alerted
from kiro_crew.monitoring.github_pull_request import (
    GitHubCheck,
    _bounded_checks,
    _pr_comment_activity_page,
    _pr_comment_body_digest,
    _pr_review_activity_page,
)
from kiro_crew.monitoring.models import (
    DEFAULT_MONITOR_COALESCE_SECS,
    MonitorDecision,
    MonitorObservationStatus,
    MonitorState,
)
from kiro_crew.monitoring.pull_request import (
    PullRequestCheck,
    PullRequestFacts,
    build_pull_request_probe_result,
)

_HEAD = "0123456789abcdef0123456789abcdef01234567"
_TARGET = "https://github.com/owner/repo/pull/123"


def _facts(*, checks: tuple[PullRequestCheck, ...], **changes: object) -> PullRequestFacts:
    values: dict[str, object] = {
        "kind": "github_pull_request",
        "target": "github.com/owner/repo#123",
        "state": "open",
        "draft": False,
        "head_revision": _HEAD,
        "mergeability": "mergeable",
        "review_decision": "review_required",
        "checks": checks,
        "checks_complete": True,
        "unresolved_review_threads": 0,
        "review_threads_complete": True,
    }
    values.update(changes)
    return PullRequestFacts(**values)


def _state() -> MonitorState:
    return MonitorState(
        kind="github_pull_request",
        target=_TARGET,
        objective="review_ready",
        created_ts=1_000.0,
    )


def test_passing_check_overflow_is_complete_but_verdict_overflow_is_not() -> None:
    passing = tuple(GitHubCheck(f"check-{index}", "passed") for index in range(101))
    failed = tuple(GitHubCheck(f"check-{index}", "failed") for index in range(101))

    bounded_passed, passed_complete = _bounded_checks(passing)
    bounded_failed, failed_complete = _bounded_checks(failed)

    assert len(bounded_passed) == 100
    assert passed_complete is True
    assert len(bounded_failed) == 100
    assert failed_complete is False


def test_failed_and_pending_overflow_still_sets_the_incomplete_sentinel() -> None:
    for check_state in ("failed", "pending"):
        checks = tuple(PullRequestCheck(f"check-{index}", check_state) for index in range(101))
        result = build_pull_request_probe_result(_facts(checks=checks))

        assert result.canonical["checks_complete"] is False
        assert result.canonical["checks"]["unknown"] == ["checks:incomplete"]


def test_green_checks_waiting_for_review_wake_once_and_do_not_stall() -> None:
    state = _state()
    running = build_pull_request_probe_result(
        _facts(checks=(PullRequestCheck("ci", "pending"),))
    ).observation
    assert running.status is MonitorObservationStatus.PENDING
    assert decide_monitor(state, running, now=1_000.0).decision is MonitorDecision.RECORD_ONLY

    green = build_pull_request_probe_result(
        _facts(checks=(PullRequestCheck("ci", "passed"),))
    ).observation
    assert green.status is MonitorObservationStatus.PENDING
    assert decide_monitor(state, green, now=1_001.0).decision is MonitorDecision.WAKE_ACTIONABLE
    stamp_monitor_alerted(state, now=1_001.0)
    for tick in range(1, 15):
        verdict = decide_monitor(state, green, now=1_001.0 + tick * 300.0)
        assert verdict.decision is MonitorDecision.NO_CHANGE
        assert state.stall_streak == 0


def test_human_comment_on_a_pending_pr_wakes_once_but_own_and_bot_comments_do_not() -> None:
    state = _state()
    comments = {
        "comments": {
            "nodes": [
                {
                    "body": "please update the description",
                    "author": {"login": "reviewer", "__typename": "User"},
                }
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": "comment-end"},
        }
    }
    bodies, humans, complete, authors_complete, has_next, cursor, _end = _pr_comment_activity_page(
        comments, "pull-author"
    )
    assert complete and authors_complete and not has_next and cursor is None
    human_digest = _pr_comment_body_digest(humans)
    observation = build_pull_request_probe_result(
        _facts(
            checks=(PullRequestCheck("ci", "pending"),),
            pr_comment_body_digest=_pr_comment_body_digest(bodies),
            pr_human_comment_digest=human_digest,
        )
    ).observation

    assert (
        decide_monitor(state, observation, now=1_000.0).decision is MonitorDecision.WAKE_ACTIONABLE
    )
    stamp_monitor_alerted(state, now=1_000.0)
    assert decide_monitor(state, observation, now=1_001.0).decision is MonitorDecision.NO_CHANGE

    comments["comments"]["nodes"][0]["body"] = "please also update the summary"
    bodies, humans, *_ = _pr_comment_activity_page(comments, "pull-author")
    edited = build_pull_request_probe_result(
        _facts(
            checks=(PullRequestCheck("ci", "pending"),),
            pr_comment_body_digest=_pr_comment_body_digest(bodies),
            pr_human_comment_digest=_pr_comment_body_digest(humans),
        )
    ).observation
    assert edited.conditions[0].key != observation.conditions[0].key
    edit_time = 1_000.0 + DEFAULT_MONITOR_COALESCE_SECS + 1
    assert decide_monitor(state, edited, now=edit_time).decision is MonitorDecision.WAKE_ACTIONABLE
    stamp_monitor_alerted(state, now=edit_time)
    assert decide_monitor(state, edited, now=edit_time + 1).decision is MonitorDecision.NO_CHANGE

    for author in (
        {"login": "pull-author", "__typename": "User"},
        {"login": "review-bot[bot]", "__typename": "Bot"},
    ):
        page = {
            "comments": {
                "nodes": [{"body": "same comment body", "author": author}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
        body_fingerprints, human_fingerprints, *_ = _pr_comment_activity_page(page, "pull-author")
        assert human_fingerprints == []
        no_human_activity = build_pull_request_probe_result(
            _facts(
                checks=(PullRequestCheck("ci", "pending"),),
                pr_comment_body_digest=_pr_comment_body_digest(body_fingerprints),
            )
        ).observation
        assert decide_monitor(_state(), no_human_activity, now=1_000.0).decision is (
            MonitorDecision.RECORD_ONLY
        )


def test_human_review_wakes_a_pending_or_newly_ready_pr() -> None:
    reviews = {
        "reviews": {
            "nodes": [
                {
                    "body": "looks good",
                    "state": "APPROVED",
                    "author": {"login": "reviewer", "__typename": "User"},
                }
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    human_reviews, complete, authors_complete, has_next, cursor, _end = _pr_review_activity_page(
        reviews, "pull-author"
    )
    assert complete and authors_complete and not has_next and cursor is None
    digest = _pr_comment_body_digest(human_reviews)
    state = _state()
    pending = build_pull_request_probe_result(
        _facts(
            checks=(PullRequestCheck("ci", "pending"),),
            pr_human_review_digest=digest,
        )
    ).observation
    assert decide_monitor(state, pending, now=1_000.0).decision is MonitorDecision.WAKE_ACTIONABLE
    stamp_monitor_alerted(state, now=1_000.0)
    assert decide_monitor(state, pending, now=1_001.0).decision is MonitorDecision.NO_CHANGE

    ready = build_pull_request_probe_result(
        _facts(
            checks=(PullRequestCheck("ci", "passed"),),
            review_decision="approved",
            pr_human_review_digest=digest,
        )
    ).observation
    ready_state = _state()
    assert ready.status is MonitorObservationStatus.SUCCESS
    assert decide_monitor(ready_state, ready, now=1_000.0).decision is (
        MonitorDecision.WAKE_ACTIONABLE
    )
    stamp_monitor_alerted(ready_state, now=1_000.0)
    assert decide_monitor(ready_state, ready, now=1_001.0).decision is MonitorDecision.STOP_SUCCESS
