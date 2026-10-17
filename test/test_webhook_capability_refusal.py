"""A webhook run that a crew member's capability check refuses names the cause.

When a member's agent file was edited outside the Capabilities page, the member
session start raises ``CapabilityError("materialization_changed")`` before any
agent runs. A dashboard chat names that and links to Capabilities; the webhook
run must say the same in the places an operator reads it -- the run-history row
and the delivered result -- instead of an unexplained internal failure. These
tests drive the real ``_run_hook_agent`` -> ``_run_hook_inner`` path with a
session factory that raises the refusal.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import webhooks
from kiro_crew.agent_capabilities import CapabilityError
from kiro_crew.dashboard.handlers import hooks as H
from kiro_crew.session_capabilities import CapabilityStartupError

_SESSION_KEY = f"{H._HOOK_SESSION_PREFIX}capability-refusal"


def _state(error: BaseException) -> MagicMock:
    state = MagicMock(memory_startup_task=None)
    state.context_builder = None
    state.sessions.get_or_create = AsyncMock(side_effect=error)
    state.sessions.record_success = MagicMock()
    state.sessions.record_failure = AsyncMock()
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.owner_id = None
    state.slack_client = None
    state.notify = MagicMock()
    return state


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(webhooks, "config_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(H, "_HOOK_STORE_PATH", Path(tmp_path) / "hooks.json")
    monkeypatch.setattr(H, "_sel", lambda: MagicMock())
    from kiro_crew.dashboard.handlers import usage

    monkeypatch.setattr(usage, "_write_token_record", lambda *_a, **_k: None)
    H._reset_hook_inflight()
    yield
    H._reset_hook_inflight()


async def _run(state: MagicMock, agent: str = "reviewer") -> dict:
    before = H._hook_semaphore._value
    await H._hook_semaphore.acquire()
    await H._run_hook_agent(state, _SESSION_KEY, "hi", "Bot", agent, True, 30)
    assert H._hook_semaphore._value == before, "capacity semaphore leaked a permit"
    return webhooks.run_store().list_runs()[0]


def _edited_file_error() -> CapabilityError:
    error = CapabilityError("materialization_changed")
    error.member = "reviewer"
    return error


@pytest.mark.asyncio
async def test_an_edited_agent_file_is_named_in_the_run_list(wired):
    state = _state(_edited_file_error())
    row = await _run(state)

    assert row["outcome"] == "error"
    assert "internal failure" not in row["detail"]
    assert "edited outside the Capabilities page" in row["detail"]
    assert "reviewer" in row["detail"]
    state.sessions.record_failure.assert_awaited_once_with(_SESSION_KEY)


@pytest.mark.asyncio
async def test_an_edited_agent_file_is_named_in_the_delivered_output(wired):
    state = _state(_edited_file_error())
    await _run(state)

    delivered = state.notify.call_args.args[2]
    assert "edited outside the Capabilities page" in delivered
    assert "Capabilities" in delivered


@pytest.mark.asyncio
async def test_a_startup_refusal_names_its_code_and_the_destination(wired):
    """A refusal that carries no member falls back to the hook's own agent."""
    row = await _run(_state(CapabilityStartupError("capability_state_unreadable")), "ops")

    assert row["outcome"] == "error"
    assert "capability_state_unreadable" in row["detail"]
    assert "ops" in row["detail"]
    assert "internal failure" not in row["detail"]


@pytest.mark.asyncio
async def test_a_raced_start_asks_for_a_resend_not_a_capabilities_fix(wired):
    """A start that raced a save is cleared by resending, so the text says
    that first and does not claim the member's spec was refused."""
    row = await _run(_state(CapabilityStartupError("capability_startup_raced")))

    assert "capability_startup_raced" in row["detail"]
    assert "could not be verified" in row["detail"]
    assert "Send the webhook again" in row["detail"]
    assert "refused" not in row["detail"]


@pytest.mark.asyncio
async def test_an_unknown_failure_still_reads_as_internal(wired):
    """Negative control: only capability refusals get the named result."""
    row = await _run(_state(RuntimeError("something else broke")))

    assert row["outcome"] == "error"
    assert "internal failure" in row["detail"]
