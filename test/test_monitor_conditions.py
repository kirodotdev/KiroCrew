"""Named conditions in the structured engine: the two behaviours layer 3 carries.

A per-entry model earns review only if the two behaviours it exists to carry are
pinned, because they are the two the whole-subject fingerprint could not express:

* an ``IMMEDIATE`` condition bypassing the coalescing floor, and
* a ``NEVER`` condition surviving a head change that resets its siblings.

``test/test_irq_port_baseline.py`` and ``test/test_irq.py`` are the oracle for
both on the cron path, which this change does not touch. These are the same
behaviours asserted in engine terms, so a port that cannot reproduce them has not
moved the behaviour.

Each pinned behaviour is stated with its differential: the assertion that the
same position WITHOUT the claim behaves the other way. Without the differential a
test passes on an engine that bypasses the floor for everything, or one that
never resets anything.

The window rides on ``MonitorState``; a decision reads and updates it and the
caller persists the same state, so no file is held here.
"""

from __future__ import annotations

import pytest

from kiro_crew import irq
from kiro_crew.monitoring.decision import decide_monitor, stamp_monitor_alerted
from kiro_crew.monitoring.models import (
    DEFAULT_MONITOR_COALESCE_SECS,
    MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET,
    MAX_MONITOR_CONDITION_KEY_CHARS,
    MAX_MONITOR_CONDITIONS,
    MAX_MONITOR_FIXED_CONDITIONS,
    MONITOR_REVISION_KEY_SPACE,
    MONITOR_STICKY_KEY_SPACE,
    MonitorBudgets,
    MonitorCondition,
    MonitorDecision,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorResetsOn,
    MonitorSeverity,
    MonitorState,
    ProviderErrorKind,
    monitor_condition_dedupe_key,
)
from kiro_crew.monitoring.pull_request import (
    PullRequestCheck,
    PullRequestFacts,
    build_pull_request_probe_result,
    pull_request_conditions,
)

_FLOOR = DEFAULT_MONITOR_COALESCE_SECS

_RED_CHECK = MonitorCondition(
    key="red:build",
    severity=MonitorSeverity.WAKE,
    resets_on=MonitorResetsOn.REVISION,
)
_SECOND_RED_CHECK = MonitorCondition(
    key="red:lint",
    severity=MonitorSeverity.WAKE,
    resets_on=MonitorResetsOn.REVISION,
)
_CONFLICT = MonitorCondition(
    key="conflict",
    severity=MonitorSeverity.IMMEDIATE,
    resets_on=MonitorResetsOn.REVISION,
)
_THREADS = MonitorCondition(
    key="unresolved_threads",
    severity=MonitorSeverity.WAKE,
    resets_on=MonitorResetsOn.NEVER,
)


def _state(**overrides: object) -> MonitorState:
    values: dict[str, object] = {
        "kind": "github_pull_request",
        "target": "owner/repo/pull/1",
        "objective": "review_ready",
        "created_ts": 0.0,
        "budgets": MonitorBudgets(max_runtime_secs=10_000_000),
    }
    values.update(overrides)
    return MonitorState(**values)  # type: ignore[arg-type]


def _actionable(
    *conditions: MonitorCondition,
    fingerprint: str = "fp-1",
    head_changed: bool = False,
) -> MonitorObservation:
    return MonitorObservation(
        fingerprint,
        MonitorObservationStatus.ACTIONABLE,
        head_changed=head_changed,
        conditions=conditions,
    )


def _decide(state: MonitorState, observation: MonitorObservation, *, now: float):
    return decide_monitor(state, observation, now=now).decision


def _facts(**overrides: object) -> PullRequestFacts:
    values: dict[str, object] = {
        "kind": "github_pull_request",
        "target": "https://github.com/owner/repo/pull/1",
        "state": "open",
        "draft": False,
        "head_revision": "0123456789abcdef",
        "mergeability": "mergeable",
        "review_decision": "none",
        "checks": (),
        "unresolved_review_threads": 0,
        "review_threads_complete": True,
        "checks_complete": True,
    }
    values.update(overrides)
    return PullRequestFacts(**values)  # type: ignore[arg-type]


