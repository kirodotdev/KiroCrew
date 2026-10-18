"""Reading the runs and jobs a tick judges, within the tick's read budget.

The repo-wide run listings (``_iter_repo_runs``, ``gather_all_candidate_runs``, the
cancelled index ``list_recent_cancelled_runs``), the per-tick bound on job reads that picks
which live runs are classified (``live_runs_within_read_bound``, ``top_up_unread_evidence``,
``drop_ghost_runs``), and ``list_jobs``. The bounds are knobs of ``runner_watchdog``. The
ones the tests patch (``GHOST_AFTER``, ``LIVE_CLASSIFY_READS``, ``LIVE_EVIDENCE_RESERVE``,
``LIVE_EVIDENCE_TOPUP_READS``, ``RECOVERY_LISTING_MAX_PAGES``) are read here as
``runner_watchdog.<name>`` at call time, so a patch reaches them; the others are imported
once, and a test that starts patching one makes the patch-reach guard fail until it is
read the same way.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Callable

import runner_watchdog
from runner_watchdog import (
    _WATCHED_SET,
    ACTIONABLE_CANDIDATE_STATUSES,
    CANDIDATE_STATUSES,
    JOBS_LISTING_MAX_PAGES,
    PAGE_SIZE,
    REPO_LISTING_MAX_PAGES,
    REPO_LISTING_MAX_WINDOWS,
    REPO_LISTING_RESULT_CEILING,
    Api,
    ApiError,
    _safe_text,
)
from runner_watchdog_parts.classify import (
    DispatchEvidence,
    Policy,
    _fmt_delta,
    _workflow_of,
    is_fork_run,
    parse_timestamp,
)
from runner_watchdog_parts.heal_safety import heal_safe_declared


def _runs_path(repo: str, workflow: str) -> str:
    return f"repos/{repo}/actions/workflows/{workflow}/runs"


def _iter_repo_runs(
    api: Api,
    repo: str,
    *,
    status: str,
    max_pages: int,
    truncated: list[str] | None = None,
    get: Callable[[str], Any] | None = None,
    log: Callable[[str], None] = print,
    created_at_most: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield runs of EVERY workflow with the given status, newest first, across at
    most ``max_pages`` pages of ``PAGE_SIZE``.

    ``created_at_most`` narrows the listing to runs created at or before that
    instant (``created=<=…``); see ``_iter_actionable_runs``.

    ``GET /repos/{repo}/actions/runs?status=…`` is repo-wide: one paginated call
    returns runs of every workflow, so a single call chain per status covers the
    watched set. The caller filters to the watched set from each run's ``path``.

    Paging stops at the first EMPTY page, or at ``max_pages`` -- the page cap that
    bounds a runaway. A SHORT page does not end this listing: measured against
    `status=queued` on this repository, the endpoint returns 98, then 100, then
    100, then 99 while `total_count` stands at 927, so a page below ``PAGE_SIZE``
    is a property of the index rather than the tail. Treating one as the end stops
    a tick after page one, which collapses the reach to the newest ~2 minutes of
    runs while the orphan threshold is 15 minutes, and hides a six-hour outage.

    An empty page is not proof of the tail either, because the endpoint serves one
    at ``REPO_LISTING_RESULT_CEILING`` too. So reach is judged by comparing what was
    yielded against the first page's ``total_count`` -- but only a shortfall with a
    NAMEABLE cause appends to ``truncated``: the walk spent ``max_pages``, or it
    stopped on an empty page at that ceiling. A shortfall below both is CHURN, since
    ``total_count`` is a page-one snapshot of a set that mutates while the walk runs,
    and it is logged rather than recorded. Opt-in per call site on purpose:
    truncating the recovery pass is normal and its docstring says so, and a
    ``pending`` listing hides nothing a heal could act on, while an unread tail on an
    ACTIONABLE status means the tick could not look where orphans are.
    """
    fetch = get or api.get
    page = 1
    yielded = 0
    total: int | None = None
    hit_cap = False
    while page <= max_pages:
        # The page size never changes between pages: ``page`` is an offset in
        # units of ``per_page``, so a smaller page would re-read the head of the
        # listing and never reach the runs it was meant to fetch.
        params: dict[str, Any] = {"status": status, "per_page": PAGE_SIZE, "page": page}
        if created_at_most is not None:
            params["created"] = f"<={created_at_most}"
        query = urllib.parse.urlencode(params)
        payload = fetch(f"repos/{repo}/actions/runs?{query}")
        batch = (payload or {}).get("workflow_runs") or []
        if total is None:
            reported = (payload or {}).get("total_count")
            total = reported if isinstance(reported, int) and reported > 0 else None
        if not batch:
            break
        yield from batch
        yielded += len(batch)
        hit_cap = page == max_pages
        page += 1
    if total is None or yielded >= total:
        return
    # A shortfall alone does not mean the tail went unread. ``total_count`` is a
    # snapshot of page one, and a live status set MUTATES during a ten-page walk:
    # a run that leaves `queued` between two page reads lands as `yielded < total`
    # with every page delivered. Failing a tick on that would fail on CHURN, which
    # on a busy repository is most ticks -- and a signal that fires for a non-reason
    # is the one nobody reads.
    #
    # So the tick only fails where the shortfall has a NAMEABLE cause: the walk
    # spent its page cap, or it stopped on an empty page at the result ceiling,
    # where the endpoint serves one instead of the next run. Everything else is
    # reported as the warning it was before.
    at_ceiling = yielded >= REPO_LISTING_RESULT_CEILING
    message = (
        f"{status} run listing reached {yielded} of {total} run(s) "
        f"({page - 1} page(s) read, cap {max_pages}, API ceiling "
        f"{REPO_LISTING_RESULT_CEILING})"
    )
    if hit_cap or at_ceiling:
        log(f"::warning::{message}; the oldest were not read")
        if truncated is not None:
            truncated.append(message)
        return
    log(f"::warning::{message}; the set moved during the walk, and every page was delivered")


