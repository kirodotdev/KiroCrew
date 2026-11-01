"""``slot_prime._primes_run`` is bounded by the same limit as ``_state``.

Both are keyed on the cells a prime has run for. ``_state`` drops a cell once its
prime succeeds, so only ``_primes_run`` would remember every cell the gateway has
ever primed; it must stop at ``_STATE_LIMIT`` like ``_state`` does.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.crew_log import eager, projection, slot_prime


@pytest.fixture
def quiet_prime(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run ``_run`` inline with the fold and the eager bookkeeping stubbed."""
    monkeypatch.setattr(slot_prime, "_STATE_LIMIT", 2)
    monkeypatch.setattr(eager, "_claim_thread_for_metrics", lambda: None)
    monkeypatch.setattr(eager, "_begin_pass", lambda: 0.0)
    monkeypatch.setattr(eager, "_close_pass", lambda **_kwargs: None)
    monkeypatch.setattr(eager, "_publish_fold", lambda *_args: None)
    monkeypatch.setattr(projection, "read_slot_projection", lambda slot, name: {"slot": slot})
    slot_prime.reset_for_tests()
    yield
    slot_prime.reset_for_tests()


def _prime(slot: str) -> None:
    key = ("home", slot, "work")
    with slot_prime._lock:
        slot_prime._state[key] = slot_prime._Prime(running=True)
    slot_prime._run(key, slot, "work")


def test_completed_counts_stop_at_the_state_limit(quiet_prime: None) -> None:
    for index in range(5):
        _prime(f"slot-{index}")

    assert len(slot_prime._primes_run) == 2
    # The newest cells are the ones kept; the oldest counts are forgotten first.
    assert set(slot_prime._primes_run) == {("home", "slot-3", "work"), ("home", "slot-4", "work")}


def test_a_cell_under_the_limit_keeps_counting(quiet_prime: None) -> None:
    _prime("slot-a")
    _prime("slot-a")

    assert slot_prime._primes_run[("home", "slot-a", "work")] == 2