class TestTheImmediateBypass:
    """An urgent condition fires now, because waiting observes nothing further."""

    def test_an_immediate_condition_fires_inside_the_floor(self) -> None:
        """A conflict arriving while the window is open is not held.

        A conflicted pull request dispatches no checks, so the pending count the
        floor waits on never drains: holding the wake would strand the owner for
        the whole floor on a signal that is already actionable.
        """
        state = _state()
        assert _decide(state, _actionable(_RED_CHECK), now=100.0) is (
            MonitorDecision.WAKE_ACTIONABLE
        )
        stamp_monitor_alerted(state, now=100.0)

        conflicted = _actionable(_RED_CHECK, _CONFLICT, fingerprint="fp-2")
        assert _decide(state, conflicted, now=100.0 + _FLOOR * 0.1) is (
            MonitorDecision.WAKE_ACTIONABLE
        )

    def test_a_wake_condition_in_the_same_position_is_held(self) -> None:
        """The differential. Without it the test above passes on an engine that
        bypasses the floor for every condition, which is no floor at all."""
        state = _state()
        assert _decide(state, _actionable(_RED_CHECK), now=100.0) is (
            MonitorDecision.WAKE_ACTIONABLE
        )
        stamp_monitor_alerted(state, now=100.0)

        second = _actionable(_RED_CHECK, _SECOND_RED_CHECK, fingerprint="fp-2")
        assert _decide(state, second, now=100.0 + _FLOOR * 0.1) is MonitorDecision.RECORD_ONLY
        # And the held condition still arrives once the floor passes.
        assert _decide(state, second, now=100.0 + _FLOOR * 1.1) is (MonitorDecision.WAKE_ACTIONABLE)

    def test_immediate_bypasses_the_floor_but_not_the_mask(self) -> None:
        """Urgency buys one wake per interval, never one per tick.

        An unmasked urgent condition would wake the owner on every tick for as
        long as the conflict lasts, which is a worse failure than a bounded delay.
        """
        state = _state()
        assert _decide(state, _actionable(_CONFLICT), now=0.0) is (MonitorDecision.WAKE_ACTIONABLE)
        stamp_monitor_alerted(state, now=0.0)

        assert _decide(state, _actionable(_CONFLICT), now=60.0) is MonitorDecision.NO_CHANGE


