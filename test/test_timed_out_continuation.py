"""A call whose own silence timer ended the turn is handed back to the model.

The session handle cancels the turn when a call's silence timer runs out, since ACP
has no per-call cancel, and ends it as a tool stall with ``silence_timer=`` evidence.
The model has not yet been told. The dashboard and a sub-agent run queue the timeout
continuation as their tool-stall recovery; a surface that only ended on the stall
would leave a channel reply, a cron run or a task-runner step with the model never
told the call timed out and nothing retrying it. Each test here drives one of
the surfaces that hand it back (a messaging channel turn, the native Slack turn,
an agent channel member's turn, the CLI chat, the task runner and
``stream_and_collect``) through its real turn loop, with a provider whose first
prompt ends on such a stall, and checks that the same session is sent the
continuation and that the turn ends on the model's answer to it, with an oracle
stall and the budget left as they were. The last two drive the dashboard chat's
``_turn_stop_recovery`` and a sub-agent run's ``_yield_for_stop_recovery_impl``,
which queue the same wording as their recovery prompt for a silence expiry and
the usual stall nudge for an oracle verdict.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from slack_handler_helpers import _clean_approval_state  # noqa: F401
from slack_handler_helpers import FakeProvider, FakeSessionManager

from conftest import MockSlackClient
from kiro_crew.acp.client import AcpTimeoutError
from kiro_crew.acp.timed_out_continuation import continue_timed_out_calls, timed_out_continuation
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    STOP_REASON_END_TURN,
    STOP_REASON_TOOL_STALL,
    STOP_RECOVERY_MAX_RETRIES,
    AcpEvent,
    AcpPromptStats,
    TurnUsage,
)
from kiro_crew.llm_helpers import provider_last_turn_usage, stream_and_collect
from kiro_crew.messaging import TransportCapabilities, TurnDriver
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.slack.handler import handle_message
from kiro_crew.timed_out_call import TOOL_STALL_RECOVERY_PREFIX, build_timed_out_continuation

_TITLE = "Editing notes.md"
#: What ``_end_stalled_tool`` writes when a built-in's silence timer runs out.
_SILENCE = (
    "verdict=unknown; idle_secs=301; processes_terminated=0; "
    "silence_timer=builtin: no frame for this call in 301s (bound 300s)"
)
#: An oracle verdict: a shell child that exited without a result frame.
_ORACLE = "verdict=dead; idle_secs=40; processes_terminated=1; shell child exited"


def _stall(evidence: str = _SILENCE, credits: float = 1.5) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_COMPLETE,
        stop_reason=STOP_REASON_TOOL_STALL,
        title=_TITLE,
        text=evidence,
        usage=TurnUsage(credits=credits),
    )


def _end(credits: float = 2.0) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN, usage=TurnUsage(credits=credits)
    )


def _text(text: str) -> AcpEvent:
    return AcpEvent(kind=EVENT_TEXT_CHUNK, text=text)


#: The prompt that ended on the lost edit, and the model's answer to the timeout.
_STALLED = [AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-edit", title=_TITLE), _stall()]
_ANSWER = [_text("The edit timed out; it did not land, so I wrote it again."), _end()]


class _Session:
    """One ACP session: each prompt streams the next script, the last repeating.

    It records every prompt sent and, like the session handle, installs fresh
    per-prompt billing stats as each prompt starts.
    """

    def __init__(self, *scripts: list[AcpEvent]) -> None:
        self._scripts = list(scripts)
        self.prompts: list[str] = []
        self.last_prompt_stats = AcpPromptStats()
        self.approved: list = []
        self.rejected: list = []

    def _script(self) -> list[AcpEvent]:
        return self._scripts[min(len(self.prompts), len(self._scripts)) - 1]

    async def stream(self, message: str, **_kwargs: Any) -> AsyncIterator[AcpEvent]:
        self.prompts.append(message)
        script = self._script()
        stats = AcpPromptStats()
        for ev in script:
            if ev.kind == EVENT_COMPLETE:
                stats.credits = ev.usage.credits
        self.last_prompt_stats = stats
        await asyncio.sleep(0)
        for ev in script:
            yield ev

    async def approve_tool(self, request_id, *args, **kwargs):
        self.approved.append(request_id)

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)


async def _collect(stream: AsyncIterator[AcpEvent]) -> list[AcpEvent]:
    return [ev async for ev in stream]


# ── The continuation itself ──


def test_the_continuation_names_the_call_and_asks_for_a_checked_retry():
    from kiro_crew.dashboard.state import TOOL_STALL_RECOVERY_PREFIX as dashboard_prefix
    from kiro_crew.dashboard.state import build_tool_stall_recovery_prompt

    text = timed_out_continuation(_stall())

    assert text.startswith(f"{TOOL_STALL_RECOVERY_PREFIX}\n")
    assert f"Kiro Crew cancelled {_TITLE} with a timeout error" in text
    assert "~5 minute(s)" in text
    assert "check whether it took effect, then retry it" in text
    # One wording on each surface: the dashboard's recovery for the same stall,
    # under the envelope its recovery card renders.
    assert dashboard_prefix == TOOL_STALL_RECOVERY_PREFIX
    assert text == (
        f"{dashboard_prefix}\n" + build_tool_stall_recovery_prompt(_TITLE, 301, timed_out=True)
    )
    assert text == build_timed_out_continuation(_TITLE, 301)


@pytest.mark.parametrize(
    "event",
    [
        _stall(_ORACLE),
        _end(),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="error: pipe died", text=_SILENCE),
    ],
    ids=["oracle-stall", "end-turn", "other-error"],
)
def test_only_a_silence_timeout_is_continued(event):
    assert timed_out_continuation(event) == ""


# ── In the turn: the shared stream wrapper ──


@pytest.mark.asyncio
async def test_the_turn_goes_on_with_the_continuation_on_the_same_session():
    session = _Session(_STALLED, _ANSWER)

    events = await _collect(continue_timed_out_calls(session.stream, "fix the notes"))

    assert session.prompts == ["fix the notes", timed_out_continuation(_stall())]
    # The stall's completion is held back: the consumer sees one turn that ends
    # on the model's answer, billed for both prompts.
    assert [ev.kind for ev in events] == [EVENT_TOOL_CALL, EVENT_TEXT_CHUNK, EVENT_COMPLETE]
    assert events[-1].stop_reason == STOP_REASON_END_TURN
    assert events[-1].usage.credits == 3.5


@pytest.mark.asyncio
async def test_an_oracle_stall_reaches_the_consumer_as_before():
    oracle = [AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-sh"), _stall(_ORACLE)]
    session = _Session(oracle, _ANSWER)

    events = await _collect(continue_timed_out_calls(session.stream, "build it"))

    assert session.prompts == ["build it"]
    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert events[-1].usage.credits == 1.5


@pytest.mark.asyncio
async def test_the_continuations_are_bounded_by_the_recovery_budget():
    session = _Session(_STALLED)

    events = await _collect(continue_timed_out_calls(session.stream, "fix the notes"))

    assert len(session.prompts) == STOP_RECOVERY_MAX_RETRIES + 1
    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert events[-1].usage.credits == 1.5 * (STOP_RECOVERY_MAX_RETRIES + 1)


# ── A background turn: stream_and_collect, and cron through it ──


@pytest.mark.asyncio
async def test_a_background_turn_is_continued_and_billed_for_both_prompts():
    session = _Session([_text("Writing the notes."), _stall()], _ANSWER)
    completions: list[AcpEvent] = []

    text = await stream_and_collect(
        session,  # type: ignore[arg-type]
        "fix the notes",
        retry_transient=False,
        on_complete=completions.append,
    )

    assert session.prompts == ["fix the notes", timed_out_continuation(_stall())]
    assert text == "Writing the notes.\n\nThe edit timed out; it did not land, so I wrote it again."
    assert [ev.stop_reason for ev in completions] == [STOP_REASON_END_TURN]
    assert provider_last_turn_usage(session).credits == 3.5


@pytest.mark.asyncio
async def test_a_background_turn_acts_on_nothing_past_the_stalled_completion():
    """What the stalled prompt's stream still carries after its completion (a late
    chunk, a second completion) is read to its end and not acted on: the reply is
    the two prompts' text, and ``on_complete`` sees the turn's one completion."""
    session = _Session([_text("Writing the notes."), _stall(), _text("LATE"), _end()], _ANSWER)
    completions: list[AcpEvent] = []

    text = await stream_and_collect(
        session,  # type: ignore[arg-type]
        "fix the notes",
        retry_transient=False,
        on_complete=completions.append,
    )

    assert session.prompts == ["fix the notes", timed_out_continuation(_stall())]
    assert text == "Writing the notes.\n\nThe edit timed out; it did not land, so I wrote it again."
    assert [ev.stop_reason for ev in completions] == [STOP_REASON_END_TURN]


@pytest.mark.asyncio
async def test_a_continued_turn_the_backend_never_finishes_keeps_both_prompts_text():
    """The continuation's prompt ends unfinished: the timeout error carries the
    text of the stalled prompt and of the continuation, as the reply would have."""
    unfinished = AcpEvent(kind=EVENT_COMPLETE, stop_reason="timeout")
    session = _Session([_text("Writing the notes."), _stall()], [_text("Retrying."), unfinished])

    with pytest.raises(AcpTimeoutError) as excinfo:
        await stream_and_collect(
            session,  # type: ignore[arg-type]
            "fix the notes",
            retry_transient=False,
        )

    assert excinfo.value.partial_output == "Writing the notes.\n\nRetrying."


@pytest.mark.asyncio
async def test_a_background_oracle_stall_ends_the_turn_as_before():
    session = _Session([_text("Building."), _stall(_ORACLE)], _ANSWER)
    completions: list[AcpEvent] = []

    text = await stream_and_collect(
        session,  # type: ignore[arg-type]
        "build it",
        retry_transient=False,
        on_complete=completions.append,
    )

    assert session.prompts == ["build it"]
    assert text == "Building."
    assert [ev.stop_reason for ev in completions] == [STOP_REASON_TOOL_STALL]


@pytest.mark.asyncio
async def test_a_cron_run_ends_on_the_models_answer_to_the_timeout():
    from kiro_crew.slack.gateway import _cron_stream_with_posttoken_resume

    session = _Session([_text("Checking the feed."), _stall()], _ANSWER)
    stops: list[str] = []

    text, carried = await _cron_stream_with_posttoken_resume(
        session,
        "check the feed",
        job_name="feed",
        directive_refusals=[],
        on_complete=lambda ev: stops.append(ev.stop_reason),
    )

    assert session.prompts[1] == timed_out_continuation(_stall())
    assert text.endswith("so I wrote it again.")
    assert stops == [STOP_REASON_END_TURN]
    assert carried is None


# ── A channel turn: TurnDriver, and the native Slack turn ──


class _Recorder(Renderer):
    def __init__(self) -> None:
        super().__init__(TransportCapabilities())
        self.events: list[tuple] = []

    async def on_text_chunk(self, text):
        self.events.append(("text_chunk", text))

    async def on_thinking(self, text):
        self.events.append(("thinking", text))

    async def on_tool_call(self, tool_call_id, title, tool_kind="", tool_purpose=""):
        self.events.append(("tool_call", tool_call_id))

    async def on_prompt_choice(
        self, options, request_id, tool_title="", tool_purpose="", tool_input=""
    ):
        self.events.append(("prompt_choice", request_id))

    async def on_compaction(self, pct):
        self.events.append(("compaction", pct))

    async def on_done(self, stop_reason=""):
        self.events.append(("done", stop_reason))

    async def on_steer_consumed(self, summary=""):
        self.events.append(("steer_consumed", summary))


@pytest.mark.asyncio
async def test_a_channel_turn_replies_with_the_models_answer_to_the_timeout():
    session = _Session(_STALLED, _ANSWER)
    renderer = _Recorder()
    driver = TurnDriver(session, renderer)

    reply = await driver.run("fix the notes")

    assert len(session.prompts) == 2
    assert session.prompts[1] == timed_out_continuation(_stall())
    assert reply.endswith("so I wrote it again.")
    assert driver.last_stop_reason == STOP_REASON_END_TURN
    assert [e for e in renderer.events if e[0] == "done"] == [("done", STOP_REASON_END_TURN)]


class _SlackSession(FakeProvider):
    """The Slack handler's provider double, one script per prompt."""

    def __init__(self, *scripts: list[AcpEvent]) -> None:
        super().__init__([])
        self._session = _Session(*scripts)

    @property
    def prompts(self) -> list[str]:
        return self._session.prompts

    def stream(self, message, timeout=120.0):  # type: ignore[override]
        return self._session.stream(message)


