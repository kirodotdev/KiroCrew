"""The pipeline-conductor skill's contract, pinned against its own text.

The conductor's decisions live in two places: the bundled scripts, which are
testable as code, and the skill body, which was testable nowhere until this file
existed. That asymmetry is what this file closes. A clause the scripts depend on
-- the exit-code branch table for the claim preflight, the ``TERMINAL`` action,
the delivery counters that size admission -- can be dropped by a well-meaning
rewrite of a markdown file and nothing anywhere goes red, so the procedure and
the scripts silently stop agreeing.

The assertions are deliberately about SUBSTANCE, not sentences, and the dividing
line is stated so a later edit has a rule to follow: assert a phrase when it
states a RULE -- an obligation, a prohibition, a defined behavior, a name the
scripts share -- and do NOT assert one that only explains WHY the rule exists.
Rationale is what a rewrite is entitled to reword; pinning it produces a typo
detector that every legitimate edit has to fight, which is the failure mode that
makes text tests get deleted.

The prompt is read from the module constant rather than by running the
installer: ``test_pipeline_conductor_agent.py`` already proves that constant
reaches the written agent config, and re-stubbing the installer here would
couple this file to that one for no additional coverage.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew import agent

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "src" / "kiro_crew" / "builtin_skills" / "pipeline-conductor"
SKILL_MD = SKILL_DIR / "SKILL.md"
DESIGN_DOC = REPO_ROOT / "docs" / "design" / "pipeline-conductor.md"

#: The three scripts the procedure delegates its deterministic half to. Named
#: here rather than globbed from the directory on purpose: the point is that the
#: PROSE cites each one, and a glob would pass on a skill body that mentions
#: none of them.
BUNDLED_SCRIPTS = ("claim_preflight.py", "fleet_probe.py", "credit_spend.py")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Collapse whitespace and lowercase, so a phrase assertion survives a
    rewrap. Markdown re-flows every time a sentence is edited, and a check that
    breaks on a line break is testing the paragraph filler, not the contract.
    Leading blockquote markers go too: the work-order brief is a quote block, so
    a wrapped sentence inside it carries a ``>`` mid-phrase."""
    joined = " ".join(line.lstrip().lstrip(">") for line in text.splitlines())
    return " ".join(joined.split()).lower()


def _section(text: str, heading: str) -> str:
    """Return one markdown section's body, subsections included.

    Scoping an assertion to its own section is what keeps a check honest: a
    cadence phrase belonging to credit budgets must not be able to satisfy (or
    break) a rule about merge reconciliation just because both live in one file.
    """
    lines = text.splitlines()
    depth = len(heading) - len(heading.lstrip("#"))
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == heading.strip()),
        None,
    )
    assert start is not None, f"section missing from the skill: {heading}"
    body: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.lstrip("#")
        level = len(line) - len(stripped)
        if line.startswith("#") and level <= depth:
            break
        body.append(line)
    return "\n".join(body)


def _skill_section(heading: str) -> str:
    return _section(_read(SKILL_MD), heading)


class TestAgentPromptNamesEveryScript:
    def test_prompt_names_the_script_this_change_adds(self):
        """Only ``claim_preflight.py`` is pinned here. The other two are already
        pinned in the installer test's own name list, and re-asserting them would
        be a second place to update for one fact -- while the preflight is the one
        that gates every dispatch, so an agent unaware of it falls back to the
        one-question predicate this change replaced."""
        assert "claim_preflight.py" in _flat(agent._PIPELINE_CONDUCTOR_SYSTEM_PROMPT)

    def test_prompt_states_the_verdicts_the_conductor_branches_on(self):
        prompt = agent._PIPELINE_CONDUCTOR_SYSTEM_PROMPT
        for verdict in ("CLAIM", "SKIP", "CLOSE", "UNKNOWN"):
            assert verdict in prompt, verdict

    def test_prompt_carries_the_absent_script_rule(self):
        """A prompt that asserts the scripts are present would have the agent
        treat a file-not-found as an anomaly rather than as UNKNOWN."""
        prompt = _flat(agent._PIPELINE_CONDUCTOR_SYSTEM_PROMPT)
        assert "does not carry" in prompt
        assert "never as permission" in prompt