def _is_watched(run: dict[str, Any]) -> bool:
    """Whether a repo-wide-listed run belongs to a watched workflow, by its ``path``."""
    return _workflow_of(run) in _WATCHED_SET


def list_runs(api: Api, repo: str, workflow: str, *, status: str, cap: int) -> list[dict[str, Any]]:
    """Newest runs of one watched workflow with the given status filter, at most
    ``cap``, oldest first.

    Workflow-scoped on purpose, and not replaceable by a page of the repo-wide
    listing: that index is ordered by CREATION time, while this sample wants a
    recent COMPLETION. Watched workflows differ by an order of magnitude in
    duration (`fast-gate.yml` runs in about 1.5 minutes, `ci.yml` in about 20),
    so a repo-wide page mixes them and a long run that finished seconds ago sits
    below every short run created since it started. Scoped to one workflow, whose
    runs share a duration, creation order tracks completion order and the newest
    page holds the newest finishes. The total stays capped at ``COMPLETED_SAMPLE``
    either way.

    A SHORT page is not the tail here either: this is the same status-filtered runs
    index as the repo-wide listing, only scoped to one workflow, and that index
    returns pages below ``per_page`` mid-listing. So paging stops on the cap or an
    EMPTY page, never on a short one. In practice a watched workflow's first page
    already exceeds ``cap``, so the empty-page read costs nothing for the
    workflows this samples."""
    runs: list[dict[str, Any]] = []
    page = 1
    while len(runs) < cap:
        # The page size never changes between pages: ``page`` is an offset in
        # units of ``per_page``, so a smaller final page would re-read the head
        # of the listing and never reach the runs it was meant to fetch.
        query = urllib.parse.urlencode({"status": status, "per_page": PAGE_SIZE, "page": page})
        payload = api.get(f"{_runs_path(repo, workflow)}?{query}")
        batch = (payload or {}).get("workflow_runs") or []
        if not batch:
            break
        runs.extend(batch)
        page += 1
    del runs[cap:]
    return sorted(runs, key=lambda run: run["created_at"])