@pytest.mark.asyncio
async def test_a_native_slack_turn_replies_with_the_models_answer_to_the_timeout():
    slack = MockSlackClient()
    provider = _SlackSession(_STALLED, _ANSWER)
    sessions = FakeSessionManager(provider)

    await handle_message(slack, sessions, "C1", "fix the notes", None, "msg1", "U1")

    assert len(provider.prompts) == 2
    assert provider.prompts[1] == timed_out_continuation(_stall())
    posted = " ".join(
        str(a[1].get("text", "")) for a in slack.actions if a[0] in ("post", "update")
    )
    assert "so I wrote it again." in posted


# ── An agent channel member's turn, and the CLI chat ──


@pytest.mark.asyncio
async def test_an_agent_channel_posts_the_models_answer_to_the_timeout():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import channel as channel_mod
    from kiro_crew.channel import _stream_task

    session = _Session(_STALLED, _ANSWER)
    agent = SimpleNamespace(
        id="a1",
        role="dev",
        agent_name="dev",
        session_key="channel:c1:a1",
        approval_policy=SimpleNamespace(value="writes"),
        _approval_future=None,
    )
    room = SimpleNamespace(id="c1", trusted=False, members={})
    room._broadcast = MagicMock()
    room.post = AsyncMock()

    verdict = await _stream_task(agent, room, session, "fix the notes", "t1")

    assert verdict == channel_mod._STREAM_OK
    assert session.prompts == ["fix the notes", timed_out_continuation(_stall())]
    # One reply, the model's answer after the timeout, in the turn's thread.
    assert room.post.await_count == 1
    assert room.post.await_args.args[1] == _ANSWER[0].text
    assert room.post.await_args.kwargs["thread_id"] == "t1"