class TestClaimPreflightIsDocumented:
    """Dispatch step 3 used to restate a coverage predicate in prose. Prose
    cannot be tested, which is how it stayed blind to merged PRs, to prose
    self-claims, and to code that does not exist on the base yet."""

    HEADING = "### Preflight: `claim_preflight.py`"

    def test_dispatch_cites_the_script_and_not_a_prose_predicate(self):
        dispatch = _flat(_skill_section("## Pickup and dispatch"))
        assert "claim_preflight.py" in dispatch
        # The replaced predicate was a bare open-PR search presented as THE
        # coverage check. Its return would re-open the blind spot.
        assert "gh pr list --search" not in dispatch

    def test_an_absent_preflight_script_has_a_defined_behavior(self):
        """The gate cites a script, so the case where the install does not carry
        it needs an answer. Undefined, it is a file-not-found with no path
        forward; answered, it is just another UNKNOWN, which is never
        permission."""
        dispatch = _flat(_skill_section("## Pickup and dispatch"))
        assert "absent from your install" in dispatch
        assert "never permission" in dispatch
        # And specifically not a silent downgrade to the predicate it replaced.
        assert "never fall through to a bare open-pr search" in dispatch

    def test_the_skill_does_not_assume_its_scripts_are_present(self):
        """One script lands in a sibling change, so the skill's own opening must
        not assert a bundled count: a reader who is told three ship has no
        reason to reach the absent-script rule at all."""
        body = _flat(SKILL_MD.read_text(encoding="utf-8"))
        assert "presence is not assumed" in body
        assert "check at first use" in body
        assert "three bundled scripts" not in body

    def test_every_exit_code_has_a_documented_conductor_action(self):
        rows = {
            row.split("|")[1].strip()
            for row in _skill_section(self.HEADING).splitlines()
            if row.startswith("|") and row.count("|") >= 3
        }
        for code in ("0", "10", "11", "2", "3"):
            assert code in rows, f"exit code {code} has no row in the branch table"

    def test_all_four_verdicts_are_named(self):
        preflight = _skill_section(self.HEADING)
        for verdict in ("CLAIM", "SKIP", "CLOSE", "UNKNOWN"):
            assert verdict in preflight, verdict

    def test_unknown_is_never_permission(self):
        """The load-bearing half of having exit codes at all: an unanswerable
        question must not read as a green light."""
        preflight = _flat(_skill_section(self.HEADING))
        assert "never treat this as permission" in preflight

    def test_a_prose_closure_request_requires_author_authorization(self):
        """CLOSE is a WRITE driven by ingested text on an unattended cycle, and
        anyone can comment on a public item. Without an authorization condition
        this verdict lets an untrusted commenter close a live issue."""
        preflight = _flat(_skill_section(self.HEADING))
        assert "reporter or a repository insider" in preflight
        # An unauthorized phrase must not silently become a weaker verdict
        # either: it is simply not a closure request.
        assert "falls through to the remaining checks" in preflight

    def test_a_prose_self_claim_from_anyone_else_is_not_a_veto(self):
        """A veto anyone can cast is a denial-of-work channel: one comment would
        suppress a queued item indefinitely and nothing would report it. The
        signal is downgraded to risk=high rather than discarded."""
        preflight = _flat(_skill_section(self.HEADING))
        assert "from the item's reporter or a repository insider" in preflight
        assert "not a veto" in preflight

    def test_close_requires_closure_evidence_not_a_bare_reference(self):
        """A merged PR that only mentions the item is indistinguishable from one
        that fixed it, so a mention must decide nothing: closing live work and
        starving a partially-fixed item are both wrong."""
        preflight = _flat(_skill_section(self.HEADING))
        assert "claims to close the item" in preflight
        assert "not a bare cross-reference" in preflight
        assert "a mention decides nothing" in preflight

    def test_the_five_checks_are_all_named(self):
        preflight = _skill_section(self.HEADING)
        for check in (
            "merged_prs",
            "open_prs",
            "prose_claim",
            "symbol_on_base",
            "recency",
        ):
            assert check in preflight, check

    def test_a_check_that_cannot_change_the_verdict_is_not_documented(self):
        """A per-candidate forge call whose result decides nothing is pure cost
        against a shared rate limit, so it is not part of the contract."""
        preflight = _skill_section(self.HEADING)
        assert "closedByPullRequestsReferences" not in preflight
        assert "Five checks" in preflight

    def test_symbol_absence_alone_does_not_veto(self):
        """Unconditionally, that check parks every feature request naming a
        symbol it PROPOSES to add -- a permanent false veto on a whole class."""
        preflight = _flat(_skill_section(self.HEADING))
        assert "absence alone is not a skip" in preflight
        assert "downgrades to **claim** `risk=high`" in preflight

    def test_risk_high_has_a_consumer_not_just_an_annotation(self):
        """An annotation nothing acts on reads as caution and changes nothing --
        the same defect as a prose predicate."""
        preflight = _flat(_skill_section(self.HEADING))
        assert "not batched" in preflight

    def test_batch_snapshot_is_never_the_authority(self):
        """A batch verdict orders the queue; only a live recheck authorizes the
        claim, because coverage can appear in the seconds between them."""
        preflight = _flat(_skill_section(self.HEADING))
        assert "never the authority" in preflight
        assert "recheck" in preflight
        assert "mandatory" in preflight

    def test_an_already_fixed_item_is_triage_debt_not_a_dispatch(self):
        assert "triage debt" in _flat(_skill_section(self.HEADING))


