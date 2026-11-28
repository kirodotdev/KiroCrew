"""R7 regression: lease renewals must be scoped to their originating Run."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from kiro_byo.turn_runner import TurnRunnerMixin
from kiro_byo import turn_runner


class Harness(TurnRunnerMixin):
    runtime_trace = None

    def trace(self, message):
        pass


def _wait_for(condition, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.002)
    assert condition(), "heartbeat did not reach expected state"


@pytest.mark.parametrize("failure", ["missing", "expired", "io"])
def test_current_lease_renewal_failure_fails_closed(monkeypatch, failure):
    monkeypatch.setattr(turn_runner, "RUN_LEASE_HEARTBEAT_SECONDS", 0.001)
    renewed = threading.Event()

    def fail(*args, **kwargs):
        renewed.set()
        if failure == "expired":
            raise ValueError("expired")
        if failure == "io":
            raise OSError("storage offline")
        return None

    h = Harness()
    h._kiro_durable_run_store = SimpleNamespace(renew_lease=fail)
    session = SimpleNamespace(active_run_lease_stop=None)
    try:
        h._start_run_lease_heartbeat(session, "current", "owner")
        assert renewed.wait(1)
        _wait_for(lambda: session.active_run_lease_lost)
    finally:
        h._stop_run_lease_heartbeat(session)


@pytest.mark.parametrize("failure", ["missing", "expired"])
def test_late_old_renewal_cannot_poison_next_run(monkeypatch, failure):
    """Reproduce the stop(timeout) vs in-flight disk-renewal race."""
    monkeypatch.setattr(turn_runner, "RUN_LEASE_HEARTBEAT_SECONDS", 0.001)
    old_entered = threading.Event()
    release_old = threading.Event()
    new_renewed = threading.Event()

    def renew(run_id, *, owner_token, lease_ttl):
        if run_id == "old":
            old_entered.set()
            assert release_old.wait(2)
            if failure == "missing":
                return None
            raise ValueError("stale owner")
        new_renewed.set()
        return {"run_id": "new", "lease": {"owner_token": owner_token}}

    h = Harness()
    h._kiro_durable_run_store = SimpleNamespace(renew_lease=renew)
    session = SimpleNamespace(active_run_lease_stop=None)
    old_thread = None
    try:
        h._start_run_lease_heartbeat(session, "old", "old-owner")
        old_thread = session.active_run_lease_thread
        assert old_entered.wait(1)
        h._stop_run_lease_heartbeat(session)  # returns before blocked old renewal
        assert old_thread.is_alive()
        h._start_run_lease_heartbeat(session, "new", "new-owner")
        assert new_renewed.wait(1)
        release_old.set()
        old_thread.join(timeout=1)
        assert not old_thread.is_alive()
        assert session.active_run_lease_lost is False
        assert session.active_run_lease_stop is not None
    finally:
        release_old.set()
        h._stop_run_lease_heartbeat(session)
        if old_thread:
            old_thread.join(timeout=1)


def test_current_renewal_success_does_not_mark_lease_lost(monkeypatch):
    monkeypatch.setattr(turn_runner, "RUN_LEASE_HEARTBEAT_SECONDS", 0.001)
    renewed = threading.Event()

    def renew(run_id, *, owner_token, lease_ttl):
        renewed.set()
        return {"run_id": run_id}

    h = Harness()
    h._kiro_durable_run_store = SimpleNamespace(renew_lease=renew)
    session = SimpleNamespace(active_run_lease_stop=None)
    try:
        h._start_run_lease_heartbeat(session, "run", "owner")
        assert renewed.wait(1)
        assert session.active_run_lease_lost is False
    finally:
        h._stop_run_lease_heartbeat(session)
