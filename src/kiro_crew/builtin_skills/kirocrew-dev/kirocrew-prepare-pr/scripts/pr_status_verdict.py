"""The readiness verdict: what a PR's state, check rows and review markers decide.

Loaded by ``pr_status.py``, which re-exports every name defined here but its loader; it is
not run on its own. It holds the classification of one check row, the collapse of rows a newer run
of the same check displaced, the readiness decision itself (``decide``: exit code 0, 10
or 20 and the status line), whether the pinned review lanes have settled the head, and
the shape of the ``--json`` report. Reading the PR, its checks and its comments from GitHub stays in
``pr_status.py``, with every name a test replaces there. New work on what blocks, waits
or passes goes here.

``decide`` reads the review contract's lane classes, so this module loads
``_review_contract.py`` the way ``pr_status.py`` loads its siblings, and ``pr_status.py``
takes its ``_review_contract`` from here: one contract per load.
"""

import importlib.machinery
import importlib.util
import os


class _NoBytecodeSourceLoader(importlib.machinery.SourceFileLoader):
    """Load shipped source normally while suppressing cache writes."""

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        source = self.get_data(path)
        return self.source_to_code(source, path)

    def set_data(self, path, data, *, _mode=0o666):
        return None


def _load_sibling(filename, name):
    """Load a sibling script without cwd, sys.path, or bytecode side effects."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    loader = _NoBytecodeSourceLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot import kirocrew-prepare-pr sibling script: " + path)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


_review_contract = _load_sibling("_review_contract.py", "_prepare_pr_review_contract")


# Explicit state classification (classify every state; fail closed).
PASS_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
# StatusContext (legacy commit statuses) use .state rather than .conclusion.
CTX_PASS = {"SUCCESS"}
CTX_RUNNING = {"PENDING", "EXPECTED"}
ROLLUP_HEAD_MOVED_NOTICE = (
    "CI check status DISCARDED - the PR head changed between the core read and "
    "the rollup read (concurrent push); treat CI as UNKNOWN and re-run for a "
    "consistent snapshot"
)


def _host_id(value):
    """``value`` when it can serve as a host-supplied id, else None.

    bool is refused because it is an int subclass and would group rows under True.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def classify_check(entry):
    """Return 'pass' | 'running' | 'fail' for one statusCheckRollup entry.

    Fail-closed: any unrecognized COMPLETED conclusion or unknown shape counts
    as 'fail' rather than silently passing.
    """
    status = (entry.get("status") or "").upper()
    conclusion = (entry.get("conclusion") or "").upper()
    state = (entry.get("state") or "").upper()
    if status:  # CheckRun
        if status != "COMPLETED":
            return "running"  # queued/in-progress/any non-terminal state
        return "pass" if conclusion in PASS_CONCLUSIONS else "fail"
    if state:  # StatusContext
        if state in CTX_PASS:
            return "pass"
        if state in CTX_RUNNING:
            return "running"
        return "fail"
    return "fail"  # unknown shape -> fail closed


def superseded_key(entry):
    """The identity a CheckRun row can be superseded within, or None to exempt it.

    The workflow DEFINITION's id, the run's triggering EVENT and the check name.
    Never the workflow's display name: a host permits two workflow files to carry
    one ``name:``, and each may publish a check of the same name, so a label would
    group two independent workflows and the collapse would drop one of them. The
    event is in it because one file can declare several triggers, and a file on
    ``push`` and ``pull_request`` produces two runs of itself on one commit --
    concurrent dispatches rather than an attempt and its replacement, so only a
    later run of the SAME trigger may replace an earlier one.

    None whenever the host did not supply a component, as for a check run an app
    created outside Actions (a checkSuite with no workflowRun). Grouping such rows
    under a missing key would put every one of them into one identity and drop
    all but the newest; the only thing left to key on would be the display name,
    which is what this refuses.
    """
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        return None
    definition = _host_id(entry.get("workflowDefinitionId"))
    if definition is None:
        return None
    event = entry.get("workflowRunEvent")
    if not isinstance(event, str) or not event:
        return None
    return (definition, event, name)