class TestProbeSignalsAreDocumented:
    HEADING = "## The probe cycle"

    def test_action_table_has_a_terminal_row(self):
        """A finished worker and a wedged one read identically without it, so
        the intervention ladder fires on the wrong worker."""
        rows = [
            row
            for row in _skill_section(self.HEADING).splitlines()
            if row.startswith("|") and "`TERMINAL`" in row.split("|")[1]
        ]
        assert rows, "the action table has no TERMINAL row"
        action = rows[0].split("|")[2].lower()
        assert "close" in action
        # A terminal report must not be nudged: there is nothing to re-arm.
        assert "not nudge" in action or "never nudge" in action

    def test_tail_index_is_the_no_progress_discriminator(self):
        probe = _skill_section(self.HEADING)
        assert "i=" in probe
        flat = _flat(probe)
        assert "unchanged index" in flat
        assert "no progress" in flat

    def test_the_index_is_absolute_not_window_relative(self):
        """A window-relative index saturates once a session outgrows the tail
        bound and then reads as precisely the frozen counter the field exists to
        detect, so the semantics have to be stated, not implied."""
        flat = _flat(_skill_section(self.HEADING))
        assert "absolute per-session message counter" in flat
        assert "counted from the start" in flat

    def test_the_tail_bound_is_a_parse_bound_not_an_io_bound(self):
        """The whole transcript is read regardless; the bound only limits how
        much gets parsed. A conductor told otherwise would mis-price the index
        and mis-tune the setting."""
        flat = _flat(_skill_section(self.HEADING))
        assert "tail_bytes" in flat
        assert "parsed" in flat

    def test_the_one_time_digest_rotation_is_documented(self):
        """The fired-line digest is keyed on classified tail text, so a
        classifier change rotates digests and already-handled signals re-fire
        once. Undocumented, that burst reads as a fleet-wide regression."""
        flat = _flat(_skill_section(self.HEADING))
        assert "re-fires once" in flat

    def test_no_progress_sends_the_conductor_to_effect_not_liveness(self):
        probe = _flat(_skill_section(self.HEADING))
        assert "effect" in probe
        # The honest provenance of "still working": not the probe.
        assert "session_read_message" in probe
        assert "running flag" in probe

    def test_ok_line_carries_the_delivery_and_attribution_counters(self):
        probe = _skill_section(self.HEADING)
        for field in ("init-timeout", "watchdog", "foreign", "cwd=fleet"):
            assert field in probe, field

    def test_the_banned_row_matches_a_line_of_any_ownership_class(self):
        """The row's own pattern must not require the cwd field, or a line from a
        probe that does not classify matches nothing at all."""
        rows = [
            row
            for row in _skill_section(self.HEADING).splitlines()
            if row.startswith("|") and "BANNED" in row.split("|")[1]
        ]
        assert rows, "the action table has no BANNED row"
        assert "cwd=" not in rows[0].split("|")[1]
        # And it must route to the class-keyed response, not to a boolean.
        assert "ownership class" in rows[0].lower()

    def test_classification_tolerates_leading_markdown(self):
        """A worker writing ``**BLOCKED:**`` is following the protocol; anchoring
        on a bare prefix silently reclassifies the one message that most needs to
        be read as an escalation."""
        probe = _flat(_skill_section(self.HEADING))
        assert "tolerates leading markdown" in probe
        assert "strips leading emphasis" in probe

    def test_the_degraded_path_is_named_as_the_current_default(self):
        """On a build whose probe predates these fields the fallback fires on
        every cycle, so writing it as an edge case would understate a changed
        default."""
        probe = _flat(_skill_section(self.HEADING))
        assert "effective default" in probe

    def test_absent_probe_fields_are_unknown_not_a_clean_reading(self):
        """Symmetric to the preflight's absent-script arm, and load-bearing for
        the same reason: with no delivery counters the posture table's `ample`
        row would be satisfied by the ABSENCE of its own instrument, which is
        precisely the defect admission was re-keyed to close."""
        probe = _flat(_skill_section(self.HEADING))
        assert "does not emit these fields" in probe
        assert "absent is `unknown`" in probe
        # Each of the three features needs its own degraded behavior.
        assert "hold admission below `max_in_flight`" in probe
        assert "no `i=` leaves you no progress test" in probe
        assert "ages into `idle`" in probe