class TestTheResetScope:
    """A new head clears what belongs to the revision and keeps what does not."""

    def test_a_sticky_condition_survives_a_head_change(self) -> None:
        """A review thread is not made new by a force-push.

        Scoped to the revision instead, every such condition would be re-reported
        in full the tick after any head change: a force-push would replay every
        comment ever seen as though it had just arrived.
        """
        state = _state()
        assert _decide(state, _actionable(_THREADS), now=0.0) is (MonitorDecision.WAKE_ACTIONABLE)
        stamp_monitor_alerted(state, now=0.0)
        assert state.coalesce_alerted == {monitor_condition_dedupe_key(_THREADS): 0.0}

        pushed = _actionable(_THREADS, fingerprint="fp-2", head_changed=True)
        assert _decide(state, pushed, now=10.0) is MonitorDecision.NO_CHANGE
        # The mask is what survived, not merely the decision.
        assert monitor_condition_dedupe_key(_THREADS) in state.coalesce_alerted

    def test_a_revision_condition_is_cleared_by_the_same_head_change(self) -> None:
        """The differential, and the sibling half of the same tick.

        Without it the test above passes on an engine that resets nothing, where
        every mask survives every head change and a genuinely new failure on the
        new head stays silenced for the rest of the re-alert interval.
        """
        state = _state()
        assert _decide(state, _actionable(_RED_CHECK, _THREADS), now=0.0) is (
            MonitorDecision.WAKE_ACTIONABLE
        )
        stamp_monitor_alerted(state, now=0.0)
        assert set(state.coalesce_alerted) == {
            monitor_condition_dedupe_key(_RED_CHECK),
            monitor_condition_dedupe_key(_THREADS),
        }

        pushed = _actionable(_RED_CHECK, _THREADS, fingerprint="fp-2", head_changed=True)
        # Past the floor, so the coalescing window is not what decides this tick:
        # the only question left is whether the check's mask survived the head
        # change, and it did not.
        assert _decide(state, pushed, now=_FLOOR * 1.1) is MonitorDecision.WAKE_ACTIONABLE
        # One half of the memory went and the other stayed, in the same reset.
        assert monitor_condition_dedupe_key(_RED_CHECK) not in state.coalesce_alerted
        assert monitor_condition_dedupe_key(_THREADS) in state.coalesce_alerted

    def test_a_cleared_condition_inside_the_floor_is_held_not_silenced(self) -> None:
        """The reset unmasks a condition; the floor still decides when it lands.

        A new head arriving seconds after a wake is a burst, so the cleared
        condition is RECORD_ONLY rather than NO_CHANGE -- held, which is a
        deferral, not suppressed, which would be a lost wake.
        """
        state = _state()
        assert _decide(state, _actionable(_RED_CHECK, _THREADS), now=0.0) is (
            MonitorDecision.WAKE_ACTIONABLE
        )
        stamp_monitor_alerted(state, now=0.0)

        pushed = _actionable(_RED_CHECK, _THREADS, fingerprint="fp-2", head_changed=True)
        assert _decide(state, pushed, now=10.0) is MonitorDecision.RECORD_ONLY
        # And the held wake arrives once the window ages past the floor.
        assert _decide(state, pushed, now=_FLOOR * 1.1) is MonitorDecision.WAKE_ACTIONABLE

    def test_the_reset_reaches_a_condition_this_tick_does_not_report(self) -> None:
        """Scoped by key space, not by the conditions in hand.

        A failing check can go unreported for a tick and come back. Keyed off only
        what the head-change tick reports, its mask would survive and suppress a
        real wake for the rest of the re-alert interval.
        """
        state = _state(coalesce_alerted={monitor_condition_dedupe_key(_SECOND_RED_CHECK): 0.0})
        pushed = _actionable(_RED_CHECK, fingerprint="fp-2", head_changed=True)
        _decide(state, pushed, now=10.0)
        assert monitor_condition_dedupe_key(_SECOND_RED_CHECK) not in state.coalesce_alerted


class TestTheDeliveredSet:
    """Only the conditions a wake delivered are masked by it."""

    def test_a_masked_condition_is_not_re_masked_by_a_sibling_wake(self) -> None:
        """Masking the whole tick would silence a condition it never delivered.

        The sticky condition here is inside its interval, so the wake is the
        check's alone. Extending the sticky mask would push its re-assertion a
        full interval further out for a wake it took no part in.
        """
        state = _state(coalesce_alerted={monitor_condition_dedupe_key(_THREADS): 0.0})
        assert _decide(state, _actionable(_RED_CHECK, _THREADS), now=100.0) is (
            MonitorDecision.WAKE_ACTIONABLE
        )
        stamp_monitor_alerted(state, now=100.0)

        assert state.coalesce_alerted == {
            monitor_condition_dedupe_key(_THREADS): 0.0,
            monitor_condition_dedupe_key(_RED_CHECK): 100.0,
        }


