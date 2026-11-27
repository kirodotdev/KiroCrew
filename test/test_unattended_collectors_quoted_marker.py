"""Unattended turns whose text a person reads drop a quoted directive marker line.

A directive tool's result ends in a ``[[KIROCREW_SESSION_DIRECTIVE]]`` marker line the
model reads, so a reply that repeats the result carries that line too. It is protocol:
each test drives one collector's real code and reads the text it stores or delivers.
The marker is split across chunks where the collector reads the stream itself; where it
takes the whole reply from ``stream_and_collect``, that call answers with the text.

Mutation: drop the ``strip_quoted_markers`` call at a site (the cron collector, the
completion turn, ``agent_exec.step_text``, the task step, the sub-agent result, the
webhook turn, the app turn) and its test goes red.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import test_app_agent_turn_sdk as app_turns
import test_taskrunner_deny_notice as task_steps
import test_webhook_permission_requests as webhook_turns
from idle_tab_drain_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _idle_tab_drains_head_as_turn,
)
from subagent_stop_reason_helpers import _manager, _mock_sessions, _single_turn, _spawn_and_wait
from test_app_agent_turn_sdk import (  # noqa: F401 -- fixtures register by import
    _host_default,
    admitted,
)

import kiro_crew.subagent_persistence as sp
from kiro_crew import llm_helpers, session_directive
from kiro_crew.acp.types import STOP_REASON_END_TURN, AcpEvent, TurnUsage
from kiro_crew.hooks import TOOL_ALLOW, ToolHookResult
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.slack import gateway as gw
from kiro_crew.task_models import Task
from kiro_crew.workflows import agent_pool

_QUOTED = session_directive.encode("autonudge_stop", {"reason": "done"}, "Stop REQUESTED.")
# Split inside the marker; the reply's last line has no newline.
_CHUNKS = ["Result:\n", _QUOTED[:-9], _QUOTED[-9:] + "\n", "Done."]
_TEXT = "".join(_CHUNKS)
_CLEAN = "Result:\nStop REQUESTED.\nDone."


class _Scripted:
    async def stream(self, message: str, **_kw) -> AsyncIterator[LLMEvent]:
        for chunk in _CHUNKS:
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=chunk)
        yield LLMEvent(kind=EVENT_COMPLETE, text="")


@pytest.mark.asyncio
async def test_a_cron_runs_result_drops_the_quoted_marker():
    """The collector both cron agent paths stream through, so the stored result and
    every delivery of it carry no marker line."""
    refusals: list[str] = []
    text, carried = await gw._cron_stream_with_posttoken_resume(
        _Scripted(), "cron prompt", job_name="t1", directive_refusals=refusals
    )
    assert text == _CLEAN
    assert refusals == [] and carried is None


@pytest.mark.asyncio
async def test_the_parents_reply_to_a_sub_agent_report_drops_the_quoted_marker(monkeypatch):
    monkeypatch.setattr(gw, "stream_and_collect", AsyncMock(return_value=_TEXT))
    orch = SimpleNamespace(sessions=MagicMock())
    info = SimpleNamespace(id="sa-1")
    reply = await gw.GatewayOrchestrator._inject_with_retry(
        orch, info, object(), "report", "C1:ts.1", "slack"  # type: ignore[arg-type]
    )
    assert reply == _CLEAN


@pytest.mark.asyncio
async def test_a_workflow_steps_text_drops_the_quoted_marker():
    assert await agent_pool._run_step(_Scripted(), "go", key="wf:r1:0") == _CLEAN


class _QuotingTaskProvider(task_steps._Provider):
    async def stream(self, message: str):
        for chunk in _CHUNKS:
            yield LLMEvent(kind="text_chunk", text=chunk)
        yield LLMEvent(kind="complete")


@pytest.mark.asyncio
async def test_a_task_steps_result_drops_the_quoted_marker(tmp_path):
    task = Task(index=1, title="T", description="d")
    ctx = task_steps._ctx(ToolHookResult(action=TOOL_ALLOW))
    assert await task_steps._run_step(_QuotingTaskProvider(), ctx, tmp_path=tmp_path, task=task)
    assert task.result == _CLEAN


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")
async def test_the_sub_agent_result_the_parent_reads_drops_the_quoted_marker():
    stream, _ = _single_turn(STOP_REASON_END_TURN, chunks=tuple(_CHUNKS))
    info = await _spawn_and_wait(_manager(_mock_sessions(stream)))

    assert info.result == _CLEAN
    result_file = sp._agent_dir(info.id) / "result.txt"
    assert result_file.read_text(encoding="utf-8") == _CLEAN


class _QuotingWebhookClient(webhook_turns._FakeClient):
    async def stream(self, _message: str):
        for chunk in _CHUNKS:
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text=chunk)
        yield AcpEvent(kind=EVENT_COMPLETE, usage=TurnUsage(duration_ms=0, credits=1.0))


def test_a_webhook_turns_result_drops_the_quoted_marker(monkeypatch):
    *_, result_text = webhook_turns._drive(monkeypatch, None, client=_QuotingWebhookClient())
    assert result_text == _CLEAN


@pytest.mark.usefixtures("admitted")
def test_an_app_agent_turns_reply_drops_the_quoted_marker(monkeypatch):
    async def _stream(provider, message: str, **_kw) -> str:
        return _TEXT

    monkeypatch.setattr(llm_helpers, "stream_and_collect", _stream)
    sdk = app_turns._sdk(app_turns._Manager())
    reply = asyncio.run(asyncio.wait_for(sdk.run(f"{app_turns.APP}-q-1", "a line"), 30.0))
    assert reply == _CLEAN
