"""Turn wall-clock accounting for the task_runner dispatch surface (issue #647).

``task_executor`` persists a per-turn usage row at two sites — the main
execution turn in :func:`execute_task` and the separate model turn in
:func:`self_review`. The acp provider never assigns ``TurnUsage.duration_ms``
(only the removed claude_code provider did), so each site now measures its own
wall clock and passes it as the ``elapsed_ms`` fallback. ``_build_token_record``
records ``duration_ms = provider value when non-zero, ELSE elapsed_ms``.

These tests prove, per site:
  * the row records a non-zero duration sourced from the local clock when the
    provider is silent (the acp reality), and
  * a negative control — a provider-reported duration still WINS over the local
    clock — so the positive assertion cannot pass vacuously.

The REAL ``persist_token_record_async`` runs (so the precedence in
``_build_token_record`` is exercised for real); only the disk append
(``_write_token_record``) is intercepted, so no usage shard is written.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import task_executor
from kiro_crew.acp.types import TurnUsage
from kiro_crew.dashboard.handlers import usage
from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
from kiro_crew.task_models import Project, Task

# A turn long enough that ``int(elapsed_s * 1000)`` is unambiguously >= 1 on any
# host (asyncio.sleep is a lower bound), yet trivially short for the suite.
_TURN_SLEEP_S = 0.02
# A provider-reported duration the ~20 ms local clock can never coincide with,
# so "provider wins" is distinguishable from "local clock was used".
_PROVIDER_MS = 987654


def _complete_event(duration_ms: int) -> LLMEvent:
    """An EVENT_COMPLETE carrying a provider ``duration_ms`` (0 == silent)."""
    return LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(duration_ms=duration_ms))


def _intercept_usage(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Run the REAL persist path but capture the built record instead of writing.

    Patches the record's disk append and the client-reading helpers (which would
    otherwise need a live provider). Returns the list capturing each record.
    """
    captured: list[dict] = []
    monkeypatch.setattr(usage, "_write_token_record", lambda record, now: captured.append(record))
    monkeypatch.setattr(usage, "read_context_tokens", lambda *a, **k: (100, 1000))
    monkeypatch.setattr(usage, "read_effective_agent", lambda *a, **k: "agentX")
    monkeypatch.setattr(usage, "read_effective_model", lambda *a, **k: "test-model")
    fake_config = MagicMock()
    fake_config.load.return_value = SimpleNamespace(agent=SimpleNamespace(provider="acp"))
    monkeypatch.setattr(task_executor, "KiroCrewConfig", fake_config)
    return captured


def _make_run(task: Task) -> Project:
    run = Project(spec_path="spec.md", spec_content="body")
    run.task_id = "task-test"
    run.tasks = [task]
    run.branch_name = ""  # no git branch -> skip diff/commit/revert paths
    run.work_dir = ""
    return run


class _StreamClient:
    """Fake ACP client whose stream sleeps (a measurable turn) then completes."""

    def __init__(self, event: LLMEvent) -> None:
        self._event = event

    async def stream(self, _prompt: str):
        await asyncio.sleep(_TURN_SLEEP_S)
        yield self._event


def _make_sessions(client: object) -> MagicMock:
    sessions = MagicMock()
    sessions.open_task_session = AsyncMock(return_value=(client, True, False))
    sessions.record_success = MagicMock()
    sessions.check_context_usage = MagicMock()
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_failure = AsyncMock()
    return sessions


async def _run_execute_task(monkeypatch: pytest.MonkeyPatch, provider_ms: int) -> dict:
    """Drive one ``execute_task`` turn; return its single captured usage row."""
    captured = _intercept_usage(monkeypatch)
    monkeypatch.setattr(task_executor, "check_context", AsyncMock())
    monkeypatch.setattr(task_executor, "build_task_prompt", AsyncMock(return_value="PROMPT"))

    task = Task(index=1, title="do the thing", description="desc")
    run = _make_run(task)
    client = _StreamClient(_complete_event(provider_ms))
    sessions = _make_sessions(client)

    ok = await task_executor.execute_task(
        run,
        task,
        sessions,
        None,  # ctx -> no episodic embed
        "agentX",  # agent
        None,  # on_tool_approval
        False,  # auto_test -> skip run_tests
        None,  # test_cmd
        "",  # work_dir
        AsyncMock(),  # on_notify (unused on the happy path)
        "task-test:task1",
    )
    assert ok is True
    assert len(captured) == 1, "execute_task must persist exactly one row per turn"
    return captured[0]


async def _run_self_review(monkeypatch: pytest.MonkeyPatch, provider_ms: int) -> dict:
    """Drive one ``self_review`` turn; return its single captured usage row."""
    captured = _intercept_usage(monkeypatch)

    async def _review_stream(*_a, **_k):
        await asyncio.sleep(_TURN_SLEEP_S)
        return {"ok": True}

    monkeypatch.setattr(task_executor, "stream_and_collect_json", _review_stream)
    monkeypatch.setattr(
        task_executor,
        "provider_last_turn_usage",
        MagicMock(return_value=_complete_event(provider_ms)),
    )

    task = Task(index=1, title="do the thing", description="desc")
    run = _make_run(task)
    sessions = MagicMock()
    sessions.open_task_session = AsyncMock(return_value=(MagicMock(), True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()

    ok = await task_executor.self_review(run, task, sessions, "agentX", "task-test:task1")
    assert ok is True
    assert len(captured) == 1, "self_review must persist exactly one row per turn"
    return captured[0]


@pytest.mark.asyncio
async def test_execute_task_records_local_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # Provider silent (the acp reality) -> the row's duration is the local clock.
    record = await _run_execute_task(monkeypatch, provider_ms=0)
    assert record["surface"] == "task_runner"
    assert isinstance(record["duration_ms"], int)
    assert record["duration_ms"] >= 1, "execute_task turn must record a non-zero local duration"


@pytest.mark.asyncio
async def test_execute_task_provider_duration_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    # Negative control: a provider-reported duration WINS over the local clock,
    # so the positive test above cannot be passing vacuously.
    record = await _run_execute_task(monkeypatch, provider_ms=_PROVIDER_MS)
    assert record["duration_ms"] == _PROVIDER_MS


@pytest.mark.asyncio
async def test_self_review_records_local_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    record = await _run_self_review(monkeypatch, provider_ms=0)
    assert record["surface"] == "task_runner"
    assert isinstance(record["duration_ms"], int)
    assert record["duration_ms"] >= 1, "self_review turn must record a non-zero local duration"


@pytest.mark.asyncio
async def test_self_review_provider_duration_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    # Negative control for the review turn (see execute_task counterpart).
    record = await _run_self_review(monkeypatch, provider_ms=_PROVIDER_MS)
    assert record["duration_ms"] == _PROVIDER_MS