class TestThePullRequestDerivation:
    """The adapter names every condition the subject carries at once."""

    def test_changes_requested_and_unresolved_threads_both_survive(self) -> None:
        """The representation defect, repaired.

        ``blocking_review`` is one precedence winner, so a pull request carrying
        both records only the first and the second is lost before anything can act
        on it. As two conditions each is masked, aged and reset on its own.
        """
        facts = _facts(review_decision="changes_requested", unresolved_review_threads=3)
        result = build_pull_request_probe_result(facts)
        keys = [condition.key for condition in result.observation.conditions]

        assert "changes_requested" in keys
        assert "unresolved_threads" in keys
        # The narrowed summary still records only the first, which is why the
        # conditions rather than that field are what the engine reads.
        assert result.canonical["blocking_review"] == "changes_requested"

    def test_both_review_conditions_are_sticky(self) -> None:
        """A review verdict and a review thread belong to the conversation.

        A force-push does not answer a reviewer, so neither condition may be
        cleared by one.
        """
        facts = _facts(review_decision="changes_requested", unresolved_review_threads=1)
        conditions = {
            condition.key: condition
            for condition in pull_request_conditions(
                build_pull_request_probe_result(facts).canonical
            )
        }

        assert conditions["changes_requested"].resets_on is MonitorResetsOn.NEVER
        assert conditions["unresolved_threads"].resets_on is MonitorResetsOn.NEVER

    def test_a_failing_check_resets_on_the_revision(self) -> None:
        """The differential for the two above: a check IS a property of the head."""
        facts = _facts(checks=(PullRequestCheck("build", "failed"),))
        conditions = {
            condition.key: condition
            for condition in build_pull_request_probe_result(facts).observation.conditions
        }

        assert conditions["red:build"].resets_on is MonitorResetsOn.REVISION
        assert conditions["red:build"].severity is MonitorSeverity.WAKE

    def test_a_conflict_is_the_immediate_one_and_behind_is_not(self) -> None:
        """Urgency is claimed by the condition under which waiting learns nothing.

        A conflicted branch dispatches no checks. A branch merely behind its
        target still builds, so waiting continues to observe something.
        """
        conflicted = build_pull_request_probe_result(_facts(mergeability="conflicting"))
        behind = build_pull_request_probe_result(_facts(mergeability="behind"))

        assert [condition.severity for condition in conflicted.observation.conditions] == [
            MonitorSeverity.IMMEDIATE
        ]
        assert [condition.severity for condition in behind.observation.conditions] == [
            MonitorSeverity.WAKE
        ]

    def test_a_pending_subject_names_no_conditions(self) -> None:
        """Conditions are carried only where the window reads them.

        The fixture is a DRAFT pull request that also has unresolved review
        threads: the draft makes it PENDING, and the threads are a condition the
        derivation would name, so a carrier that ignored the status would show
        one here. Without the threads the subject derives nothing anyway and the
        assertion could not tell the two apart.
        """
        result = build_pull_request_probe_result(_facts(draft=True, unresolved_review_threads=2))
        assert result.observation.status is MonitorObservationStatus.PENDING
        assert pull_request_conditions(result.canonical) != ()
        assert result.observation.conditions == ()


class TestTheVocabularyParity:
    """Two vocabularies exist only while two drivers do, so neither may drift."""

    def test_the_severity_members_match_the_cron_kernel(self) -> None:
        """Same three names, so the retirement is a deletion rather than a port."""
        assert [item.name for item in MonitorSeverity] == [item.name for item in irq.Severity]

    def test_the_reset_scope_members_match_the_cron_kernel(self) -> None:
        assert [item.name for item in MonitorResetsOn] == [item.name for item in irq.ResetsOn]

    def test_the_key_spaces_match_the_cron_kernel(self) -> None:
        """The persisted encoding of the distinction is the same two characters.

        Read through the kernel's own module attributes, so a rename there
        reddens here rather than leaving two encodings that look alike.
        """
        assert MONITOR_REVISION_KEY_SPACE == irq._EPOCH_SENTINEL
        assert MONITOR_STICKY_KEY_SPACE == irq._STICKY_SENTINEL


class TestTheConditionBoundary:
    """Malformed conditions are refused where they arrive, not inside a tick."""

    def test_a_reserved_key_space_prefix_is_refused(self) -> None:
        """A probe never writes a key space itself.

        Condition keys are attacker-influenceable -- a workflow names its own
        jobs -- so a key that could forge the sticky space is refused.
        """
        with pytest.raises(ValueError, match="reserved key space"):
            MonitorCondition(key=f"{MONITOR_STICKY_KEY_SPACE}forged")

    def test_duplicate_condition_keys_are_refused(self) -> None:
        """Two conditions under one key are one the engine would mask twice."""
        with pytest.raises(ValueError, match="unique"):
            MonitorObservation(
                "fp-1",
                MonitorObservationStatus.ACTIONABLE,
                conditions=(_RED_CHECK, _RED_CHECK),
            )

    def test_a_provider_error_may_not_name_conditions(self) -> None:
        """A failed read is no evidence ABOUT the subject, so it has none to name."""
        with pytest.raises(ValueError, match="conditions are not valid"):
            MonitorObservation(
                "",
                MonitorObservationStatus.PROVIDER_ERROR,
                provider_error=ProviderErrorKind.TRANSIENT,
                conditions=(_RED_CHECK,),
            )

    def test_a_subject_naming_no_conditions_is_a_legal_shape(self) -> None:
        """The engine reads it as one revision-scoped condition keyed by fingerprint.

        That is what a subject reduced to one fingerprint was before conditions
        existed, so an adapter that names none is a legal plug-in rather than a
        migration debt.
        """
        state = _state()
        bare = MonitorObservation("fp-bare", MonitorObservationStatus.ACTIONABLE)
        assert _decide(state, bare, now=0.0) is MonitorDecision.WAKE_ACTIONABLE
        assert state.coalesce_windows == {f"{MONITOR_REVISION_KEY_SPACE}fp-bare": 0.0}


