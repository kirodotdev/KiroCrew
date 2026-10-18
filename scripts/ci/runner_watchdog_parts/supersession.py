"""Whether a run still carries its branch's or pull request's verdict, or a newer run does.

``newest_run_id_for_branch`` and ``_current_or_successor_for_pull_request`` find the run
that carries the verdict now; ``is_current_run`` and ``supersession_clears_hold`` turn that
into a heal or hold decision; and an unanswerable listing raises ``LookupInconclusive``,
which stays in ``runner_watchdog``. ``is_current_run`` is a patch seam: every caller,
here and in the other owners, reaches it as ``runner_watchdog.is_current_run``.
"""

from __future__ import annotations

import urllib.parse
from typing import Callable

import runner_watchdog
from runner_watchdog import (
    BRANCH_LISTING_DEPTH,
    BRANCH_LISTING_MAX_PAGES,
    PULL_REQUEST_LISTING_DEPTH,
    SUPERSEDED_SUCCESSOR_UNIDENTIFIED,
    SUPERSEDED_WITHOUT_SUCCESSOR,
    Api,
    ApiError,
    LookupInconclusive,
)
from runner_watchdog_parts.classify import Policy, RunVerdict, _label, _pull_request_numbers
from runner_watchdog_parts.listing import _runs_path


def newest_run_id_for_branch(api: Api, repo: str, verdict: RunVerdict) -> int:
    """The newest run of this branch, event AND head repository.

    The listing's ``branch`` filter matches the head branch by NAME, and a fork
    pull request whose branch happens to share the name (``main``, ``patch-1``)
    is listed alongside, newest first. Pages are read until BOTH the newest
    same-head-repository run and the run being judged have been seen: the first
    is the answer, the second is the proof that the listing reached far enough
    to be trusted. A listing that shows an older same-repository run but not the
    run being judged is inconsistent (the judged run exists and is newer), and
    answering "superseded" from it would leave a cancelled run behind with a
    green tick, so the lookup raises instead. So does a listing that runs out of
    pages: "still newest" is the answer that lets a re-run cancel a newer run
    through the concurrency group, and it is never the default.
    """
    newest: int | None = None
    page = 1
    while page <= BRANCH_LISTING_MAX_PAGES:
        query = urllib.parse.urlencode(
            {
                "branch": verdict.head_branch,
                "event": verdict.event,
                "per_page": BRANCH_LISTING_DEPTH,
                "page": page,
            }
        )
        try:
            payload = api.get(f"{_runs_path(repo, verdict.workflow)}?{query}")
        except ApiError as exc:
            # The listing is the only thing standing between a re-run and a
            # newer run's concurrency group; without it no verdict is safe.
            raise LookupInconclusive(
                f"the branch listing for {verdict.head_repo}:{verdict.head_branch} could not be read: {exc}"
            ) from exc
        runs = (payload or {}).get("workflow_runs") or []
        for run in runs:
            run_id = int(run["id"])
            head_repo = str((run.get("head_repository") or {}).get("full_name") or "")
            if newest is None and head_repo.lower() == verdict.head_repo.lower():
                newest = run_id
            if run_id == verdict.run_id:
                # The judged run is in view, so the newest-first prefix above it
                # is complete: whatever same-repository run came first is the answer.
                return newest if newest is not None else run_id
        if not runs:
            # An EMPTY page is the tail; a SHORT one is not. This is the same
            # status-ordered runs index that returns pages below ``per_page``
            # mid-listing, only filtered by branch and event, and stopping on a
            # short page here answers "not within the newest listed runs" while
            # the judged run sits on the very next page -- refusing a re-run the
            # evidence allows, and leaving a cancelled run behind a green tick.
            break
        page += 1
    raise LookupInconclusive(
        f"run {verdict.run_id} of {verdict.head_repo}:{verdict.head_branch} ({verdict.event}) is not "
        f"within the {BRANCH_LISTING_MAX_PAGES * BRANCH_LISTING_DEPTH} newest listed runs, so whether a "
        f"newer run exists cannot be told"
    )


