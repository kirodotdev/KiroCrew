"""Behavioral contract for probe-first monitor decisions."""

from __future__ import annotations

import pathlib

import pytest

from kiro_crew.monitoring.decision import (
    decide_monitor,
    monitor_budget_reason,
    monitor_stall_reason,
)
from kiro_crew.monitoring.github_provider_errors import REASON_SHARED_COOLDOWN
from kiro_crew.monitoring.models import (
    DEFAULT_MONITOR_CADENCE_SECS,
    DEFAULT_MONITOR_COALESCE_SECS,
    DEFAULT_MONITOR_REALERT_SECS,
    DEFAULT_MONITOR_RUNTIME_SECS,
    DEFAULT_MONITOR_STALL_MIN_SECS,
    DEFAULT_MONITOR_STALL_TICKS,
    MIN_MONITOR_CADENCE_SECS,
    MONITOR_REVISION_KEY_SPACE,
    MONITOR_STOP_APPROVAL_STALL,
    MONITOR_STOP_PROVIDER_ERROR_BUDGET,
    MONITOR_STOP_RUNTIME_BUDGET,
    MONITOR_STOP_VERDICT_STALL,
    MonitorBudgets,
    MonitorCondition,
    MonitorDecision,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorResetsOn,
    MonitorState,
    MonitorVerdict,
    ProviderErrorKind,
    monitor_condition_dedupe_key,
    monitor_state_from_dict,
    monitor_state_to_dict,
)


def _state(**changes: object) -> MonitorState:
    values: dict[str, object] = {
        "kind": "github_pull_request",
        "target": "owner/repo#123",
        "objective": "review_ready",
        "created_ts": 1_000.0,
    }
    values.update(changes)
    return MonitorState(**values)


@pytest.mark.parametrize(
    ("observation", "state", "expected"),
    [
        (
            MonitorObservation("pending-a", MonitorObservationStatus.PENDING),
            _state(last_fingerprint="pending-a"),
            MonitorDecision.NO_CHANGE,
        ),
        (
            MonitorObservation("pending-b", MonitorObservationStatus.PENDING),
            _state(last_fingerprint="pending-a"),
            MonitorDecision.RECORD_ONLY,
        ),
        (
            MonitorObservation("failure-b", MonitorObservationStatus.ACTIONABLE),
            _state(last_fingerprint="pending-a"),
            MonitorDecision.WAKE_ACTIONABLE,
        ),
        (
            MonitorObservation("failure-b", MonitorObservationStatus.ACTIONABLE),
            _state(
                last_fingerprint="failure-b",
                last_wake_fingerprint="failure-b",
                # A real wake records the alert time, so the within-period
                # suppression is asserted against state the caller produces:
                # now=1100 sits well inside the re-alert period from 1000. The
                # key carries the revision key space, which is where a subject
                # that names no conditions of its own is remembered.
                coalesce_alerted={f"{MONITOR_REVISION_KEY_SPACE}failure-b": 1_000.0},
            ),
            MonitorDecision.NO_CHANGE,
        ),
        (
            MonitorObservation("ready-c", MonitorObservationStatus.SUCCESS),
            _state(last_fingerprint="pending-a"),
            MonitorDecision.STOP_SUCCESS,
        ),
        (
            MonitorObservation(
                "closed-c",
                MonitorObservationStatus.BLOCKED,
                reason_code="pull_request_closed",
            ),
            _state(last_fingerprint="pending-a"),
            MonitorDecision.STOP_BLOCKED,
        ),
    ],
)
def test_observation_changes_control_when_a_model_turn_is_allowed(
    observation: MonitorObservation,
    state: MonitorState,
    expected: MonitorDecision,
) -> None:
    """A model turn is reserved for a new actionable fingerprint."""
    assert decide_monitor(state, observation, now=1_100.0).decision is expected


def test_changed_head_does_not_wake_while_readiness_is_pending() -> None:
    """A push with incomplete evidence must not spend a model turn."""
    observation = MonitorObservation(
        "pending-new-head",
        MonitorObservationStatus.PENDING,
        head_changed=True,
    )

    assert (
        decide_monitor(_state(), observation, now=1_100.0).decision is MonitorDecision.RECORD_ONLY
    )


def test_success_after_a_changed_head_can_reach_terminal_success() -> None:
    """The changed-head wake must not make a later identical green probe immortal."""
    changed = MonitorObservation(
        "green-new-head",
        MonitorObservationStatus.SUCCESS,
        head_changed=True,
    )
    settled = MonitorObservation("green-new-head", MonitorObservationStatus.SUCCESS)

    assert (
        decide_monitor(_state(), changed, now=1_100.0).decision is MonitorDecision.WAKE_ACTIONABLE
    )
    assert (
        decide_monitor(
            _state(last_fingerprint="green-new-head"),
            settled,
            now=1_101.0,
        ).decision
        is MonitorDecision.STOP_SUCCESS
    )


def test_cumulative_provider_error_budget_is_a_hard_bound() -> None:
    state = _state(
        provider_error_count=3,
        consecutive_provider_errors=0,
        budgets=MonitorBudgets(max_provider_errors=3),
    )

    assert monitor_budget_reason(state, now=1_100.0) == "provider_error_budget"