def _iter_actionable_runs(
    api: Api,
    repo: str,
    *,
    status: str,
    truncated: list[str],
    get: Callable[[str], Any] | None = None,
    log: Callable[[str], None] = print,
) -> Iterator[dict[str, Any]]:
    """Yield every run of an ACTIONABLE status, reading past the result ceiling.

    One listing stops at ``REPO_LISTING_RESULT_CEILING``, and the unread tail is the
    oldest runs -- where an orphan sits. So when a window ends at the ceiling (or
    its page cap), the next window lists the same status created at or before the
    oldest run already read. ``<=`` rather than ``<`` so runs that share that
    second are not skipped; the caller drops the repeats by id.

    Truncation is recorded only for the LAST window, and only when it still hit a
    nameable cause: the window cap ran out, or a full window made no progress
    (every run in it shares one ``created_at``), so narrowing cannot reach further.
    """
    upper: str | None = None
    for window in range(1, REPO_LISTING_MAX_WINDOWS + 1):
        sink: list[str] = []
        oldest: str | None = None
        for run in _iter_repo_runs(
            api,
            repo,
            status=status,
            max_pages=REPO_LISTING_MAX_PAGES,
            truncated=sink,
            get=get,
            log=log,
            created_at_most=upper,
        ):
            created = _safe_text(run.get("created_at"))
            if created and (oldest is None or created < oldest):
                oldest = created
            yield run
        if not sink:
            return
        if oldest is None or oldest == upper or window == REPO_LISTING_MAX_WINDOWS:
            truncated.extend(f"{message} after {window} created window(s)" for message in sink)
            return
        log(f"{status} run listing: narrowing to runs created at or before {oldest}")
        upper = oldest


def gather_all_candidate_runs(
    api: Api,
    repo: str,
    *,
    get: Callable[[str], Any] | None = None,
    log: Callable[[str], None] = print,
) -> tuple[list[dict[str, Any]], ApiError | None, list[str]]:
    """Live runs of every watched workflow, any rate-limit abort, and any listing
    that hit its page cap; runs oldest first.

    One paginated repo-wide listing per candidate status covers every workflow at
    once, and each run is kept only if its ``path`` names a watched workflow. This
    is one listing per status instead of one per watched workflow, and it reaches a
    fleet-routed workflow nobody registered (returned, then dropped for not being
    in the watched set) -- breadth a per-workflow chain cannot have. It is NOT
    strictly more: breadth and depth are separate axes, and this form buys the
    first only while ``_iter_repo_runs`` pages deep enough for the second. No
    newest-N truncation is applied here -- an orphan is an OLD run at the tail of
    the newest-first listing, so keeping only the newest N would discard exactly
    the runs being hunted; the page cap bounds the listing cost, and the global
    per-tick heal cap still bounds how many runs are acted on.

    An actionable status is read through ``_iter_actionable_runs``, which narrows
    by created window past the result ceiling. The third element names each
    ACTIONABLE status whose listing still did not reach its own ``total_count``. It is a tick-level failure rather than a note, because the
    unread tail is where an orphan sits: see ``OUTCOME_LISTING_TRUNCATED``. A
    ``pending`` shortfall is logged and not recorded, because nothing in that status
    is ever healed -- and `pending` is exactly what grows during the saturation this
    script has to survive, so failing a tick over it would go red for a non-reason
    at the worst moment.
    """
    truncated: list[str] = []
    seen: dict[int, dict[str, Any]] = {}
    try:
        for status in CANDIDATE_STATUSES:
            if status in ACTIONABLE_CANDIDATE_STATUSES:
                runs = _iter_actionable_runs(
                    api, repo, status=status, truncated=truncated, get=get, log=log
                )
            else:
                runs = _iter_repo_runs(
                    api, repo, status=status, max_pages=REPO_LISTING_MAX_PAGES, get=get, log=log
                )
            for run in runs:
                if _is_watched(run):
                    seen.setdefault(int(run["id"]), run)
    except ApiError as exc:
        if not exc.rate_limited:
            raise
        return sorted(seen.values(), key=lambda run: run["created_at"]), exc, truncated
    return sorted(seen.values(), key=lambda run: run["created_at"]), None, truncated


def list_all_candidate_runs(
    api: Api, repo: str, *, log: Callable[[str], None] = print
) -> list[dict[str, Any]]:
    """Live runs of every watched workflow, deduplicated, oldest first."""
    runs, aborted, _truncated = gather_all_candidate_runs(api, repo, log=log)
    if aborted is not None:
        raise aborted
    return runs


