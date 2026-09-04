"""CircuitBreaker keeps ``_states`` bounded (no per-identity leak).

The gateway keys the breaker by ``PoolKey.stable_hash()`` — an open-ended space
(agent x server x channel x config) — so fully-recovered or aged-out keys must
not accumulate for the daemon's lifetime.
"""

from __future__ import annotations

from kiro_crew.mcp_gateway.breaker import MAX_TRACKED_KEYS, CircuitBreaker


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_record_healthy_drops_the_key() -> None:
    clk = _Clock()
    b = CircuitBreaker(now_fn=clk)
    b.record_death("k", uptime_secs=0.1)
    assert "k" in b._states
    b.record_healthy("k")
    assert "k" not in b._states  # entry removed, not just cleared


def test_cooldown_close_drops_the_key() -> None:
    clk = _Clock()
    b = CircuitBreaker(fast_death_limit=2, open_cooldown_secs=30.0, now_fn=clk)
    b.record_death("k", 0.1)
    b.record_death("k", 0.1)  # trips OPEN
    assert b.allow("k") is False
    clk.t += 31.0  # cooldown elapsed
    assert b.allow("k") is True
    assert "k" not in b._states  # cleaned on cooldown-close


def test_states_pruned_under_cap() -> None:
    clk = _Clock()
    b = CircuitBreaker(window_secs=10.0, now_fn=clk)
    # Seed more than the cap with a single (soon-stale) fast death each; none
    # trip OPEN (default limit 5), so all are prunable once their death ages out.
    for i in range(MAX_TRACKED_KEYS + 50):
        b.record_death(f"k{i}", 0.1)
    # Advance past the window so every seeded death is stale, then one more
    # death fires the over-cap prune of fully-inactive keys.
    clk.t += 20.0
    b.record_death("trigger", 0.1)
    assert len(b._states) <= MAX_TRACKED_KEYS


def test_abandoned_open_keys_are_evicted_under_cap() -> None:
    # An OPEN key never probed again is unreachable by allow()/record_healthy;
    # the over-cap prune must hard-evict the oldest OPEN keys too.
    clk = _Clock()
    b = CircuitBreaker(fast_death_limit=1, now_fn=clk)  # 1 fast death -> OPEN
    for i in range(MAX_TRACKED_KEYS + 50):
        clk.t += 0.001  # distinct opened_at so eviction order is deterministic
        b.record_death(f"k{i}", 0.1)  # trips OPEN immediately, never probed again
    assert len(b._states) <= MAX_TRACKED_KEYS