@pytest.mark.parametrize(
    ("error", "consecutive_errors", "expected"),
    [
        (ProviderErrorKind.TRANSIENT, 0, MonitorDecision.RETRY_PROVIDER),
        (ProviderErrorKind.RATE_LIMITED, 1, MonitorDecision.RETRY_PROVIDER),
        (ProviderErrorKind.TRANSIENT, 2, MonitorDecision.STOP_BLOCKED),
        (ProviderErrorKind.AUTHENTICATION, 0, MonitorDecision.STOP_BLOCKED),
        (ProviderErrorKind.AUTHORIZATION, 0, MonitorDecision.STOP_BLOCKED),
        (ProviderErrorKind.NOT_FOUND, 0, MonitorDecision.STOP_BLOCKED),
        (ProviderErrorKind.SETUP, 0, MonitorDecision.STOP_BLOCKED),
    ],
)
def test_provider_failures_never_buy_a_model_turn(
    error: ProviderErrorKind,
    consecutive_errors: int,
    expected: MonitorDecision,
) -> None:
    """Transient failures back off; deterministic or repeated failures stop."""
    observation = MonitorObservation(
        "",
        MonitorObservationStatus.PROVIDER_ERROR,
        provider_error=error,
    )

    assert (
        decide_monitor(
            _state(
                consecutive_provider_errors=consecutive_errors,
                budgets=MonitorBudgets(max_provider_errors=3),
            ),
            observation,
            now=1_100.0,
        ).decision
        is expected
    )


@pytest.mark.parametrize(
    ("state", "now"),
    [
        (_state(budgets=MonitorBudgets(max_runtime_secs=100)), 1_100.0),
        (
            _state(agent_turns=8, budgets=MonitorBudgets(max_agent_turns=8)),
            1_100.0,
        ),
        (
            _state(
                input_tokens=150_000,
                output_tokens=100_000,
                budgets=MonitorBudgets(max_tokens=250_000),
            ),
            1_100.0,
        ),
    ],
)
def test_exhausted_budget_prevents_even_an_actionable_wake(
    state: MonitorState,
    now: float,
) -> None:
    """A spent budget cannot dispatch one extra unattended model turn."""
    observation = MonitorObservation(
        "new-failure",
        MonitorObservationStatus.ACTIONABLE,
    )

    assert decide_monitor(state, observation, now=now).decision is MonitorDecision.STOP_BUDGET


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_runtime_secs", 0),
        ("max_agent_turns", 0),
        ("max_tokens", 0),
        ("max_provider_errors", 0),
    ],
)
def test_structured_monitor_budgets_cannot_be_unlimited(field: str, value: int) -> None:
    """First-class monitors reject legacy goal-loop unlimited values."""
    with pytest.raises(ValueError, match=field):
        MonitorBudgets(**{field: value})


def test_provider_error_observation_requires_an_error_category() -> None:
    """A provider failure without a category cannot choose retry versus stop."""
    with pytest.raises(ValueError, match="provider_error"):
        MonitorObservation("", MonitorObservationStatus.PROVIDER_ERROR)


def test_non_error_observation_requires_a_fingerprint() -> None:
    """A comparable observation cannot bypass wake deduplication."""
    with pytest.raises(ValueError, match="fingerprint"):
        MonitorObservation("", MonitorObservationStatus.ACTIONABLE)


@pytest.mark.parametrize("fingerprint", (None, 1, True, ""))
def test_non_error_observation_requires_a_nonempty_string_fingerprint(fingerprint: object) -> None:
    """Only a real canonical fingerprint can participate in wake deduplication."""
    with pytest.raises(ValueError, match="fingerprint"):
        MonitorObservation(fingerprint, MonitorObservationStatus.ACTIONABLE)


def test_observation_rejects_untyped_status_and_provider_error() -> None:
    """Raw strings cannot bypass enum checks into a wake-capable decision."""
    with pytest.raises(ValueError, match="status"):
        MonitorObservation("actionable", "actionable")
    with pytest.raises(ValueError, match="provider_error"):
        MonitorObservation(
            "",
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error="transient",
        )


def test_provider_error_observation_requires_a_string_fingerprint() -> None:
    """Provider errors may omit a fingerprint but cannot carry an untyped one."""
    with pytest.raises(ValueError, match="fingerprint"):
        MonitorObservation(
            [],
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error=ProviderErrorKind.TRANSIENT,
        )


def test_provider_error_observation_rejects_a_changed_head_fact() -> None:
    """A failed provider call cannot claim to have observed a new revision."""
    with pytest.raises(ValueError, match="head_changed"):
        MonitorObservation(
            "",
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error=ProviderErrorKind.TRANSIENT,
            head_changed=True,
        )


@pytest.mark.parametrize(("field", "value"), (("reason_code", 42), ("summary", {})))
def test_observation_requires_string_metadata_fields(field: str, value: object) -> None:
    """Persisted/displayed observation metadata has one unambiguous text shape."""
    with pytest.raises(ValueError, match=field):
        MonitorObservation(
            "",
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error=ProviderErrorKind.TRANSIENT,
            **{field: value},
        )


