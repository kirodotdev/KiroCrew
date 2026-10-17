"""The shared recovery schedule: bounded jitter, capped backoff, escalation, cooldown."""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from kiro_crew.recovery import policy as pol
from kiro_crew.recovery.ladder import (
    L1_TOOL_CALL,
    L4_GATEWAYD,
    L5_GATEWAY,
    LADDER,
    LAYERS,
)


def _layer(**overrides) -> pol.LayerPolicy:
    base = dict(layer="t", trigger="test", max_attempts=3)
    base.update(overrides)
    return pol.LayerPolicy(**base)


class TestBackoffShape:
    def test_raw_backoff_doubles_from_the_base(self):
        lp = _layer(base_secs=2.0, max_secs=120.0)
        assert [lp.raw_backoff_secs(a) for a in (1, 2, 3, 4)] == [2.0, 4.0, 8.0, 16.0]

    def test_raw_backoff_is_capped(self):
        lp = _layer(base_secs=2.0, max_secs=120.0)
        assert lp.raw_backoff_secs(7) == 120.0
        assert lp.raw_backoff_secs(50) == 120.0  # exponent clamp, no overflow

    def test_attempt_below_one_reads_as_the_first_retry(self):
        lp = _layer(base_secs=2.0)
        assert lp.raw_backoff_secs(0) == 2.0
        assert lp.raw_backoff_secs(-9) == 2.0

    def test_jitter_is_bounded_to_the_upper_half(self):
        """Equal jitter: every draw lands in [raw/2, raw] -- never a hot loop."""
        lp = _layer(base_secs=2.0, max_secs=120.0)
        rng = random.Random(1234)
        for attempt in range(1, 8):
            raw = lp.raw_backoff_secs(attempt)
            for _ in range(200):
                d = lp.backoff_secs(attempt, rng=rng)
                assert raw / 2.0 <= d <= raw, (attempt, d, raw)

    def test_jitter_actually_spreads(self):
        lp = _layer(base_secs=2.0)
        rng = random.Random(7)
        draws = {round(lp.backoff_secs(3, rng=rng), 6) for _ in range(50)}
        assert len(draws) > 10

    def test_jitter_can_be_switched_off(self):
        lp = _layer(base_secs=2.0, jitter=False)
        assert lp.backoff_secs(3) == 8.0

    def test_jittered_delay_never_exceeds_the_cap(self):
        lp = _layer(base_secs=2.0, max_secs=5.0)
        rng = random.Random(9)
        assert all(lp.backoff_secs(a, rng=rng) <= 5.0 for a in range(1, 20))


class TestRetryAfter:
    def test_server_hint_is_a_floor(self):
        lp = _layer(base_secs=2.0, max_secs=120.0, jitter=False)
        assert lp.backoff_secs(1, retry_after_secs=30) == 30.0

    def test_backoff_wins_when_larger_than_the_hint(self):
        lp = _layer(base_secs=2.0, max_secs=120.0, jitter=False)
        assert lp.backoff_secs(6, retry_after_secs=3) == 64.0

    def test_hint_is_capped_too(self):
        """A hostile retry_after cannot park a task past the ladder's cap."""
        lp = _layer(base_secs=2.0, max_secs=120.0)
        assert lp.backoff_secs(1, retry_after_secs=10_000) == 120.0

    def test_non_positive_hint_is_ignored(self):
        lp = _layer(base_secs=2.0, jitter=False)
        assert lp.backoff_secs(1, retry_after_secs=0) == 2.0
        assert lp.backoff_secs(1, retry_after_secs=-5) == 2.0


class TestEscalation:
    def test_escalates_after_n_failures(self):
        lp = _layer(max_attempts=3)
        assert not lp.should_escalate(1)
        assert not lp.should_escalate(2)
        assert lp.should_escalate(3)
        assert lp.should_escalate(4)

    def test_zero_attempts_means_never_automatic(self):
        lp = _layer(max_attempts=0)
        assert lp.should_escalate(0)
        assert lp.should_escalate(1)