class TestConductorOwnedState:
    HEADING = "## Conductor-owned state: `conductor-status/v1`"

    def test_status_schema_section_defines_every_field(self):
        schema = _skill_section(self.HEADING)
        for field in (
            "tally",
            "workers",
            "parked",
            "events_tail",
            "resource",
            "conductor_tasks",
            "open_rulings",
        ):
            assert field in schema, field

    def test_open_rulings_carries_its_shape(self):
        schema = _skill_section(self.HEADING)
        for key in ("worker", "pr", "question", "asked_at"):
            assert key in schema, key

    def test_open_rulings_is_reviewed_every_cycle_and_clears_on_delivery(self):
        """The probe is RIGHT to suppress a handled signal -- that is what keeps
        a quiet cycle quiet -- so a worker on an escalation hold goes silent and
        the conductor's own debt is invisible unless it keeps this list."""
        schema = _flat(_skill_section(self.HEADING))
        assert "every cycle" in schema
        assert "delivered" in schema

    def test_last_index_is_stored_so_no_progress_is_a_comparison(self):
        assert "last_index" in _skill_section(self.HEADING)

    def test_the_ledger_wins_where_the_status_file_overlaps_it(self):
        """`workers` necessarily restates some per-item state the ledger holds.
        Two independent spellings of one fact drift silently, so one of them has
        to be named authoritative."""
        schema = _flat(_skill_section(self.HEADING))
        assert "the ledger wins" in schema
        assert "cache" in schema

    def test_open_rulings_names_both_mechanisms_that_fill_it(self):
        """The list is only as good as its inputs, and it has two: the probe must
        keep BLOCKED sticky across samples, and the worker must keep saying it.
        Either alone still loses the debt -- stickiness cannot recover a signal
        never emitted, and a re-stated signal is still overwritten without it."""
        schema = _flat(_skill_section(self.HEADING))
        assert "sticky across samples" in schema
        assert "keep the `blocked:` prefix on every turn" in schema


class TestReconcileIsUnfilteredAndPerCycle:
    HEADING = "## Merge, cleanup, reconcile"

    def test_reconcile_runs_every_cycle_not_on_a_schedule(self):
        merge = _skill_section(self.HEADING)
        assert "every cycle" in _flat(merge)
        # A scheduled cadence here is the defect: merge is invisible between
        # sweeps, and merge is the pipeline's most important state change.
        assert not re.search(
            r"every ~?\d+\s+cycles", merge, re.IGNORECASE
        ), "the reconcile is back on a schedule"

    def test_reconcile_call_is_unfiltered(self):
        merge = _skill_section(self.HEADING)
        assert "unfiltered" in _flat(merge)
        # --state all is what makes a merge visible; an open-only sweep
        # structurally cannot see one.
        assert "--state all" in merge

    def test_reconcile_bounds_the_page_and_reads_a_full_page_as_truncation(self):
        """Measured: the default page is 30 and an over-long list comes back
        silently trimmed, so an unbounded call can drop the newest merge -- the
        exact thing the query exists to find."""
        merge = _skill_section(self.HEADING)
        assert "--limit" in merge
        flat = _flat(merge)
        assert "truncated" in flat
        assert "not a verdict" in flat

    def test_the_reconcile_names_its_repository_explicitly(self):
        """The conductor owns no worktree, so the ambient directory is whatever
        the session sits in: an omitted --repo either errors or answers about a
        different repository, which reads as "no merges"."""
        merge = _skill_section(self.HEADING)
        assert "--repo {repo}" in merge

    def test_the_watchlist_trap_is_named(self):
        """Filtering the reconcile to the PRs already tracked reproduces the
        original blind spot one level down."""
        merge = _flat(_skill_section(self.HEADING))
        assert "watchlist" in merge

    def test_mergeable_unknown_is_secondary(self):
        merge = _skill_section(self.HEADING)
        assert "mergeable=UNKNOWN" in merge
        assert "secondary" in _flat(merge)

    def test_cross_item_facts_are_harvested_in_the_same_cycle(self):
        merge = _flat(_skill_section(self.HEADING))
        assert "harvest" in merge
        assert "in the cycle they arrive" in merge

    def test_a_harvested_cross_item_fact_is_verified_before_it_is_acted_on(self):
        """A worker's report is derived from content the fleet does not control, so
        trusting a claim about somebody ELSE's item would contradict this same
        document's rule against trusting a worker's own GREEN."""
        merge = _flat(_skill_section(self.HEADING))
        assert "confirm the state against the forge" in merge