def _displaced(entry):
    """Whether removing this row of an OLDER run takes no verdict with it.

    The rollup carries no lineage edge: a greater run id proves only that a run
    started later, and a later run of one identity can be an independent dispatch
    (``synchronize`` and ``edited`` are both ``pull_request``), so recency alone
    never licenses dropping a row. A run the concurrency group CANCELLED in favour
    of its successor is the one displacement the rollup does establish, and it is
    a property of the RUN, read from the run: a row reaches CANCELLED inside live
    runs too -- fail-fast cancelling a matrix job's siblings, a job cancelled
    because something in its ``needs`` failed, an operator cancelling one job --
    and in each the run concluded FAILURE. The row's own COMPLETED+CANCELLED is
    required as well, because a cancelled run can still hold a row that reached a
    real FAILURE before the cancel landed.
    """
    if entry.get("workflowRunConclusion") != "CANCELLED":
        return False
    status = (entry.get("status") or "").upper()
    conclusion = (entry.get("conclusion") or "").upper()
    return status == "COMPLETED" and conclusion == "CANCELLED"


def collapse_superseded(rollup):
    """Drop the check rows a newer run of the same check has displaced.

    A host keeps a replaced round's rows in the rollup beside the round that
    replaced them, so counting every row inflates the failure count with
    attempts that are not live. This is the rule the structured monitor's GitHub
    provider enforces (``_mark_superseded_rows``), applied by removal:

    * Identity is ``superseded_key``: workflow definition, run event, check name.
    * Newest is the greatest RUN id, which increases monotonically. Not the row's
      ``startedAt`` -- that is when a JOB got a runner, and a busy queue starts an
      older run's job after a newer run's.
    * A row is removed only when it belongs to an older run than the newest of
      its identity AND ``_displaced`` holds. A replaced round's FAILURE rows stay;
      a cancelled row inside a live run stays; a cancelled NEWEST run stays, since
      dropping it would revive the verdict it superseded.
    * Two rows of ONE run never displace each other: a workflow can publish a
      check run under its own job's display name, so both are live.
    * A row without a key or a run id is exempt and takes no part in choosing the
      newest run, since it may BE the run that would supersede the others.
    * A StatusContext is never collapsed. The host reports one status per
      context, the latest, so there is nothing to order; the provider keeps every
      status row too, and a rule here would be a second place to be wrong.

    Input order is preserved. When in doubt, over-report rather than hide a
    live failure.
    """
    newest_run: dict[tuple, int] = {}
    for e in rollup:
        if e.get("context"):
            continue
        key = superseded_key(e)
        run_id = _host_id(e.get("workflowRunId"))
        if key is None or run_id is None:
            continue
        if key not in newest_run or run_id > newest_run[key]:
            newest_run[key] = run_id
    kept = []
    for e in rollup:
        if e.get("context"):
            kept.append(e)
            continue
        key = superseded_key(e)
        run_id = _host_id(e.get("workflowRunId"))
        if key is None or run_id is None or run_id == newest_run[key]:
            kept.append(e)
            continue
        if not _displaced(e):
            kept.append(e)
    return kept


def reviewer_round_settled(marker_eval):
    """Whether AI review for this head is decided, regardless of the other checks.

    True only when the fleet was PINNED (``--reviewers`` / the loop's own
    profile names), the comments were readable, every pinned lane is answered
    for this head -- a fresh ``[<NAME>-REVIEWED]`` stamp, or an accepted human
    override record naming this head -- and at least one posted
    ``[BLOCK-MERGE]``. That combination is terminal for the head: the diff has
    to change, so the tests, packaging and lint runs still in flight are
    running on a commit that is already condemned.

    Deliberately narrow in three ways. It needs a pinned fleet, because in
    discovery mode an empty ``stale`` only says every lane that has SPOKEN is
    fresh -- a lane still composing its review is invisible, and acting on the
    first blocker would throw its verdict away. It needs a blocker, because a
    settled round with no blocker has nothing to act on and must fall through
    to the normal running/clean path. And it says nothing about failing
    non-reviewer checks: a red test while a lane is still pending stays a
    wait, so one round still fixes one full set of findings.
    """
    if not marker_eval:
        return False
    return bool(
        marker_eval.get("pinned")
        and marker_eval.get("ok")
        and not marker_eval.get("stale")
        and marker_eval.get("blocking")
    )