def _heal_eligible_shape(run: dict[str, Any], repo: str) -> bool:
    """Whether a listed run has a SHAPE a heal could act on, from the listing alone.

    Priority only, never authorization. Every real gate still runs afterwards and
    can still refuse: the workflow's concurrency group read at the run's own
    revision, the successor check, the saturation hold. What this reads is what
    those gates cannot reverse -- an event a heal can act on (``push`` or
    ``pull_request``), a head repository that is this one (no token can re-run a
    fork's run), and a workflow declared heal-safe for that event. All three are on
    the listing row, so the ranking costs no read.
    """
    event = _safe_text(run.get("event"))
    workflow = _workflow_of(run)
    return (
        event in {"push", "pull_request"}
        and not is_fork_run(run, repo)
        and workflow is not None
        and heal_safe_declared(workflow, event)
    )


def _can_carry_a_slow_start(run: dict[str, Any], policy: Policy) -> bool:
    """Whether a run could hold a served start that waited past ``saturation_wait``.

    Two conditions. The run must be in a status that has jobs at all -- a ``pending``
    run is held by its concurrency group with none created, so it can carry no start
    at any age. And it must be at least ``saturation_wait`` old, since a younger one
    cannot contain a wait that long.

    Deliberately NOT a third condition on ``updated_at``. A run untouched for longer
    than ``saturation_lookback`` looks unable to hold a countable start, and excluding
    those would cut the count materially: measured, 352 listed runs pass the two
    conditions above and only 193 were touched inside the lookback. But a run is quiet
    for exactly two reasons, and the API cannot tell them apart from the listing: it
    is a zombie nothing will ever happen to, or ITS JOB HAS BEEN QUEUED THAT WHOLE
    TIME -- the saturation this hold exists for. When such a job is finally served,
    the listing that called its run incapable was a snapshot taken a moment before,
    and the pre-cancel re-read narrows that window without closing it. So the price of
    the two conditions is an over-hold on zombie runs, which costs a heal, against an
    under-hold that cancels work the fleet was about to serve.
    """
    if not _may_hold_jobs(run):
        return False
    return policy.now - parse_timestamp(str(run["created_at"])) >= policy.saturation_wait


def _may_hold_jobs(run: dict[str, Any]) -> bool:
    """The age-free half of ``_can_carry_a_slow_start``: does this run have jobs at all.

    Split out because the two halves age differently. A run's STATUS is a reading of
    the listing and goes stale only when the listing is re-read; its AGE grows with
    the wall clock, so a run just under the line at the sweep is over it minutes
    later. The unread set is therefore retained by this half and re-judged on age at
    each use, rather than collapsed to one integer at the sweep's clock.
    """
    # ACTIONABLE_CANDIDATE_STATUSES answers "may this run be healed"; the question
    # here is "can this run carry a job at all". The two coincide because every status
    # in which jobs exist is also one a heal can act on, and `pending` -- held by its
    # concurrency group with no jobs created -- is in neither. A status added here for
    # heal purposes must be checked against BOTH readings before it is added.
    return _safe_text(run.get("status")) in ACTIONABLE_CANDIDATE_STATUSES


def _evidence_reserve(runs: list[dict[str, Any]], policy: Policy) -> list[dict[str, Any]]:
    """The reserve slice: newest runs that could hold a slow start.

    ``runs`` arrives oldest first. A run that cannot carry a served start past the
    line -- too young, or ``pending`` and so jobless -- can only ever show the fleet
    dispatching, so reserving a read for it leaves the reserve unable to answer the
    other way. Preferring the newest runs that CAN keeps the reserve current while
    letting it answer both.
    """
    capable = [run for run in runs if _can_carry_a_slow_start(run, policy)]
    # Falls back to the newest runs when none is capable: they still show the
    # fleet dispatching, and an empty reserve would read as an outage.
    source = capable or runs
    return source[-runner_watchdog.LIVE_EVIDENCE_RESERVE :]


