"""A structured watch on a pull request that waits on people wakes on edges, not levels.

Every tick here runs the real GitHub probe, fed by a fake ``gh`` that answers each
document from one mutable pull request by what the document selects, then the real
decision engine, and persists what the production driver persists after a
decision: the canonical reading, its fingerprint and a wake's re-alert stamp.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field

from github_pull_request_monitor_helpers import (
    _HEAD,
    _check_run,
    _envelope,
    _pr_node,
    _primary,
    _rollup_node,
    _threads_node,
)

from kiro_crew.monitoring import models
from kiro_crew.monitoring.decision import decide_monitor, stamp_monitor_alerted
from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProvider
from kiro_crew.monitoring.models import (
    MonitorBudgets,
    MonitorCondition,
    MonitorDecision,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorState,
    ProviderErrorKind,
)
from kiro_crew.monitoring.pull_request import (
    PullRequestCheck,
    PullRequestFacts,
    PullRequestProbeResult,
    build_pull_request_probe_result,
)

_TARGET = "https://github.com/owner/repo/pull/123"
_NEW_HEAD = "fedcba9876543210fedcba9876543210fedcba98"
_CADENCE = 300.0
_PERSON = {"__typename": "User", "login": "a-maintainer"}
_BOT = {"__typename": "Bot", "login": "github-actions"}
_AUTHOR_LOGIN = "pr-author"

WAKE = MonitorDecision.WAKE_ACTIONABLE


def _run(state: str) -> dict[str, object]:
    if state == "pending":
        return _check_run(status="IN_PROGRESS", conclusion="")
    return _check_run(conclusion="SUCCESS" if state == "passed" else "FAILURE")


@dataclass
class _PullRequest:
    """What the fake host serves, changed by a test between ticks."""

    head: str = _HEAD
    draft: bool = False
    review_decision: str = "REVIEW_REQUIRED"
    merge_state: str = "BLOCKED"
    checks: list[str] = field(default_factory=lambda: ["pending", "pending"])
    comments: list[dict[str, object]] = field(default_factory=list)
    reviews: list[dict[str, object]] = field(default_factory=list)
    comments_unreadable: bool = False
    checks_unreadable: bool = False

    def settle(self, state: str = "passed") -> None:
        self.checks = [state] * len(self.checks)


class _Host:
    def __init__(self, pr: _PullRequest) -> None:
        self.pr = pr

    def __call__(self, argv: Sequence[str], **_: object) -> subprocess.CompletedProcess[str]:
        document = next(arg[len("query=") :] for arg in argv if arg.startswith("query="))
        pr = self.pr
        payload = _primary(
            headRefOid=pr.head,
            isDraft=pr.draft,
            reviewDecision=pr.review_decision,
            mergeStateStatus=pr.merge_state,
            statusCheckRollup=[
                {**_run(state), "name": f"lane-{index}"} for index, state in enumerate(pr.checks)
            ],
        )
        if "statusCheckRollup" in document and pr.checks_unreadable:
            return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr="HTTP 502")
        if "statusCheckRollup" in document:
            body = _envelope(_rollup_node(payload))
        elif "reviewThreads" in document:
            body = _envelope(_threads_node([]))
        elif "comments(" in document:
            node: dict[str, object] = {
                "author": {"login": _AUTHOR_LOGIN},
                "comments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": deepcopy(pr.comments),
                },
            }
            if "reviews(" in document:
                node["reviews"] = {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": deepcopy(pr.reviews),
                }
            body = _envelope(None if pr.comments_unreadable else node)
        else:
            body = _envelope(_pr_node(payload))
        return subprocess.CompletedProcess(list(argv), 0, stdout=json.dumps(body), stderr="")


class _Watch:
    """One armed watch: probe, decide and persist, as one production tick does."""

    def __init__(self, pr: _PullRequest) -> None:
        self.provider = GitHubPullRequestProvider(resolver=lambda: "gh", runner=_Host(pr))
        self.state = MonitorState(
            kind="github_pull_request",
            target=_TARGET,
            objective="review_ready",
            created_ts=0.0,
            budgets=MonitorBudgets(max_runtime_secs=86_400),
            cadence_secs=int(_CADENCE),
        )
        self.now = 0.0
        self.reason = ""

    def tick(self, *, after: float = _CADENCE) -> MonitorDecision:
        self.now += after
        state = self.state
        result = self.provider.probe(
            (_TARGET,), previous_observations={_TARGET: deepcopy(state.last_observation)}
        )[_TARGET]
        decision = decide_monitor(state, result.observation, now=self.now).decision
        if result.observation.status is not MonitorObservationStatus.PROVIDER_ERROR:
            state.last_observation = deepcopy(result.canonical)
            state.last_fingerprint = result.observation.fingerprint
        if decision is WAKE:
            state.last_wake_reason_code = result.observation.reason_code
            stamp_monitor_alerted(state, now=self.now)
        self.reason = result.observation.reason_code
        return decision

    def ticks(self, count: int) -> list[MonitorDecision]:
        return [self.tick() for _ in range(count)]


def test_checks_finishing_green_wake_once_and_never_re_assert() -> None:
    pr = _PullRequest()
    watch = _Watch(pr)
    assert WAKE not in watch.ticks(2)
    pr.settle()
    assert watch.tick() is WAKE
    assert watch.reason == "review_required"
    quiet = [*watch.ticks(3), watch.tick(after=models.DEFAULT_MONITOR_REALERT_SECS + 1)]
    assert quiet == [MonitorDecision.NO_CHANGE] * 4
    assert watch.state.stall_streak == 0


def test_checks_finishing_on_a_pr_blocked_on_something_other_than_review_wake() -> None:
    pr = _PullRequest(review_decision="")
    watch = _Watch(pr)
    watch.tick()
    pr.settle()
    assert watch.tick() is WAKE
    assert watch.reason == "mergeability_pending"


def test_a_watch_armed_on_a_green_pr_under_an_old_person_comment_does_not_wake() -> None:
    pr = _PullRequest(checks=["passed"], comments=[{"body": "an old note", "author": _PERSON}])
    watch = _Watch(pr)
    assert WAKE not in watch.ticks(4)


def test_a_person_comment_wakes_once_and_bots_and_the_author_never_do() -> None:
    pr = _PullRequest(checks=["passed"])
    watch = _Watch(pr)
    watch.tick()
    pr.comments.append({"body": "verdict v1", "author": _BOT})
    pr.comments.append({"body": "rebased", "author": {"__typename": "User", "login": "PR-Author"}})
    assert watch.tick() is not WAKE
    pr.comments.append({"body": "hold: please wait for the design review", "author": _PERSON})
    assert watch.tick() is WAKE
    pr.comments[0]["body"] = "verdict v2"
    assert WAKE not in watch.ticks(3)


def test_a_person_review_with_a_body_wakes_and_one_without_does_not() -> None:
    pr = _PullRequest(checks=["passed"])
    watch = _Watch(pr)
    watch.tick()
    pr.reviews.append({"body": "", "author": _PERSON})
    assert watch.tick() is not WAKE
    pr.reviews.append({"body": "please split this", "author": _PERSON})
    assert watch.tick() is WAKE


def test_a_deleted_account_does_not_silence_people() -> None:
    pr = _PullRequest(checks=["passed"], comments=[{"body": "gone", "author": None}])
    watch = _Watch(pr)
    watch.tick()
    pr.comments.append({"body": "a question", "author": _PERSON})
    assert watch.tick() is WAKE


def test_a_comment_posted_while_the_read_fails_wakes_once_it_recovers() -> None:
    pr = _PullRequest(checks=["passed"], comments=[{"body": "first", "author": _PERSON}])
    watch = _Watch(pr)
    watch.tick()
    pr.comments_unreadable = True
    assert watch.tick() is not WAKE
    pr.comments_unreadable = False
    assert watch.tick() is not WAKE
    pr.comments_unreadable = True
    pr.comments.append({"body": "second", "author": _PERSON})
    assert watch.tick() is not WAKE
    pr.comments_unreadable = False
    assert watch.tick() is WAKE


def test_an_empty_board_after_a_push_is_not_settled() -> None:
    pr = _PullRequest(checks=["passed"])
    watch = _Watch(pr)
    watch.tick()
    pr.head, pr.checks = _NEW_HEAD, []
    assert watch.tick() is not WAKE
    pr.checks = ["pending", "pending"]
    assert watch.tick() is not WAKE
    pr.settle()
    assert watch.tick() is WAKE


def test_a_failure_right_after_a_settle_wake_wakes_at_once() -> None:
    pr = _PullRequest()
    watch = _Watch(pr)
    watch.tick()
    pr.settle()
    assert watch.tick() is WAKE
    pr.checks[0] = "failed"
    assert watch.tick(after=15.0) is WAKE
    assert watch.reason == "checks_failed"


def test_a_draft_settling_does_not_wake() -> None:
    pr = _PullRequest(draft=True)
    watch = _Watch(pr)
    watch.tick()
    pr.settle()
    assert WAKE not in watch.ticks(3)


def test_more_than_100_passing_checks_finishing_wake_once() -> None:
    pr = _PullRequest(checks=["pending"] * 112)
    watch = _Watch(pr)
    watch.tick()
    pr.settle()
    assert watch.tick() is WAKE
    assert WAKE not in watch.ticks(2)


def test_a_draft_marked_ready_wakes_once_its_re_run_checks_finish_green() -> None:
    pr = _PullRequest(draft=True)
    watch = _Watch(pr)
    watch.tick()
    pr.settle()
    watch.tick()
    pr.draft, pr.checks[0] = False, "pending"
    assert watch.tick() is not WAKE
    pr.settle()
    assert watch.tick() is WAKE
    assert WAKE not in watch.ticks(2)


def test_a_failed_checks_read_does_not_wake_again_for_the_same_green_board() -> None:
    pr = _PullRequest()
    watch = _Watch(pr)
    watch.tick()
    pr.settle()
    assert watch.tick() is WAKE
    pr.checks_unreadable = True
    assert watch.tick() is MonitorDecision.RETRY_PROVIDER
    pr.checks_unreadable = False
    assert WAKE not in watch.ticks(2)


def test_green_checks_re_running_on_the_same_head_wake_only_after_a_failure() -> None:
    pr = _PullRequest()
    watch = _Watch(pr)
    watch.tick()
    pr.settle()
    assert watch.tick() is WAKE
    pr.checks[0] = "pending"
    assert watch.tick() is not WAKE
    pr.settle()
    assert watch.tick() is not WAKE
    pr.checks[0] = "failed"
    assert watch.tick() is WAKE
    pr.checks[0] = "pending"
    watch.tick()
    pr.settle()
    assert watch.tick() is WAKE


def test_a_permanent_supplemental_failure_still_stops_on_an_edge_tick() -> None:
    state = MonitorState(
        kind="github_pull_request",
        target=_TARGET,
        objective="review_ready",
        created_ts=0.0,
    )
    observation = MonitorObservation(
        "fingerprint",
        MonitorObservationStatus.PENDING,
        supplemental_provider_error=ProviderErrorKind.AUTHORIZATION,
        reason_code="review_threads_incomplete",
        conditions=(MonitorCondition(key="checks_settled"),),
    )
    assert decide_monitor(state, observation, now=1.0).decision is MonitorDecision.STOP_BLOCKED


def test_every_provider_shares_the_checks_wake() -> None:
    def reading(state: str, previous: dict[str, object] | None) -> PullRequestProbeResult:
        facts = PullRequestFacts(
            kind="gitlab_merge_request",
            target="gitlab.com/group/project!7",
            state="open",
            draft=False,
            head_revision=_HEAD,
            mergeability="blocked",
            review_decision="review_required",
            checks=(PullRequestCheck(identity="pipeline", state=state),),
            unresolved_review_threads=0,
            review_threads_complete=True,
        )
        return build_pull_request_probe_result(facts, previous_observation=previous)

    pending = reading("pending", None)
    settled = reading("passed", pending.canonical)
    assert [c.key for c in settled.observation.conditions] == ["checks_settled"]