class TestAdmissionIsSizedOnDelivery:
    HEADING = "## Admission and resource governance"

    def test_delivery_counters_are_the_primary_instrument(self):
        admission = _skill_section(self.HEADING)
        for counter in ("init-timeout", "watchdog"):
            assert counter in admission, counter
        flat = _flat(admission)
        assert "primary instrument" in flat
        assert "stop dispatching" in flat

    def test_load_and_memory_are_explicitly_secondary(self):
        """Load can sit near zero per core and memory can read ample while the
        fleet cannot deliver, because the saturated resource is request service
        and test-runner scheduling."""
        admission = _flat(_skill_section(self.HEADING))
        assert "secondary" in admission

    def test_the_conductor_caps_its_own_forge_calls(self):
        admission = _flat(_skill_section(self.HEADING))
        assert "prefer rest over graphql" in admission
        assert "stagger" in admission

    def test_logs_are_append_only(self):
        admission = _flat(_skill_section(self.HEADING))
        assert "append-only" in admission
        assert "`mv`" in admission

    def test_the_banned_pytest_rule_is_described_as_the_code_implements_it(self):
        """The rule flags a run whose worker count was not explicitly chosen. It
        is NOT "an unbounded -n", and a bare pytest is not the safe form: it
        inherits the project's addopts."""
        admission = _flat(_skill_section(self.HEADING))
        assert "not explicitly chosen" in admission
        assert "bare `pytest` is therefore flagged" in admission
        assert "inherits the project's `addopts`" in admission

    def test_the_banned_response_is_keyed_on_four_ownership_classes(self):
        """Keying it on one actionable-or-not boolean forces an unclassified line
        to be either a false stop or a silent drop -- which is why two review
        rounds demanded opposite things of the same cell."""
        rows = {
            row.split("|")[1].strip()
            for row in _skill_section(self.HEADING).splitlines()
            if row.startswith("|") and row.count("|") >= 3
        }
        for cls in ("`cwd=fleet`", "`cwd=unknown`", "no `cwd=` field at all", "`cwd=foreign`"):
            assert cls in rows, f"{cls} has no row of its own"

    def test_only_fleet_ownership_is_enforced_with_a_stop(self):
        admission = _flat(_skill_section(self.HEADING))
        assert "every ownership class is reported; only `cwd=fleet` is enforced" in admission
        # The two middle classes act without stopping; foreign does not act.
        assert "non-stopping" in admission
        assert "stopping nobody" in admission
        assert "never grounds for stopping a session" in admission

    def test_an_unattributable_line_is_neither_enforced_nor_dropped(self):
        """A banned pid is typically gone before its line is read, so a line with
        no ownership field cannot identify a violator even in principle."""
        admission = _flat(_skill_section(self.HEADING))
        assert "fleet-wide reminder" in admission

    def test_a_legacy_line_attempts_attribution_once(self):
        """The reframe that makes the cell answerable: convert missing ownership
        into resolved ownership where possible, and enforce only on resolved."""
        admission = _flat(_skill_section(self.HEADING))
        assert "attempt attribution once" in admission
        assert "at action time" in admission

    def test_a_resolved_legacy_line_gets_the_stop(self):
        admission = _flat(_skill_section(self.HEADING))
        assert "resolves inside a fleet worktree" in admission
        assert "gets the stop" in admission

    def test_an_unresolved_legacy_line_falls_back_to_the_reminder(self):
        """Failing to resolve is the expected path, so the fallback is the common
        case and must not read as an error branch."""
        admission = _flat(_skill_section(self.HEADING))
        assert "does not resolve" in admission

    def test_the_action_time_read_is_reconciled_with_scan_time_capture(self):
        """Scan-time capture exists BECAUSE an action-time lookup usually fails, so
        the fallback has to be bounded and subordinate or the two rules read as
        contradicting each other. Pinned as the precedence, not the reasoning."""
        admission = _flat(_skill_section(self.HEADING))
        assert "scan-time classification stays the primary mechanism" in admission

    def test_unknown_and_foreign_stay_distinct(self):
        admission = _flat(_skill_section(self.HEADING))
        assert "`unknown` is still not `foreign`" in admission

    def test_the_cwd_class_is_captured_at_scan_time(self):
        """The offending runs are short-lived and the probe-to-action gap
        outlives them, so the verdict has to travel on the line."""
        admission = _flat(_skill_section(self.HEADING))
        assert "not looked up when you act" in admission

    def test_the_standing_constant_is_n0_not_a_small_worker_count(self):
        admission = _flat(_skill_section(self.HEADING))
        assert "`-n0` on every worker test run" in admission


