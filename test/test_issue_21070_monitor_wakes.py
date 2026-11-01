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


def test_passing_check_overflow_keeps_evidence_but_verdict_overflow_does_not() -> None:
    passing = tuple(GitHubCheck(f"check-{index}", "passed") for index in range(101))
    failed = tuple(GitHubCheck(f"check-{index}", "failed") for index in range(101))

    bounded_passed, passed_complete, passed_overflow = _bounded_checks(passing)
    bounded_failed, failed_complete, failed_overflow = _bounded_checks(failed)

    assert len(bounded_passed) == 100
    assert passed_complete is True
    assert passed_overflow is True
    assert len(bounded_failed) == 100
    assert failed_complete is False
    assert failed_overflow is False


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
    # What the driver persists between ticks: the recorded fingerprint.
    state.last_fingerprint = running.fingerprint

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


def test_a_watch_armed_on_an_already_green_pr_never_wakes_on_the_level() -> None:
    """The signals are levels, not edges: pre-existing ones are the baseline.

    A watch armed on a PR whose board is already complete -- with or without old
    human activity -- must not wake on its first reading. Only a level that
    CHANGES after the watch started (a new person's digest) is a wake.
    """
    old_digest = _pr_comment_body_digest(["f" * 64])
    state = _state()
    settled = build_pull_request_probe_result(
        _facts(
            checks=(PullRequestCheck("ci", "passed"),),
            pr_human_comment_digest=old_digest,
        )
    ).observation
    assert settled.status is MonitorObservationStatus.PENDING
    assert decide_monitor(state, settled, now=1_000.0).decision is MonitorDecision.NO_CHANGE
    state.last_fingerprint = settled.fingerprint
    # The baseline holds: the same level, read later, wakes nobody.
    assert decide_monitor(state, settled, now=1_000.0 + 600.0).decision is (
        MonitorDecision.NO_CHANGE
    )

    new_digest = _pr_comment_body_digest(["f" * 64, "e" * 64])
    commented = build_pull_request_probe_result(
        _facts(
            checks=(PullRequestCheck("ci", "passed"),),
            pr_human_comment_digest=new_digest,
        )
    ).observation
    assert decide_monitor(state, commented, now=1_000.0 + 1_200.0).decision is (
        MonitorDecision.WAKE_ACTIONABLE
    )


def test_a_watch_armed_on_an_already_approved_pr_stops_without_a_turn() -> None:
    """Terminal success carries no wake for activity that predates the watch."""
    digest = _pr_comment_body_digest(["a" * 64])
    state = _state()
    ready = build_pull_request_probe_result(
        _facts(
            checks=(PullRequestCheck("ci", "passed"),),
            review_decision="approved",
            pr_human_review_digest=digest,
        )
    ).observation
    assert ready.status is MonitorObservationStatus.SUCCESS
    assert decide_monitor(state, ready, now=1_000.0).decision is MonitorDecision.STOP_SUCCESS


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

    # The digest was readable at arm time, so it is the baseline, not a wake.
    assert decide_monitor(state, observation, now=1_000.0).decision is MonitorDecision.NO_CHANGE
    state.last_fingerprint = observation.fingerprint

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
    # The review already existed when the watch was armed: baseline, not a wake.
    assert decide_monitor(state, pending, now=1_000.0).decision is MonitorDecision.NO_CHANGE
    state.last_fingerprint = pending.fingerprint

    # A SECOND review lands during the watch: a new digest, so it wakes.
    reviews["reviews"]["nodes"].append(
        {
            "body": "one more thought",
            "state": "COMMENTED",
            "author": {"login": "peer", "__typename": "User"},
        }
    )
    human_reviews, *_ = _pr_review_activity_page(reviews, "pull-author")
    new_digest = _pr_comment_body_digest(human_reviews)
    assert new_digest != digest
    reviewed = build_pull_request_probe_result(
        _facts(
            checks=(PullRequestCheck("ci", "pending"),),
            pr_human_review_digest=new_digest,
        )
    ).observation
    assert decide_monitor(state, reviewed, now=1_100.0).decision is MonitorDecision.WAKE_ACTIONABLE
    stamp_monitor_alerted(state, now=1_100.0)

    ready = build_pull_request_probe_result(
        _facts(
            checks=(PullRequestCheck("ci", "passed"),),
            review_decision="approved",
            pr_human_review_digest=new_digest,
        )
    ).observation
    assert ready.status is MonitorObservationStatus.SUCCESS
    # The terminal success carries the same already-delivered digest, so the
    # condition is masked and the watch stops without another turn.
    assert decide_monitor(state, ready, now=1_101.0).decision is MonitorDecision.STOP_SUCCESS