class TestEveryBuildPathAgreesWithTheEngine:
    """A state's own dedupe entries must be visible to the engine that reads them.

    The loader is not the only way a state is built: callers and tests construct
    one directly. A key carrying no space matches nothing the engine computes, so
    such an entry is silently inert -- a window that never closes, and a mask that
    reads as never alerted and wakes again. Adopting at construction is what makes
    that unconstructable rather than merely unlikely.
    """

    def test_a_bare_mask_key_handed_to_the_constructor_still_masks(self) -> None:
        """The whole point: a directly built state's mask is not silently dead."""
        state = _state(
            last_fingerprint="red-1",
            last_wake_fingerprint="red-1",
            coalesce_alerted={"red-1": 1_000.0},
        )
        assert state.coalesce_alerted == {f"{MONITOR_REVISION_KEY_SPACE}red-1": 1_000.0}

        # Inside the re-alert interval the adopted key masks, so the tick is quiet.
        repeat = MonitorObservation("red-1", MonitorObservationStatus.ACTIONABLE)
        assert _decide(state, repeat, now=1_100.0) is MonitorDecision.NO_CHANGE

    def test_a_bare_window_key_handed_to_the_constructor_is_the_open_window(self) -> None:
        """Left bare, the window would never be found, so the floor could not hold."""
        state = _state(coalesce_windows={"red-1": 0.0})
        assert state.coalesce_windows == {f"{MONITOR_REVISION_KEY_SPACE}red-1": 0.0}

    def test_a_key_already_in_a_space_is_left_exactly_as_given(self) -> None:
        """Adoption is idempotent, so a sticky key is not dragged into the revision space."""
        sticky = f"{MONITOR_STICKY_KEY_SPACE}conflict"
        revision = f"{MONITOR_REVISION_KEY_SPACE}red-1"
        state = _state(coalesce_alerted={sticky: 5.0, revision: 6.0})
        assert state.coalesce_alerted == {sticky: 5.0, revision: 6.0}


class TestANewHeadRetiresRevisionMemoryWhateverTheTickReports:
    """The reset belongs to the head change, not to the actionable path.

    A force-push is normally seen FIRST as a pending tick: the new head's checks
    are dispatched and none has finished. If the reset lived only where an
    actionable change is folded, that tick would record and return with the old
    head's mask still standing, and the new head's first real failure would be
    masked for a whole re-alert interval. Each case below is stated with its
    differential: the same position with no head change keeps the mask, so a
    passing test cannot come from an engine that resets unconditionally.
    """

    def _masked_state(self) -> MonitorState:
        state = _state()
        assert _decide(state, _actionable(_RED_CHECK), now=0.0) is MonitorDecision.WAKE_ACTIONABLE
        stamp_monitor_alerted(state, now=0.0)
        assert state.coalesce_alerted, "the fixture must leave a revision-scoped mask standing"
        return state

    def test_a_pending_tick_on_a_new_head_drops_the_old_revision_mask(self) -> None:
        """The reported case: pending is how a force-push is usually first seen."""
        state = self._masked_state()
        pending = MonitorObservation(
            "pending-new",
            MonitorObservationStatus.PENDING,
            head_changed=True,
        )
        assert _decide(state, pending, now=1.0) is MonitorDecision.RECORD_ONLY
        assert state.coalesce_alerted == {}

        # The masked condition returning on the new head now wakes, which is the
        # user-visible half: without the reset it would be suppressed.
        assert _decide(state, _actionable(_RED_CHECK), now=2.0) is MonitorDecision.WAKE_ACTIONABLE

    def test_a_pending_tick_on_the_same_head_keeps_the_mask(self) -> None:
        """The differential. Without it the test passes on an engine that resets always."""
        state = self._masked_state()
        pending = MonitorObservation("pending-same", MonitorObservationStatus.PENDING)
        assert _decide(state, pending, now=1.0) is MonitorDecision.RECORD_ONLY
        assert state.coalesce_alerted != {}

    def test_a_provider_error_can_never_claim_a_new_head(self) -> None:
        """A read that failed is no evidence about the head, so it cannot name one.

        This is what keeps the hoisted reset from firing on a failed read: the
        combination is refused at construction, so no placement in the decision
        order has to defend it.
        """
        with pytest.raises(ValueError, match="head_changed is not valid"):
            MonitorObservation(
                "",
                MonitorObservationStatus.PROVIDER_ERROR,
                provider_error=ProviderErrorKind.TRANSIENT,
                head_changed=True,
            )