class TestOutageRecoveryAndLoopLiveness:
    HEADING = "## Outage recovery and loop liveness"

    def test_recovery_is_a_fleet_wide_sweep(self):
        """The worker whose ERR line surfaced is the one that was mid-call, not
        the only casualty, so the obligation is a sweep over the whole fleet."""
        body = _flat(_skill_section(self.HEADING))
        assert "all workers" in body
        assert "resume" in body
        assert "re-arm" in body

    def test_re_arm_is_conditional_on_something_external_changing(self):
        body = _flat(_skill_section(self.HEADING))
        assert "conditional" in body
        assert "live pr" in body
        assert "external" in body

    def test_the_conductor_checks_its_own_loop_and_states_the_limit(self):
        """The obligation, not the wording: a missed wake within the patrol
        interval means re-arm, and the residual hole is named as a limit rather
        than left for the reader to discover."""
        body = _flat(_skill_section(self.HEADING))
        assert "patrol interval" in body
        assert "monitor_start" in body


class TestWorkOrderBriefClauses:
    HEADING = "### The work-order brief (seed message skeleton)"

    def test_escalating_test_wrappers_are_banned_by_name(self):
        """A wrapper that escalates to the full suite satisfies the letter of a
        targeted-only brief, so the ban has to name the shapes."""
        brief = _skill_section(self.HEADING)
        named = [n for n in ("make test", "tox", "nox", "local-gate") if n in brief]
        assert len(named) >= 3, f"only named {named}"

    def test_worker_runs_pass_n0_explicitly(self):
        """Omitting ``-n`` is not single process: it inherits the project's
        ``addopts``, commonly ``-n auto``. And a small explicit ``-n <N>`` is the
        LESS safe option, since ``auto`` goes through a memory budget an explicit
        count bypasses. So the clause has to name ``-n0``."""
        brief = _flat(_skill_section(self.HEADING))
        assert "-n0" in brief
        assert "explicitly" in brief
        assert "inherits" in brief
        assert "addopts" in brief
        # The old wording ("no xdist at all", "single process") let a reader
        # conclude a bare pytest was the safe form.
        assert "do not substitute a small `-n <n>`" in brief

    def test_targeted_runs_are_bounded_and_non_interactive(self):
        brief = _skill_section(self.HEADING)
        assert "timeout" in brief
        assert "</dev/null" in brief

    def test_remote_operations_carry_the_credential_clauses(self):
        brief = _skill_section(self.HEADING)
        assert "GIT_TERMINAL_PROMPT=0" in brief
        assert "gh auth setup-git" in brief

    def test_a_hung_push_is_measured_before_it_is_explained(self):
        brief = _flat(_skill_section(self.HEADING))
        assert "pre-push hook" in brief
        assert "before naming a cause" in brief

    def test_babysit_polling_is_staggered_and_prefers_rest(self):
        brief = _flat(_skill_section(self.HEADING))
        assert "stagger" in brief
        assert "rest over graphql" in brief

    def test_the_prefix_must_be_bare_leading_text(self):
        """A bolded prefix reads as no status at all, and it fails on exactly the
        message that most needs to be heard."""
        brief = _flat(_skill_section(self.HEADING))
        assert "bare leading text" in brief
        assert "no bold" in brief
        assert "matched at the start of the message" in brief

    def test_a_blocked_worker_keeps_the_blocked_prefix(self):
        """The conductor samples the newest protocol message, so a WORKING: line
        posted after a BLOCKED: overwrites the escalation before anything sees
        it, and the ruling ends up owed by nobody."""
        brief = _flat(_skill_section(self.HEADING))
        assert "keep that prefix on every turn" in brief
        assert "escalation hold" in brief
        assert "newest protocol message" in brief


