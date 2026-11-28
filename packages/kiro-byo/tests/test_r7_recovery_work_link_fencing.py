"""R7: Crew task reassignment must not replay a different durable Run."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.turn_runner import TurnRunnerMixin


class Harness(TurnRunnerMixin):
    runtime_trace = None

    def __init__(self, store):
        self._kiro_durable_run_store = store
        self.errors = []

    def error(self, request_id, code, message):
        self.errors.append((request_id, code, message))

    def trace(self, message):
        pass


def _session(link):
    return SimpleNamespace(
        session_id="crew-worker", state_lock=threading.RLock(),
        host_work_link=link, messages=[{"role": "user", "content": "original"}],
        events=[], model="unused",
    )


@pytest.mark.parametrize("old_link,new_link", [
    ({"taskId": "crew-work:old", "role": "worker"}, {"taskId": "crew-work:new", "role": "worker"}),
    ({"taskId": "crew-work:old", "role": "worker"}, {}),
    ({}, {"taskId": "crew-work:new", "role": "worker"}),
])
def test_reassignment_rejects_durable_recovery_without_mutating_session(tmp_path, old_link, new_link):
    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("crew-worker", metadata={"host_work_link": old_link, "history_start": 0, "user_event_seq": 0})
    store.mark_recovery(run["run_id"], "pending_restart")
    h = Harness(store)
    s = _session(new_link)
    before_messages = list(s.messages)
    assert not h._rewind_durable_prompt(1, "crew-worker", s, None, run["run_id"], "resume")
    assert h.errors and h.errors[-1][1] == -32010
    assert "assignment changed" in h.errors[-1][2]
    assert s.messages == before_messages
    assert not s.events


def test_same_crew_assignment_not_blocked_by_identity_guard(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    link = {"taskId": "crew-work:old", "role": "worker"}
    run = store.create_run("crew-worker", metadata={"host_work_link": link, "history_start": 1, "user_event_seq": 0})
    store.mark_recovery(run["run_id"], "pending_restart")
    h = Harness(store)
    s = _session(link)
    h._persist_session = lambda session: True
    h._runtime_event = lambda *args, **kwargs: None
    assert h._rewind_durable_prompt(1, "crew-worker", s, None, run["run_id"], "resume")
    assert not h.errors


def test_legacy_unlinked_run_still_resumable_in_unlinked_session(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("crew-worker", metadata={"history_start": 1, "user_event_seq": 0})
    store.mark_recovery(run["run_id"], "pending_restart")
    h = Harness(store)
    s = _session({})
    h._persist_session = lambda session: True
    h._runtime_event = lambda *args, **kwargs: None
    assert h._rewind_durable_prompt(1, "crew-worker", s, None, run["run_id"], "resume")
    assert not h.errors


def test_corrupted_prior_link_fails_closed_instead_of_becoming_unlinked(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("crew-worker", metadata={
        "host_work_link": {"taskId": "INVALID TASK ID"}, "history_start": 1, "user_event_seq": 0,
    })
    store.mark_recovery(run["run_id"], "pending_restart")
    h = Harness(store)
    s = _session({})
    assert not h._rewind_durable_prompt(1, "crew-worker", s, None, run["run_id"], "resume")
    assert "invalid Crew work association" in h.errors[-1][2]
