"""Turn loops that collect a turn's text drop a refused model's text on a refusal hop.

On a V2 content-filter refusal kiro-cli streams the refused model's text, then a
``model_fallback`` hop (``cause: refused``), then the fallback model's answer, then a
promotion notice and ``end_turn`` (``test/fixtures/acp_frames/kiro/refusal-fallback-live.jsonl``).
``stream_and_collect`` (cron, heartbeat), the background one-liner, a team-channel
agent's post, a webhook hook turn, a task-runner step and the task-runner refine turn
fold every event through ``RefusalHopText``, so what each hands over is the answer
alone. When nothing answers after the hop, the refused text is kept. Each loop test
drives the loop's real code, with its provider and the session plumbing scripted.

Mutation: drop one loop's ``fold`` call and that loop's case goes red.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import channel as channel_mod
from kiro_crew import llm_helpers, task_executor
from kiro_crew.acp.client import AcpError
from kiro_crew.acp.engine_fallback import ModelFallbackNotice
from kiro_crew.acp.types import STOP_REASON_REFUSAL
from kiro_crew.agent_sdk.drivers.acp_vocab import RefusalHopText
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers import hooks as hooks_handlers
from kiro_crew.dashboard.handlers.taskrunner import _run_refine
from kiro_crew.hooks import HookManager
from kiro_crew.llm_helpers import run_bg_oneliner, stream_and_collect
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_MODEL_FALLBACK,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    LLMEvent,
)
from kiro_crew.task_models import Project, Task

pytestmark = pytest.mark.usefixtures("healthy_host_memory", "ample_host_free_memory")

_REFUSED = "I can't help with that."
_ANSWER = "Here is the answer."


def _text(text: str) -> LLMEvent:
    return LLMEvent(kind=EVENT_TEXT_CHUNK, text=text)


def _hop(*, promoted: bool = False, cause: str = "refused") -> LLMEvent:
    notice = ModelFallbackNotice("model-a", "model-b", cause, promoted=promoted)  # type: ignore[arg-type]
    return LLMEvent(kind=EVENT_MODEL_FALLBACK, model_fallback=notice)


def _end(stop_reason: str = "end_turn") -> LLMEvent:
    return LLMEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason)


_TOOL = LLMEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-1", title="Read a file")
#: The captured wire order of a refusal the fallback recovered.
_RECOVERED = [_text(_REFUSED), _hop(), _text(_ANSWER), _hop(promoted=True), _end()]


class _Scripted:
    """A provider that answers each prompt with *events*."""

    def __init__(self, events: list, *, then_raise: BaseException | None = None) -> None:
        self._events = events
        self._then_raise = then_raise
        self.prompts: list[str] = []

    async def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def stream(self, message: str, **_kw):
        self.prompts.append(message)
        for event in self._events:
            yield event
        if self._then_raise is not None:
            raise self._then_raise


async def _collected(provider: _Scripted) -> str:
    return await stream_and_collect(provider, "q")  # type: ignore[arg-type]


async def _one_liner(provider: _Scripted) -> str:
    provider.prompt = provider.stream  # type: ignore[attr-defined]
    provider.destroy = AsyncMock()  # type: ignore[attr-defined]
    provider.last_prompt_stats = SimpleNamespace(credits=0.0)  # type: ignore[attr-defined]
    return await run_bg_oneliner(
        SimpleNamespace(get_bg_session=AsyncMock(return_value=provider)), "p"
    )


async def _channel_post(provider: _Scripted) -> str:
    ch = channel_mod.Channel(id="c1", topic="t")
    agent = ch.add_agent("dev", agent_name="dev")
    assert agent is not None
    await channel_mod._stream_task(agent, ch, provider, "hi")
    return "".join(m.content for m in ch.messages if m.from_id == agent.id)


async def _hook_result(provider: _Scripted) -> str:
    # ``is_new=False`` skips the context build; the stream loop is the one a turn runs.
    state = MagicMock()
    state.sessions.get_or_create = AsyncMock(return_value=(provider, False, False))
    with patch("kiro_crew.context.session_store_for_turn", AsyncMock(return_value="")):
        return await hooks_handlers._run_hook_inner(state, "hook:echo", "hi", None)


async def _step_result(provider: _Scripted, tmp_path) -> str:
    sessions = MagicMock()
    sessions.open_task_session = AsyncMock(return_value=(provider, True, False))
    sessions.release_subagent_runtime = AsyncMock()
    sessions.reset = AsyncMock()
    ctx = MagicMock(spec=ContextBuilder)
    ctx.hooks = MagicMock(spec=HookManager)
    ctx.build_message = MagicMock(return_value=("prompt", None))
    run = Project(spec_path="t.md", spec_content="s", status="running", task_id="tid")
    task = Task(index=1, title="T", description="d")
    run.tasks = [task]
    with patch.object(task_executor.KiroCrewConfig, "load") as cfg:
        cfg.return_value.agent.provider = "acp"
        assert await task_executor.execute_task(
            run=run,
            task=task,
            sessions=sessions,
            ctx=ctx,
            agent="",
            on_tool_approval=None,
            auto_test=False,
            test_cmd=None,
            work_dir=tmp_path,
            on_notify=AsyncMock(),
            session_key="k",
        )
    return task.result


async def _refined(provider: _Scripted) -> str:
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.reset = AsyncMock()
    state = SimpleNamespace(
        sessions=sessions,
        _refine_task=None,
        _refine_text="",
        _refine_error="",
        _refine_status="idle",
        _refine_session_key="",
        _refine_answer_future=None,
        broadcast_ws=MagicMock(),
        push_refresh=MagicMock(),
    )
    await _run_refine(state, "build a thing")
    assert state._refine_status == "done"
    return state.broadcast_ws.call_args.args[1]["text"]


_LOOPS = {
    "stream-and-collect": lambda provider, _tmp: _collected(provider),
    "background-one-liner": lambda provider, _tmp: _one_liner(provider),
    "channel-post": lambda provider, _tmp: _channel_post(provider),
    "hook-turn": lambda provider, _tmp: _hook_result(provider),
    "task-step": _step_result,
    "refine-turn": lambda provider, _tmp: _refined(provider),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("loop", list(_LOOPS))
async def test_a_recovered_refusal_delivers_the_fallback_answer_alone(loop, tmp_path):
    delivered = await _LOOPS[loop](_Scripted(_RECOVERED), tmp_path)
    assert delivered.strip() == _ANSWER


@pytest.mark.asyncio
async def test_a_refusal_nothing_answered_keeps_the_providers_words():
    provider = _Scripted([_text(_REFUSED), _hop(), _end(STOP_REASON_REFUSAL)])
    assert await _collected(provider) == _REFUSED


def _rejected_model_error() -> AcpError:
    error = AcpError("model rejected", transient=False)
    error.rejected_model = "model-a"
    error.advertised = ["model-b"]
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "kwargs"),
    [
        (AcpError("ServiceUnavailableException", transient=True), {}),
        (_rejected_model_error(), {"model_fallback": True}),
    ],
    ids=["transient", "rejected-model"],
)
async def test_an_error_after_the_hop_does_not_replay_the_prompt(error, kwargs, monkeypatch):
    # The refused text still streamed: a retry that replays only an attempt with no
    # text would send the prompt a second time.
    monkeypatch.setattr(llm_helpers, "transient_retry_delay", lambda _attempt: 0.0)
    provider = _Scripted([_text(_REFUSED), _hop()], then_raise=error)
    with pytest.raises(AcpError):
        await stream_and_collect(provider, "q", **kwargs)  # type: ignore[arg-type]
    assert len(provider.prompts) == 1


def _folded(events: list) -> str:
    refusal, text = RefusalHopText(), ""
    for event in events:
        text = refusal.fold(event, text)
        if event.kind == EVENT_TEXT_CHUNK:
            text += event.text
    return text


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        # Text before the last tool call is an earlier model call's.
        (
            [_text("Checking. "), _TOOL, _text(_REFUSED), _hop(), _text(_ANSWER), _end()],
            "Checking. " + _ANSWER,
        ),
        # The fallback answered, then its next call was refused bare.
        (
            [_text(_REFUSED), _hop(), _text("Let me look. "), _TOOL, _end(STOP_REASON_REFUSAL)],
            "Let me look. ",
        ),
        # A second hop with no new text keeps the first refusal's words.
        ([_text(_REFUSED), _hop(), _hop(), _end(STOP_REASON_REFUSAL)], _REFUSED),
        # A promotion and a capacity hop move nothing visible.
        (
            [
                _text("Partial "),
                _hop(promoted=True),
                _hop(cause="unavailable"),
                _text("rest"),
                _end(),
            ],
            "Partial rest",
        ),
    ],
    ids=["after-a-tool-call", "answered-then-refused", "second-hop", "not-a-refusal"],
)
def test_the_cut_takes_only_the_refused_model_s_text(events, expected):
    assert _folded(events) == expected
