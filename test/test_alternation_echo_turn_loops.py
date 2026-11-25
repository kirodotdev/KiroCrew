"""Turn loops that read the stream's text themselves drop the alternation placeholder.

The backend fills a history that ends on a tool result with a placeholder assistant
message, and the model repeats it as the first text of a later turn. A team-channel
agent's post, ``kirocrew chat``'s printed reply, a task-runner step's result, a
sub-agent's result, an eval scenario turn's response and a webhook hook turn's result
stream each prompt through ``alternation_echo.drop_echo``, so none of them carries it.
So does the native Slack turn, and the channel turn driver streams each timed-out call's
continuation through it: both hold only a turn's first text themselves, so a
continuation that opens with it after earlier text drops it too. Each loop test drives
the loop's real code, with its provider and the session plumbing around it scripted.

Mutation: hand one loop its prompt stream unwrapped and that loop's case goes red.
"""

from __future__ import annotations

import contextlib
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from idle_tab_drain_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _idle_tab_drains_head_as_turn,
)
from slack_handler_helpers import FakeProvider, FakeSessionManager
from subagent_stop_reason_helpers import _manager, _mock_sessions, _spawn_and_wait

from conftest import MockSlackClient
from kiro_crew import channel as channel_mod
from kiro_crew import cli_chat, task_executor
from kiro_crew.acp.types import STOP_REASON_TOOL_STALL
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers import hooks as hooks_handlers
from kiro_crew.eval.runner import EvalRunner
from kiro_crew.eval.scenario import Turn
from kiro_crew.hooks import HookManager
from kiro_crew.messaging import TransportCapabilities, TurnDriver
from kiro_crew.messaging.alternation_echo import OPENER
from kiro_crew.messaging.renderer import SilentRenderer
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    LLMEvent,
)
from kiro_crew.slack.handler import handle_message
from kiro_crew.task_models import Project, Task

pytestmark = pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")

PLACEHOLDER = (
    "(No assistant response was recorded for the preceding turn. This "
    "placeholder was inserted automatically to preserve message "
    "alternation \u2014 it is not a reply the assistant produced.)"
)
#: What ``_end_stalled_tool`` writes when a built-in's silence timer runs out.
_SILENCE = (
    "verdict=unknown; idle_secs=301; processes_terminated=0; "
    "silence_timer=builtin: no frame for this call in 301s (bound 300s)"
)


def _text(text: str) -> LLMEvent:
    return LLMEvent(kind=EVENT_TEXT_CHUNK, text=text)


_TOOL = LLMEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-1", title="Read a file")
_END = LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")
#: A prompt that ends because the call's own silence timer ran out.
_STALLED = LLMEvent(
    kind=EVENT_COMPLETE, stop_reason=STOP_REASON_TOOL_STALL, title="Read a file", text=_SILENCE
)
#: The field case: the placeholder, token by token, then the model's own words.
_ECHO_THEN_WORDS = [
    _text(PLACEHOLDER[:30]),
    _text(PLACEHOLDER[30:]),
    _text("\n\nHere it is."),
    _END,
]


class _Scripted:
    """A provider that answers each prompt with the next script, the last repeating."""

    def __init__(self, *scripts: list[LLMEvent]) -> None:
        self._scripts = list(scripts)
        self.prompts: list[str] = []

    async def stream(self, message: str, **_kw):
        self.prompts.append(message)
        for event in self._scripts[min(len(self.prompts), len(self._scripts)) - 1]:
            yield event


async def _events(events: list[LLMEvent]):
    for event in events:
        yield event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "expected"),
    [
        (_ECHO_THEN_WORDS, ["Here it is.", "complete"]),
        (
            [_text(PLACEHOLDER[:30]), _text(PLACEHOLDER[30:]), _TOOL, _text("Here it is."), _END],
            ["tool_call", "Here it is.", "complete"],
        ),
        ([_text(PLACEHOLDER), _END], ["complete"]),
        (
            [_text("(No assistant"), _text(" was needed here.) Done."), _END],
            ["(No assistant", " was needed here.) Done.", "complete"],
        ),
        ([_text("(No assistant"), _TOOL, _END], ["(No assistant", "tool_call", "complete"]),
        ([_text("(No assistant")], ["(No assistant"]),
        (
            [_text("Done. "), _TOOL, _text(PLACEHOLDER), _END],
            ["Done. ", "tool_call", PLACEHOLDER, "complete"],
        ),
        (
            [
                _text(PLACEHOLDER[:30]),
                LLMEvent(kind=EVENT_THINKING_CHUNK, text="hm"),
                _text(PLACEHOLDER[30:]),
                _END,
            ],
            ["thinking_chunk", "complete"],
        ),
        ([_text(PLACEHOLDER), _TOOL, _text(PLACEHOLDER), _END], ["tool_call", "complete"]),
    ],
    ids=[
        "words-after",
        "then-tool",
        "only",
        "opens-like-it",
        "prefix-held-to-a-tool-call",
        "prefix-held-to-the-end",
        "after-the-reply-started",
        "thinking-passes-a-hold",
        "held-again-after-a-dropped-one",
    ],
)
async def test_a_prompt_loses_only_the_placeholder_its_text_opens_with(events, expected):
    # Imported here so the loop tests below still collect, and fail, without it.
    from kiro_crew.messaging.alternation_echo import drop_echo

    out = [e async for e in drop_echo(_events(events))]
    assert [e.text if e.kind == EVENT_TEXT_CHUNK else e.kind for e in out] == expected


