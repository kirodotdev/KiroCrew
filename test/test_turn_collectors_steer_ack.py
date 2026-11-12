"""Turn-text collectors drop kiro-cli's ``[STEERING steer-…]`` acknowledgement.

The host steers a refusal reason into a running turn and kiro-cli has the model
acknowledge it inline. That line is protocol, so a collector that hands the turn's
text to a person or a record must not keep it. Each test drives a collector's real
code with the marker split across chunks, as a provider delivers it.

Mutation: drop the ``strip_steering_markers`` call (or, for Slack, the stream
filter in ``_AnswerStream.on_text``) at a sink and its test goes red.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import MockSlackClient
from kiro_crew import channel as channel_mod
from kiro_crew import task_planner
from kiro_crew.dashboard.handlers import optimizer as optimizer_mod
from kiro_crew.eval import runner as runner_mod
from kiro_crew.eval.judge import LLMJudge
from kiro_crew.eval.runner import EvalRunner
from kiro_crew.eval.scenario import Turn
from kiro_crew.llm_helpers import run_bg_oneliner, stream_and_collect
from kiro_crew.messaging import auto_title
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.slack.handler import handle_message

# Braces inside: a sink that slices JSON out of the raw text breaks on it.
_ACK = "[STEERING steer-83cc0a1b: The notice named {the rule}, so I stopped.]"
# Split inside the marker: a collector that strips per chunk would miss it.
_CHUNKS = ["Done. ", _ACK[:14], _ACK[14:], " Pushed nothing."]
_CLEAN = "Done.  Pushed nothing."


def _events(chunks: list[str] = _CHUNKS) -> list[LLMEvent]:
    return [LLMEvent(kind=EVENT_TEXT_CHUNK, text=c) for c in chunks] + [
        LLMEvent(kind=EVENT_COMPLETE, text="")
    ]


def _with_ack(answer: str) -> list[str]:
    """*answer* preceded by the ack, the marker split across two chunks."""
    return [_ACK[:14], _ACK[14:], answer]


class _Scripted:
    def __init__(self, chunks: list[str] = _CHUNKS) -> None:
        self._events = _events(chunks)

    async def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def stream(self, message: str, **_kw) -> AsyncIterator[LLMEvent]:
        for event in self._events:
            yield event


@pytest.mark.asyncio
async def test_stream_and_collect_drops_the_ack():
    text = await stream_and_collect(_Scripted(), "q", retry_transient=False)  # type: ignore[arg-type]
    assert text == _CLEAN


@pytest.mark.asyncio
async def test_auto_title_drops_the_ack():
    text = await auto_title._stream_title(_Scripted(), "q", source="test")
    assert text == _CLEAN


def test_steering_ack_is_kept_off_the_slack_stream():
    handler_tests = importlib.import_module("test_slack_handler")

    class _StreamingSlack(MockSlackClient):
        def __init__(self) -> None:
            super().__init__()
            self._stream_enabled = True

    slack = _StreamingSlack()
    sessions = handler_tests.FakeSessionManager(
        handler_tests.FakeProvider([LLMEvent(kind="text_chunk", text=c) for c in _CHUNKS])
    )
    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    sent = "".join(str(payload.get("text", "")) for _name, payload in slack.actions)
    assert "STEERING" not in sent.upper()
    assert "Pushed nothing." in sent


@pytest.mark.asyncio
@pytest.mark.usefixtures("ample_host_free_memory")
async def test_background_one_liner_drops_the_ack():
    session = _Scripted()
    session.prompt = session.stream  # type: ignore[attr-defined]
    session.destroy = AsyncMock()  # type: ignore[attr-defined]
    session.last_prompt_stats = SimpleNamespace(credits=0.0)  # type: ignore[attr-defined]
    sessions = SimpleNamespace(get_bg_session=AsyncMock(return_value=session))
    assert await run_bg_oneliner(sessions, "p") == _CLEAN


@pytest.mark.asyncio
async def test_optimizer_drops_the_ack(monkeypatch):
    monkeypatch.setattr(optimizer_mod, "sel", MagicMock)
    client = _Scripted(_with_ack("rewritten"))
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state = MagicMock(sessions=sessions)
    request = MagicMock()
    request.json = AsyncMock(return_value={"prompt": "refactor the auth module", "context": ""})
    ready = SimpleNamespace(is_ready=lambda: True)
    request.app = {"state": state, "kiro_prerequisite_service": ready}
    resp = await optimizer_mod.handle_optimize(request)
    assert json.loads(resp.text or "")["optimized"] == "rewritten"


@pytest.mark.asyncio
async def test_task_decomposition_reads_past_the_ack():
    """No strip needed here: ``parse_tasks`` already reads the plan out of prose."""
    plan = json.dumps({"tasks": [{"title": "Write it", "description": "write it"}]})
    client = _Scripted(_with_ack(plan))
    sessions = SimpleNamespace(
        open_task_session=AsyncMock(return_value=(client, True, False)),
        release=MagicMock(),
        reset=AsyncMock(),
    )
    tasks = await task_planner.decompose("spec", sessions)  # type: ignore[arg-type]
    assert [t.description for t in tasks] == ["write it"]


@pytest.mark.asyncio
async def test_channel_agent_reply_drops_the_ack():
    agent = SimpleNamespace(
        id="a1",
        role="dev",
        agent_name="dev",
        session_key="channel:c1:a1",
        approval_policy=SimpleNamespace(value="writes"),
        _approval_future=None,
    )
    ch = SimpleNamespace(
        id="c1", trusted=False, members={}, _broadcast=MagicMock(), post=AsyncMock()
    )
    await channel_mod._stream_task(agent, ch, _Scripted(), "hi")
    assert ch.post.await_args.args[1] == _CLEAN


@pytest.mark.asyncio
async def test_eval_turn_response_drops_the_ack(monkeypatch):
    monkeypatch.setattr(runner_mod, "refusal_for", lambda event, **kw: None)
    provider = _Scripted()
    runner = EvalRunner(provider_factory=lambda key, **kw: provider)
    result = await runner._run_turn(provider, Turn(user="go"), "eval-session")
    assert result.agent_response == _CLEAN


@pytest.mark.asyncio
async def test_eval_judge_reads_past_the_ack():
    provider = _Scripted(_with_ack('{"score": 4, "reason": "ok"}'))
    judge = LLMJudge(provider_factory=lambda key, **kw: provider)
    await judge.start()
    verdict = await judge.judge_turn("desc", "criteria", "user", "assistant")
    assert verdict.score == 4
