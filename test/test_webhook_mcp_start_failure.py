"""A webhook run names the MCP servers its session could not start.

A webhook turn runs in an ephemeral session, so a server that failed to start
there leaves the run quietly short of tools: the agent only sees its tools
missing, and nothing in the run list or the delivered output says why. These
tests drive the real ``_run_hook_agent`` -> ``_run_hook_inner`` path with a
session whose own MCP report records a failed server, and read the outcome
where an operator reads it: the run-history row and the delivered result.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import webhooks
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_TEXT_CHUNK,
    AcpEvent,
)
from kiro_crew.dashboard.handlers import hooks as H

_SESSION_KEY = f"{H._HOOK_SESSION_PREFIX}mcp-start"


class _FakeClient:
    def __init__(self, report: McpSessionReport, *, crash: bool = False) -> None:
        self._report = report
        self._crash = crash
        self._agent = "kirocrew"
        self._model = "test-model"

    def mcp_session_report(self) -> McpSessionReport:
        return self._report

    async def stream(self, _message: str):
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="the agent answer")
        if self._crash:
            raise RuntimeError("backend went away mid-turn")
        yield AcpEvent(kind=EVENT_COMPLETE)


def _state(client: _FakeClient) -> MagicMock:
    state = MagicMock()
    state.context_builder = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.record_success = MagicMock()
    state.sessions.record_failure = AsyncMock()
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.owner_id = None
    state.slack_client = None
    state.notify = MagicMock()
    return state


def _report(*, failed: bool) -> McpSessionReport:
    report = McpSessionReport()
    report.begin_session([])
    if failed:
        report.record_event(
            EVENT_MCP_SERVER_INIT_FAILURE,
            "azure-devops",
            "Cannot find module 'keytar.node'",
        )
    return report


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(webhooks, "config_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(H, "_HOOK_STORE_PATH", Path(tmp_path) / "hooks.json")
    monkeypatch.setattr(H, "_sel", lambda: MagicMock())
    # The usage row is not this test's subject; keep it off disk.
    from kiro_crew.dashboard.handlers import usage

    monkeypatch.setattr(usage, "_write_token_record", lambda *_a, **_k: None)
    H._reset_hook_inflight()
    yield
    H._reset_hook_inflight()


async def _run(state: MagicMock) -> dict:
    before = H._hook_semaphore._value
    await H._hook_semaphore.acquire()
    await H._run_hook_agent(state, _SESSION_KEY, "hi", "Bot", None, True, 30)
    assert H._hook_semaphore._value == before, "capacity semaphore leaked a permit"
    return webhooks.run_store().list_runs()[0]


@pytest.mark.asyncio
async def test_failed_mcp_server_is_named_in_the_run_list(wired):
    row = await _run(_state(_FakeClient(_report(failed=True))))

    assert row["outcome"] == "completed"
    assert "azure-devops" in row["detail"]
    assert "failed to start" in row["detail"]
    assert "keytar" in row["detail"]
    # The delivery fact is still there beside it.
    assert "Delivered to notifications" in row["detail"]


@pytest.mark.asyncio
async def test_failed_mcp_server_is_named_in_the_delivered_output(wired):
    state = _state(_FakeClient(_report(failed=True)))
    await _run(state)

    delivered = state.notify.call_args.args[2]
    assert delivered.startswith("the agent answer")
    assert "azure-devops" in delivered
    assert "failed to start" in delivered


@pytest.mark.asyncio
async def test_a_crashed_run_still_names_the_failed_server(wired):
    """The report is read in the runner's teardown, so a turn that never
    completes still says which server did not start."""
    row = await _run(_state(_FakeClient(_report(failed=True), crash=True)))

    assert row["outcome"] == "error"
    assert "azure-devops" in row["detail"]


@pytest.mark.asyncio
async def test_a_clean_session_adds_nothing(wired):
    """Negative control: no failure, no notice, unchanged detail and output."""
    state = _state(_FakeClient(_report(failed=False)))
    row = await _run(state)

    assert row["detail"] == "Delivered to notifications"
    assert state.notify.call_args.args[2] == "the agent answer"