def build_report(
    *,
    number,
    url,
    head_sha,
    readiness_kind,
    failing_checks,
    n_fail,
    n_unresolved,
    marker_eval,
    code,
    status,
    green_age=None,
    supersession_eval=None,
):
    """Build the --json report.

    Every field here has a named consumer in the babysit skill; nothing is
    emitted speculatively. The shape is split because the whole object is NOT
    safe to compare between runs:

    ``progress_key`` holds only fields that change when real progress happens,
    so a polling loop can compare it byte-for-byte to tell "still stuck" from
    "something moved". Everything unstable lives under ``advisory``, which the
    loop reads when checking its exit conditions but never includes in the
    comparison:

    * a finding a writer rebutted or deferred is re-raised every round, so a key
      including finding counts would never stabilise and a stall would never be
      recognised -- and the count also moves when a bot merely re-words a comment;
    * ``unresolved_threads`` degrades to null on any API error or page cap, so a
      transient blip would read as progress.

    Ambient PR state (mergeable, merge_state, review_decision, check totals) is
    deliberately NOT emitted: the prose above already prints all of it, the loop
    reads it there, and ``pr_findings.py`` owns the exit-20 drill-in. A second
    machine-readable copy with no reader is a surface to keep in sync for nothing.
    """
    return {
        "exit_code": code,
        "pr": number,
        "status": status,
        "url": url,
        # Compare THIS between cycles -- nothing else.
        "progress_key": {
            "checks_failing": n_fail,
            "exit_code": code,
            "failing_checks": sorted(failing_checks),
            "head_sha": head_sha,
            "readiness_kind": readiness_kind,
            # The verdict reason, so a CHANGED blocker cannot read as an
            # unchanged one. exit_code and the failing-check set do not
            # distinguish "blocked by a conflict" from "blocked by a review
            # marker": both are exit 20 and can carry an identical check set, so
            # without this a real change in what blocks the PR would extend a
            # stall streak instead of resetting it. Deterministic by
            # construction -- decide() joins its reasons in a fixed order from
            # sorted inputs.
            "status": status,
        },
        "advisory": {
            "blocking_reviewers": sorted(marker_eval.get("blocking") or []),
            "bot_comments_readable": bool(marker_eval.get("ok")),
            "elided_stamp_reviewers": sorted(marker_eval.get("elided") or []),
            "findings": dict(marker_eval.get("findings") or {}),
            # Verdicts for THIS head that a later same-head sample replaced. The
            # lanes whose block was dropped are the gating half and reach the
            # status line through decide(); these two fields are what a machine
            # reads instead of the human-only report section. `readable` false is
            # UNKNOWN, never "none found".
            "superseded_verdicts": {
                "blocking_dropped": sorted((supersession_eval or {}).get("blocking_dropped") or []),
                "lanes_seen": int((supersession_eval or {}).get("lanes_seen") or 0),
                "readable": bool((supersession_eval or {}).get("ok")),
            },
            # Advisory, and deliberately OUTSIDE progress_key: the base moving is
            # not this PR making progress, and on a repo that merges every couple
            # of minutes a commit count in the key would reset the stall streak
            # forever. The babysit trigger reads this field; the tripwire does not.
            "green_age": dict(green_age or {"ok": False, "reason": "not measured"}),
            # {lane: actor} a repository writer adjudicated at this head instead
            # of the model. Advisory and OUTSIDE progress_key for the same
            # reason the finding counts are: it is state about the head, not a
            # thing that moves when the PR makes progress. Separate from
            # stale_reviewers rather than merged into it -- a reader auditing
            # this head has to be able to tell a model verdict from a human one.
            "overridden_reviewers": dict(sorted((marker_eval.get("overridden") or {}).items())),
            "stale_reviewers": sorted(marker_eval.get("stale") or []),
            "unresolved_threads": n_unresolved,
        },
    }


