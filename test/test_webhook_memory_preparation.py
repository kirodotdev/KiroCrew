"""A webhook turn accepted before memory preparation finishes waits for it.

The gateway closes the memory barrier at boot and opens it once the startup
preparation task (restore, then init) completes. A dashboard chat turn waits
for that task with ``wait_for_memory_preparation`` before it reads memory. The
hook path runs the real ``_run_hook_agent`` here, with the inner turn replaced
by one that takes the real first memory step of a turn, ``session_store_for_turn``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew import memory_startup
from kiro_crew.memory_startup import MemoryStartup

SESSION_KEY = "hook:default:1791000000"


@pytest.fixture(autouse=True)
def _pin_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.webhooks.config_dir", lambda: tmp_path)


@pytest.fixture
def closed_barrier():
    startup = MemoryStartup.begin()  # what the gateway does at boot
    try:
        yield startup
    finally:
        startup.stop()
        startup.release()


def _hook_state(tmp_path, monkeypatch) -> tuple[object, list[str]]:
    from kiro_crew import context
    from kiro_crew.dashboard.handlers import hooks

    state = _make_state(tmp_path)
    state.sessions.record_failure = AsyncMock()
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.notify = MagicMock()
    state.slack_client = None
    sent_to_model: list[str] = []

    async def inner(_state, session_key, message, *_a, **_k):
        await context.session_store_for_turn(SimpleNamespace(conversation_log=None), session_key)
        sent_to_model.append(message)
        return "done"

    monkeypatch.setattr(hooks, "_run_hook_inner", inner)
    return state, sent_to_model


def _notified(state) -> str:
    return " ".join(str(call) for call in state.notify.call_args_list)


async def _run(state) -> None:
    """Run the hook the way the route handler hands it to the runner.

    The handler claims the session key and takes a permit from the process-wide
    semaphore; the runner's ``finally`` gives both back. Calling the runner
    without taking them would release a permit that was never taken, and the
    semaphore is unbounded, so later tests that fill it to get a 429 would
    find spare permits.
    """
    from kiro_crew.dashboard.handlers import hooks

    hooks._hook_inflight_sessions.add(SESSION_KEY)
    await hooks._hook_semaphore.acquire()
    await asyncio.wait_for(
        hooks._run_hook_agent(state, SESSION_KEY, "deploy finished", "ci", None, True, 300), 20
    )


@pytest.fixture(autouse=True)
def _hook_capacity_is_balanced():
    from kiro_crew.dashboard.handlers import hooks

    permits_before = hooks._hook_semaphore._value
    yield
    assert hooks._hook_semaphore._value == permits_before
    assert SESSION_KEY not in hooks._hook_inflight_sessions


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_hook_accepted_before_memory_is_ready_runs_once_it_is(
    tmp_path, monkeypatch, closed_barrier
):
    state, sent_to_model = _hook_state(tmp_path, monkeypatch)

    async def prepare() -> None:
        await asyncio.sleep(0.2)
        assert closed_barrier.complete()

    state.memory_startup_task = asyncio.create_task(prepare())
    await _run(state)

    assert sent_to_model == ["deploy finished"]
    assert "internal failure" not in _notified(state)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_hook_that_outwaits_memory_preparation_fails_naming_it(
    tmp_path, monkeypatch, closed_barrier
):
    monkeypatch.setattr(memory_startup, "MEMORY_ADMISSION_WAIT_SECONDS", 0.2)
    state, sent_to_model = _hook_state(tmp_path, monkeypatch)
    never = asyncio.Event()
    state.memory_startup_task = asyncio.create_task(never.wait())
    try:
        await _run(state)
    finally:
        state.memory_startup_task.cancel()

    assert sent_to_model == []
    notified = _notified(state)
    assert "internal failure" not in notified
    assert "memory" in notified.lower()


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_hook_after_readiness_does_not_wait(tmp_path, monkeypatch, closed_barrier):
    assert closed_barrier.complete()
    state, sent_to_model = _hook_state(tmp_path, monkeypatch)
    state.memory_startup_task = None
    await _run(state)

    assert sent_to_model == ["deploy finished"]
