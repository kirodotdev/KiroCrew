"""The two non-messaging ``compact()`` dispatches honour the backend capability.

A backend outside ``ACP_BACKENDS_COMPACT`` treats ``/compact`` as ordinary text
and never answers with a compaction status, so sending it buys nothing: the task
runner then waits out the whole compaction budget before resetting anyway, and
the CLI REPL restarts the provider regardless. Both sites must ask the one shared
predicate (``messaging.commands.compact_unsupported_backend``) and skip the call.

Each case has a supported-backend control, so the skip cannot pass vacuously.
"""

from __future__ import annotations

import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import task_executor
from kiro_crew.acp.types import TurnUsage
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    LLMEvent,
)
from kiro_crew.task_models import SESSION_PREFIX, Project, Task

_RUN_TASK_ID = "task-compact-gate"
_SESSION_KEY = f"{SESSION_PREFIX}:{_RUN_TASK_ID}:task1"


class _OverflowClient:
    """Turn 1 asks a permission the policy bails on (a context overflow); turn 2 ends."""

    def __init__(self, unsupported: str | None) -> None:
        self.manual_compact_unsupported_backend = unsupported
        self.turns = 0
        self.compact = AsyncMock()
        self.wait_for_compaction = AsyncMock(return_value={"type": "completed"})

    async def stream(self, _prompt: str):
        self.turns += 1
        if self.turns == 1:
            yield LLMEvent(kind=EVENT_PERMISSION_REQUEST, request_id="r1")
            return
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
        yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(duration_ms=1))


async def _bailing_settle(_ask, _policy):
    return SimpleNamespace(outcome="bailed", meta={"pct": 95.0})


async def _run_overflow(monkeypatch: pytest.MonkeyPatch, client: _OverflowClient) -> MagicMock:
    monkeypatch.setattr(task_executor, "check_context", AsyncMock())
    monkeypatch.setattr(task_executor, "build_task_prompt", AsyncMock(return_value="PROMPT"))
    monkeypatch.setattr(task_executor.tool_permission, "settle", _bailing_settle)
    monkeypatch.setattr(task_executor, "persist_token_record_async", AsyncMock(), raising=False)
    fake_config = MagicMock()
    fake_config.load.return_value = SimpleNamespace(agent=SimpleNamespace(provider="acp"))
    monkeypatch.setattr(task_executor, "KiroCrewConfig", fake_config)

    task = Task(index=1, title="t", description="d")
    run = Project(spec_path="spec.md", spec_content="body")
    run.task_id = _RUN_TASK_ID
    run.tasks = [task]
    run.branch_name = ""
    run.work_dir = ""
    sessions = MagicMock()
    sessions.open_task_session = AsyncMock(return_value=(client, True, False))
    sessions.compact_wait_budget_secs = MagicMock(return_value=300)
    sessions.reset = AsyncMock()
    sessions.record_failure = AsyncMock()
    await task_executor.execute_task(
        run, task, sessions, None, "agentX", None, False, None, "", AsyncMock(), _SESSION_KEY
    )
    assert client.turns == 2
    return sessions


@pytest.mark.asyncio
async def test_task_runner_skips_compact_on_a_backend_that_cannot_serve_it(monkeypatch):
    client = _OverflowClient(ACP_BACKEND_KAS)
    sessions = await _run_overflow(monkeypatch, client)
    client.compact.assert_not_awaited()
    client.wait_for_compaction.assert_not_awaited()
    sessions.reset.assert_awaited_once_with(_SESSION_KEY)


@pytest.mark.asyncio
async def test_task_runner_still_compacts_a_backend_that_can(monkeypatch):
    client = _OverflowClient(None)
    sessions = await _run_overflow(monkeypatch, client)
    client.compact.assert_awaited_once()
    sessions.reset.assert_not_awaited()


class _ReplProvider:
    def __init__(self, unsupported: str | None) -> None:
        self.manual_compact_unsupported_backend = unsupported
        self.compact = AsyncMock()
        self.shutdown = AsyncMock()
        self.start = AsyncMock()

    def context_usage_pct(self) -> float:
        return 99.0


async def _run_repl(monkeypatch: pytest.MonkeyPatch, provider: _ReplProvider) -> None:
    import kiro_crew.cli_chat as cli_chat

    lines = iter(["hello"])

    def fake_input(_prompt: str = "") -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(cli_chat, "_send_and_print", AsyncMock())
    cfg = types.SimpleNamespace(session=types.SimpleNamespace(autocompact_pct=70.0))
    await cli_chat._interactive(provider, cfg)
    # The restart still happens either way: it is what actually frees the context.
    provider.shutdown.assert_awaited_once()
    provider.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_cli_repl_skips_compact_on_a_backend_that_cannot_serve_it(monkeypatch):
    provider = _ReplProvider(ACP_BACKEND_KAS)
    await _run_repl(monkeypatch, provider)
    provider.compact.assert_not_awaited()


@pytest.mark.asyncio
async def test_cli_repl_still_compacts_a_backend_that_can(monkeypatch):
    provider = _ReplProvider(None)
    await _run_repl(monkeypatch, provider)
    provider.compact.assert_awaited_once()


def test_auto_compaction_asks_the_shared_predicate():
    """The automatic gate holds no private copy that could drift from the channels'."""
    from kiro_crew import session_compaction
    from kiro_crew.messaging import commands

    assert session_compaction._compact_unsupported_backend is commands.compact_unsupported_backend