def test_unknown_monitor_state_version_fails_closed() -> None:
    """A newer persisted policy cannot inherit today's permissive branches."""
    observation = MonitorObservation(
        "new-failure",
        MonitorObservationStatus.ACTIONABLE,
    )

    assert (
        decide_monitor(
            _state(version=99),
            observation,
            now=1_100.0,
        ).decision
        is MonitorDecision.STOP_BLOCKED
    )


class TestVerdictCarriesItsEvidence:
    """A verdict must name the observations it was rendered against.

    A bare decision selects an effect and says nothing about what was seen, so
    the evidence had to be re-derived downstream from persisted state the
    verdict never named. That re-derivation is what kept a subject reduced to
    one comparable fingerprint, because a consumer rebuilding the evidence
    itself cannot be handed a list it never asked for.
    """

    @pytest.mark.parametrize(
        ("observation", "state"),
        [
            (
                MonitorObservation("pending-b", MonitorObservationStatus.PENDING),
                _state(last_fingerprint="pending-a"),
            ),
            (
                MonitorObservation("failure-b", MonitorObservationStatus.ACTIONABLE),
                _state(last_fingerprint="pending-a"),
            ),
            (
                MonitorObservation("ready-c", MonitorObservationStatus.SUCCESS),
                _state(last_fingerprint="pending-a"),
            ),
            (
                MonitorObservation(
                    "",
                    MonitorObservationStatus.PROVIDER_ERROR,
                    provider_error=ProviderErrorKind.TRANSIENT,
                ),
                _state(),
            ),
            (
                MonitorObservation("any", MonitorObservationStatus.PENDING),
                _state(version=99),
            ),
        ],
    )
    def test_every_decision_path_names_the_observation_it_judged(
        self,
        observation: MonitorObservation,
        state: MonitorState,
    ) -> None:
        """Including the paths that never inspect it, such as a rejected version.

        Callers advance durable probe state for any verdict a probe produced, so
        a path that returns without reading the observation must still name it.
        """
        verdict = decide_monitor(state, observation, now=1_100.0)

        assert verdict.entries == (observation,)

    def test_a_spent_budget_still_names_the_observation_it_refused_to_act_on(self) -> None:
        state = _state(agent_turns=99, budgets=MonitorBudgets(max_agent_turns=1))
        observation = MonitorObservation("failure-b", MonitorObservationStatus.ACTIONABLE)

        verdict = decide_monitor(state, observation, now=1_100.0)

        assert verdict.decision is MonitorDecision.STOP_BUDGET
        assert verdict.entries == (observation,)


class TestVerdictRejectsMalformedPayloads:
    def test_the_decision_must_be_the_enum(self) -> None:
        with pytest.raises(ValueError, match="decision must be a MonitorDecision"):
            MonitorVerdict(decision="wake_actionable")  # type: ignore[arg-type]

    def test_entries_must_be_an_immutable_tuple(self) -> None:
        observation = MonitorObservation("a", MonitorObservationStatus.PENDING)
        with pytest.raises(ValueError, match="entries must be a tuple"):
            MonitorVerdict(
                decision=MonitorDecision.NO_CHANGE,
                entries=[observation],  # type: ignore[arg-type]
            )

    def test_an_entry_must_be_an_observation(self) -> None:
        with pytest.raises(ValueError, match="every verdict entry must be a MonitorObservation"):
            MonitorVerdict(
                decision=MonitorDecision.NO_CHANGE,
                entries=("failure-b",),  # type: ignore[arg-type]
            )

    def test_several_entries_are_accepted_before_any_probe_reports_them(self) -> None:
        """The plural shape is the point: it must not need widening later."""
        first = MonitorObservation("a", MonitorObservationStatus.ACTIONABLE)
        second = MonitorObservation("b", MonitorObservationStatus.PENDING)

        verdict = MonitorVerdict(
            decision=MonitorDecision.WAKE_ACTIONABLE,
            entries=(first, second),
        )

        assert verdict.entries == (first, second)