async def _channel_post(provider: _Scripted, _tmp_path) -> str:
    ch = channel_mod.Channel(id="c1", topic="t")
    agent = ch.add_agent("dev", agent_name="dev")
    assert agent is not None
    await channel_mod._stream_task(agent, ch, provider, "hi")
    return "".join(m.content for m in ch.messages if m.from_id == agent.id)


async def _cli_reply(provider: _Scripted, _tmp_path) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        await cli_chat._send_and_print(provider, "q")
    return out.getvalue()


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


async def _subagent_result(provider: _Scripted, _tmp_path) -> str:
    info = await _spawn_and_wait(_manager(_mock_sessions(provider.stream)))
    assert info.outcome == "completed"
    return info.result


async def _eval_response(provider: _Scripted, _tmp_path) -> str:
    runner = EvalRunner(provider_factory=lambda key, **kw: provider)
    return (await runner._run_turn(provider, Turn(user="go"), "eval-s")).agent_response


async def _hook_result(provider: _Scripted, _tmp_path) -> str:
    # ``is_new=False`` skips the context build; the stream loop is the one a resumed turn
    # on a reused ``sessionKey`` runs.
    state = MagicMock()
    state.sessions.get_or_create = AsyncMock(return_value=(provider, False, False))
    with patch("kiro_crew.context.session_store_for_turn", AsyncMock(return_value="")):
        return await hooks_handlers._run_hook_inner(state, "hook:echo", "hi", None)


_LOOPS = {
    "channel-post": _channel_post,
    "cli-chat": _cli_reply,
    "task-step": _step_result,
    "subagent": _subagent_result,
    "eval-turn": _eval_response,
    "hook-turn": _hook_result,
}


async def _driver_reply(provider: _Scripted, _tmp_path) -> str:
    return await TurnDriver(provider, SilentRenderer(TransportCapabilities())).run("hi")


class _SlackProvider(FakeProvider):
    """The Slack fake's session surface, answering each prompt from *scripted*."""

    def __init__(self, scripted: _Scripted) -> None:
        super().__init__()
        self._scripted = scripted

    def stream(self, message, timeout=120.0):
        return self._scripted.stream(message)


async def _slack_sent(provider: _Scripted, _tmp_path) -> str:
    slack = MockSlackClient()
    sessions = FakeSessionManager(_SlackProvider(provider))
    await handle_message(slack, sessions, "C1", "q?", None, "m1", "U1")
    return repr(slack.actions)


#: The loops that hold a turn's first text themselves, and everything each sent.
_FIRST_TEXT_HOLDERS = {"channel-driver": _driver_reply, "slack-native": _slack_sent}


@pytest.mark.asyncio
@pytest.mark.parametrize("loop", list(_LOOPS))
async def test_a_turn_that_opens_with_the_placeholder_delivers_only_the_words_after_it(
    loop, tmp_path
):
    delivered = await _LOOPS[loop](_Scripted(_ECHO_THEN_WORDS), tmp_path)
    assert delivered.strip() == "Here it is."


@pytest.mark.asyncio
async def test_a_timed_out_call_s_continuation_drops_the_placeholder_it_opens_with(tmp_path):
    """The continuation is a new prompt on a session whose history ends on the call."""
    provider = _Scripted([_text("Checking. "), _TOOL, _STALLED], _ECHO_THEN_WORDS)
    assert (await _channel_post(provider, tmp_path)).strip() == "Checking. Here it is."
    assert len(provider.prompts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("loop", list(_FIRST_TEXT_HOLDERS))
async def test_a_continuation_after_the_turn_s_first_text_drops_the_placeholder_too(loop, tmp_path):
    provider = _Scripted([_text("Checking. "), _TOOL, _STALLED], _ECHO_THEN_WORDS)
    sent = await _FIRST_TEXT_HOLDERS[loop](provider, tmp_path)
    assert len(provider.prompts) == 2
    assert "Checking." in sent and "Here it is." in sent
    assert OPENER not in sent