def decide(
    state,
    mergeable,
    merge_state,
    decision,
    draft,
    readiness_kind,
    n_running,
    n_fail,
    n_checks,
    readiness_context,
    marker_eval=None,
    head_run="skip",
    rollup_notice="",
    disposition_eval=None,
    supersession_eval=None,
):
    """Resolve PR state to (exit_code, status line). Fail-closed.

    Exit codes: 0 = clean, 10 = wait (nothing to do yet), 20 = act.

    Precedence is the load-bearing part, and it is ordered by "can waiting
    change this answer?" rather than by how the fields arrive:

    1. A non-open PR is terminal, and must be decided BEFORE any wait: GitHub
       reports mergeable=UNKNOWN for merged/closed PRs forever, so waiting on
       it returns 10 on every poll and a loop never stops.
    2. Conditions waiting CANNOT fix outrank "still running". A conflicted PR is
       the case that matters: the host cannot build a merge ref for it, so it
       dispatches no pull_request workflows at all and every check visible
       belongs to the old head. Ranking in-flight checks first reports "running"
       forever while nothing can complete -- a stall only a human notices.
       BEHIND, draft and CHANGES_REQUESTED behave the same way: each survives
       any amount of waiting and needs the author to act. A SETTLED reviewer
       round (``reviewer_round_settled``) joins them: once every pinned lane
       has stamped this head and one blocks, the edit is required, so the
       backend/frontend runs still in flight are spending minutes on a commit
       already condemned -- Phase 3 cancels them instead of waiting them out.
       Only the AI-review lanes gate this; a non-reviewer check still running
       does not hold the decision open, and a red one does not force it. The disposition
       evaluation (``disposition_eval``, built from disposition_violations)
       belongs here too, in BOTH its states: a violation is cleared only by
       the author editing or deleting the offending comment, and an
       UNREADABLE evaluation is not "no violations" -- while either waits
       behind an in-flight round, the reviewer bots rewrite their stamped
       comments in place, so the judged-head span mapping the verdict needs
       disappears with the old stamps. It gates because a record that claims
       several findings, another lane's finding, or no finding at all is
       exactly the blanket ruling the one-lane / one-rationale rule forbids
       -- and the adjudication ledger would consume it as written.
    3. Only then is "still running" a wait, and an uncomputed mergeability too.
    4. Everything left is a check-result verdict -- including the reviewer-side
       conditions: ``marker_eval`` (from evaluate_reviewer_markers; None skips
       the gate) and ``head_run`` (True/False/None from head_run_exists; the
       string "skip" skips the gate). Both are evaluated ONLY here, after the
       running gate: while a round is in flight the reviewer bots may simply
       not have posted yet, and a stale stamp mid-round is expected, not a
       defect. Both fail closed on "could not read".
       ``rollup_notice`` -- the non-empty NOTICE string when the rollup read
       degraded (fetch failure, or a concurrent push between the two reads) --
       never changes the exit code either, only which reason an empty rollup
       earns: a degraded read names its environment cause, so the reason (and
       ``progress_key.status``, which carries it) distinguishes an environment
       gap from a genuinely check-less PR.
    """
    if state != "OPEN":
        return 20, "STATUS: BLOCKED - PR state is {} (not OPEN; terminal)".format(state or "?")

    blocked_now = []
    if mergeable == "CONFLICTING" or merge_state in ("DIRTY", "CONFLICTING"):
        blocked_now.append("merge conflict / not mergeable")
    if merge_state == "BEHIND":
        blocked_now.append("branch is BEHIND base - re-sync onto the latest base")
    if draft:
        blocked_now.append("PR is a draft")
    if decision == "CHANGES_REQUESTED":
        blocked_now.append("review decision is CHANGES_REQUESTED")
    if reviewer_round_settled(marker_eval):
        # The reviewer round is DONE even though the rollup is not: every
        # pinned lane stamped this head and at least one blocks. Waiting for
        # the remaining lanes (backend/frontend tests, packaging) cannot
        # change that the diff must be edited, and their verdicts do not
        # survive the edit anyway -- the push re-runs them on the new head.
        # So this belongs with the other "waiting cannot fix this" conditions
        # ABOVE the running gate. Requires a PINNED fleet: discovery mode
        # cannot tell "all lanes reported" from "one lane reported first",
        # and acting there would drop a verdict that was still coming.
        blocked_now.append(
            "reviewer round complete with blocking marker [BLOCK-MERGE] on "
            "current head from: " + ", ".join(sorted(marker_eval["blocking"]))
        )
    if disposition_eval is not None:
        # A disposition violation is a condition waiting cannot fix -- only the
        # AUTHOR editing or deleting the comment clears it -- so it belongs
        # with the other author-action conditions ABOVE the running gate.
        # Deferring it behind an in-flight round loses the evidence: the
        # reviewer bots rewrite their stamped comments in place when the round
        # completes, and the judged-head span mapping the violation was
        # computed from disappears with the old stamps. An UNREADABLE
        # evaluation (records or the finding map could not be fetched) gates
        # here for the same reason: "could not read" is not "no violations",
        # and waiting through the round destroys the very evidence a re-read
        # would need. Sorted by construction (disposition_violations returns a
        # sorted list), so the joined reason -- which travels in
        # ``progress_key.status`` -- is deterministic.
        if not disposition_eval.get("ok"):
            blocked_now.append("disposition records could not be established (fail-closed)")
        for v in disposition_eval.get("violations") or []:
            blocked_now.append("disposition rule: " + v)
    if blocked_now:
        return 20, "STATUS: BLOCKED - " + "; ".join(blocked_now)

    # An observed running lane keeps the round open regardless of the
    # aggregate. The aggregate's context name is a forgeable display string, so
    # if a passed aggregate could conclude the "still running" gate, a forged
    # green posted while a real lane is still QUEUED/IN_PROGRESS would skip this
    # branch and reach CLEAN before the real failure lands -- the same
    # forged-green-to-CLEAN vector, moved into a timing window. So an observed
    # running row means RUNNING on its own terms, and a failing aggregate is
    # reported below. The aggregate subtracts neither a pending nor a failing
    # row.
    if readiness_kind == "running" or n_running > 0:
        return 10, "STATUS: RUNNING (round not complete)"
    if mergeable not in ("MERGEABLE", "CONFLICTING"):
        return 10, "STATUS: RUNNING (mergeability not yet computed: {})".format(
            mergeable or "UNKNOWN"
        )

    reasons = []
    # An observed failing row is authoritative on its own terms: a passed
    # aggregate does not clear it. The aggregate's context name is a display
    # string any status publisher on the pull request can set, so letting a
    # green aggregate erase a failing row would let a forged green flip this
    # tool to CLEAN over a real failure. So report a failing row whenever one
    # exists, independent of the aggregate, and report a failing aggregate as
    # its own reason. The aggregate subtracts no observed row -- neither a
    # pending one (handled by the running gate above) nor a failing one here.
    if readiness_kind == "fail":
        reasons.append("{} reported action required".format(readiness_context))
    if n_fail > 0:
        reasons.append("{} check(s) failed".format(n_fail))
    if n_checks == 0:
        # An empty rollup has two very different causes, and the reason chosen
        # here travels in ``progress_key.status``, which a polling loop
        # compares byte-for-byte across runs. A degraded rollup read (the
        # NOTICE the caller printed) is an environment gap -- token scope, an
        # API error, a concurrent push -- while an empty rollup from a healthy
        # read means the host truly reports no checks for this head. One
        # shared reason would make those indistinguishable, so a loop would
        # re-poll a token problem forever instead of escalating it.
        if rollup_notice == ROLLUP_HEAD_MOVED_NOTICE:
            reasons.append(
                "CI status unreadable - the PR head moved between reads "
                "(concurrent push) - transient environment, re-run for a "
                "consistent snapshot (fail-closed)"
            )
        elif rollup_notice:
            reasons.append(
                "CI status unreadable - the rollup fetch failed (a token "
                "without Checks read access, a 403, or a rate limit) - "
                "environment gap, not a code defect (fail-closed)"
            )
        else:
            reasons.append("no CI checks reported - cannot confirm CI (fail-closed)")
    if marker_eval is not None:
        if not marker_eval.get("ok"):
            reasons.append("reviewer comments could not be read (fail-closed)")
        else:
            if marker_eval.get("blocking"):
                # sorted(): ``blocking`` is a set, and joining a set of strings
                # is only order-stable WITHIN one process. This status string is
                # part of progress_key, which a polling loop compares
                # byte-for-byte across separate runs, so an unsorted join would
                # make two identical states differ whenever more than one
                # reviewer blocks.
                reasons.append(
                    "blocking review marker [BLOCK-MERGE] on current head from: "
                    + ", ".join(sorted(marker_eval["blocking"]))
                )
            if marker_eval.get("stale"):
                reasons.append(
                    "stale reviewer stamp(s) - no [<NAME>-REVIEWED] for current head: "
                    + ", ".join(marker_eval["stale"])
                )
    # A block this lane raised for THIS head, replaced by a later sample that does
    # not block. Waiting cannot fix it -- the replacement already happened -- so it
    # is an act reason, matching what the required status does with the same field.
    #
    # An UNREADABLE evaluation gates here too, exactly as the marker and
    # disposition evaluations above do: "could not read the stored history" is not
    # "no verdict was superseded", and this exit code is what arms `gh pr merge
    # --auto`, so failing open here would let one transient GraphQL failure arm
    # auto-merge over a dropped review block. The printed report already calls this
    # state "fail-closed - this is not a clean result"; without this branch the
    # exit code contradicted its own report. It sits BELOW the running gate rather
    # than in ``blocked_now`` because an unreadable history is transient and
    # waiting genuinely can fix it -- unlike a disposition violation, which only
    # the comment's author can clear.
    #
    # This is not the required status's rule and does not change it: there,
    # ``ok=false`` maps to `pending`, never to a red, because a status that turns
    # "I could not answer" into a failure blocks every writer on a transient. The
    # two surfaces answer different questions -- "is this revision red" versus
    # "may this loop arm auto-merge" -- and UNKNOWN answers no to the second.
    #
    # Scoped to cause="unreadable", the read that FAILED on a lane that exists.
    # The other cause, an empty population, is already covered here: the
    # supersession read only runs when the markers were readable, so zero lanes
    # examined means no bound lane comment exists, and "no [<NAME>-REVIEWED] for
    # current head" above is the reason that states it. Failing closed on that too
    # would add a second voice for one condition and report BLOCKED on every pull
    # request whose reviewers have not commented yet, for a population where
    # nothing could have been superseded.
    if supersession_eval is not None and supersession_eval.get("cause") == "unreadable":
        reasons.append(
            "superseded review verdicts could not be established (fail-closed) - "
            + (supersession_eval.get("error") or "supersession could not be evaluated")
        )
    for lane in (supersession_eval or {}).get("blocking_dropped") or []:
        # Both halves of this text were wrong and each sent the reader somewhere
        # the code does not look. `/ai-review override <lane> <head>` is an exit
        # for EVERY lane, not a new head, and on a fork PR too: the accepted
        # record itself adjudicates every block the lane raised at this head
        # before it was recorded (superseded_verdicts reads it), and the
        # lane's override arm, same-repo and fork alike, also replaces the slot
        # with a note that carries no `[<LANE>-REVIEWED] <head>` stamp, so
        # `current_stamped` goes False. And
        # what clears the GPT family by decision is the workflow-authored
        # `(all downgraded on adjudication)` heading, NOT the
        # `[BLOCK-MERGE-DOWNGRADED]` marker: _sanctioned_downgrade reads the
        # heading precisely because the marker sits in embedded model output.
        # Naming a new head first is the expensive advice, since it discards every
        # other lane's verdict for this head.
        if lane.upper() in _review_contract.DOWNGRADE_LANES:
            remedy = (
                "if the clear is legitimate, record it at this head: "
                "/ai-review override, or an adjudication pass, whose "
                "'(all downgraded on adjudication)' heading is read here as "
                "cleared. Pushing a new head also re-samples it, at the cost of "
                "every other lane's verdict for this head"
            )
        else:
            remedy = (
                "if the clear is legitimate, record it at this head with "
                "/ai-review override, whose accepted record adjudicates every "
                "block this lane raised at this head before it, on a fork PR "
                "too. This lane has no "
                "adjudication heading, so the only other exit is a new head, at "
                "the cost of every other lane's verdict for this head"
            )
        reasons.append(
            "superseded verdict: {} blocked this head in a replaced sample and the "
            "body now presented does not - read the comment's stored history; "
            "{}".format(lane, remedy)
        )
    if head_run is False:
        reasons.append(
            "no pull_request-event workflow run for the current head - the "
            "checks shown may belong to an older head (stale or conflicted PR)"
        )
    elif head_run is None:
        reasons.append(
            "could not confirm a pull_request-event run for the current head (fail-closed)"
        )
    if merge_state and merge_state not in (
        "CLEAN",
        "HAS_HOOKS",
        "UNSTABLE",
        "BLOCKED",
        "DIRTY",
        "CONFLICTING",
        "DRAFT",
        "BEHIND",
    ):
        # BLOCKED = pending required review (expected for a review-ready PR);
        # anything unrecognized is fail-closed.
        reasons.append("unrecognized merge state '{}' (fail-closed)".format(merge_state))

    if reasons:
        return 20, "STATUS: BLOCKED - " + "; ".join(reasons)
    return 0, "STATUS: CLEAN (readiness passed, mergeable, no blocking review decision)"
