"""A crewmate's scheduled turn reports to its thread like one of its workers.

When a crewmate-bound cron agent turn ends with a CHANGED result, the crewmate's
pinned thread is woken with the ``[Worker report]`` hidden turn
(``mate_thread_wakes.note_cron_result``), on the job's attested member binding and
its own ``cron:`` key. An identical result (the cron's ``last_posted_hash``
dedupe) wakes nobody, and a cron that is no crewmate's never does.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew import mate_worker
from kiro_crew import members as members_mod
from kiro_crew.cron import CronJob
from kiro_crew.dashboard import mate_thread_wakes as wakes
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

CAPTURED = ExecutionContext("alpha", MemoryStoreRef("member-alpha", "alpha"), "member", "kirocrew")


class _State:
    """The dashboard state the wake reads: one idle crewmate thread."""

    def __init__(self):
        self.thread = SimpleNamespace(
            key="member-alpha",
            agent="alpha",
            mode=members_mod.DM_SLOT_MODE,
            executor="",
            running=False,
            _queue=[],
            task=None,
        )
        self._slots = {self.thread.key: self.thread}
        self.dashboard_state = self

    def get_slot(self, key):
        return self._slots.get(key)


@pytest.fixture
def woken(monkeypatch):
    """Every hidden turn the flush starts, as ``(thread key, prompt)``."""
    turns: list[tuple[str, str]] = []
    from kiro_crew.dashboard import mate_welcome

    monkeypatch.setattr(mate_welcome, "app_is_stopping", lambda state: False)
    monkeypatch.setattr(
        mate_welcome,
        "dispatch_hidden_turn",
        lambda state, slot, prompt, human_seen: turns.append((slot.key, prompt)),
    )
    # The Dashboard line reads the page off the loop; these tests pin the wake alone.
    monkeypatch.setattr(wakes, "page_line", lambda slug: "", raising=False)
    monkeypatch.setattr(
        members_mod,
        "read_dm_binding",
        lambda slug: {"slot_key": "member-alpha", "member": "alpha"} if slug == "alpha" else None,
    )
    return turns


def _job(**over) -> CronJob:
    fields = {
        "id": "hourly",
        "name": "slack digest",
        "message": "read slack",
        "member_id": "alpha",
        "execution_context": CAPTURED.to_record(),
    }
    fields.update(over)
    return CronJob(**fields)


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)
    # The flush reads the Dashboard page off the loop, so wait for it rather than a
    # fixed number of loop turns.
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if others:
        await asyncio.wait(others, timeout=5)


async def _deliver(state, job, result, rh):
    """The cron's own delivery, cut short after the dedupe by ``silent``."""
    from kiro_crew.slack import gateway

    gw = gateway.GatewayOrchestrator.__new__(gateway.GatewayOrchestrator)
    gw.dashboard_state = state
    state.has_slot = lambda key: False
    state.notify = MagicMock()
    return await gateway.GatewayOrchestrator._deliver_cron_agent_result(
        gw, job, f"cron:{job.id}", MagicMock(), result, rh, None, None
    )


@pytest.mark.asyncio
async def test_a_changed_crewmate_cron_result_wakes_the_conductor_once(monkeypatch, woken):
    monkeypatch.setattr(mate_worker, "cron_worker_execution", lambda execution, config=None: True)
    state = _State()
    job = _job(silent=True, last_posted_hash="old")

    await _deliver(state, job, "3 new threads in #oncall", "new")
    await _settle()

    assert len(woken) == 1
    key, prompt = woken[0]
    assert key == "member-alpha"
    assert prompt.startswith(wakes.WORKER_REPORT_PREFIX)
    assert "cron:hourly" in prompt and "3 new threads in #oncall" in prompt


@pytest.mark.asyncio
async def test_a_job_the_crewmates_worker_created_reports_too(monkeypatch, woken):
    """``bind_cron_memory`` captures the worker's own record for a job it creates."""
    monkeypatch.setattr(mate_worker, "cron_worker_execution", lambda execution, config=None: None)
    worker = CAPTURED.with_template(mate_worker.MATE_WORKER_AGENT, mate_worker.MATE_WORKER_AGENT)
    job = _job(execution_context=worker.to_record(), silent=True, last_posted_hash="old")

    await _deliver(_State(), job, "done", "new")
    await _settle()

    assert [key for key, _ in woken] == ["member-alpha"]


@pytest.mark.asyncio
async def test_an_identical_crewmate_cron_result_wakes_nobody(monkeypatch, woken):
    monkeypatch.setattr(mate_worker, "cron_worker_execution", lambda execution, config=None: True)
    state = _State()
    job = _job(last_posted_hash="same", last_posted_at=time.time())

    await _deliver(state, job, "nothing new", "same")
    await _settle()

    assert woken == []
    assert job.consecutive_dupes == 1


@pytest.mark.asyncio
async def test_a_cron_that_is_no_crewmates_never_wakes_a_thread(monkeypatch, woken):
    state = _State()
    template = ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")
    plain = _job(member_id="", execution_context=template.to_record(), silent=True)
    await _deliver(state, plain, "changed", "new")
    # A crewmate still on a shared template ran no worker turn either.
    monkeypatch.setattr(mate_worker, "cron_worker_execution", lambda execution, config=None: None)
    await _deliver(state, _job(silent=True), "changed", "new")
    await _settle()

    assert woken == []


@pytest.mark.asyncio
async def test_a_key_the_job_did_not_mint_is_not_its_report(monkeypatch, woken):
    monkeypatch.setattr(mate_worker, "cron_worker_execution", lambda execution, config=None: True)
    state = _State()

    assert not await wakes.note_cron_result(state, _job(), "chat-1-2", "forged")
    assert not await wakes.note_cron_result(state, _job(member_id="beta"), "cron:hourly", "x")
    await _settle()
    assert woken == []