def _open_pull_request_heads(api: Api, repo: str, verdict: RunVerdict) -> set[str]:
    """The head SHAs of every OPEN pull request whose head is this run's branch.

    Read from the pulls API by ``head=<owner>:<branch>``, which is exact on the
    branch name within one head repository -- a fork's same-named branch is a
    different owner and is not listed. Two pull requests open on the same head
    branch return the same SHA twice; the set is what matters. One page, and a full
    page is refused: it may have a tail, and "current" must never be answered from
    a listing that might not contain the run's pull request.
    """
    owner = verdict.head_repo.partition("/")[0] or repo.partition("/")[0]
    query = urllib.parse.urlencode(
        {
            "head": f"{owner}:{verdict.head_branch}",
            "state": "open",
            "per_page": PULL_REQUEST_LISTING_DEPTH,
        }
    )
    try:
        pulls = api.get(f"repos/{repo}/pulls?{query}")
    except ApiError as exc:
        raise LookupInconclusive(
            f"the open pull requests on {verdict.head_repo}:{verdict.head_branch} could not be read: {exc}"
        ) from exc
    if not isinstance(pulls, list):
        raise LookupInconclusive(
            f"the open pull requests on {verdict.head_repo}:{verdict.head_branch} came back malformed"
        )
    if len(pulls) >= PULL_REQUEST_LISTING_DEPTH:
        raise LookupInconclusive(
            f"{verdict.head_repo}:{verdict.head_branch} has {len(pulls)} or more open pull requests, "
            f"more than one page; whether run {verdict.run_id} is current cannot be told"
        )
    return {
        str(((pull or {}).get("head") or {}).get("sha") or "").lower()
        for pull in pulls
        if isinstance(pull, dict)
    } - {""}


