"""Behavioral contract for probe-first monitor decisions."""

from __future__ import annotations

import pytest

from kiro_crew.monitoring.decision import decide_monitor, monitor_budget_reason
from kiro_crew.monitoring.github_provider_errors import REASON_SHARED_COOLDOWN
from kiro_crew.monitoring.models import (
    MONITOR_STOP_PROVIDER_ERROR_BUDGET,
    MonitorBudgets,
    MonitorDecision,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorState,
    MonitorVerdict,
    ProviderErrorKind,
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
                # now=1100 sits well inside the re-alert period from 1000.
                coalesce_alerted={"failure-b": 1_000.0},
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
