"""A live backend that lost a session recovers on every turn surface, not only the dashboard.

The dashboard chat already recovers this way. The other surfaces that run a turn on a
mapped session each own one "replace the session and run it again" seam; the
lost-session answer (``acp_error_is_session_not_found``) is routed into it:

- Slack thread: the nested replay the compaction failure already uses.
- channel agent: ``_stream_task``'s verdict into ``_recover_busy_agent``.
- cron (single agent): the ACP-death reset-and-retry arm.
- cron (agent sequence): a one-shot re-run of the step.
- subagent completion injected into a parent: reset + re-claim once.
- subagent run: the one-shot respawn on a fresh task.

Each surface retries ONCE, then ends on a clear error. The fake ACP raises the
same ``AcpError`` the adapter sends.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_cron_acp_retry import gw_and_cb  # noqa: F401  (fixture)

from kiro_crew.acp.client import AcpError
from kiro_crew.llm_helpers import SESSION_NOT_FOUND_GIVE_UP_TEXT, SESSION_NOT_FOUND_RETRY_NOTICE
from kiro_crew.providers.base import EVENT_TOOL_CALL, LLMEvent


def _lost() -> AcpError:
    return AcpError("Session not found: 3f2a-backend-sid")


# ── Slack thread ────────────────────────────────────────────────────────────


class _ScriptedSlackProvider:
    """A Slack provider whose user turns play scripts; an exception item is raised."""

    def __init__(self, scripts: list[list[Any]]):
        from test_slack_handler import FakeProvider

        self._base = FakeProvider()
        self._scripts = scripts
        self.turns = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    async def stream(self, message, timeout=120.0):
        if "hello" not in message or message.startswith("You are a session naming agent"):
            async for event in self._base.stream(message, timeout):
                yield event
            return
        script = self._scripts[min(self.turns, len(self._scripts) - 1)]
        self.turns += 1
        for item in script:
            if isinstance(item, BaseException):
                raise item
            yield item


def _answer() -> list[Any]:
    return [LLMEvent(kind="text_chunk", text="The answer is 42"), LLMEvent(kind="complete")]


@pytest.mark.asyncio
async def test_slack_lost_session_resets_and_replays_once():
    from test_slack_handler import FakeSessionManager, MockSlackClient, _visible_texts

    from kiro_crew.slack.handler import handle_message

    slack = MockSlackClient()
    provider = _ScriptedSlackProvider([[_lost()], _answer()])
    sessions = FakeSessionManager(provider)

    await handle_message(slack, sessions, "C1", "hello", "thread1", "msg1", "U1")

    assert provider.turns == 2
    assert "reset:thread1" in sessions.removed
    texts = _visible_texts(slack)
    assert any(SESSION_NOT_FOUND_RETRY_NOTICE in t for t in texts), texts
    assert any("The answer is 42" in t for t in texts), texts
    assert sessions.replay_gaps == [("open", "thread1"), ("close", "thread1")]


@pytest.mark.asyncio
async def test_slack_second_loss_ends_on_a_clear_error():
    from test_slack_handler import FakeSessionManager, MockSlackClient, _visible_texts

    from kiro_crew.slack.handler import handle_message

    slack = MockSlackClient()
    provider = _ScriptedSlackProvider([[_lost()]])
    sessions = FakeSessionManager(provider)

    await handle_message(slack, sessions, "C1", "hello", "thread1", "msg1", "U1")

    assert provider.turns == 2, "one replay, never a loop"
    texts = _visible_texts(slack)
    assert any(SESSION_NOT_FOUND_GIVE_UP_TEXT in t for t in texts), texts
    assert sessions.replay_gaps[0] == ("open", "thread1")
    assert sessions.replay_gaps[-1] == ("close", "thread1"), "no gap left open"


@pytest.mark.asyncio
async def test_slack_lost_after_output_is_not_replayed():
    from test_slack_handler import FakeSessionManager, MockSlackClient

    from kiro_crew.slack.handler import handle_message

    slack = MockSlackClient()
    provider = _ScriptedSlackProvider([[LLMEvent(kind="text_chunk", text="half"), _lost()]])
    sessions = FakeSessionManager(provider)

    await handle_message(slack, sessions, "C1", "hello", "thread1", "msg1", "U1")

    assert provider.turns == 1
    assert "reset:thread1" in sessions.removed, "the lost binding is still dropped"


# ── channel agent ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_channel_lost_session_is_replaced_and_replayed_once():
    from test_channel_prompt_busy import FakeSessions, _make_agent, _make_channel, _text_client

    from kiro_crew import channel as channel_mod

    agent = _make_agent()
    ch = _make_channel()
    seen: list[str] = []
    sessions = FakeSessions([_text_client("done", seen)])

    client = await channel_mod._recover_busy_agent(
        agent, ch, sessions, "please run", cause=channel_mod._STREAM_LOST
    )

    assert client is not None
    assert seen == ["please run"]
    assert [r[0] for r in sessions.resets] == [agent.session_key]
    posted = [c.args[1] for c in ch.post.await_args_list]
    assert SESSION_NOT_FOUND_RETRY_NOTICE in posted


@pytest.mark.asyncio
async def test_channel_stream_task_classifies_a_lost_session():
    from test_channel_prompt_busy import _make_agent, _make_channel, _raising_client

    from kiro_crew import channel as channel_mod

    verdict = await channel_mod._stream_task(
        _make_agent(), _make_channel(), _raising_client(_lost()), "hi"
    )
    assert verdict == channel_mod._STREAM_LOST


@pytest.mark.asyncio
async def test_channel_lost_after_a_tool_is_replaced_without_replay():
    from test_channel_prompt_busy import FakeSessions, _make_agent, _make_channel, _text_client

    from kiro_crew import channel as channel_mod

    agent = _make_agent()
    ch = _make_channel()
    client = SimpleNamespace()

    async def _stream(message):
        yield SimpleNamespace(kind=EVENT_TOOL_CALL, text="write_file")
        raise _lost()

    client.stream = _stream
    verdict = await channel_mod._stream_task(agent, ch, client, "hi")
    assert verdict == channel_mod._STREAM_LOST_AFTER_TOOL

    seen: list[str] = []
    sessions = FakeSessions([_text_client("never", seen)])
    replacement = await channel_mod._recover_busy_agent(agent, ch, sessions, "hi", cause=verdict)
    assert replacement is not None
    assert seen == [], "a turn that ran a tool is not re-sent"


@pytest.mark.asyncio
async def test_channel_second_loss_returns_none():
    from test_channel_prompt_busy import FakeSessions, _make_agent, _make_channel, _raising_client

    from kiro_crew import channel as channel_mod

    sessions = FakeSessions([_raising_client(_lost())])
    assert (
        await channel_mod._recover_busy_agent(
            _make_agent(), _make_channel(), sessions, "hi", cause=channel_mod._STREAM_LOST
        )
        is None
    )


# ── cron (single agent and sequence) ────────────────────────────────────────


def _job(**kw):
    from kiro_crew.cron import CronJob, CronSchedule

    return CronJob(
        id=kw.pop("id", "snf"),
        name="snf",
        message="msg",
        schedule=CronSchedule(kind="every", every_secs=60),
        **kw,
    )


def _run_cron(gw, get_cb, capture_cron, job, *, stream=None, resume=None):
    """Fire *job* once with either ``stream_and_collect`` or the cron resume seam faked."""
    target, fake = (
        ("kiro_crew.slack.gateway.stream_and_collect", stream)
        if stream is not None
        else ("kiro_crew.slack.gateway._cron_stream_with_posttoken_resume", resume)
    )

    async def go():
        await gw._init_cron()
        return await get_cb()(job)

    with (
        patch(target, side_effect=fake),
        patch(
            "kiro_crew.slack.gateway.CronService.create", new=AsyncMock(side_effect=capture_cron)
        ),
    ):
        return asyncio.run(go())


def test_cron_lost_session_resets_and_retries_once(gw_and_cb):  # noqa: F811
    gw, get_cb, capture_cron = gw_and_cb
    calls = 0

    async def stream(*_a, **_k):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _lost()
        return "recovered"

    result = _run_cron(gw, get_cb, capture_cron, _job(), stream=stream)
    assert calls == 2
    assert result == "recovered"
    gw.sessions.reset.assert_awaited()


def test_cron_second_loss_fails_with_a_clear_error(gw_and_cb):  # noqa: F811
    gw, get_cb, capture_cron = gw_and_cb
    gw.dashboard_state = MagicMock()
    calls = 0

    async def stream(*_a, **_k):
        nonlocal calls
        calls += 1
        raise _lost()

    with pytest.raises(AcpError):
        _run_cron(gw, get_cb, capture_cron, _job(), stream=stream)
    assert calls == 2
    gw.dashboard_state.notify.assert_called_once()
    assert SESSION_NOT_FOUND_GIVE_UP_TEXT in str(gw.dashboard_state.notify.call_args)


def test_cron_sequence_step_reloads_once(gw_and_cb):  # noqa: F811
    gw, get_cb, capture_cron = gw_and_cb
    gw.cron_svc = None
    calls = 0

    async def resume(_c, _m, **_k):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _lost()
        return f"ok{calls}", None

    _run_cron(gw, get_cb, capture_cron, _job(agent_sequence=["a", "b"], id="seq"), resume=resume)

    assert calls == 3, "step a twice (one reload), then step b"
    reset_keys = [c.args[0] for c in gw.sessions.reset.await_args_list]
    assert reset_keys.count("cron:seq:a") >= 2


def test_cron_sequence_step_second_loss_raises_clear_error(gw_and_cb):  # noqa: F811
    gw, get_cb, capture_cron = gw_and_cb
    gw.cron_svc = None
    calls = 0

    async def resume(_c, _m, **_k):
        nonlocal calls
        calls += 1
        raise _lost()

    with pytest.raises(AcpError, match=SESSION_NOT_FOUND_GIVE_UP_TEXT):
        _run_cron(
            gw, get_cb, capture_cron, _job(agent_sequence=["a", "b"], id="seq2"), resume=resume
        )
    assert calls == 2


# ── subagent run ────────────────────────────────────────────────────────────


async def _settle(mgr, info, rounds: int = 400) -> None:
    """Wait for *info* to end, following any respawned task."""
    for _ in range(rounds):
        task = mgr._tasks.get(info.id)
        if task is not None and not task.done():
            await task
        recovery = mgr._tasks.get(f"{info.id}:recovery")
        if recovery is not None and not recovery.done():
            await recovery
            continue
        if info.done:
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_subagent_lost_session_respawns_once(healthy_host_memory):
    from test_subagent_turn_resilience import _manager, _mock_sessions

    sessions = _mock_sessions(lambda _m: None)
    mgr = _manager(sessions)
    attempts = 0

    async def run_inner(info, _key):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _lost()
        info.result = "recovered"
        info.done = True

    mgr._run_inner = AsyncMock(side_effect=run_inner)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("work")
        assert info is not None
        await _settle(mgr, info)

    assert attempts == 2
    assert info.error == ""
    assert info.result == "recovered"
    assert info._session_not_found_retry_used is True


@pytest.mark.asyncio
async def test_subagent_lost_session_after_a_tool_is_terminal(healthy_host_memory):
    from test_subagent_turn_resilience import _manager, _mock_sessions

    sessions = _mock_sessions(lambda _m: None)
    mgr = _manager(sessions)
    mgr._schedule_cancel_recovery = MagicMock()

    async def run_inner(info, _key):
        info.tool_count = 1
        raise _lost()

    mgr._run_inner = AsyncMock(side_effect=run_inner)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("work")
        assert info is not None
        await mgr._tasks[info.id]

    mgr._schedule_cancel_recovery.assert_not_called()
    assert info.done is True
    assert "could repeat side effects" in info.error


@pytest.mark.asyncio
async def test_subagent_second_loss_names_the_give_up(healthy_host_memory):
    from test_subagent_turn_resilience import _manager, _mock_sessions

    sessions = _mock_sessions(lambda _m: None)
    mgr = _manager(sessions)
    attempts = 0

    async def run_inner(info, _key):
        nonlocal attempts
        attempts += 1
        raise _lost()

    mgr._run_inner = AsyncMock(side_effect=run_inner)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("work")
        assert info is not None
        await _settle(mgr, info)

    assert attempts == 2
    assert info.done is True
    assert SESSION_NOT_FOUND_GIVE_UP_TEXT in info.error


# ── subagent completion injected into a parent session ──────────────────────


@pytest.mark.asyncio
async def test_injection_reloads_the_parent_once():
    from test_gateway_pipe_death import PARENT_KEY, _finished_subagent, _gateway_with_on_done

    orch, on_done = _gateway_with_on_done()
    info = _finished_subagent()
    calls = 0

    async def stream(*_a, **_k):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _lost()
        return "synthesized"

    with patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream):
        await on_done(info)

    assert calls == 2
    orch.sessions.reset.assert_any_await(PARENT_KEY)
    orch.subagent_mgr.notify_injection_failed.assert_not_called()


@pytest.mark.asyncio
async def test_injection_second_loss_is_reported_not_looped():
    from test_gateway_pipe_death import _finished_subagent, _gateway_with_on_done

    orch, on_done = _gateway_with_on_done()
    info = _finished_subagent()
    calls = 0

    async def stream(*_a, **_k):
        nonlocal calls
        calls += 1
        raise _lost()

    with patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream):
        await on_done(info)

    assert calls == 2, "one re-load, no retry ladder on a lost session"
    orch.subagent_mgr.notify_injection_failed.assert_called_once()
    assert SESSION_NOT_FOUND_GIVE_UP_TEXT in str(
        orch.subagent_mgr.notify_injection_failed.call_args
    )


# ── a lost session after a tool ran is not re-sent ──────────────────────────


def _lost_after_tool() -> AcpError:
    from kiro_crew.llm_helpers import TOOL_ACTIVITY_ATTR

    exc = _lost()
    setattr(exc, TOOL_ACTIVITY_ATTR, True)
    return exc


@pytest.mark.asyncio
async def test_stream_and_collect_tags_tool_activity_on_a_lost_session():
    from kiro_crew.llm_helpers import acp_error_after_tool_activity, stream_and_collect

    provider = SimpleNamespace()

    async def _stream(_message):
        yield LLMEvent(kind=EVENT_TOOL_CALL, text="write_file", tool_call_id="t1")
        raise _lost()

    provider.stream = _stream
    with pytest.raises(AcpError) as caught:
        await stream_and_collect(provider, "hi")
    assert acp_error_after_tool_activity(caught.value) is True


def test_cron_lost_after_a_tool_is_not_retried(gw_and_cb):  # noqa: F811
    gw, get_cb, capture_cron = gw_and_cb
    calls = 0

    async def stream(*_a, **_k):
        nonlocal calls
        calls += 1
        raise _lost_after_tool()

    with pytest.raises(AcpError):
        _run_cron(gw, get_cb, capture_cron, _job(), stream=stream)
    assert calls == 1


def test_cron_sequence_step_lost_after_a_tool_is_not_rerun(gw_and_cb):  # noqa: F811
    from kiro_crew.llm_helpers import SESSION_NOT_FOUND_NOT_REPLAYED_TEXT

    gw, get_cb, capture_cron = gw_and_cb
    gw.cron_svc = None
    calls = 0

    async def resume(_c, _m, **_k):
        nonlocal calls
        calls += 1
        raise _lost_after_tool()

    with pytest.raises(AcpError, match="this one was not re-run"):
        _run_cron(
            gw, get_cb, capture_cron, _job(agent_sequence=["a", "b"], id="seq3"), resume=resume
        )
    assert calls == 1
    assert "not re-run" in SESSION_NOT_FOUND_NOT_REPLAYED_TEXT


@pytest.mark.asyncio
async def test_injection_lost_after_a_tool_is_not_reloaded():
    from test_gateway_pipe_death import _finished_subagent, _gateway_with_on_done

    orch, on_done = _gateway_with_on_done()
    calls = 0

    async def stream(*_a, **_k):
        nonlocal calls
        calls += 1
        raise _lost_after_tool()

    with patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream):
        await on_done(_finished_subagent())

    assert calls == 1


@pytest.mark.asyncio
async def test_cron_continuation_loss_is_marked_as_activity():
    """A loss during the post-token CONTINUE must never let cron re-send the task."""
    from kiro_crew.llm_helpers import acp_error_after_tool_activity
    from kiro_crew.slack.gateway import _cron_stream_with_posttoken_resume

    calls = 0

    async def stream(_client, _msg, *, on_chunk=None, **_k):
        nonlocal calls
        calls += 1
        if calls == 1:
            on_chunk("appended the row")
            raise AcpError("throttled", transient=True)
        raise _lost()

    with (
        patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream),
        patch("kiro_crew.slack.gateway.asyncio.sleep", new=AsyncMock()),
        patch("kiro_crew.slack.gateway.provider_last_turn_usage") as usage,
    ):
        usage.return_value = SimpleNamespace(credits=0.0)
        with pytest.raises(AcpError) as caught:
            await _cron_stream_with_posttoken_resume(object(), "task", job_name="j")
    assert calls == 2
    assert acp_error_after_tool_activity(caught.value) is True


def test_cron_alert_names_the_skipped_rerun_after_a_tool(gw_and_cb):  # noqa: F811
    from kiro_crew.llm_helpers import SESSION_NOT_FOUND_NOT_REPLAYED_TEXT

    gw, get_cb, capture_cron = gw_and_cb
    gw.dashboard_state = MagicMock()

    async def stream(*_a, **_k):
        raise _lost_after_tool()

    with pytest.raises(AcpError):
        _run_cron(gw, get_cb, capture_cron, _job(), stream=stream)
    alert = str(gw.dashboard_state.notify.call_args)
    assert SESSION_NOT_FOUND_NOT_REPLAYED_TEXT in alert
    assert SESSION_NOT_FOUND_GIVE_UP_TEXT not in alert