@pytest.mark.asyncio
async def test_the_cli_chat_prints_the_models_answer_to_the_timeout(capsys):
    from kiro_crew import cli_chat

    session = _Session([_text("Editing the notes. "), *_STALLED], _ANSWER)

    await cli_chat._send_and_print(session, "fix the notes")  # type: ignore[arg-type]

    out, err = capsys.readouterr()
    assert session.prompts == ["fix the notes", timed_out_continuation(_stall())]
    assert out.rstrip().endswith(_ANSWER[0].text)
    assert out.index("Editing the notes.") < out.index(_ANSWER[0].text)
    assert "timed out." not in err


@pytest.mark.asyncio
async def test_the_cli_chat_prints_an_oracle_stall_as_before(capsys):
    from kiro_crew import cli_chat

    oracle = [
        _text("Building."),
        AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-sh"),
        _stall(_ORACLE),
    ]
    session = _Session(oracle, _ANSWER)

    await cli_chat._send_and_print(session, "build it")  # type: ignore[arg-type]

    out, _err = capsys.readouterr()
    assert session.prompts == ["build it"]
    assert _ANSWER[0].text not in out


# ── The two recovery surfaces: the dashboard chat and a sub-agent run ──
#
# Neither streams the continuation inside the turn: each ends the turn on the stall
# and queues its own tool-stall recovery on the same session, which for a silence
# expiry must be the timed-out wording. Both run through their real recovery code.