class TestAnUnattemptedProbeNeverPredictsRetirement:
    """The third outcome, at the layer that SPENDS the budget rather than charges it.

    ``shadow.apply_monitor_probe`` and the production counting site in
    ``autonudge`` both already refuse to charge a shared-cooldown skip, and both
    are pinned. Neither of them sees this: ``_provider_error_decision`` reads the
    SAME budget one tick into the future (``consecutive_provider_errors + 1``),
    so a skip arriving on a watch one real error short of its ceiling retires it
    on a failure nothing will ever count -- and the watch made no API call of its
    own to earn that.
    """

    @staticmethod
    def _skip() -> MonitorObservation:
        return MonitorObservation(
            "fp-cooldown",
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error=ProviderErrorKind.RATE_LIMITED,
            reason_code=REASON_SHARED_COOLDOWN,
        )

    @staticmethod
    def _real() -> MonitorObservation:
        """The same shape MINUS the one field that says nothing was attempted."""
        return MonitorObservation(
            "fp-cooldown",
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error=ProviderErrorKind.RATE_LIMITED,
            reason_code="provider_rate_limited",
        )

    def test_a_skip_one_error_short_of_the_ceiling_retries_instead(self) -> None:
        state = _state(
            consecutive_provider_errors=2,
            budgets=MonitorBudgets(max_provider_errors=3),
        )

        assert (
            decide_monitor(state, self._skip(), now=1_100.0).decision
            is MonitorDecision.RETRY_PROVIDER
        )

    def test_the_same_observation_with_a_real_reason_still_retires(self) -> None:
        """The differential is the pin: without it the first test passes on a
        decision that stopped distinguishing the two."""
        state = _state(
            consecutive_provider_errors=2,
            budgets=MonitorBudgets(max_provider_errors=3),
        )

        assert (
            decide_monitor(state, self._real(), now=1_100.0).decision
            is MonitorDecision.STOP_BLOCKED
        )

    def test_a_budget_the_watch_really_spent_still_stops_a_skip(self) -> None:
        """Only the PREDICTION is corrected. An exhausted budget is not a
        prediction, so ``monitor_budget_reason`` keeps the stop -- otherwise this
        fix would make a shared cooldown a way to outlive the ceiling forever.
        """
        state = _state(
            provider_error_count=3,
            budgets=MonitorBudgets(max_provider_errors=3),
        )

        assert monitor_budget_reason(state, now=1_100.0) == MONITOR_STOP_PROVIDER_ERROR_BUDGET
        assert (
            decide_monitor(state, self._skip(), now=1_100.0).decision is MonitorDecision.STOP_BUDGET
        )

    def test_a_skip_on_a_non_retryable_kind_is_still_blocked(self) -> None:
        """The kind gate runs FIRST and stays first: an unattempted probe that
        somehow reports a non-retryable kind is not made retryable by being
        unattempted.
        """
        observation = MonitorObservation(
            "fp-cooldown",
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error=ProviderErrorKind.AUTHENTICATION,
            reason_code=REASON_SHARED_COOLDOWN,
        )

        assert (
            decide_monitor(_state(), observation, now=1_100.0).decision
            is MonitorDecision.STOP_BLOCKED
        )


def _suppressed_red(
    fingerprint: str = "red-1",
    reason: str = "checks_failed",
    conditions: tuple[MonitorCondition, ...] = (),
) -> MonitorObservation:
    """An actionable subject already alerted, so every tick decides NO_CHANGE."""
    return MonitorObservation(
        fingerprint,
        MonitorObservationStatus.ACTIONABLE,
        reason_code=reason,
        summary="One check is failing.",
        conditions=conditions,
    )


def _suppressed_red_state(**changes: object) -> MonitorState:
    return _state(
        last_fingerprint="red-1",
        last_wake_fingerprint="red-1",
        coalesce_alerted={f"{MONITOR_REVISION_KEY_SPACE}red-1": 1_000.0},
        **changes,
    )


_T0 = 1_100.0


def _tick_at(state: MonitorState, index: int) -> float:
    """The clock at tick *index*, advancing by the watch's own cadence.

    The trip needs elapsed wall-clock, so a test that advances by one second per
    tick would never reach the floor. Advancing by the cadence is also what a real
    driver does.
    """
    return _T0 + index * state.cadence_secs