def drop_ghost_runs(
    runs: list[dict[str, Any]], policy: Policy, log: Callable[[str], None] = print
) -> list[dict[str, Any]]:
    """The listed live runs minus the ghosts: records past ``GHOST_AFTER`` in a live status.

    Applied before the read bound is drawn, so a ghost is neither read nor counted as
    an unread run able to hold a slow start (see ``GHOST_AFTER`` for why both matter).
    Dropping is by AGE alone, from the listing: the reads that could tell more -- jobs,
    or the run itself -- are the cost being saved. The age is taken from the NEWER of
    ``created_at`` and ``run_started_at``. A re-run keeps the ``created_at`` of the run
    it re-runs and moves ``run_started_at`` to the attempt, so an attempt an operator
    (or this watchdog) started on a run two days old is as young as that attempt; a
    ghost never started, and its two stamps agree. One line names how many were
    dropped, so a tick that acted on a shorter set than it listed says so.
    """
    live: list[dict[str, Any]] = []
    ghosts = 0
    oldest: datetime | None = None
    for run in runs:
        created = parse_timestamp(str(run["created_at"]))
        started_raw = run.get("run_started_at")
        started = parse_timestamp(str(started_raw)) if started_raw else created
        newest = max(created, started)
        if policy.now - newest >= runner_watchdog.GHOST_AFTER:
            ghosts += 1
            oldest = newest if oldest is None or newest < oldest else oldest
            continue
        live.append(run)
    if ghosts:
        log(
            f"::notice::{ghosts} listed run(s) older than {_fmt_delta(runner_watchdog.GHOST_AFTER)} dropped as "
            f"ghosts (oldest last started {oldest.isoformat() if oldest else '?'}): GitHub "
            "cancels a job that has not started within a day, so a run still listed live "
            "past that has nothing a heal could act on, and reading it would only spend the bound"
        )
    return live