#: ``_ORACLE``'s nudge, for a call the chat names by its title and no command.
_ORACLE_NUDGE_ARGS = {"processes_terminated": 1}


@pytest.mark.parametrize(
    ("evidence", "timed_out"), [(_SILENCE, True), (_ORACLE, False)], ids=["silence", "oracle"]
)
@pytest.mark.asyncio
async def test_the_dashboard_chat_recovery_hands_a_timed_out_call_back(
    tmp_path, evidence, timed_out
):
    """``_run_chat`` ends on the stall and ``_turn_stop_recovery`` queues the
    recovery prompt: the timed-out body for a silence expiry, the usual stall
    nudge for an oracle verdict."""
    from unittest.mock import AsyncMock, patch

    from chat_runner_coverage_helpers import _complete as _chat_complete
    from chat_runner_coverage_helpers import _drive, _runner_state, _set_stream, _slot

    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.state import build_tool_stall_recovery_prompt

    state, client = _runner_state(tmp_path)
    slot = _slot()
    _set_stream(client, [_chat_complete(STOP_REASON_TOOL_STALL, title=_TITLE, text=evidence)])

    with patch.object(chat_runner, "_start_next_queued_turn", new=AsyncMock(return_value=True)):
        await _drive(state, slot)

    assert slot._tool_stall_retries == 1
    queued = slot._queue[0]["content"]
    if timed_out:
        assert queued == build_timed_out_continuation(_TITLE, 301)
    else:
        assert queued == f"{TOOL_STALL_RECOVERY_PREFIX}\n" + build_tool_stall_recovery_prompt(
            _TITLE, 40, **_ORACLE_NUDGE_ARGS
        )
        assert "with a timeout error" not in queued