def _current_or_successor_for_pull_request(api: Api, repo: str, verdict: RunVerdict) -> int:
    """The pull-request analogue of ``newest_run_id_for_branch``: judged by HEAD SHA,
    then by the listing for a newer run AT that SHA.

    Two reads. The pulls API says which head SHAs the open pull requests on the
    branch have; the runs listing (this branch, this event, same head repository,
    paged newest-first until the judged run is seen, as the push path does) says
    which newer runs exist. Neither alone is enough.

    The SHA answers supersession by PUSH, and answers it without the ambiguity a
    branch-name listing has: two pull requests sharing a head branch carry the same
    head SHA, so a run at that SHA is current for both, and nothing here needs
    ``pull_requests[].number`` for that. No open pull request has the branch: the
    request closed or merged, ``SUPERSEDED_WITHOUT_SUCCESSOR``. The head moved: the
    successor is the newest listed run at an open head that the payloads identify as
    the same pull request's.

    The listing answers supersession WITHOUT a push. `labeled`, `unlabeled`,
    `edited` and `reopened` each start a new run at the SAME head SHA, and every
    declared pull-request workflow cancels the run in progress when one arrives. A
    re-run of the older run would cancel that newer one through the group and
    stand in its place -- with no successor named, nothing would restore it. So a
    newer same-repository run at the judged run's own SHA is read as this run's
    successor when the two payloads name a common pull request, ignored as a sibling
    pull request's run (a shared head branch) when they name disjoint ones, and
    otherwise -- most same-repository runs carry no ``pull_requests[]`` at all --
    the answer is ``LookupInconclusive``, naming the runs, because "current" is what
    licenses a cancel through the other run's group and is never assumed. That is
    deliberately not resolved by observing the group's own cancel: every orphan this
    script meets exists during a fleet outage, where the run to observe stays queued
    and observation cannot settle, and a green outcome resting on an unobserved
    provider behaviour is the false green this script must never produce.

    A successor is named only when identified as the same pull request's. When the
    head moved while the pull request stays open, the answer is
    ``SUPERSEDED_SUCCESSOR_UNIDENTIFIED`` whether a newer run at the new head is
    listed and unidentified or not listed yet: superseded for every judgement made
    before a mutation, never a run to restore -- restoring a sibling's run would
    leave the same pull request's run, cancelled through the group, unreported.
    """
    open_heads = _open_pull_request_heads(api, repo, verdict)
    if not open_heads:
        return SUPERSEDED_WITHOUT_SUCCESSOR
    own_sha = verdict.head_sha.lower()
    at_open_head = own_sha in open_heads
    successor: int | None = None
    ambiguous: list[int] = []
    page = 1
    while page <= BRANCH_LISTING_MAX_PAGES:
        query = urllib.parse.urlencode(
            {
                "branch": verdict.head_branch,
                "event": verdict.event,
                "per_page": BRANCH_LISTING_DEPTH,
                "page": page,
            }
        )
        try:
            payload = api.get(f"{_runs_path(repo, verdict.workflow)}?{query}")
        except ApiError as exc:
            raise LookupInconclusive(
                f"the branch listing for {verdict.head_repo}:{verdict.head_branch} could not be read: {exc}"
            ) from exc
        runs = (payload or {}).get("workflow_runs") or []
        for run in runs:
            run_id = int(run["id"])
            if run_id == verdict.run_id:
                # The judged run is in view, so every newer run of the branch has
                # been seen and the answer is settled. An identified same-pull-request
                # successor decides it whatever else was seen.
                if successor is not None:
                    return successor
                if ambiguous:
                    raise LookupInconclusive(
                        f"run(s) {', '.join(str(r) for r in ambiguous)} are newer {verdict.event} "
                        f"runs of {verdict.head_repo}:{verdict.head_branch} at the same head SHA "
                        f"{own_sha[:12]} as run {verdict.run_id}, and whether each belongs to the "
                        f"same pull request (its successor, from a label or edit) or to another pull "
                        f"request sharing the branch cannot be told from the payloads; re-running "
                        f"{verdict.run_id} could cancel a successor, so neither is assumed"
                    )
                if at_open_head:
                    return verdict.run_id
                # The head moved past this run while its pull request stays open. Whether
                # a newer run at the new head is listed and unidentified, or not listed
                # yet, the answer is the same: superseded, with no run to restore.
                return SUPERSEDED_SUCCESSOR_UNIDENTIFIED
            head_repo = str((run.get("head_repository") or {}).get("full_name") or "")
            if head_repo.lower() != verdict.head_repo.lower():
                continue
            head_sha = str(run.get("head_sha") or "").lower()
            if head_sha not in open_heads:
                continue
            theirs = _pull_request_numbers(run)
            same_pull_request = bool(set(theirs) & set(verdict.pull_request_numbers))
            if same_pull_request:
                if successor is None:
                    successor = run_id
                continue
            if theirs and verdict.pull_request_numbers:
                # Disjoint, both known: a sibling pull request's run on a shared head
                # branch, in its own concurrency group. Neither successor nor threat.
                continue
            if head_sha == own_sha:
                ambiguous.append(run_id)
        if not runs:
            break
        page += 1
    raise LookupInconclusive(
        f"run {verdict.run_id} of {verdict.head_repo}:{verdict.head_branch} ({verdict.event}) is not "
        f"within the {BRANCH_LISTING_MAX_PAGES * BRANCH_LISTING_DEPTH} newest listed runs, so which "
        f"run succeeded it cannot be told"
    )


def current_or_successor_id(api: Api, repo: str, verdict: RunVerdict) -> int:
    """The run that carries this branch's verdict now: this run, its successor, or
    ``SUPERSEDED_WITHOUT_SUCCESSOR``.

    Dispatches on the event. A push run is judged by the branch listing
    (``newest_run_id_for_branch``); a pull-request run by head SHA against the open
    pull requests on the branch and by the listing for a newer run at that SHA
    (``_current_or_successor_for_pull_request``). Every
    caller asks this, never one shape's function directly, so the two shapes cannot
    drift apart at one call site and not another.
    """
    if verdict.event == "pull_request":
        return _current_or_successor_for_pull_request(api, repo, verdict)
    return newest_run_id_for_branch(api, repo, verdict)