class TestTheConditionCapHoldsEverythingABoundedProbeCanName:
    """A cap enforced by a slice must exceed the population, or it deletes blockers.

    Two different bounds on one population is the defect: the canonical projection
    bounds each check bucket, and a smaller condition cap then silently drops the
    tail -- worst exactly when a subject has the most wrong with it, and with no
    count of what went missing.
    """

    def test_the_cap_is_derived_from_the_bound_it_must_cover(self) -> None:
        """Written out as a literal, the two numbers drift apart unnoticed."""
        assert MAX_MONITOR_CONDITIONS == (
            MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET + MAX_MONITOR_FIXED_CONDITIONS
        )

    def test_a_fully_loaded_subject_loses_no_blocker(self) -> None:
        """Every check the projection admits, plus every fixed condition, survives."""
        failed = [f"check-{index}" for index in range(MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET)]
        conditions = pull_request_conditions(
            {
                "checks": {"failed": failed},
                "review_decision": "changes_requested",
                "unresolved_review_threads": 3,
                "mergeability": "conflicting",
            }
        )
        keys = [condition.key for condition in conditions]
        assert len(keys) == len(failed) + 3
        assert len(keys) == len(set(keys))
        for identity in failed:
            assert f"red:{identity}" in keys
        for fixed in ("changes_requested", "unresolved_threads", "conflict"):
            assert fixed in keys

        # The engine must accept what the adapter can legitimately produce.
        MonitorObservation(
            "fp",
            MonitorObservationStatus.ACTIONABLE,
            conditions=conditions,
        )

    def test_two_long_check_identities_keep_separate_keys(self) -> None:
        """Truncation alone collapses a matrix job's suffix, so one failure vanishes."""
        shared = "x" * MAX_MONITOR_CONDITION_KEY_CHARS
        conditions = pull_request_conditions(
            {"checks": {"failed": [f"{shared}-shard-1", f"{shared}-shard-2"]}}
        )
        keys = [condition.key for condition in conditions]
        assert len(keys) == 2
        assert keys[0] != keys[1]
        for key in keys:
            assert len(key) <= MAX_MONITOR_CONDITION_KEY_CHARS


_COMMENT = MonitorCondition(
    key="review_comment_bodies:cccc",
    severity=MonitorSeverity.WAKE,
    resets_on=MonitorResetsOn.NEVER,
)
_COMMENT_CHANGED = MonitorCondition(
    key="review_comment_bodies:dddd",
    severity=MonitorSeverity.WAKE,
    resets_on=MonitorResetsOn.NEVER,
)