def live_runs_within_read_bound(
    runs: list[dict[str, Any]], policy: Policy, log: Callable[[str], None] = print
) -> tuple[list[dict[str, Any]], dict[int, datetime]]:
    """The live runs whose jobs this sweep may read: oldest first, evidence reserved.

    The reads serve two purposes that pull opposite ways. Classifying an orphan
    wants the OLDEST runs, the only ones that can be actionable. Judging whether
    the fleet still dispatches wants a run's served CodeBuild starts, which is the
    evidence ``resolve_hold`` weighs. A bound spent purely oldest first starves
    the second at exactly backlog scale, and a sweep with no dispatch evidence
    heals nothing, which is safe but useless precisely when the watchdog is
    needed. So the head of the bound goes to the oldest runs and its tail is
    reserved for evidence.

    The reserve takes the newest runs THAT ARE THEMSELVES at least
    ``policy.saturation_wait`` old. A younger run cannot contain a start that
    waited that long, so reserving the newest runs outright yields prompt starts
    only and can never see the slow start that holds -- the reserve would answer
    one of the hold's two questions and be structurally blind to the other. When
    no run is old enough, the newest are taken anyway: they can still show the
    fleet dispatching, and a sweep with nothing at all falls to the
    completed-run sample instead.

    Within the head, runs that could actually be acted on go first: the
    ``_heal_eligible_shape`` pair AND an age past the orphan threshold. Oldest-first
    alone
    ranks by age, and the oldest live runs at this repository are runs no heal can
    ever act on: measured on this repository, 220 watched live runs sit past the
    orphan threshold and 18 past a day, the oldest 36 days, runs that stay listed
    and therefore re-read the same slots on every tick -- a fork's, which no token
    can re-run, or a closed pull request's, which is cancelled and not re-read. A
    same-repository run of a workflow declared heal-safe for its event -- the only
    kind that can be cleared, and the kind that holds a branch's concurrency slot
    while it is stuck -- ranked 30th of 40 slots at six hours old. That margin shrinks as
    zombies accumulate, so age is the ordering WITHIN each class rather than
    across them.

    The age condition is what keeps the priority honest now that the reserve no
    longer absorbs the youngest runs: every ``push`` run of a heal-safe workflow
    carries the eligible shape, so shape alone would let this minute's pushes
    outrank an orphan stuck for hours. They are still read, just not ahead of it.
    """
    runs = drop_ghost_runs(runs, policy, log)
    if len(runs) <= runner_watchdog.LIVE_CLASSIFY_READS:
        return runs, {}
    # Disjoint by construction, so a run cannot be read twice: the reserve is cut
    # out of the listing before the classify pool is drawn from what remains.
    reserved = _evidence_reserve(runs, policy)
    reserved_ids = {run["id"] for run in reserved}
    pool = [run for run in runs if run["id"] not in reserved_ids]
    # Sized off the reserve ACTUALLY taken, not off the constant. A short listing hands
    # back fewer than LIVE_EVIDENCE_RESERVE slots, and subtracting the constant would
    # leave that difference unspent -- reading fewer runs than the bound allows, which
    # classifies fewer orphans and leaves more unread capable runs holding the heal.
    oldest = runner_watchdog.LIVE_CLASSIFY_READS - len(reserved)
    banded = sum(1 for run in reserved if _can_carry_a_slow_start(run, policy))
    # One line, emitted here rather than before the reserve is computed: the split it
    # names has to be the split this tick actually took. Interpolating the constants
    # earlier printed 40/10 on a tick that then spent 47/3, so the read-budget
    # diagnostic described a division no sweep performed.
    log(
        f"::notice::{len(runs)} live runs reached the per-tick live job-read cap of "
        f"{runner_watchdog.LIVE_CLASSIFY_READS}; the {oldest} classified are drawn actionable-shaped "
        "first (heal-eligible and past the orphan threshold) then oldest first, and "
        f"{len(reserved)} reads are reserved for the newest runs able to hold a start "
        f"that waited {_fmt_delta(policy.saturation_wait)} ({banded} of them can), whose "
        "served CodeBuild starts are the dispatch evidence; the rest wait for the next "
        "tick, and any unread run old enough to hold a slow start holds the heal back"
        + (
            ""
            if banded == len(reserved)
            else " -- the shortfall is the newest-runs fallback, which reports the fleet "
            "dispatching but cannot show a slow start"
        )
    )

    def actionable_shape(run: dict[str, Any]) -> bool:
        # Shape AND age: a run younger than the orphan threshold is never actionable,
        # and every run of a heal-safe workflow carries the heal-eligible shape, so
        # shape alone would let the youngest pushes outrank an orphan that has been
        # stuck for hours. They are still read, just not ahead of it.
        return (
            _heal_eligible_shape(run, policy.repo)
            and policy.now - parse_timestamp(str(run["created_at"])) >= policy.orphan_after
        )

    eligible = [run for run in pool if actionable_shape(run)]
    rest = [run for run in pool if not actionable_shape(run)]
    classified = (eligible + rest)[:oldest]
    # The caller reads jobs in the order given and the sweep's log is read
    # chronologically, so the whole selection is sorted by age. Sorting the classify
    # slice alone was enough only while the reserve was the newest runs outright; the
    # reserve is now an age band in the middle, so it has to be sorted in with them.
    selected = sorted(classified + reserved, key=lambda run: run["created_at"])
    selected_ids = {run["id"] for run in selected}
    # The second value is the premise the partial hold is judged on: the runs this
    # sweep did not read that could hold a served start past the threshold, kept as
    # id -> creation time so the judgement is re-made on the clock of each later use
    # instead of frozen here. Retaining the runs that COULD have carried a start rather
    # than reporting "the bound was reached" is what makes the hold's premise
    # checkable: a sweep whose unread band could not have held one may act. It does not
    # make the hold rare here -- about 302 unread capable runs against a 50-read bound
    # -- it makes it exact: a bound spent entirely on runs too young, or on jobless
    # `pending` runs, leaves nothing unseen and releases the heal, where keying on the
    # bound alone never did.
    unread_candidates = {
        int(run["id"]): parse_timestamp(str(run["created_at"]))
        for run in runs
        if run["id"] not in selected_ids and _may_hold_jobs(run)
    }
    return selected, unread_candidates


