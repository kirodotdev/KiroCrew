"""The claimed-not-started probe a parent-end teardown asks before it reaps a run.

``taskq_row_is_claimed_unstarted_async`` (``admission/taskq_bridge_cancel.py``) answers True
only for a row the store holds as ``admitted``: claimed, its claimer between the claim and the
registration. Every other answer -- queued, started, missing, unreadable -- is False, because
the teardown spares a run only on positive evidence. An unreadable store is read again on the
durable sweep's bound before the probe gives up.
"""

from __future__ import annotations

import asyncio
import logging
import types

import pytest

from kiro_crew import taskq
from kiro_crew.subagent_manager.admission import taskq_bridge_cancel as cancel_owner
from kiro_crew.subagent_manager.admission.taskq_bridge import _TaskqBridgeMixin

_ADMISSION_LOGGER = "kiro_crew.subagent_manager.admission"


def _store(tmp_path):
    return taskq.TaskStore(tmp_path / "t.db").open()


def _row(row_id: str) -> taskq.TaskRecord:
    return taskq.TaskRecord(
        id=row_id,
        kind=taskq.KIND_SUBAGENT,
        session_key="dash:probe",
        params={"task": f"task {row_id}"},
    )


def _bridge(store: object) -> types.SimpleNamespace:
    return types.SimpleNamespace(taskq_store=lambda: store)


def _probe(bridge: object, agent_id: str) -> bool:
    return asyncio.run(_TaskqBridgeMixin.taskq_row_is_claimed_unstarted_async(bridge, agent_id))


class _UnreadableStore:
    """A store whose reads fail the first *failures* times, then answer *rec*."""

    def __init__(self, failures: int, rec: object = None) -> None:
        self.failures = failures
        self.rec = rec
        self.reads = 0

    def get(self, agent_id: str) -> object:
        return self.rec

    async def run(self, fn, *args):
        self.reads += 1
        if self.reads <= self.failures:
            raise taskq.TaskStoreUnavailable("database is locked")
        return fn(*args)


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-read at once: the pause between tries is the owner's constant, set to 0 here."""
    monkeypatch.setattr(cancel_owner, "_STATE_READ_BACKOFF_SECS", 0.0)


def test_a_claimed_row_that_has_not_started_is_claimed(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        store.accept([_row("adm1")])
        assert store.claim("adm1") is not None
        assert store.get("adm1").state == taskq.ADMITTED
        assert _probe(_bridge(store), "adm1") is True
    finally:
        store.close()


def test_a_queued_row_and_a_missing_row_are_not_claimed(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        store.accept([_row("q1")])
        assert store.get("q1").state == taskq.QUEUED
        assert _probe(_bridge(store), "q1") is False
        assert _probe(_bridge(store), "never-accepted") is False
    finally:
        store.close()


def test_no_store_means_no_claim() -> None:
    assert _probe(_bridge(None), "any") is False


def test_a_started_row_is_not_claimed() -> None:
    started = types.SimpleNamespace(state=taskq.RUNNING)
    assert _probe(_bridge(_UnreadableStore(0, started)), "run1") is False


@pytest.mark.usefixtures("no_backoff")
def test_an_unreadable_store_is_read_again_then_answers_not_claimed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _UnreadableStore(failures=cancel_owner._STATE_READ_ATTEMPTS)
    caplog.set_level(logging.WARNING, logger=_ADMISSION_LOGGER)
    assert _probe(_bridge(store), "adm1") is False
    assert store.reads == cancel_owner._STATE_READ_ATTEMPTS
    warned = [
        r
        for r in caplog.records
        if r.name == _ADMISSION_LOGGER and "could not read the state of adm1" in r.getMessage()
    ]
    assert len(warned) == 1


@pytest.mark.usefixtures("no_backoff")
def test_a_read_that_fails_once_answers_from_the_next_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    admitted = types.SimpleNamespace(state=taskq.ADMITTED)
    store = _UnreadableStore(failures=1, rec=admitted)
    caplog.set_level(logging.WARNING, logger=_ADMISSION_LOGGER)
    assert _probe(_bridge(store), "adm1") is True
    assert store.reads == 2
    assert [r for r in caplog.records if r.name == _ADMISSION_LOGGER] == []