class TestPartitioningAcrossWorkers:
    HEADING = "### Partitioning one change across several workers"

    def test_ownership_is_exclusive_per_file(self):
        body = _flat(_skill_section(self.HEADING))
        assert "exclusive file ownership" in body

    def test_exclusive_ownership_does_not_remove_the_review_order_dependency(self):
        """A premise-level reviewer counts consumers in the BASE, so the PR that
        builds a mechanism reads as dead code until the PR that wires it lands.
        The split fixes conflicts and creates this instead."""
        body = _flat(_skill_section(self.HEADING))
        assert "merge order" in body
        assert "wiring pr lands before or with the building pr" in body

    def test_the_remedy_is_sequencing_and_never_moving_another_workers_hunk(self):
        """Moving the hunk dissolves the ownership split, creates the conflict the
        split existed to prevent, and hides a review-order problem as a code
        change."""
        body = _flat(_skill_section(self.HEADING))
        assert "never move another worker's hunk" in body


class TestCodifiedPractices:
    def test_the_four_way_collision_check_survives(self):
        assert "four-way collision check" in _flat(_skill_section("## Pickup and dispatch"))

    def test_the_claim_is_atomic_and_released_with_evidence(self):
        dispatch = _flat(_skill_section("## Pickup and dispatch"))
        assert "claim atomically" in dispatch
        assert "assignee in one call" in dispatch
        assert "unclaim" in dispatch
        assert "evidence comment" in dispatch

    def test_a_failed_dispatch_releases_the_claim(self):
        """A claim with no session behind it looks like work in progress to every
        other operator, and no probe line, SLA timer or reclaim path covers it -
        the ledger never recorded a dispatch."""
        dispatch = _flat(_skill_section("## Pickup and dispatch"))
        assert "if any post-claim step fails" in dispatch
        assert "unclaim before you move on" in dispatch

    def test_session_create_must_pass_the_worker_agent(self):
        """An unset agent binds the worker to the conductor's own agent, which
        has no file-writing tool, and the whole batch refuses the work."""
        mechanics = _skill_section("### Dispatch mechanics")
        assert "session_create" in mechanics
        flat = _flat(mechanics)
        assert "explicitly" in flat
        assert "canary" in flat

    def test_a_base_wide_fix_is_searched_for_before_it_is_commissioned(self):
        assert "already exists" in _flat(_skill_section("### Dispatch mechanics"))

    def test_green_verification_names_what_it_catches(self):
        verification = _flat(_skill_section("## Independent green verification"))
        assert "mis-reported head sha" in verification

    def test_adjudication_carries_the_named_patterns(self):
        adjudication = _flat(_skill_section("## Adjudication (BLOCKED) and overrides"))
        # One sample, then the class.
        assert "one sample" in adjudication
        # Subtraction over an Nth patch.
        assert "subtraction" in adjudication
        # A re-run replaces a run; it does not retry a job.
        assert "replaces" in adjudication
        assert "check-run set" in adjudication
        # Security findings never go to a public channel.
        assert "public channel" in adjudication
        # A park records its dependency and releases the claim.
        assert "park with the dependency" in adjudication


class TestDesignDocTracksTheSkill:
    """The design doc carries the intent; a skill clause with no rationale
    recorded anywhere is the first thing a rewrite drops."""

    def test_design_doc_defines_the_status_schema(self):
        doc = _read(DESIGN_DOC)
        assert "conductor-status/v1" in doc
        assert "open_rulings" in doc

    def test_design_doc_argues_the_delivery_based_admission_model(self):
        doc = _flat(_read(DESIGN_DOC))
        assert "admission is sized on delivery" in doc

    def test_design_doc_names_all_three_scripts(self):
        doc = _read(DESIGN_DOC)
        for script in BUNDLED_SCRIPTS:
            assert script in doc, script
