"""Behavioural tests for .github/workflows/pr-readiness-sweep.yml.

The sweep's entire decision logic lives in one `run:` block of shell + jq that no
other test touches. These tests extract that script and execute it for real with
`gh` replaced by a stub, so the re-fire CONDITIONS are verified rather than
assumed -- and the condition is the whole point: too narrow and a frozen verdict
stays frozen, too broad and every genuinely-failing PR gets dispatched every 15
minutes forever.

The run block reads its evidence through .github/scripts/readiness_sweep_scan.py,
which pages the open pull requests over GraphQL. So each Runner links the REAL
scanner into the fixture workspace and the `gh` stub answers `api graphql` by
translating three REST-shaped fixtures (see GRAPHQL_STUB). The scanner, the shell
and the decision are therefore all exercised end to end, and the fixtures stay
readable as "statuses" and "check runs" rather than as GraphQL documents.

Skipped where the POSIX toolchain the script needs (bash, jq, GNU `date -d`) is
unavailable, which is the case on the Windows leg of the matrix. Mirrors the
explicit nt guard in test_issue_triage_workflow.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pr_readiness_sweep_helpers import (
    GH_STUB,
    REPO_ROOT,
    SCANNER,
    WORKFLOW,
    Runner,
    _gnu_date,
    _script,
    install_scanner,
)

pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists()
    or not SCANNER.exists()
    or os.name == "nt"
    or shutil.which("bash") is None
    or shutil.which("jq") is None
    or not _gnu_date(),
    reason="requires the workflow plus a POSIX bash, jq and GNU date",
)


@pytest.fixture(scope="module")
def script() -> str:
    return _script()


@pytest.fixture
def runner(tmp_path: Path, script: str) -> Runner:
    return Runner(tmp_path, script)


# ── The pending freeze (the sweep's original purpose) ────────────────────────


def test_stale_pending_is_refired(runner: Runner) -> None:
    """The dropped-event freeze: a lane finished after the pending was published.

    The fixture carries the check evidence that makes it that shape: the verdict
    is old and a lane completed later, so the recompute reads something the frozen
    verdict never saw.
    """
    dispatched = runner.sweep(
        state="pending",
        status_at="2020-01-01T00:00:00Z",
        check_completed_at="2020-01-01T00:30:00Z",
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]
    assert "sha=4328fd0f941f09ff10f245fbdb4accf7c246febe" in dispatched[0]


def test_stale_pending_with_no_later_check_evidence_is_left_alone(runner: Runner) -> None:
    """A pending newer than every check on its head cannot change, so it is not nudged.

    A readiness lane added to the monitored list after a head was pushed has zero
    runs on that immutable head and can never acquire one, so the aggregator
    counts it "(not started)" and republishes the same pending for as long as the
    pull request stays open. Age alone made the sweep re-fire that recompute every
    cycle, and each recompute re-derived the identical verdict, so the nudge
    burned Actions minutes on 22 pull requests and changed nothing.

    The test is the one modes 2, 4 and 5 already use, read in the pending
    direction: the verdict here is NEWER than the newest completed check, so no
    evidence has landed since it was computed and a recompute has nothing new to
    read. Self-terminating rather than permanent -- the moment any lane completes
    after the verdict the condition below flips and the nudge resumes.
    """
    assert (
        runner.sweep(
            state="pending",
            status_at="2026-08-07T19:16:13Z",
            check_completed_at="2026-08-07T19:01:24Z",
        )
        == []
    )


def test_a_pending_with_no_checks_at_all_is_left_alone(runner: Runner) -> None:
    """Zero completed checks is not evidence of a freeze.

    A head whose lanes are all still queueing carries no completed check-run, and
    its pending is honest: the completion events are still owed and each one
    recomputes. Nudging here cannot help, because the recompute reads the same
    empty evidence the verdict already read.
    """
    assert runner.sweep(state="pending", status_at="2020-01-01T00:00:00Z") == []


def test_fresh_pending_is_left_alone(runner: Runner) -> None:
    """A pending with no completed check is left alone whatever its age.

    Two minutes old here, with nothing completed on the head: its lanes are
    still owed, and the first completion is what brings it to the evidence
    test. There is no age at which a check-less pending is nudged.
    """
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert runner.sweep(state="pending", status_at=recent) == []


# ── The evaluate-to-publish window ──────────────────────────────────────────

PUBLISH_LAG_SECONDS = 180


def test_a_check_completed_inside_the_publish_lag_is_evidence(runner: Runner) -> None:
    """A lane the verdict could not have seen still counts, though it pre-dates it.

    The aggregator reads lane state, then finishes its other reads and writes its
    summary before publishing, so its view is older than its publish stamp. A
    lane completing in that gap is invisible to the verdict; if its own
    completion event is then dropped, comparing against the stamp alone calls the
    check old and nothing ever recomputes. The verdict here is published two
    minutes after the check, inside the bound.
    """
    dispatched = runner.sweep(
        state="pending",
        status_at="2026-08-07T19:16:13Z",
        check_completed_at="2026-08-07T19:14:13Z",
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]


def test_a_check_one_second_inside_the_publish_lag_is_evidence(runner: Runner) -> None:
    """The near edge, to pin the bound's value and not merely its existence."""
    assert (
        len(
            runner.sweep(
                state="pending",
                status_at="2026-08-07T19:16:13Z",
                check_completed_at="2026-08-07T19:13:14Z",
            )
        )
        == 1
    )


def test_a_check_at_the_publish_lag_floor_is_not_evidence(runner: Runner) -> None:
    """The far edge. The window is BOUNDED, which is what makes mode 1 terminate.

    A check exactly publish_lag_seconds before the verdict is old evidence: the
    aggregator's gap cannot have been that wide, so the verdict did read it.
    Without this edge the floor would drift toward "any check at all", which is
    the age-only retry the evidence test replaces.
    """
    assert (
        runner.sweep(
            state="pending",
            status_at="2026-08-07T19:16:13Z",
            check_completed_at="2026-08-07T19:13:13Z",
        )
        == []
    )


def test_a_republished_verdict_carries_the_same_check_below_the_floor(
    runner: Runner,
) -> None:
    """Self-termination, as the sweep actually reaches it.

    A rescue dispatches the aggregator, which republishes at some P' at or after
    the tick that dispatched it. The pending it replaced was examined only once
    at least publish_lag_seconds old, so the check that earned the rescue sits
    at least the lag before that tick -- and therefore at least the lag before
    P'. The next sweep sees the SAME newest check below the NEW floor and does
    not repeat the nudge. This models the second pass with the gap at its
    tightest: a republish exactly one lag after the check.
    """
    assert (
        runner.sweep(
            state="pending",
            status_at="2026-08-07T19:17:13Z",
            check_completed_at="2026-08-07T19:14:13Z",
        )
        == []
    )