def is_current_run(api: Api, repo: str, verdict: RunVerdict) -> bool:
    """Re-running a run that a newer push has superseded would cancel the newer run
    through the workflow's own concurrency group, so every re-run checks this first."""
    return current_or_successor_id(api, repo, verdict) == verdict.run_id


def supersession_clears_hold(
    api: Api, policy: Policy, verdict: RunVerdict, log: Callable[[str], None]
) -> bool:
    """Whether a fleet hold has nothing left to protect on this run. One branch listing.

    The dispatch-evidence hold exists so a queued job that a runner may still pick
    up is not cancelled out from under the run that wants its result. A run a newer
    push has superseded has no result anyone wants: the branch moved on, and the heal
    path's own pre-re-run check will decline to re-run it for exactly that reason. So
    whether the fleet is dispatching, saturated or out decides nothing about it.

    What holding it DOES cost: a workflow whose group keeps superseded runs alive
    (``cancel-in-progress: false``, which ``ci.yml`` and ``fast-gate.yml`` both set on
    ``main``) admits one running plus one pending run, so a superseded run that never
    terminates holds the running slot and GitHub evicts every later commit's run from
    the pending slot. Measured: ``fast-gate.yml`` run 35893226216 sat ``queued`` on one
    orphaned job for 6 hours behind a partial-evidence hold; every later ``main`` Fast
    Gate was evicted without running, and ``ci.yml``'s ``await-fast-gate`` failed
    closed at its 720-second budget on each one, so 25 pushes produced 11 failures and
    zero verdicts.

    Asked ONLY when a hold would otherwise apply, so the ordinary orphan pays no extra
    listing. Push and pull-request runs alike, through ``current_or_successor_id``: a
    pull-request run is judged by head SHA against its branch's open pull requests
    and by the listing for a newer run at that SHA.
    The head repository must be this repository; a fork cannot push to its branches,
    so that test is belt-and-braces rather than the fork boundary itself.

    A lookup that cannot answer leaves the hold standing: cancelling needs
    supersession ESTABLISHED, never assumed from a failed read.

    Releasing the hold does not by itself cancel anything. The run stays an orphan
    and reaches ``heal_runs``, which for any attempt past the first asks
    ``_rerun_attempt_may_be_cancelled`` immediately before its cancel: a
    superseded later attempt may be somebody's own ``gh run rerun``, so it is left
    untouched under a FAILED outcome that names the run. That guard sits at the
    cancel rather than here so that a held run and an unheld one end the same
    way -- reported red, not held green with no outcome -- and so the answer is
    given once, where the irreversible step is.
    """
    if verdict.event not in {"push", "pull_request"}:
        return False
    if verdict.head_repo.lower() != policy.repo.lower():
        return False
    try:
        if runner_watchdog.is_current_run(api, policy.repo, verdict):
            return False
    except LookupInconclusive as exc:
        log(
            f"{_label(verdict)}: the fleet hold stands, because whether a newer run "
            f"supersedes this one cannot be told ({exc})"
        )
        return False
    log(
        f"{_label(verdict)}: a newer push supersedes it, so the fleet hold has no result "
        f"to protect and is not applied; freeing its concurrency group is what a cancel "
        f"would then buy, the re-run check still declines to re-run it, and past attempt 1 "
        f"the heal path declines even the cancel"
    )
    return True