class TestTheStallStreak:
    """A watch that keeps reaching the same conclusion retires itself.

    The engine, not an instruction: the counter lives in ``MonitorState`` and is
    folded by ``decide_monitor``, so it survives the compaction an advisory prose
    counter is lost to. The trip needs BOTH a repeated-conclusion count and
    elapsed wall-clock, the second measured rather than translated from the first.
    """

    def _run(self, state: MonitorState, ticks: int, **kwargs: object) -> list[MonitorDecision]:
        """Feed the same suppressed-red observation for *ticks* real ticks."""
        observation = _suppressed_red(**kwargs)  # type: ignore[arg-type]
        return [
            decide_monitor(state, observation, now=_tick_at(state, index)).decision
            for index in range(ticks)
        ]

    def test_identical_counted_verdicts_retire_the_watch(self) -> None:
        """Built at the REAL constants, so the trigger is one production reaches.

        Every tick is the ordinary suppressed-red shape: actionable, already
        alerted, inside its re-alert interval, so dedupe answers NO_CHANGE and
        nothing about the verdict moves. At the default cadence twelve of those
        span 3300s, past the floor, so the count is what binds.
        """
        state = _suppressed_red_state()
        decisions = self._run(state, DEFAULT_MONITOR_STALL_TICKS)

        assert decisions[:-1] == [MonitorDecision.NO_CHANGE] * (DEFAULT_MONITOR_STALL_TICKS - 1)
        assert decisions[-1] is MonitorDecision.STOP_BLOCKED
        assert state.stall_streak == DEFAULT_MONITOR_STALL_TICKS
        last = _tick_at(state, DEFAULT_MONITOR_STALL_TICKS - 1)
        assert monitor_stall_reason(state, now=last) == MONITOR_STOP_VERDICT_STALL

    def test_one_tick_short_of_the_count_keeps_watching(self) -> None:
        """The differential: without it the test above passes on a stop that
        fires at any count, including one."""
        state = _suppressed_red_state()
        decisions = self._run(state, DEFAULT_MONITOR_STALL_TICKS - 1)

        assert set(decisions) == {MonitorDecision.NO_CHANGE}
        assert state.stall_streak == DEFAULT_MONITOR_STALL_TICKS - 1
        last = _tick_at(state, DEFAULT_MONITOR_STALL_TICKS - 2)
        assert monitor_stall_reason(state, now=last) == ""

    def test_the_count_alone_never_retires_a_fast_watch(self) -> None:
        """The floor is MEASURED, so a short cadence cannot shorten it.

        Twelve ticks at the 15s minimum is 165s. Retiring there would kill a watch
        whose agent is still working -- and no ceiling derived from the cadence is
        needed to prevent it, because the elapsed time is read directly.
        """
        state = _suppressed_red_state(cadence_secs=MIN_MONITOR_CADENCE_SECS)
        needed = DEFAULT_MONITOR_STALL_MIN_SECS // MIN_MONITOR_CADENCE_SECS + 1
        decisions = self._run(state, needed)

        assert set(decisions[:-1]) == {MonitorDecision.NO_CHANGE}
        assert decisions[DEFAULT_MONITOR_STALL_TICKS - 1] is MonitorDecision.NO_CHANGE
        assert decisions[-1] is MonitorDecision.STOP_BLOCKED
        assert state.stall_streak == needed
        assert needed > DEFAULT_MONITOR_STALL_TICKS

    def test_a_cadence_change_mid_streak_needs_no_invalidation(self) -> None:
        """Nothing is derived from the cadence, so nothing goes stale.

        A ceiling derived from the cadence would break here: a streak opened at
        the 300s default would carry that ceiling into a 15s cadence and trip a
        quarter of the way into the floor. Measuring elapsed time removes the
        translation, so the same sequence simply keeps waiting.
        """
        state = _suppressed_red_state()
        decide_monitor(state, _suppressed_red(), now=_T0)
        assert state.stall_streak == 1
        assert state.stall_started_at == _T0

        state.cadence_secs = MIN_MONITOR_CADENCE_SECS
        for index in range(1, DEFAULT_MONITOR_STALL_TICKS + 4):
            now = _T0 + index * MIN_MONITOR_CADENCE_SECS
            assert decide_monitor(state, _suppressed_red(), now=now).decision is (
                MonitorDecision.NO_CHANGE
            )
        # Well past the old ceiling, nowhere near the floor.
        assert state.stall_streak > DEFAULT_MONITOR_STALL_TICKS
        elapsed = (DEFAULT_MONITOR_STALL_TICKS + 3) * MIN_MONITOR_CADENCE_SECS
        assert elapsed < DEFAULT_MONITOR_STALL_MIN_SECS
        assert monitor_stall_reason(state, now=_T0 + elapsed) == ""

    def test_the_digest_is_derived_and_never_a_copy_of_the_verdict(self) -> None:
        """One source of truth, one layer up.

        The persisted value is a digest of an EARLIER verdict -- a hash, not the
        verdict, and not equal to the subject fingerprint the same record already
        carries. Nothing holds a second copy for it to drift from.
        """
        state = _suppressed_red_state()
        decide_monitor(state, _suppressed_red(), now=_T0)

        assert len(state.stall_digest) == 64
        assert state.stall_digest != state.last_fingerprint
        assert state.stall_digest != _suppressed_red().fingerprint

    def test_a_pending_tick_zeroes_the_streak(self) -> None:
        """A tick with checks still in flight concludes nothing AND interrupts.

        Zeroing rather than skipping is what makes the clock safe to read in the
        trip: a streak left standing through a long pending stretch would let time
        alone satisfy the floor, and the next merge or close would carry a stall's
        reason. The price is that a flapping subject never accumulates a streak
        and is retired by its runtime budget -- later, but honestly labelled.
        """
        state = _suppressed_red_state()
        self._run(state, DEFAULT_MONITOR_STALL_TICKS - 1)
        assert state.stall_streak == DEFAULT_MONITOR_STALL_TICKS - 1

        pending = MonitorObservation(
            "pending-1",
            MonitorObservationStatus.PENDING,
            reason_code="checks_pending",
        )
        assert decide_monitor(state, pending, now=4_400.0).decision is (MonitorDecision.RECORD_ONLY)
        assert state.stall_streak == 0
        assert state.stall_digest == ""
        assert state.stall_started_at == 0.0
        assert monitor_stall_reason(state, now=4_400.0) == ""

    def test_a_provider_error_tick_zeroes_the_streak(self) -> None:
        """No evidence about the subject, and it already has its own budget."""
        state = _suppressed_red_state()
        self._run(state, DEFAULT_MONITOR_STALL_TICKS - 1)

        error = MonitorObservation(
            "",
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error=ProviderErrorKind.RATE_LIMITED,
            reason_code="provider_rate_limited",
        )
        decide_monitor(state, error, now=4_400.0)

        assert state.stall_streak == 0
        assert monitor_stall_reason(state, now=4_400.0) == ""

    def test_a_settled_tick_that_acted_zeroes_the_streak(self) -> None:
        """A wake is the watch working, so the streak starts over, not resumes."""
        state = _suppressed_red_state()
        self._run(state, DEFAULT_MONITOR_STALL_TICKS - 1)

        moved = _suppressed_red("red-2", "merge_conflict")
        assert decide_monitor(state, moved, now=4_400.0).decision is (
            MonitorDecision.WAKE_ACTIONABLE
        )
        assert state.stall_digest == ""
        assert state.stall_streak == 0
        assert state.stall_started_at == 0.0

    def test_a_differing_counted_verdict_restarts_the_streak_at_one(self) -> None:
        """A counted tick that differs IS the first of a possible new streak.

        Zeroing instead would need one extra identical tick before the stop and
        make the count mean something other than identical conclusions.
        """
        state = _suppressed_red_state()
        self._run(state, DEFAULT_MONITOR_STALL_TICKS - 1)
        first_digest = state.stall_digest

        # Same suppressed-red shape, so still NO_CHANGE, but a different reason
        # makes it a different verdict.
        relabelled = _suppressed_red("red-1", "unresolved_review_threads")
        assert decide_monitor(state, relabelled, now=4_400.0).decision is (
            MonitorDecision.NO_CHANGE
        )
        assert state.stall_streak == 1
        assert state.stall_started_at == 4_400.0
        assert state.stall_digest != first_digest

    def test_a_head_change_alone_restarts_the_streak(self) -> None:
        """Progress the fingerprint cannot show still moves the digest.

        ``head_changed`` is inside the digest, so a new commit breaks the streak
        even when every other fact about the subject reads the same.

        The subject is carried by a ``NEVER`` condition deliberately. A new head
        clears the revision-scoped half of the dedupe memory, so a
        revision-scoped condition would legitimately wake on this tick and the
        DECISION would move alongside the digest, leaving nothing attributable to
        the digest alone. A review thread survives a force-push, so its mask
        survives it too and the digest is the only thing that notices the commit.
        """
        threads = MonitorCondition(key="unresolved_threads", resets_on=MonitorResetsOn.NEVER)
        state = _state(
            last_fingerprint="red-1",
            last_wake_fingerprint="red-1",
            coalesce_alerted={monitor_condition_dedupe_key(threads): 1_000.0},
        )
        held = _suppressed_red(conditions=(threads,))
        for index in range(DEFAULT_MONITOR_STALL_TICKS - 1):
            assert decide_monitor(state, held, now=_tick_at(state, index)).decision is (
                MonitorDecision.NO_CHANGE
            )

        pushed = MonitorObservation(
            "red-1",
            MonitorObservationStatus.ACTIONABLE,
            reason_code="checks_failed",
            summary="One check is failing.",
            head_changed=True,
            conditions=(threads,),
        )
        # Dedupe still suppresses the wake -- the sticky condition is inside its
        # re-alert interval -- so the DECISION is unchanged and the digest is the
        # only thing that notices the new commit.
        assert decide_monitor(state, pushed, now=4_400.0).decision is MonitorDecision.NO_CHANGE
        assert state.stall_streak == 1

    def test_a_coalescing_hold_never_retires_the_watch(self) -> None:
        """A tick held by the coalescing floor is waiting on purpose.

        ``RECORD_ONLY`` is a deliberate deferral, so it zeroes rather than counts.
        Counting it retired the watch before the window could release the change
        it was folding -- losing that wake entirely.
        """
        state = _state(last_fingerprint="red-1", cadence_secs=MIN_MONITOR_CADENCE_SECS)
        assert decide_monitor(state, _suppressed_red(), now=0.0).decision is (
            MonitorDecision.WAKE_ACTIONABLE
        )
        # The caller stamps the alert time next to its own persist.
        state.coalesce_alerted[f"{MONITOR_REVISION_KEY_SPACE}red-1"] = 0.0

        held = _suppressed_red("red-2")
        for index in range(1, DEFAULT_MONITOR_STALL_TICKS + 3):
            now = float(MIN_MONITOR_CADENCE_SECS * index)
            assert decide_monitor(state, held, now=now).decision is MonitorDecision.RECORD_ONLY
            assert state.stall_streak == 0

        # The arithmetic that made a tick-only ceiling wrong, on the record.
        assert MIN_MONITOR_CADENCE_SECS * DEFAULT_MONITOR_STALL_TICKS < (
            DEFAULT_MONITOR_COALESCE_SECS
        )
        # And the wake it was folding still arrives once the floor passes.
        assert decide_monitor(state, held, now=DEFAULT_MONITOR_COALESCE_SECS + 1.0).decision is (
            MonitorDecision.WAKE_ACTIONABLE
        )

    def test_the_three_stops_stay_distinguishable_in_the_record(self) -> None:
        """The cycle-cap anti-pattern, pinned.

        A stall that recorded what a convergence records, or what a spent budget
        records, would be a cap wearing a new field: the reader could not tell
        which of the three happened, and the three have different remedies.
        """
        converged = _state(last_fingerprint="red-1")
        assert (
            decide_monitor(
                converged,
                MonitorObservation(
                    "green-1",
                    MonitorObservationStatus.SUCCESS,
                    reason_code="review_ready",
                ),
                now=_T0,
            ).decision
            is MonitorDecision.STOP_SUCCESS
        )
        assert monitor_stall_reason(converged, now=_T0) == ""

        spent = _suppressed_red_state(budgets=MonitorBudgets(max_runtime_secs=10))
        assert decide_monitor(spent, _suppressed_red(), now=_T0).decision is (
            MonitorDecision.STOP_BUDGET
        )
        assert monitor_budget_reason(spent, now=_T0) == MONITOR_STOP_RUNTIME_BUDGET
        assert monitor_stall_reason(spent, now=_T0) == ""

        stalled = _suppressed_red_state()
        last = self._run(stalled, DEFAULT_MONITOR_STALL_TICKS)[-1]
        tripped_at = _tick_at(stalled, DEFAULT_MONITOR_STALL_TICKS - 1)
        assert last is MonitorDecision.STOP_BLOCKED
        assert monitor_stall_reason(stalled, now=tripped_at) == MONITOR_STOP_VERDICT_STALL
        assert monitor_budget_reason(stalled, now=tripped_at) == ""
        assert MONITOR_STOP_VERDICT_STALL != MONITOR_STOP_APPROVAL_STALL

    def test_a_spent_budget_outranks_a_streak_one_tick_from_tripping(self) -> None:
        """Budget keeps position two, so its own reason is what gets recorded."""
        state = _suppressed_red_state()
        self._run(state, DEFAULT_MONITOR_STALL_TICKS - 1)

        state.budgets = MonitorBudgets(max_runtime_secs=10)
        assert decide_monitor(state, _suppressed_red(), now=4_400.0).decision is (
            MonitorDecision.STOP_BUDGET
        )
        assert state.stall_streak == 0
        assert monitor_stall_reason(state, now=4_400.0) == ""

    def test_a_real_block_at_the_streak_edge_is_not_filed_as_a_stall(self) -> None:
        """A closed pull request is a close, whatever the streak says."""
        state = _suppressed_red_state()
        self._run(state, DEFAULT_MONITOR_STALL_TICKS - 1)

        closed = MonitorObservation(
            "closed-1",
            MonitorObservationStatus.BLOCKED,
            reason_code="pull_request_closed",
        )
        assert decide_monitor(state, closed, now=4_400.0).decision is (MonitorDecision.STOP_BLOCKED)
        assert state.stall_streak == 0
        assert monitor_stall_reason(state, now=4_400.0) == ""

    def test_a_provider_stop_after_a_long_quiet_stretch_is_not_filed_as_a_stall(self) -> None:
        """The case that MAKES the unsettled zeroing load-bearing.

        A merge or a close is settled, so the other branch zeroes it. An
        unsettled TERMINAL tick -- a non-retryable provider error -- is the one
        that reaches the reason composition without a settled decision of its own.
        Leave a streak standing through a pending stretch and the clock alone
        satisfies the floor, so that provider stop is filed ``verdict_stall``
        instead of naming the provider.
        """
        state = _suppressed_red_state(cadence_secs=MIN_MONITOR_CADENCE_SECS)
        self._run(state, DEFAULT_MONITOR_STALL_TICKS)
        assert state.stall_streak == DEFAULT_MONITOR_STALL_TICKS
        # Short of the floor, so nothing has tripped yet.
        assert monitor_stall_reason(state, now=_tick_at(state, DEFAULT_MONITOR_STALL_TICKS)) == ""

        pending = MonitorObservation(
            "pending-1",
            MonitorObservationStatus.PENDING,
            reason_code="checks_pending",
        )
        decide_monitor(state, pending, now=_T0 + 200.0)

        # Long enough that elapsed since the streak began clears the floor.
        stopped_at = _T0 + DEFAULT_MONITOR_STALL_MIN_SECS * 2
        refused = MonitorObservation(
            "",
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error=ProviderErrorKind.AUTHENTICATION,
            reason_code="provider_authentication",
        )
        assert decide_monitor(state, refused, now=stopped_at).decision is (
            MonitorDecision.STOP_BLOCKED
        )
        assert monitor_stall_reason(state, now=stopped_at) == ""

    def test_a_merge_after_a_long_quiet_stretch_is_still_a_success(self) -> None:
        """The clock-driven form of the anti-pattern, pinned.

        The trip reads elapsed time, so a streak left standing while the clock ran
        past the floor would hand this merge a stall's reason. The pending stretch
        zeroes the streak, so it cannot.
        """
        state = _suppressed_red_state(cadence_secs=MIN_MONITOR_CADENCE_SECS)
        self._run(state, DEFAULT_MONITOR_STALL_TICKS)
        assert state.stall_streak == DEFAULT_MONITOR_STALL_TICKS

        pending = MonitorObservation(
            "pending-1",
            MonitorObservationStatus.PENDING,
            reason_code="checks_pending",
        )
        decide_monitor(state, pending, now=_T0 + 200.0)

        merged_at = _T0 + DEFAULT_MONITOR_STALL_MIN_SECS * 2
        merged = MonitorObservation(
            "green-1",
            MonitorObservationStatus.SUCCESS,
            reason_code="pull_request_merged",
        )
        assert decide_monitor(state, merged, now=merged_at).decision is (
            MonitorDecision.STOP_SUCCESS
        )
        assert monitor_stall_reason(state, now=merged_at) == ""

    def test_a_clock_that_went_backwards_does_not_retire_the_watch(self) -> None:
        """A negative interval reads as not-yet, which keeps the watch alive."""
        state = _suppressed_red_state()
        self._run(state, DEFAULT_MONITOR_STALL_TICKS - 1)

        assert monitor_stall_reason(state, now=0.0) == ""

    def test_both_thresholds_are_bounded_by_numbers_already_in_the_module(self) -> None:
        """Anchors, not assertions -- the answer to "who chose these".

        Each threshold is fixed on both sides by an existing constant, so a reader
        can check it rather than take it. The count must exceed the floor's tick
        equivalent at the default cadence, or it never binds; it must fall inside
        the ticks a default watch gets before its runtime budget, or the stall can
        never fire. The floor must clear the coalescing window by a wide margin,
        or a folded burst looks like a stall; and it must stay well under the
        re-alert interval, because a re-alert zeroes the streak, so a floor at or
        past it could never be reached. Exact values are pinned too, so moving
        either has to be acknowledged in the diff.
        """
        assert DEFAULT_MONITOR_STALL_TICKS == 12
        assert DEFAULT_MONITOR_STALL_MIN_SECS == 1800

        floor_in_default_ticks = DEFAULT_MONITOR_STALL_MIN_SECS // DEFAULT_MONITOR_CADENCE_SECS
        ticks_before_the_runtime_budget = (
            DEFAULT_MONITOR_RUNTIME_SECS // DEFAULT_MONITOR_CADENCE_SECS
        )
        assert floor_in_default_ticks == 6
        assert ticks_before_the_runtime_budget == 48
        assert floor_in_default_ticks < DEFAULT_MONITOR_STALL_TICKS
        assert DEFAULT_MONITOR_STALL_TICKS < ticks_before_the_runtime_budget

        assert DEFAULT_MONITOR_STALL_MIN_SECS > DEFAULT_MONITOR_COALESCE_SECS * 5
        assert DEFAULT_MONITOR_STALL_MIN_SECS * 5 < DEFAULT_MONITOR_REALERT_SECS
        # Reachable at the fast end too: the floor is what binds there, and it
        # fits inside the same runtime budget.
        assert DEFAULT_MONITOR_STALL_MIN_SECS < DEFAULT_MONITOR_RUNTIME_SECS

        # The two OUTER bounds are pinned exactly as well, not only by relation.
        # A relation alone lets either move while the spec paragraph below keeps
        # quoting the old number, which is the drift this case exists to stop.
        assert DEFAULT_MONITOR_COALESCE_SECS == 240.0
        assert DEFAULT_MONITOR_REALERT_SECS == 21_600

        # And the paragraph is read, not trusted. The spec states these chains as
        # prose a reader is invited to check, so the numbers it quotes are built
        # from the constants here: move a constant and this fails naming the file
        # that has to change with it.
        spec = (
            pathlib.Path(__file__).resolve().parents[1]
            / "docs"
            / "system-specs"
            / "modules"
            / "monitor-architecture.md"
        ).read_text(encoding="utf-8")
        ticks_chain = (
            f"{floor_in_default_ticks} < {DEFAULT_MONITOR_STALL_TICKS} "
            f"< {ticks_before_the_runtime_budget}"
        )
        secs_chain = (
            f"{int(DEFAULT_MONITOR_COALESCE_SECS)} << {DEFAULT_MONITOR_STALL_MIN_SECS} "
            f"<< {DEFAULT_MONITOR_REALERT_SECS}"
        )
        assert ticks_chain in spec, f"monitor-architecture.md no longer states {ticks_chain}"
        assert secs_chain in spec, f"monitor-architecture.md no longer states {secs_chain}"

    def test_a_record_written_before_the_fields_starts_a_fresh_streak(self) -> None:
        """Absent is absent, and here that is the honest reading.

        The record carries ``last_decision`` and ``last_fingerprint`` but not the
        rest of the last verdict's entry, so a seeded digest could claim a match
        that never happened -- and the only direction that error runs is retiring
        a live watch.
        """
        raw = monitor_state_to_dict(
            _state(last_fingerprint="red-1", last_decision=MonitorDecision.NO_CHANGE)
        )
        raw.pop("stall_digest")
        raw.pop("stall_streak")
        raw.pop("stall_started_at")

        restored = monitor_state_from_dict(raw)

        assert restored.stall_digest == ""
        assert restored.stall_streak == 0
        assert restored.stall_started_at == 0.0

    def test_a_present_streak_is_preserved_across_a_round_trip(self) -> None:
        """Present-but-empty and present-but-set are both left alone."""
        raw = monitor_state_to_dict(
            _suppressed_red_state(stall_digest="d" * 64, stall_streak=5, stall_started_at=_T0)
        )

        restored = monitor_state_from_dict(raw)

        assert restored.stall_digest == "d" * 64
        assert restored.stall_streak == 5
        assert restored.stall_started_at == _T0

    def test_a_malformed_streak_is_refused_rather_than_coerced(self) -> None:
        with pytest.raises(ValueError, match="stall_streak must be a non-negative integer"):
            _state(stall_streak=-1)
        with pytest.raises(ValueError, match="stall_digest must be a string"):
            _state(stall_digest=object())
        with pytest.raises(ValueError, match="stall_started_at must be a finite"):
            _state(stall_started_at=-1.0)