class TestTracker:
    def test_consecutive_failures_count_up(self):
        t = pol.RecoveryTracker(cooldown_secs=600)
        assert t.record_failure("u", now=0.0) == 1
        assert t.record_failure("u", now=1.0) == 2
        assert t.attempts("u", now=2.0) == 2

    def test_cooldown_forgets_a_run_of_failures(self):
        t = pol.RecoveryTracker(cooldown_secs=600)
        t.record_failure("u", now=0.0)
        t.record_failure("u", now=10.0)
        assert t.attempts("u", now=609.0) == 2
        assert t.attempts("u", now=610.0) == 0
        assert t.record_failure("u", now=611.0) == 1  # starts over

    def test_success_resets(self):
        t = pol.RecoveryTracker()
        t.record_failure("u", now=0.0)
        t.record_success("u")
        assert t.attempts("u", now=0.5) == 0
        assert t.failing_since("u", now=0.5) is None

    def test_failing_since_is_the_first_failure_of_the_run(self):
        t = pol.RecoveryTracker(cooldown_secs=600)
        t.record_failure("u", now=5.0)
        t.record_failure("u", now=9.0)
        assert t.failing_since("u", now=10.0) == 5.0

    def test_units_are_independent(self):
        t = pol.RecoveryTracker()
        t.record_failure("a", now=0.0)
        assert t.attempts("b", now=0.0) == 0

    def test_bounded_size(self):
        t = pol.RecoveryTracker()
        for i in range(pol.TRACKER_MAX_UNITS + 50):
            t.record_failure(f"u{i}", now=float(i))
        assert len(t) == pol.TRACKER_MAX_UNITS
        assert t.attempts("u0", now=0.0) == 0  # oldest evicted


class TestPolicyTable:
    def test_every_layer_is_present_in_order(self):
        assert tuple(LADDER.layers) == LAYERS

    def test_unknown_layer_is_loud(self):
        with pytest.raises(KeyError):
            LADDER.layer("L9_nope")

    def test_l5_is_never_automatic(self):
        lp = LADDER.layer(L5_GATEWAY)
        assert lp.automatic is False
        assert lp.max_attempts == 0
        assert lp.escalate_to is None

    def test_shared_defaults_match_the_rfc(self):
        assert pol.DEFAULT_BACKOFF_BASE_SECS == 2.0
        assert pol.DEFAULT_BACKOFF_MAX_SECS == 120.0
        lp = LADDER.layer(L1_TOOL_CALL)
        assert (lp.base_secs, lp.max_secs) == (2.0, 120.0)

    def test_bare_schedule_backoff(self):
        p = pol.RecoveryPolicy(base_secs=1.0, max_secs=8.0, jitter=False)
        assert p.backoff_secs(1) == 1.0
        assert p.backoff_secs(10) == 8.0


class TestConfigOverride:
    def test_from_config_reads_the_agent_keys(self):
        cfg = SimpleNamespace(
            agent=SimpleNamespace(recovery_backoff_base_secs=4.0, recovery_backoff_max_secs=30.0)
        )
        p = pol.RecoveryPolicy.from_config(cfg)
        lp = p.layer(L1_TOOL_CALL)
        assert (lp.base_secs, lp.max_secs) == (4.0, 30.0)
        assert lp.raw_backoff_secs(10) == 30.0

    def test_from_config_without_the_keys_returns_the_defaults(self):
        assert pol.RecoveryPolicy.from_config(SimpleNamespace(agent=SimpleNamespace())) is LADDER
        assert pol.RecoveryPolicy.from_config(None) is LADDER

    def test_from_config_rejects_bools_and_garbage(self):
        cfg = SimpleNamespace(
            agent=SimpleNamespace(recovery_backoff_base_secs=True, recovery_backoff_max_secs="x")
        )
        assert pol.RecoveryPolicy.from_config(cfg) is LADDER

    def test_override_is_clamped_and_cap_never_below_base(self):
        p = LADDER.with_schedule(base_secs=500.0, max_secs=0.001)
        assert p.base_secs == pol.BACKOFF_BASE_MAX_SECS
        assert p.max_secs >= p.base_secs

    def test_pinned_layer_keeps_its_own_schedule(self):
        """The gatewayd supervisor's cap is derived from the stub's reconnect budget."""
        p = LADDER.with_schedule(base_secs=10.0, max_secs=600.0)
        l4 = p.layer(L4_GATEWAYD)
        assert l4.pinned is True
        assert (l4.base_secs, l4.max_secs) == (
            LADDER.layer(L4_GATEWAYD).base_secs,
            LADDER.layer(L4_GATEWAYD).max_secs,
        )
        assert p.layer(L1_TOOL_CALL).max_secs == 600.0


def test_policy_module_is_pure_at_import():
    """No config, no asyncio, no clock read at import -- the stub can afford it."""
    import importlib
    import sys

    mod = importlib.import_module("kiro_crew.recovery.policy")
    src = open(mod.__file__, encoding="utf-8").read()
    assert "import asyncio" not in src
    assert "kiro_crew.config" not in src
    assert "\nimport time" not in src  # no clock: callers pass ``now``
    assert "kiro_crew.recovery.policy" in sys.modules
