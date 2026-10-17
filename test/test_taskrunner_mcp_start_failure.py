"""A task-runner step names the MCP servers its session could not start.

A step runs headless, so a server that failed to start in its session leaves the
step quietly short of tools. These tests drive the real ``execute_task`` turn
with a session whose own MCP report records a failed server, and read the
outcome where an operator reads it: the run's dashboard notifications.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import task_executor
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.types import EVENT_MCP_SERVER_INIT_FAILURE
from kiro_crew.dashboard.handlers import usage
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.task_models import SESSION_PREFIX, Project, Task

_RUN_TASK_ID = "task-mcp-start"
_SESSION_KEY = f"{SESSION_PREFIX}:{_RUN_TASK_ID}:task1"


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


class _FakeClient:
    def __init__(self, report: McpSessionReport, *, crash: bool = False) -> None:
        self._report = report
        self._crash = crash

    def mcp_session_report(self) -> McpSessionReport:
        return self._report

    def reset_report(self) -> None:
        self._report = _report(failed=False)

    async def stream(self, _prompt: str):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
        if self._crash:
            raise RuntimeError("backend went away mid-turn")
        yield LLMEvent(kind=EVENT_COMPLETE)


@pytest.fixture()
def quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    # The usage row is not this test's subject; keep it off disk.
    monkeypatch.setattr(usage, "_write_token_record", lambda *_a, **_k: None)
    monkeypatch.setattr(usage, "read_context_tokens", lambda *a, **k: (100, 1000))
    monkeypatch.setattr(usage, "read_effective_agent", lambda *a, **k: "agentX")
    monkeypatch.setattr(usage, "read_effective_model", lambda *a, **k: "test-model")
    fake_config = MagicMock()
    fake_config.load.return_value = SimpleNamespace(agent=SimpleNamespace(provider="acp"))
    monkeypatch.setattr(task_executor, "KiroCrewConfig", fake_config)
    monkeypatch.setattr(task_executor, "check_context", AsyncMock())
    monkeypatch.setattr(task_executor, "build_task_prompt", AsyncMock(return_value="PROMPT"))


async def _run(client: _FakeClient) -> tuple[bool, list[tuple[str, str]]]:
    task = Task(index=1, title="do the thing", description="desc")
    run = Project(spec_path="spec.md", spec_content="body")
    run.task_id = _RUN_TASK_ID
    run.tasks = [task]
    run.branch_name = ""
    run.work_dir = ""
    sessions = MagicMock()
    sessions.open_task_session = AsyncMock(return_value=(client, True, False))
    sessions.record_success = MagicMock()
    sessions.check_context_usage = MagicMock()
    sessions.release = MagicMock()
    # A real reset shuts the provider down, which replaces its MCP report with
    # an empty one; the failure handlers reset before the attempt's teardown.
    sessions.reset = AsyncMock(side_effect=lambda _key: client.reset_report())
    sessions.record_failure = AsyncMock()
    on_notify = AsyncMock()

    ok = await task_executor.execute_task(
        run, task, sessions, None, "agentX", None, False, None, "", on_notify, _SESSION_KEY
    )
    return ok, [(c.args[0], c.args[1]) for c in on_notify.call_args_list]


def _mcp_notices(notices: list[tuple[str, str]]) -> list[str]:
    return [body for title, body in notices if "MCP servers unusable" in title]


@pytest.mark.asyncio
async def test_failed_mcp_server_is_named_to_the_operator(quiet) -> None:
    ok, notices = await _run(_FakeClient(_report(failed=True)))

    assert ok is True
    [body] = _mcp_notices(notices)
    assert "azure-devops" in body
    assert "failed to start" in body
    assert "keytar" in body


@pytest.mark.asyncio
async def test_a_failing_step_names_the_server_once_across_retries(quiet) -> None:
    """A turn that never completes still says which server did not start, even
    though its failure handler reset the session before teardown, and a retry
    does not repeat the notice."""
    ok, notices = await _run(_FakeClient(_report(failed=True), crash=True))

    assert ok is False
    [body] = _mcp_notices(notices)
    assert "azure-devops" in body


@pytest.mark.asyncio
async def test_a_clean_session_adds_no_notice(quiet) -> None:
    """Negative control: no failure, no MCP notice."""
    ok, notices = await _run(_FakeClient(_report(failed=False)))

    assert ok is True
    assert _mcp_notices(notices) == []