def top_up_unread_evidence(
    api: Api,
    policy: Policy,
    evidence: DispatchEvidence,
    log: Callable[[str], None] = print,
    *,
    read_jobs: Callable[[int], list[dict[str, Any]]] | None = None,
) -> tuple[int, int]:
    """Read the unread runs that hold the partial-evidence hold, when that buys a heal.

    ``live_runs_within_read_bound`` leaves the runs past its bound unread, and every
    unread run old enough to hold a slow start holds every heal of the tick
    (``SKIPPED_PARTIAL_EVIDENCE``). That bound is spent on every tick; this read is
    spent only on a tick that has an orphan to judge, which is the caller's decision
    and the reason this is not folded into the bound. The runs are read oldest first so
    the sweep's log stays chronological, each one's served starts are absorbed as
    evidence, and each one leaves the retained unread set, so the hold judged after
    this call is judged on what remains.

    All or nothing against ``LIVE_EVIDENCE_TOPUP_READS``: the capable set is read in
    full or not at all. A partial read cannot lift the hold -- any capable run still
    unread holds it -- so it would spend reads against the shared installation quota
    for no heal. The set is judged at ``policy.now``: a run under the age line at this
    call is not read, and if it crosses the line before a later judgement it turns the
    hold on from that moment, which is the same rule the hold itself follows.

    Returns ``(read, unread)``: how many runs were read, and how many capable runs are
    still unread because the set exceeded the bound.
    """
    reader = read_jobs or (lambda run_id: list_jobs(api, policy.repo, run_id))
    capable = sorted(
        (created, run_id)
        for run_id, created in evidence.unread_candidates.items()
        if policy.now - created >= policy.saturation_wait
    )
    if not capable:
        return 0, 0
    if len(capable) > runner_watchdog.LIVE_EVIDENCE_TOPUP_READS:
        log(
            f"::notice::{len(capable)} unread live runs could hold a slow CodeBuild start, past "
            f"the {runner_watchdog.LIVE_EVIDENCE_TOPUP_READS}-read top-up bound; none read, since reading part of "
            "them cannot lift the partial-evidence hold"
        )
        return 0, len(capable)
    log(
        f"::notice::reading the jobs of {len(capable)} unread live run(s) that could hold a slow "
        f"CodeBuild start (top-up bound {runner_watchdog.LIVE_EVIDENCE_TOPUP_READS}), because an orphan is waiting "
        "on the saturation question they hold open"
    )
    for _created, run_id in capable:
        evidence.absorb(reader(run_id), policy)
        evidence.unread_candidates.pop(run_id, None)
    return len(capable), 0


def list_recent_cancelled_runs(
    api: Api, repo: str, *, log: Callable[[str], None] = print
) -> list[dict[str, Any]]:
    """Cancelled runs of every watched workflow, oldest cancellation update first.

    The reach is BEST-EFFORT and deliberately carries no coverage claim. GitHub orders
    this index by CREATION time while recovery selects by cancellation time, and offers
    no ordering by the latter, so nothing readable from the listing can establish that
    every run cancelled inside the window was seen: a long-lived run cancelled during a
    burst sits past any page cap, which is exactly what the 2026-09-20 orphans looked
    like. Three rounds of review went into inventing a signal for that gap; each spelling
    claimed more than the API can answer, so the honest form is the truncation WARNING
    the paging already logs, plus this docstring. A cancelled run that never got listed
    needs `gh run rerun` by hand, and the reach only changes how often that is true.
    """
    watched: dict[int, dict[str, Any]] = {}
    for run in _iter_repo_runs(
        api,
        repo,
        status="cancelled",
        max_pages=runner_watchdog.RECOVERY_LISTING_MAX_PAGES,
        log=log,
    ):
        if _is_watched(run):
            watched.setdefault(int(run["id"]), run)
    return sorted(
        watched.values(),
        key=lambda run: parse_timestamp(str(run.get("updated_at") or run["created_at"])),
    )


def list_jobs(api: Api, repo: str, run_id: int) -> list[dict[str, Any]]:
    """Every job of the run's latest attempt.

    A SHORT page does not end this listing either, but the reason it is safe to keep
    reading differs from the runs index. The payload carries ``total_count``, so the
    tail is known exactly and a page below ``per_page`` costs nothing: the loop stops
    when that many jobs are in hand. The measurement behind the short-page rule was
    taken on the status-filtered runs index, not here, and ``filter=latest`` is the
    same shape of post-paging filter -- so a re-run run's jobs page can be short
    mid-listing, and a job dropped for that reason is a queued fleet job the tick
    never sees, which is the orphan evidence the whole classification rests on.
    Without ``total_count`` the listing falls back to reading until an EMPTY page,
    which costs one extra call rather than risking a dropped job.
    """
    jobs: list[dict[str, Any]] = []
    total: int | None = None
    page = 1
    while page <= JOBS_LISTING_MAX_PAGES:
        query = urllib.parse.urlencode({"per_page": PAGE_SIZE, "page": page, "filter": "latest"})
        payload = api.get(f"repos/{repo}/actions/runs/{run_id}/jobs?{query}")
        batch = (payload or {}).get("jobs") or []
        if not batch:
            return jobs
        jobs.extend(batch)
        if total is None:
            reported = (payload or {}).get("total_count")
            total = reported if isinstance(reported, int) and reported >= 0 else None
        if total is not None and len(jobs) >= total:
            return jobs
        page += 1
    return jobs