def _rerun_attempt_may_be_cancelled(
    api: Api, policy: Policy, verdict: RunVerdict, log: Callable[[str], None]
) -> bool:
    """Whether cancelling an orphan past attempt 1 can still end in a re-run.

    Cancelling a superseded orphan is never followed by a re-run: ``_rerun``
    declines a superseded run, and the ``superseded-before-cancel`` outcome is not
    a failed one. For attempt 1 that is the intended trade -- nobody re-ran it, so
    nothing anyone did is lost, and the cancel frees its concurrency group. A later
    attempt may BE somebody's ``gh run rerun`` of the stuck run, the operator
    response this script's own logs ask for, and cancelling it ends in silent loss:
    the recovery pass will not restore it either, because ``classify_cancelled_run``
    classifies a superseded cancelled run out of ``CANCELLED_ORPHAN``. So the
    question is asked HERE, immediately before the cancel and on every route to it
    (a fleet hold released by ``supersession_clears_hold`` lands here too): a run
    that is still its branch's newest is cancelled and re-run like any orphan; a
    superseded one, or one whose supersession cannot be told, is left untouched --
    and reported as a FAILED outcome, because the run then holds its concurrency
    group until a human frees it, and nothing else will tell them.
    """
    try:
        if runner_watchdog.is_current_run(api, policy.repo, verdict):
            return True
    except LookupInconclusive as exc:
        log(
            f"::error::{_label(verdict)}: left untouched, because this is attempt "
            f"{verdict.run_attempt} and whether a newer push supersedes it cannot be told "
            f"({exc}); cancelling a re-run attempt that turns out superseded would discard "
            f"somebody's work irrecoverably. It holds its concurrency group until a human "
            f"decides: `gh run cancel {verdict.run_id}` if the branch has moved on, "
            f"`gh run rerun {verdict.run_id}` if its result is still wanted."
        )
        return False
    log(
        f"::error::{_label(verdict)}: left untouched, because this is attempt "
        f"{verdict.run_attempt} and a newer push supersedes it: the cancel would not be "
        f"followed by a re-run, and a re-run attempt may be somebody's own, so cancelling "
        f"it could discard their work irrecoverably. It holds its concurrency group, and "
        f"every later push's run is evicted behind it, until a human frees it: "
        f"`gh run cancel {verdict.run_id}`."
    )
    return False


def _supersession_is_answerable(
    api: Api, policy: Policy, verdict: RunVerdict, log: Callable[[str], None]
) -> bool:
    """Whether the successor question for a first-attempt PULL-REQUEST orphan can be
    answered, asked immediately before its cancel.

    The cancel comes first and the re-run second, and only the re-run asks whether a
    newer run supersedes this one. For a first attempt a superseded answer is the
    intended trade (the cancel frees the group; nothing anyone did is lost), so the
    answer itself is not needed here. An UNANSWERABLE lookup is another matter: after
    the cancel it leaves the run cancelled with nobody to re-run it, the lost verdict
    this script exists to prevent. On a push run that takes a failed listing read,
    rare enough that the push path keeps its one-listing budget and reports it after
    the fact. On a pull-request run it is an ordinary shape: a newer run at the same
    head SHA whose pull request neither payload names, so successor or sibling cannot
    be told, and cancelling THIS run on that uncertainty would destroy a verdict that
    may be the current one. So for a pull-request run the lookup is made here, before
    anything irreversible, and an inconclusive answer leaves the run untouched under
    the same failed outcome the re-run would have reported, naming the run and the
    command. The answer is not cached: the re-run asks again on the state after the
    cancel, which is the state that matters for it, and a same-SHA run that appears in
    between is met there the same way (withdrawn, failed, named).
    """
    try:
        current_or_successor_id(api, policy.repo, verdict)
    except LookupInconclusive as exc:
        log(
            f"::error::{_label(verdict)}: left untouched, because whether a newer run supersedes "
            f"it cannot be told ({exc}); cancelling it first would leave it cancelled with nobody "
            f"able to re-run it. Decide by hand: `gh run cancel {verdict.run_id}` if the branch "
            f"or pull request has moved on, `gh run rerun {verdict.run_id}` if its result is still "
            f"wanted."
        )
        return False
    return True