def test_a_young_pending_with_later_evidence_is_delivered_on_the_next_tick(
    runner: Runner,
) -> None:
    """The ordinary delivery: the aggregator does not subscribe to `completed`.

    A pending published four minutes ago, with a lane that completed since, is
    the normal state of every head whose final lane just finished. Under the old
    fifteen-minute age gate this waited for a "freeze" that is now the design;
    the only age that applies is the publish lag, and four minutes clears it.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    published = (now - timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    completed = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    dispatched = runner.sweep(state="pending", status_at=published, check_completed_at=completed)
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]


def test_a_pending_younger_than_the_lag_is_not_examined(runner: Runner) -> None:
    """The near side of the pairing that makes mode 1 terminate.

    A pending younger than the lag may have been written by a recompute still
    inside its own evaluate-to-publish gap; a check it did not see is still
    evidence on the next tick, and examining it now would nudge on a stamp the
    dispatching recompute has not finished writing.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    published = (now - timedelta(seconds=PUBLISH_LAG_SECONDS - 30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    completed = (now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert runner.sweep(state="pending", status_at=published, check_completed_at=completed) == []


def test_the_pending_minimum_age_is_the_publish_lag() -> None:
    """The pairing the termination argument rests on, asserted against the file.

    A recompute dispatched at tick S publishes at P' >= S. The pending it
    replaced was at least min_age old at S, so the check that earned the nudge
    completed before S - min_age. It sits below the new floor P' - lag iff
    min_age >= lag. Binding the two to one value is what lets the cadence drop
    without a longer age gate; a min_age below the lag would re-nudge the same
    head every tick until the two drifted apart.
    """
    sweep = WORKFLOW.read_text(encoding="utf-8")
    assert "publish_lag_seconds=%d" % PUBLISH_LAG_SECONDS in sweep
    assert 'pending_min_age_seconds="$publish_lag_seconds"' in sweep
    assert '[ "$age" -lt "$pending_min_age_seconds" ]' in sweep
    assert "$(( updated_epoch - publish_lag_seconds ))" in sweep


def test_the_sweep_runs_at_the_shortest_schedule_github_offers() -> None:
    """Delivery latency is the cadence: the aggregator subscribes to no completion."""
    sweep = WORKFLOW.read_text(encoding="utf-8")
    assert '- cron: "*/5 * * * *"' in sweep
    readiness = (WORKFLOW.parent / "pr-readiness.yml").read_text(encoding="utf-8")
    assert "types: [in_progress]" in readiness
    assert "completed" not in re.search(r"types: \[([^\]]*)\]", readiness).group(1)


# ── The read-failure pending: age is its only signal ────────────────────────

READ_FAILURE_TOKEN = "[read-failed]"
# The token PR Readiness stamps on a pending that an advisory lane owes this
# head a verdict it withheld. The sweep re-runs the owing lane rather
# than re-firing the aggregator for it.
VERDICT_UNPUBLISHED_TOKEN = "[verdict-unpublished]"

TRANSPORT_DESCRIPTION = (
    READ_FAILURE_TOKEN + " Readiness could not be evaluated"
    " (transient GitHub API failure); it will be re-evaluated"
)

# The SECOND read-failure site's description. It shares the token and nothing
# else: the prose is the publisher's ordinary pending sentence, because this
# pending is one entry in the waiting list rather than a whole-verdict bail-out.
DISPOSITION_DESCRIPTION = (
    READ_FAILURE_TOKEN + " 1 readiness check(s) still pending;"
    " waiting on disposition records could not be read"
)

# Every phrase the publisher uses for a pending it publishes because a READ
# failed. Each such site must stamp the token; a site that does not is a pending
# the sweep will strand at the evidence test forever.
READ_FAILURE_VOCABULARY = re.compile(
    r"could not be (?:read|evaluated|established)|unreadable", re.IGNORECASE
)


def test_a_stale_transport_read_failure_pending_is_refired_without_later_evidence(
    runner: Runner,
) -> None:
    """The pending shape that keeps its age-based retry.

    pr-readiness.yml publishes this verdict when a read-only call keeps failing
    after its bounded retries, at the END of the job -- so it can post-date every
    check on the head and hold no later check evidence by construction. The
    evidence test can therefore never fire it, while a recompute resolves it
    outright, because the next run's reads succeed. Without the carve-out the
    required status stays pending with nothing able to clear it but a push.

    The fixture is deliberately the SAME shape the lane-pending test asserts is
    left alone -- verdict at 19:16:13Z, newest completed check at 19:01:24Z -- so
    the description is the only thing that differs and the only thing that can
    explain the opposite outcome.
    """
    dispatched = runner.sweep(
        state="pending",
        status_at="2026-08-07T19:16:13Z",
        check_completed_at="2026-08-07T19:01:24Z",
        description=TRANSPORT_DESCRIPTION,
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]
    assert "sha=4328fd0f941f09ff10f245fbdb4accf7c246febe" in dispatched[0]


def test_a_read_failure_pending_is_refired_whatever_its_prose_says(
    runner: Runner,
) -> None:
    """The discriminator is the token, so a differently-worded site is rescued too.

    The publisher writes a read-failure pending from more than one site, and only
    one of them phrases it as "Readiness could not be evaluated": an unreadable
    disposition record set is one entry in the waiting list, so it arrives under
    the ordinary "N readiness check(s) still pending" sentence. Matching that
    prose recognises the first site and strands the second at exactly the freeze
    this carve-out exists to prevent. Same fixture as the transport case, with
    only the prose changed.
    """
    dispatched = runner.sweep(
        state="pending",
        status_at="2026-08-07T19:16:13Z",
        check_completed_at="2026-08-07T19:01:24Z",
        description=DISPOSITION_DESCRIPTION,
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]


@pytest.mark.parametrize(
    "description",
    [
        READ_FAILURE_TOKEN + " Readiness could not be evalu",
        READ_FAILURE_TOKEN + " 12 readiness check(s) still p",
        READ_FAILURE_TOKEN,
    ],
    ids=["transport", "disposition", "token-only"],
)
def test_the_token_survives_the_description_length_cap(runner: Runner, description: str) -> None:
    """A truncated description still classifies, because the token leads it.

    A commit status description is capped at 140 characters and the tail is what
    gets cut, so the publisher puts the token first and the sweep matches the
    front. These fixtures keep only what a cap would leave behind.
    """
    assert (
        len(
            runner.sweep(
                state="pending",
                status_at="2026-08-07T19:16:13Z",
                check_completed_at="2026-08-07T19:01:24Z",
                description=description,
            )
        )
        == 1
    )


def test_the_publisher_and_the_sweep_agree_on_the_read_failure_token() -> None:
    """Pin the literal, so editing either file cannot silently strand the retry.

    The sweep classifies on a token another workflow writes. Nothing in either
    file's own tests would notice the two drifting apart, and the cost of drift
    is the exact freeze this carve-out exists to prevent.
    """
    publisher = (REPO_ROOT / ".github" / "workflows" / "pr-readiness.yml").read_text(
        encoding="utf-8"
    )
    sweep = WORKFLOW.read_text(encoding="utf-8")
    declaration = 'READ_FAILURE_TOKEN="%s"' % READ_FAILURE_TOKEN
    assert declaration in sweep
    assert declaration in publisher
    assert '"$READ_FAILURE_TOKEN"*) return 0 ;;' in sweep


def test_every_read_failure_pending_the_publisher_writes_is_stamped() -> None:
    """A new read-failure site cannot ship unstamped.

    This is the failure mode a prose match hides: someone adds a third pending
    for a read that failed, words it their own way, and the sweep -- which can
    only see the token -- leaves it frozen. Nothing at runtime complains, because
    the verdict is a legitimate pending; it simply never clears.

    So the publisher is read as text. Every `pending+=` whose subject is a failed
    read must set the flag that stamps the token, and the whole-verdict bail-out
    must carry the token itself, first, ahead of its prose.
    """
    publisher = (REPO_ROOT / ".github" / "workflows" / "pr-readiness.yml").read_text(
        encoding="utf-8"
    )
    lines = publisher.splitlines()

    sites = [
        (number, line)
        for number, line in enumerate(lines)
        if "pending+=(" in line and READ_FAILURE_VOCABULARY.search(line)
    ]
    assert sites, "no read-failure pending site found; the vocabulary has drifted"
    for number, line in sites:
        window = "\n".join(lines[number : number + 3])
        unstamped = "line %d publishes a read-failure pending unstamped: %s" % (
            number + 1,
            line.strip(),
        )
        assert "read_failure=true" in window, unstamped

    assert 'echo "description=$READ_FAILURE_TOKEN Readiness could not be evaluated' in publisher
    assert 'prefix="$READ_FAILURE_TOKEN "' in publisher


# ── The withheld advisory verdict: re-run the lane, not the aggregator ─

# The pending shape for a withheld advisory verdict. The lane's own check-run
# is `success` and PR Readiness stamps the token first, so the sweep must NOT
# re-fire the aggregator (a recompute re-derives the same named pending) and
# must instead re-run the owing lane. The token leads the description like the
# read-failure one; the ordinary pending prose can follow it.
VERDICT_UNPUBLISHED_DESCRIPTION = (
    VERDICT_UNPUBLISHED_TOKEN + " 3 readiness check(s) still pending;"
    " waiting on Design Review (verdict not published, re-run this lane)"
)


def test_a_withheld_verdict_pending_reruns_the_lane_not_the_aggregator(
    runner: Runner,
) -> None:
    """A `[verdict-unpublished]` pending re-runs the owing lane.

    The lane finished `success` and its verdict never reached the comment slot,
    so there is no check completed AFTER the verdict -- the fixture is the same
    no-later-evidence shape an ordinary lane-pending has -- and re-firing the
    aggregator would only re-derive the same named pending. The sweep must route
    to the lane re-run instead: no `pr-readiness.yml` dispatch, and the recovery
    is named in the log so a reader can see what fired.
    """
    dispatched = runner.sweep(
        state="pending",
        status_at="2026-08-07T19:16:13Z",
        check_completed_at="2026-08-07T19:01:24Z",
        description=VERDICT_UNPUBLISHED_DESCRIPTION,
    )
    assert dispatched == []
    assert "withheld advisory verdict" in runner.last_stdout
    assert "republish_withheld_verdict.py" not in runner.last_stdout  # it is invoked, not echoed
    # The invocation itself: the sweep ran the republisher for this PR.
    assert "re-running the owing lane(s)" in runner.last_stdout


def test_the_publisher_and_the_sweep_agree_on_the_verdict_unpublished_token() -> None:
    """Pin the token across the two files that must share it.

    PR Readiness stamps it; the sweep classifies on it. Nothing in either file's
    own tests would catch the two drifting apart, and the cost of drift is the
    exact freeze this recovery exists to clear -- a withheld verdict never
    re-run.
    """
    publisher = (REPO_ROOT / ".github" / "workflows" / "pr-readiness.yml").read_text(
        encoding="utf-8"
    )
    sweep = WORKFLOW.read_text(encoding="utf-8")
    declaration = 'VERDICT_UNPUBLISHED_TOKEN="%s"' % VERDICT_UNPUBLISHED_TOKEN
    assert declaration in sweep
    assert declaration in publisher
    assert '*"$VERDICT_UNPUBLISHED_TOKEN"*) return 0 ;;' in sweep
    # And the publisher leads the pending description with it, like the
    # read-failure token, so a 140-char truncation cannot strip it.
    assert 'prefix="$prefix$VERDICT_UNPUBLISHED_TOKEN "' in publisher


def test_every_unpublished_verdict_pending_site_sets_the_flag() -> None:
    """A new `(verdict not published, re-run this lane)` site cannot ship
    without arming the token.

    Same discipline as the read-failure stamp test: the sweep can only see the
    token, so a scoring site that forgets to set ``verdict_unpublished=true``
    would leave its withheld verdict frozen with nothing to re-run it.
    """
    publisher = (REPO_ROOT / ".github" / "workflows" / "pr-readiness.yml").read_text(
        encoding="utf-8"
    )
    lines = publisher.splitlines()
    sites = [
        number
        for number, line in enumerate(lines)
        if "verdict not published, re-run this lane" in line and "pending+=(" in line
    ]
    assert sites, "no unpublished-verdict pending site found; the wording has drifted"
    for number in sites:
        window = "\n".join(lines[max(0, number - 2) : number + 1])
        assert (
            "verdict_unpublished=true" in window
        ), "line %d scores an unpublished-verdict pending without arming the token" % (number + 1)


def test_an_ordinary_lane_pending_still_pays_no_status_read(runner: Runner) -> None:
    """The classifying read is made only where it can change the outcome.

    A pending with later check evidence is re-fired by the evidence test itself,
    so it never reaches the read. Asserting the recorder stays absent is the only
    way to prove that: a dispatch count cannot tell a read that happened from one
    that was skipped.
    """
    assert (
        len(
            runner.sweep(
                state="pending",
                status_at="2020-01-01T00:00:00Z",
                check_completed_at="2020-01-01T00:30:00Z",
            )
        )
        == 1
    )
    assert not runner.status_read.exists()


def test_a_young_no_evidence_pending_pays_no_status_read(runner: Runner) -> None:
    """Both description consumers are aged against STALE_MINUTES, so a pending
    past the publish lag but younger than that is skipped before the read.

    The fixture carries the withheld-verdict token, which WOULD re-run a lane if
    the pending were old enough -- so this also proves the age gate, not the
    classifier, is what leaves it alone.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    published = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    completed = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    dispatched = runner.sweep(
        state="pending",
        status_at=published,
        check_completed_at=completed,
        description=VERDICT_UNPUBLISHED_DESCRIPTION,
    )
    assert dispatched == []
    assert not runner.status_read.exists()
    assert "withheld advisory verdict" not in runner.last_stdout


def test_an_unreadable_description_leaves_the_pending_alone(runner: Runner) -> None:
    """The classifier fails CLOSED toward the evidence test.

    When the read breaks, the sweep cannot tell a read-failure pending from a
    lane-pending. Treating an unknown as a read failure would re-fire every
    ordinary lane-pending whenever the endpoint was unwell, which is the loop the
    evidence test removes; treating it as ordinary costs one sweep interval of
    delay on a rescue that the next sweep makes.
    """
    assert (
        runner.sweep(
            state="pending",
            status_at="2026-08-07T19:16:13Z",
            check_completed_at="2026-08-07T19:01:24Z",
            description=TRANSPORT_DESCRIPTION,
            status_read_fails=True,
        )
        == []
    )


# ── The re-run freeze (the case this change adds) ────────────────────────────


def test_failure_with_later_check_evidence_is_refired(runner: Runner) -> None:
    """A failure with later check evidence and no fresh event is still re-fired.

    `gh run rerun --failed` creates a new run ATTEMPT whose completion emits no
    fresh `workflow_run: completed`, so the aggregator never re-evaluates. Here
    the verdict was published at 19:01:24Z and a check finished at 19:16:13Z --
    evidence that landed after the verdict, making it stale by construction.
    """
    dispatched = runner.sweep(
        state="failure",
        status_at="2026-08-07T19:01:24Z",
        check_completed_at="2026-08-07T19:16:13Z",
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]


def test_failure_with_no_later_evidence_is_left_alone(runner: Runner) -> None:
    """The anti-storm property, and the reason age is NOT the test here.

    A PR that is genuinely failing has an old terminal verdict and no newer check
    evidence. Dispatching on `state == failure` alone would nudge it every 15
    minutes forever; this asserts it is nudged zero times.
    """
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-07T19:16:13Z",
            check_completed_at="2026-08-07T19:01:24Z",
        )
        == []
    )


def test_failure_refire_is_self_terminating(runner: Runner) -> None:
    """Republishing must end the loop.

    After a nudge, readiness becomes the NEWEST timestamp for that SHA. The next
    sweep therefore sees no evidence newer than the verdict and stops -- which is
    what makes a scheduled re-fire safe rather than a dispatch loop.
    """
    # Same evidence, but the verdict has since been republished after it.
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-07T19:20:00Z",
            check_completed_at="2026-08-07T19:16:13Z",
        )
        == []
    )


def test_failure_with_no_completed_checks_is_left_alone(runner: Runner) -> None:
    """No check evidence at all means nothing proves the verdict stale."""
    assert runner.sweep(state="failure", status_at="2026-08-07T19:01:24Z") == []


def test_check_completing_in_the_same_second_is_not_new_evidence(runner: Runner) -> None:
    """The publish and the completion that triggered it race within a second.

    Without the margin, every ordinary terminal verdict would look stale on the
    very next sweep.
    """
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-07T19:01:24Z",
            check_completed_at="2026-08-07T19:01:26Z",
        )
        == []
    )


# ── The green freeze: a verdict contradicted by later FAILING evidence ───────


@pytest.mark.parametrize("state", ["success", "error"])
def test_green_verdict_with_later_failing_evidence_is_refired(runner: Runner, state: str) -> None:
    """The unsafe direction of the same re-run mechanism.

    A job re-run that flips a lane red after a green verdict emits no fresh
    `workflow_run: completed`, so the required aggregate stays green over a
    now-red revision -- which PERMITS a merge, where a stale red only blocks one.
    """
    dispatched = runner.sweep(
        state=state,
        status_at="2026-08-07T19:01:24Z",
        check_completed_at="2026-08-07T19:16:13Z",
        check_conclusion="failure",
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]


@pytest.mark.parametrize(
    "conclusion", ["timed_out", "cancelled", "action_required", "stale", "startup_failure"]
)
def test_every_failure_class_conclusion_counts_as_red_evidence(
    runner: Runner, conclusion: str
) -> None:
    """The lane reader in pr-readiness.yml treats all six as failure-class.

    If the sweep recognised only `failure`, a lane cancelled or timed out by a
    re-run would leave the green verdict frozen.
    """
    assert (
        len(
            runner.sweep(
                state="success",
                status_at="2026-08-07T19:01:24Z",
                check_completed_at="2026-08-07T19:16:13Z",
                check_conclusion=conclusion,
            )
        )
        == 1
    )


def test_a_later_passing_check_never_refires_a_green_verdict(runner: Runner) -> None:
    """The anti-storm property for this path, and why the test is narrowed.

    Housekeeping check-runs (`Strip stale workflow-change override`, `Fork
    workflow-change guard`) legitimately complete days after a verdict on a
    long-lived PR. An unnarrowed "any check completed later" test would re-fire
    most green PRs on every sweep while proving nothing, and a later pass cannot
    turn a green verdict red anyway.
    """
    assert (
        runner.sweep(
            state="success",
            status_at="2026-08-07T19:01:24Z",
            check_completed_at="2026-08-25T09:18:07Z",
            check_conclusion="skipped",
        )
        == []
    )


def test_green_refire_is_self_terminating(runner: Runner) -> None:
    """Republishing must end this loop too, exactly as it does for `failure`."""
    assert (
        runner.sweep(
            state="success",
            status_at="2026-08-07T19:20:00Z",
            check_completed_at="2026-08-07T19:16:13Z",
            check_conclusion="failure",
        )
        == []
    )


def test_green_verdict_with_no_check_evidence_is_left_alone(runner: Runner) -> None:
    """An ordinary green PR is never nudged."""
    assert runner.sweep(state="success", status_at="2020-01-01T00:00:00Z") == []


# ── The unpublished freeze: no readiness status was ever written ─────────────


def test_a_missing_readiness_status_is_refired(runner: Runner) -> None:
    """A missing readiness status is re-fired.

    `pr-readiness.yml` does not retry its status POST and instructs a human to
    re-run the workflow. When that POST failed on `gh: HTTP 503`, the SHA carried
    no readiness status -- no `pending` to age out, no event pending -- and the
    only automatic re-runner skipped the PR because it had no status to read. The
    one case the publisher delegates to a re-run was the one case nothing re-ran.
    """
    dispatched = runner.sweep(state=None, pr_updated_at="2026-08-17T14:20:00Z")
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]
    assert "sha=4328fd0f941f09ff10f245fbdb4accf7c246febe" in dispatched[0]


def test_a_brand_new_pull_request_is_left_alone(runner: Runner) -> None:
    """Within STALE_MINUTES of the last push, the PR's own run really is coming.

    This is what keeps the new path from dispatching against every PR opened in
    the last quarter of an hour.
    """
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert runner.sweep(state=None, pr_updated_at=recent) == []


def test_a_missing_status_still_respects_the_dispatch_cap(runner: Runner) -> None:
    """The runaway backstop applies to the new path as well."""
    assert runner.sweep(state=None, pr_updated_at="2026-08-17T14:20:00Z", max_dispatch="0") == []


def test_an_unparseable_pr_timestamp_is_left_alone(runner: Runner) -> None:
    """Fail closed on a timestamp the sweep cannot read, rather than dispatching."""
    assert runner.sweep(state=None, pr_updated_at="not-a-date") == []


# ── Truncation: the oldest PRs must never be dropped silently ────────────────


def test_the_open_pr_scan_has_no_silent_ceiling(script: str) -> None:
    """The oldest open PRs must not be droppable at all.

    `gh pr list` returned newest-first and truncated SILENTLY at `--limit`, so a
    ceiling that fell behind the real open-PR count dropped the OLDEST PRs --
    precisely the frozen ones this sweep exists to rescue -- and the guard against
    it was a warning nobody could act on until it had already happened. Cursor
    paging removes the ceiling instead of sizing it, so the property is now
    structural: there is no limit left to outgrow, and none may come back.
    """
    # Comments are stripped first: the block SHOULD still explain what `gh pr
    # list` did and why the ceiling was dangerous. What must not survive is the
    # executable form of it.
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    assert "gh pr list" not in code
    assert "PR_LIST_LIMIT" not in code
    assert "--limit" not in code
    assert "readiness_sweep_scan.py" in code


def test_every_open_pull_request_is_scanned_across_pages(tmp_path: Path, script: str) -> None:
    """Nothing past the first page is dropped.

    The scan pages 25 pull requests at a time -- measured, not tidy: with each
    PR's rollup contexts attached, 100 and 50 per page both answered HTTP 504.
    Here 30 PRs are open and all five stale ones live on the SECOND page, so a
    scan that stopped after one page would rescue none of them and report a
    plausible-looking 25.
    """
    from datetime import datetime, timedelta, timezone

    fixtures = tmp_path / "fixtures"
    work = tmp_path / "work"
    bindir = tmp_path / "bin"
    for d in (fixtures, work, bindir):
        d.mkdir(parents=True)
    stub = bindir / "gh"
    stub.write_text(GH_STUB)
    stub.chmod(0o755)
    install_scanner(fixtures, work)

    fresh = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    prs = []
    for index in range(30):
        sha = f"sha{index:02d}"
        prs.append(
            {
                "number": 100 + index,
                "headRefOid": sha,
                "updatedAt": "2020-01-01T00:00:00Z",
            }
        )
        at = "2020-01-01T00:00:00Z" if index >= 25 else fresh
        (fixtures / f"status_{sha}.json").write_text(
            json.dumps([[{"context": "PR Readiness", "state": "pending", "updated_at": at}]])
        )
    (fixtures / "prs.json").write_text(json.dumps(prs))
    # Mode 1 needs a check that completed AFTER the verdict, so the stale PRs are
    # rescuable at all. This fixture is global to every PR the stub serves, and
    # that is harmless here: it post-dates the five stale verdicts and pre-dates
    # the fresh ones, which the age gate excludes before evidence is read.
    (fixtures / "check_runs.json").write_text(
        json.dumps(
            [
                {
                    "check_runs": [
                        {
                            "status": "completed",
                            "conclusion": "success",
                            "completed_at": "2020-06-01T00:00:00Z",
                        }
                    ]
                }
            ]
        )
    )

    proc = subprocess.run(  # noqa: S603 - fixed argv, test-local stub
        ["bash", "-c", script],
        cwd=work,
        env={
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(fixtures),
            "STUB_PYTHON": sys.executable,
            "REPO": "kirodotdev/KiroCrew",
            "STATUS_CONTEXT": "PR Readiness",
            "STALE_MINUTES": "15",
            "MAX_DISPATCH": "200",
        },
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Scanning 30 open pull request(s)" in proc.stdout

    dispatched = (fixtures / "dispatched.txt").read_text().splitlines()
    numbers = sorted(
        int(token.split("=", 1)[1])
        for line in dispatched
        for token in line.split()
        if token.startswith("pr=")
    )
    assert numbers == [125, 126, 127, 128, 129]


def test_a_different_status_context_never_drives_the_decision(runner: Runner) -> None:
    """Only the aggregate this sweep owns may be read.

    The SHA carries a FRESH `PR Readiness` pending (nothing to rescue) alongside a
    long-stale failing `Coverage Gate`. A sweep that matched on the wrong context
    would read the Coverage Gate failure, see later check evidence, and dispatch.
    """
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert (
        runner.sweep(
            state="pending",
            status_at=recent,
            check_completed_at="2026-08-07T19:16:13Z",
            check_conclusion="failure",
            extra_statuses=[
                {
                    "context": "Coverage Gate",
                    "state": "failure",
                    "updated_at": "2020-01-01T00:00:00Z",
                }
            ],
        )
        == []
    )


def test_only_a_foreign_status_reads_as_an_unpublished_verdict(runner: Runner) -> None:
    """A SHA with other statuses but no readiness one is still unpublished.

    This is the same shape generalised: what makes the verdict absent is that no
    `PR Readiness` context exists, not that the SHA is bare. Treating it as
    "already has a status" would leave the required aggregate permanently missing.
    """
    dispatched = runner.sweep(
        state=None,
        pr_updated_at="2026-08-17T14:20:00Z",
        extra_statuses=[
            {
                "context": "Coverage Gate",
                "state": "success",
                "updated_at": "2026-08-17T14:00:00Z",
            }
        ],
    )
    assert len(dispatched) == 1


def test_max_dispatch_caps_the_sweep(runner: Runner) -> None:
    """The runaway backstop still applies to the new path."""
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-07T19:01:24Z",
            check_completed_at="2026-08-07T19:16:13Z",
            max_dispatch="0",
        )
        == []
    )


# ── Fairness: oldest-stale-first, never PR-list order ────────────────────────

# Per-SHA readiness statuses need no second stub: the translator prefers a
# `status_<sha>.json` fixture over the shared `statuses.json` when one exists,
# which is what lets one sweep hold several PRs frozen for different lengths of
# time -- the exact thing the ordering test must vary.


def test_dispatch_is_oldest_stale_first(tmp_path: Path, script: str) -> None:
    """The longest-frozen PR is dispatched first, regardless of PR-list order.

    This is the anti-starvation property: with a per-sweep cap, dispatching in
    `gh pr list` order (newest-first) permanently defers the oldest, lowest-
    numbered frozen PRs. Ordering by how long each PR has been stale fixes that.
    """
    fixtures = tmp_path / "fixtures"
    work = tmp_path / "work"
    bindir = tmp_path / "bin"
    for d in (fixtures, work, bindir):
        d.mkdir(parents=True)
    stub = bindir / "gh"
    stub.write_text(GH_STUB)
    stub.chmod(0o755)
    install_scanner(fixtures, work)

    # PRs as `gh pr list` returns them (newest-numbered first), each frozen for a
    # DIFFERENT length of time. Staleness order (oldest first) is 3120, 3400, 3612
    # -- the opposite of the list order for the newest entry.
    prs = [
        {"number": 3612, "headRefOid": "aaa"},
        {"number": 3120, "headRefOid": "bbb"},
        {"number": 3400, "headRefOid": "ccc"},
    ]
    # No `updatedAt`: these are `pending` verdicts, so neither the unpublished arm
    # nor mode 5's gate reads it, and leaving it out keeps the fixture about the
    # one thing this test varies.
    (fixtures / "prs.json").write_text(json.dumps(prs))
    ages = {
        "aaa": "2020-01-01T00:00:03Z",  # least stale
        "bbb": "2020-01-01T00:00:01Z",  # most stale -> first
        "ccc": "2020-01-01T00:00:02Z",
    }
    for sha, at in ages.items():
        # Slurped shape: an outer array of pages.
        (fixtures / f"status_{sha}.json").write_text(
            json.dumps([[{"context": "PR Readiness", "state": "pending", "updated_at": at}]])
        )
    # One check completed after all three verdicts, so each is a mode 1 rescue and
    # the test varies only the staleness order.
    (fixtures / "check_runs.json").write_text(
        json.dumps(
            [
                {
                    "check_runs": [
                        {
                            "status": "completed",
                            "conclusion": "success",
                            "completed_at": "2020-01-01T00:01:00Z",
                        }
                    ]
                }
            ]
        )
    )

    proc = subprocess.run(  # noqa: S603 - fixed argv, test-local stub
        ["bash", "-c", script],
        cwd=work,
        env={
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(fixtures),
            "STUB_PYTHON": sys.executable,
            "REPO": "kirodotdev/KiroCrew",
            "STATUS_CONTEXT": "PR Readiness",
            "STALE_MINUTES": "15",
            "MAX_DISPATCH": "200",
        },
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr

    dispatched = (fixtures / "dispatched.txt").read_text().splitlines()
    order = []
    for line in dispatched:
        for tok in line.split():
            if tok.startswith("pr="):
                order.append(int(tok.split("=", 1)[1]))
    assert order == [3120, 3400, 3612], f"expected oldest-first, got {order}"


def test_the_sweep_never_recomputes_a_verdict_itself(script: str) -> None:
    """It may only nudge the authoritative workflow.

    A sweep that published its own verdict would be a second source of truth for
    a required status -- and could mark a PR ready without the reviewers.
    """
    assert "gh workflow run pr-readiness.yml" in script
    for forbidden in ("/statuses -X POST", "--method POST", "-X POST"):
        assert forbidden not in script, f"sweep must not write statuses: {forbidden}"


# ── Paginated reads: one verdict per SHA, not one per page ───────────────────


def test_check_evidence_is_read_across_every_page(runner: Runner) -> None:
    """`--paginate` alone makes jq emit one `max` PER PAGE.

    `date -d` then rejects the multi-line string, the epoch reads 0, and the PR is
    skipped -- silently exempting every PR with more than 100 check-runs, which on
    this repo is any PR whose lanes have been re-run. The newest evidence here
    lives on the SECOND page, so a page-blind read cannot find it.
    """
    dispatched = runner.sweep(
        state="failure",
        status_at="2026-08-07T19:01:24Z",
        check_completed_at="2026-08-07T19:00:00Z",
        extra_check_page=("success", "2026-08-07T19:16:13Z"),
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]


def test_a_green_verdict_sees_failing_evidence_on_a_later_page(runner: Runner) -> None:
    """Same pagination property for the green arm."""
    dispatched = runner.sweep(
        state="success",
        status_at="2026-08-07T19:01:24Z",
        check_completed_at="2026-08-07T19:00:00Z",
        check_conclusion="success",
        extra_check_page=("failure", "2026-08-07T19:16:13Z"),
    )
    assert len(dispatched) == 1


# ── Transport failure is not an absent verdict ───────────────────────────────


def test_a_failed_read_is_not_treated_as_unpublished(runner: Runner) -> None:
    """A GraphQL failure and a genuinely absent verdict must not read alike.

    Conflating them would turn transient GitHub trouble into a spurious re-fire of
    an arbitrary old PR -- on a shared token budget, at 15-minute intervals, on
    every PR at once. The scan enforces the distinction by SHAPE rather than by an
    ordering rule in the shell: a PR it could not read is absent from its output
    entirely, so the unpublished arm never sees one. It also exits 0 with a
    warning rather than failing, because a sweep that runs on the pages it did
    read still rescues those PRs, and the next sweep retries the rest.
    """
    dispatched = runner.sweep(
        state=None, pr_updated_at="2026-08-17T14:20:00Z", graphql_read_fails=True
    )
    assert dispatched == []
    assert "Scanning 0 open pull request(s)" in runner.last_stdout
    assert "::warning::" in runner.last_stderr
    assert "the walk ends here" in runner.last_stderr


# ── The disposition-comment freeze (the verdict depends on comment bytes) ─


def test_failure_with_a_later_disposition_edit_is_refired(runner: Runner) -> None:
    """A disposition-rule violation fails readiness, so the verdict
    depends on comment bytes -- and the aggregator has no `issue_comment`
    trigger. Correcting the comment produces no event and no check-run, so
    without this mode the red freezes on an unchanged commit."""
    dispatched = runner.sweep(
        state="failure",
        status_at="2026-08-30T19:01:24Z",
        check_completed_at="2026-08-30T18:55:00Z",
        disposition_at="2026-08-30T19:20:00Z",
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]
    assert "disposition record changed later" in runner.last_stdout


def test_failure_with_an_older_disposition_is_left_alone(runner: Runner) -> None:
    """The writer read the listing and ruled BEFORE the verdict -- that is the
    ordinary case, and the red is current, not stale. Re-firing here would
    dispatch every genuinely-violating PR every 15 minutes forever."""
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-30T19:01:24Z",
            check_completed_at="2026-08-30T18:55:00Z",
            disposition_at="2026-08-30T18:40:00Z",
            pr_updated_at="2026-08-30T19:30:00Z",
        )
        == []
    )


def test_disposition_refire_is_self_terminating(runner: Runner) -> None:
    """After the nudge republishes, readiness is the newest timestamp again, so
    the same comment edit is never counted twice -- the property that makes this
    safe to run on a schedule."""
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-30T19:25:00Z",
            check_completed_at="2026-08-30T18:55:00Z",
            disposition_at="2026-08-30T19:20:00Z",
            pr_updated_at="2026-08-30T19:30:00Z",
        )
        == []
    )


def test_a_disposition_edit_in_the_same_second_is_not_new_evidence(runner: Runner) -> None:
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-30T19:01:24Z",
            check_completed_at="2026-08-30T18:55:00Z",
            disposition_at="2026-08-30T19:01:24Z",
            pr_updated_at="2026-08-30T19:30:00Z",
        )
        == []
    )


def test_a_later_ordinary_comment_is_not_disposition_evidence(runner: Runner) -> None:
    """Only disposition-marked comments are evidence. A review bot rewriting its
    comment in place, or any human reply, must not nudge a legitimately red PR --
    that is what would turn this into a per-sweep re-fire."""
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-30T19:01:24Z",
            check_completed_at="2026-08-30T18:55:00Z",
            other_comments=[
                {
                    "body": "<!-- codex-ai-review -->\nGPT 6.1 Review",
                    "updated_at": "2026-08-30T19:40:00Z",
                }
            ],
        )
        == []
    )


def test_later_check_evidence_still_wins_without_reading_comments(runner: Runner) -> None:
    """Mode 2 is unchanged and still reports its own reason: a PR with later
    check evidence must not be re-attributed to the comment path."""
    dispatched = runner.sweep(
        state="failure",
        status_at="2026-08-30T19:01:24Z",
        check_completed_at="2026-08-30T19:16:13Z",
    )
    assert len(dispatched) == 1
    assert "a check completed later" in runner.last_stdout
    assert "disposition record changed later" not in runner.last_stdout


def test_failure_with_no_checks_and_a_later_disposition_is_still_refired(
    runner: Runner,
) -> None:
    """A PR whose head carries no completed check-run at all is not skipped
    outright by the failure arm. The comment path must still be reachable for
    it, since a disposition violation can be the ONLY reason readiness is red."""
    dispatched = runner.sweep(
        state="failure",
        status_at="2026-08-30T19:01:24Z",
        check_completed_at=None,
        disposition_at="2026-08-30T19:20:00Z",
    )
    assert len(dispatched) == 1
    assert "disposition record changed later" in runner.last_stdout


def test_green_verdict_with_a_later_disposition_is_refired(runner: Runner) -> None:
    """The PERMITTING direction, and the half that matters: a writer can post a
    rule-violating record AFTER readiness published success. The record carries
    ledger downgrade power immediately, the revision now violates the rule, and
    no event re-evaluates it -- so the required status stays green over a
    revision that should be red, which permits a merge."""
    dispatched = runner.sweep(
        state="success",
        status_at="2026-08-30T19:01:24Z",
        check_completed_at="2026-08-30T18:55:00Z",
        disposition_at="2026-08-30T19:20:00Z",
    )
    assert len(dispatched) == 1
    assert "disposition record changed later" in runner.last_stdout


def test_green_verdict_with_an_older_disposition_is_left_alone(runner: Runner) -> None:
    assert (
        runner.sweep(
            state="success",
            status_at="2026-08-30T19:01:24Z",
            check_completed_at="2026-08-30T18:55:00Z",
            disposition_at="2026-08-30T18:30:00Z",
            pr_updated_at="2026-08-30T19:30:00Z",
        )
        == []
    )


def test_green_disposition_refire_is_self_terminating(runner: Runner) -> None:
    assert (
        runner.sweep(
            state="success",
            status_at="2026-08-30T19:25:00Z",
            check_completed_at="2026-08-30T18:55:00Z",
            disposition_at="2026-08-30T19:20:00Z",
            pr_updated_at="2026-08-30T19:30:00Z",
        )
        == []
    )


def test_a_later_bot_comment_never_refires_a_green_verdict(runner: Runner) -> None:
    """The review bots rewrite their comments in place on every push. If those
    counted, this would re-fire most green PRs on every sweep."""
    assert (
        runner.sweep(
            state="success",
            status_at="2026-08-30T19:01:24Z",
            other_comments=[
                {
                    "body": "<!-- design-review -->\nDesign Review",
                    "updated_at": "2026-08-30T19:40:00Z",
                }
            ],
        )
        == []
    )


def test_failing_check_evidence_still_wins_on_a_green_verdict(runner: Runner) -> None:
    dispatched = runner.sweep(
        state="success",
        status_at="2026-08-30T19:01:24Z",
        check_completed_at="2026-08-30T19:16:13Z",
        check_conclusion="failure",
    )
    assert len(dispatched) == 1
    assert "a check FAILED later" in runner.last_stdout
    assert "disposition record changed later" not in runner.last_stdout


def test_a_disposition_three_seconds_after_a_red_verdict_is_evidence(runner: Runner) -> None:
    """No margin on comment evidence. The check-evidence modes tolerate 5s so a
    concurrently-completing check is not read as new; `-le` already excludes the
    same second, so a margin here only created a 1-5s window in which a record
    could be posted and then never re-examined -- nothing else observes
    comments, so that window was permanent."""
    dispatched = runner.sweep(
        state="failure",
        status_at="2026-08-30T19:01:24Z",
        check_completed_at="2026-08-30T18:55:00Z",
        disposition_at="2026-08-30T19:01:27Z",
    )
    assert len(dispatched) == 1


def test_a_disposition_three_seconds_after_a_green_verdict_is_evidence(
    runner: Runner,
) -> None:
    dispatched = runner.sweep(
        state="success",
        status_at="2026-08-30T19:01:24Z",
        disposition_at="2026-08-30T19:01:27Z",
    )
    assert len(dispatched) == 1


def test_a_same_second_disposition_still_terminates_the_loop(runner: Runner) -> None:
    """The property the margin was there to protect, which strict `-le` already
    gives: a record stamped in the same second as the publish is not evidence, so
    a republish cannot re-fire on the record it just answered."""
    assert (
        runner.sweep(
            state="success",
            status_at="2026-08-30T19:01:24Z",
            disposition_at="2026-08-30T19:01:24Z",
            pr_updated_at="2026-08-30T19:10:00Z",
        )
        == []
    )


def test_comments_are_not_read_when_the_pr_is_untouched_since_the_verdict(
    runner: Runner,
) -> None:
    """The one read left on the shared REST pool is skipped when it cannot find
    anything.

    Creating OR editing a comment bumps the pull request's `updatedAt`, so an
    `updatedAt` no newer than the verdict proves no disposition record moved after
    it. The fixture here is deliberately impossible -- a record stamped 19:20 on a
    PR last touched 19:00 -- because that is what makes the assertion sharp: the
    read is not merely fruitless, it never happens, and no dispatch count could
    tell those two apart.
    """
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-30T19:01:24Z",
            check_completed_at="2026-08-30T18:55:00Z",
            disposition_at="2026-08-30T19:20:00Z",
            pr_updated_at="2026-08-30T19:00:00Z",
        )
        == []
    )
    assert not runner.comments_read.exists()


# ── Review evidence ──────────────────────────────────────────────────────────
#
# Readiness reds a head a reviewer asked changes on. A review reaches the
# aggregator through the Sensitive Change Review run it starts; the scan's newest
# review time is what re-fires a terminal verdict when that run never starts or
# the readiness run it triggers does not publish.


@pytest.mark.parametrize("state", ["success", "failure"])
def test_a_review_after_a_terminal_verdict_is_refired(runner: Runner, state: str) -> None:
    # Green: a changes-requested review would otherwise leave a rejected head
    # green. Red: an approval or dismissal would otherwise leave it red.
    dispatched = runner.sweep(
        state=state,
        status_at="2026-08-30T19:01:24Z",
        check_completed_at="2026-08-30T18:55:00Z",
        review_at="2026-08-30T19:20:00Z",
    )
    assert len(dispatched) == 1
    assert "a review was submitted or dismissed" in runner.last_stdout


def test_a_pending_is_not_refired_by_a_review_alone(runner: Runner) -> None:
    # A pending verdict already holds merge, and the check completion that
    # resolves it re-reads the reviews.
    assert (
        runner.sweep(
            state="pending",
            status_at="2026-08-30T19:01:24Z",
            check_completed_at="2026-08-30T18:00:00Z",
            review_at="2026-08-30T19:20:00Z",
        )
        == []
    )


@pytest.mark.parametrize("state", ["success", "failure"])
def test_a_review_inside_the_publish_lag_is_evidence(runner: Runner, state: str) -> None:
    # Landed 60s before the stamp: inside the evaluate-to-publish gap, so the
    # verdict may never have read it.
    dispatched = runner.sweep(
        state=state,
        status_at="2026-08-30T19:01:24Z",
        check_completed_at="2026-08-30T18:55:00Z",
        review_at="2026-08-30T19:00:24Z",
    )
    assert len(dispatched) == 1


@pytest.mark.parametrize("state", ["success", "failure"])
def test_a_review_at_the_publish_lag_floor_is_not_evidence(runner: Runner, state: str) -> None:
    # Exactly 180s before the stamp, which is where a republish lands it: this
    # is what makes the review nudge self-terminating.
    assert (
        runner.sweep(
            state=state,
            status_at="2026-08-30T19:01:24Z",
            check_completed_at="2026-08-30T18:00:00Z",
            review_at="2026-08-30T18:58:24Z",
        )
        == []
    )


@pytest.mark.parametrize("state", ["success", "failure"])
def test_a_terminal_verdict_younger_than_the_lag_is_not_examined_for_reviews(
    runner: Runner, state: str
) -> None:
    # Examined from the next tick on instead, so the recompute a fresh review
    # earns cannot be re-earned by the same review on the tick after it.
    now = datetime.now(timezone.utc)

    def stamp(seconds_ago: int) -> str:
        return (now - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

    assert runner.sweep(state=state, status_at=stamp(60), review_at=stamp(30)) == []


# ── The saturation-cancel loop: skip already-active dispatches ──────────────


def test_a_queued_dispatch_prevents_another_dispatch(runner: Runner) -> None:
    """Core property of the fix.

    The sweep sees a stale PR and wants to dispatch. But a workflow_dispatch run
    for this exact (pr, sha) is already QUEUED in the same concurrency group
    (pr-readiness-wd-{pr}-{sha}). Dispatching again would cancel that queued run
    -- GitHub always evicts the pending slot when a new run enters the group,
    regardless of cancel-in-progress. The sweep must skip the pair.
    """
    sha = "4328fd0f941f09ff10f245fbdb4accf7c246febe"
    dispatched = runner.sweep(
        state="pending",
        status_at="2020-01-01T00:00:00Z",
        check_completed_at="2020-01-01T00:30:00Z",
        sha=sha,
        active_wd_runs=[{"id": 9900001, "pr": "2064", "sha": sha, "status": "queued"}],
    )
    assert dispatched == []
    assert "already queued or in progress" in runner.last_stdout


def test_an_in_progress_dispatch_prevents_another_dispatch(runner: Runner) -> None:
    """An in-progress run holds the concurrency slot; re-dispatching is wasteful."""
    sha = "4328fd0f941f09ff10f245fbdb4accf7c246febe"
    dispatched = runner.sweep(
        state="pending",
        status_at="2020-01-01T00:00:00Z",
        check_completed_at="2020-01-01T00:30:00Z",
        sha=sha,
        active_wd_runs=[{"id": 9900002, "pr": "2064", "sha": sha, "status": "in_progress"}],
    )
    assert dispatched == []
    assert "already queued or in progress" in runner.last_stdout


def test_a_completed_active_run_does_not_block_dispatch(runner: Runner) -> None:
    """Only queued/in_progress runs indicate an active slot; completed ones do not."""
    sha = "4328fd0f941f09ff10f245fbdb4accf7c246febe"
    dispatched = runner.sweep(
        state="pending",
        status_at="2020-01-01T00:00:00Z",
        check_completed_at="2020-01-01T00:30:00Z",
        sha=sha,
        active_wd_runs=[{"id": 9900003, "pr": "2064", "sha": sha, "status": "completed"}],
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]


def test_a_queued_dispatch_for_a_different_sha_does_not_block(runner: Runner) -> None:
    """The skip is keyed on the exact sha, not just the PR number.

    A queued run for (pr=2064, sha=OLD) must not suppress a dispatch for
    (pr=2064, sha=NEW): a new push was made and the stale verdict is for the
    new head, which has no active run covering it.
    """
    sha = "4328fd0f941f09ff10f245fbdb4accf7c246febe"
    dispatched = runner.sweep(
        state="pending",
        status_at="2020-01-01T00:00:00Z",
        check_completed_at="2020-01-01T00:30:00Z",
        sha=sha,
        active_wd_runs=[
            {
                "id": 9900004,
                "pr": "2064",
                "sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "status": "queued",
            }
        ],
    )
    assert len(dispatched) == 1
    assert "sha=%s" % sha in dispatched[0]


def test_no_active_runs_fixture_dispatches_normally(runner: Runner) -> None:
    """When active_wd_runs is None (the normal case) the skip set is empty."""
    dispatched = runner.sweep(
        state="pending",
        status_at="2020-01-01T00:00:00Z",
        check_completed_at="2020-01-01T00:30:00Z",
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]