class TestTheReviewCommentBodyCondition:
    """A monitor also wakes when a PR-LEVEL comment body changes.

    The review-bot verdicts (design-review, codex-ai-review, ...) post as
    PR-level issue comments, not review threads, and a bot rewrites its verdict
    IN PLACE, so this is the surface that carries the motivating signal. It is a
    SEPARATE key from the thread digest, so a change on one surface is
    distinguishable from a change on the other.
    """

    @staticmethod
    def _comment_keys(conditions: object) -> list[str]:
        return [c.key for c in conditions if c.key.startswith("review_comment_bodies:")]

    def test_a_digest_emits_one_sticky_comment_condition(self) -> None:
        conditions = pull_request_conditions(
            {"checks": {"failed": []}, "pr_comment_body_digest": "beadfeed"}
        )
        keyed = {c.key: c for c in conditions}
        assert "review_comment_bodies:beadfeed" in keyed
        condition = keyed["review_comment_bodies:beadfeed"]
        assert condition.severity is MonitorSeverity.WAKE
        assert condition.resets_on is MonitorResetsOn.NEVER

    def test_an_absent_or_empty_digest_emits_no_comment_condition(self) -> None:
        """The fail-closed representation at the adapter: the provider emits "" on
        an incomplete or empty read, and an absent or empty digest yields no
        condition. The provider-side incomplete-read assertion lives in the
        GitHub monitor tests."""
        assert self._comment_keys(pull_request_conditions({"checks": {"failed": []}})) == []
        assert (
            self._comment_keys(
                pull_request_conditions({"checks": {"failed": []}, "pr_comment_body_digest": ""})
            )
            == []
        )

    def test_the_adapter_carries_the_comment_digest_only_when_non_empty(self) -> None:
        without = build_pull_request_probe_result(
            _facts(checks=(PullRequestCheck("build", "failed"),))
        ).canonical
        assert "pr_comment_body_digest" not in without

        with_digest = build_pull_request_probe_result(
            _facts(
                checks=(PullRequestCheck("build", "failed"),),
                pr_comment_body_digest="c0ffee",
            )
        ).canonical
        assert with_digest["pr_comment_body_digest"] == "c0ffee"

    def test_a_changed_comment_digest_wakes(self) -> None:
        state = _state()
        assert _decide(state, _actionable(_COMMENT), now=0.0) is MonitorDecision.WAKE_ACTIONABLE
        stamp_monitor_alerted(state, now=0.0)
        changed = _actionable(_COMMENT_CHANGED, fingerprint="fp-2")
        assert _decide(state, changed, now=_FLOOR * 1.1) is MonitorDecision.WAKE_ACTIONABLE

    def test_an_unchanged_comment_digest_does_not_wake_again(self) -> None:
        state = _state()
        assert _decide(state, _actionable(_COMMENT), now=0.0) is MonitorDecision.WAKE_ACTIONABLE
        stamp_monitor_alerted(state, now=0.0)
        assert _decide(state, _actionable(_COMMENT), now=1.0) is MonitorDecision.NO_CHANGE

    def test_the_comment_condition_survives_a_head_change(self) -> None:
        """Sticky: a force-push does not answer a PR-level comment either."""
        state = _state()
        assert _decide(state, _actionable(_COMMENT), now=0.0) is MonitorDecision.WAKE_ACTIONABLE
        stamp_monitor_alerted(state, now=0.0)
        pushed = _actionable(_COMMENT, fingerprint="fp-2", head_changed=True)
        assert _decide(state, pushed, now=10.0) is MonitorDecision.NO_CHANGE
        assert monitor_condition_dedupe_key(_COMMENT) in state.coalesce_alerted

    def test_a_fully_loaded_subject_with_the_comment_digest_loses_no_blocker(self) -> None:
        """Four fixed conditions co-occur (a review verdict, the unresolved-thread
        count, one mergeability condition, and the comment digest), plus every
        check the projection admits; the cap, five fixed, covers them."""
        failed = [f"check-{index}" for index in range(MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET)]
        conditions = pull_request_conditions(
            {
                "checks": {"failed": failed},
                "review_decision": "changes_requested",
                "unresolved_review_threads": 3,
                "mergeability": "conflicting",
                "pr_comment_body_digest": "bbbb",
            }
        )
        keys = [condition.key for condition in conditions]
        assert len(keys) == len(failed) + 4
        assert len(keys) == len(set(keys))
        for fixed in (
            "changes_requested",
            "unresolved_threads",
            "conflict",
            "review_comment_bodies:bbbb",
        ):
            assert fixed in keys
        MonitorObservation(
            "fp",
            MonitorObservationStatus.ACTIONABLE,
            conditions=conditions,
        )