def test_an_empty_check_board_is_not_a_completed_one() -> None:
    """A board with no rows at all is not "all checks completed successfully".

    GitHub often reports no checks for a moment after a push. Reading that empty
    board as complete would wake once on the push itself and mask the REAL
    settle on the same head, so the signal requires at least one passing row.
    """
    settled = build_pull_request_probe_result(_facts(checks=())).observation

    assert settled.status is MonitorObservationStatus.PENDING
    assert [
        condition.key for condition in settled.conditions if condition.key == "checks_complete"
    ] == []


def test_checks_finishing_on_a_pr_waiting_on_something_else_still_wake() -> None:
    """Checks finishing wake their session even when review is not the blocker.

    A repository with no required reviewers holds its PR at
    ``mergeability_pending``, not ``review_required``. The issue asks for a wake
    when the checks finish, so the signal must not be gated on the review
    decision.
    """
    settled = build_pull_request_probe_result(
        _facts(
            checks=(PullRequestCheck("ci", "passed"),),
            mergeability="blocked",
            review_decision="none",
        )
    ).observation

    assert settled.status is MonitorObservationStatus.PENDING
    assert settled.reason_code == "mergeability_pending"
    assert [condition.key for condition in settled.conditions] == ["checks_complete"]


def test_a_deleted_account_does_not_silence_person_wakes() -> None:
    """One unattributable comment must not blank the whole activity digest.

    A comment whose author is null (a deleted account) used to fail the page's
    author read and drop the digest to "" on every later read, so a real
    person's later comment could never wake. The unattributable comment is
    fingerprinted instead, so the digest stays defined and a new person's
    comment still moves it.
    """
    deleted_first = {
        "comments": {
            "nodes": [
                {"body": "kicked the builds", "author": None},
                {"body": "looks fine to me", "author": {"login": "peer", "__typename": "User"}},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    bodies, humans, complete, authors_complete, *_ = _pr_comment_activity_page(
        deleted_first, "pull-author"
    )
    assert complete is True and authors_complete is True
    assert len(humans) == 2
    digest_with_deleted = _pr_comment_body_digest(humans)
    assert digest_with_deleted

    new_comment = {
        "comments": {
            "nodes": [
                {"body": "kicked the builds", "author": None},
                {"body": "looks fine to me", "author": {"login": "peer", "__typename": "User"}},
                {
                    "body": "addressed, please re-review",
                    "author": {"login": "reviewer", "__typename": "User"},
                },
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    _bodies, humans_after, *_ = _pr_comment_activity_page(new_comment, "pull-author")
    digest_after = _pr_comment_body_digest(humans_after)
    assert digest_after != digest_with_deleted

    state = _state()
    before = build_pull_request_probe_result(
        _facts(
            checks=(PullRequestCheck("ci", "pending"),),
            pr_comment_body_digest=_pr_comment_body_digest(bodies),
            pr_human_comment_digest=digest_with_deleted,
        )
    ).observation
    assert decide_monitor(state, before, now=1_000.0).decision is MonitorDecision.NO_CHANGE
    state.last_fingerprint = before.fingerprint
    after = build_pull_request_probe_result(
        _facts(
            checks=(PullRequestCheck("ci", "pending"),),
            pr_human_comment_digest=digest_after,
        )
    ).observation
    assert decide_monitor(state, after, now=1_100.0).decision is MonitorDecision.WAKE_ACTIONABLE