@pytest.mark.usefixtures("healthy_host_memory")
@pytest.mark.parametrize(
    ("evidence", "timed_out"), [(_SILENCE, True), (_ORACLE, False)], ids=["silence", "oracle"]
)
@pytest.mark.asyncio
async def test_a_subagent_run_recovery_hands_a_timed_out_call_back(evidence, timed_out):
    """The real sub-agent run ends its first prompt on the stall and
    ``_yield_for_stop_recovery_impl`` builds the in-place recovery prompt the same
    session is sent next: the timed-out body for a silence expiry, the usual stall
    nudge for an oracle verdict. The run then ends on the model's answer."""
    from subagent_stop_reason_helpers import _complete as _run_complete
    from subagent_stop_reason_helpers import _manager, _mock_sessions, _spawn_and_wait
    from subagent_stop_reason_helpers import _text as _run_text

    from kiro_crew.dashboard.state import build_tool_stall_recovery_prompt

    prompts: list[str] = []

    def stream_factory(msg: str, *_args, **_kwargs):
        prompts.append(msg)

        async def _gen():
            if len(prompts) == 1:
                yield _run_complete(STOP_REASON_TOOL_STALL, evidence)
            else:
                yield _run_text("retried it")
                yield _run_complete(STOP_REASON_END_TURN)

        return _gen()

    mgr = _manager(_mock_sessions(stream_factory))

    info = await _spawn_and_wait(mgr)

    assert info.outcome == "completed"
    assert len(prompts) == 2
    # The helper's stall names the call ``execute_bash`` running this command.
    title, command = "execute_bash", "pytest -q > run.log 2>&1"
    if timed_out:
        assert prompts[1] == build_timed_out_continuation(title, 301)
    else:
        assert prompts[1] == f"{TOOL_STALL_RECOVERY_PREFIX}\n" + build_tool_stall_recovery_prompt(
            title, 40, command=command, **_ORACLE_NUDGE_ARGS
        )
        assert "with a timeout error" not in prompts[1]
