"""Claude Code's background work, read per session on one shared claude-agent-acp process.

``_claude/sdkMessage`` frames (``acp.raw_sdk_tasks``) go through the runtime's own
reader, which reads each into the session it names
(``ClaudeHarness.take_session_frame``) and puts it on no queue. Each test sends chat A's
messages while chat B's turn reads, then checks that A's next dashboard turn draws what
they made and that B's never does.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_claude_shared_runtime import harness  # noqa: F401  (fixture, by name)
from test_claude_shared_surfaces import _answer, _run, _turn

from kiro_crew.acp import raw_sdk_tasks
from kiro_crew.acp.raw_sdk_tasks import END_COMPLETED, RetryNotice, TaskEndNotice
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import EVENT_HARNESS_NOTICE, EVENT_SUBAGENT_LIST

pytestmark = pytest.mark.usefixtures("harness")

TASK = "wy57uompy"


def _sdk(sid: str, message: dict[str, Any]) -> dict[str, Any]:
    body = {"type": "system", "session_id": sid, "uuid": "probe", **message}
    return {
        "jsonrpc": "2.0",
        "method": raw_sdk_tasks.METHOD_CLAUDE_SDK_MESSAGE,
        "params": {"sessionId": sid, "message": body},
    }


def _background_shell_ends(sid: str) -> list[dict[str, Any]]:
    started = {
        "subtype": "task_started",
        "task_id": TASK,
        "task_type": raw_sdk_tasks.TASK_TYPE_SHELL,
        "description": "ls",
        "is_backgrounded": True,
    }
    ended = {"subtype": "task_notification", "task_id": TASK, "status": "completed"}
    return [_sdk(sid, started), _sdk(sid, ended)]


def _workflow_runs(sid: str) -> list[dict[str, Any]]:
    started = {
        "subtype": "task_started",
        "task_id": TASK,
        "task_type": raw_sdk_tasks.TASK_TYPE_WORKFLOW,
        "description": "review",
    }
    row = {"type": "workflow_agent", "index": 0, "label": "x0", "state": "running"}
    progress = {"subtype": "task_progress", "task_id": TASK, "workflow_progress": [row]}
    return [_sdk(sid, started), _sdk(sid, progress)]


def _with(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One turn's frames: *frames* read while it runs, then its answer."""
    return [*frames, *_answer()]


def _providers(rt: Any, chat_a: Any, chat_b: Any) -> tuple[AcpSessionProvider, AcpSessionProvider]:
    return (
        AcpSessionProvider(chat_a, rt, session_key="a"),
        AcpSessionProvider(chat_b, rt, session_key="b"),
    )


async def _dashboard_turn(rt, provider, handle, stdout, frames) -> list[Any]:
    """A turn the dashboard marked as the one that draws background work."""
    raw_sdk_tasks.expect_dashboard_turn(provider)
    return await _turn(rt, handle, stdout, frames)


def test_a_background_commands_end_is_drawn_only_in_its_own_chat(tmp_path):
    async def scenario(rt, chat_a, chat_b, stdout):
        provider_a, provider_b = _providers(rt, chat_a, chat_b)
        events_b = await _dashboard_turn(
            rt, provider_b, chat_b, stdout, _with(_background_shell_ends("s-a"))
        )
        events_a = await _dashboard_turn(rt, provider_a, chat_a, stdout, _answer())
        return events_a, events_b

    events_a, events_b = _run(tmp_path, scenario)
    notices_a = [e.harness_notice for e in events_a if e.kind == EVENT_HARNESS_NOTICE]
    assert notices_a == [TaskEndNotice(task="ls", status=END_COMPLETED)]
    assert not [e for e in events_b if e.kind == EVENT_HARNESS_NOTICE]


def test_an_api_retry_is_drawn_only_in_its_own_chat(tmp_path):
    retry = {
        "subtype": "api_retry",
        "attempt": 1,
        "max_retries": 10,
        "retry_delay_ms": 2000,
        "error_status": 529,
        "error": "overloaded",
    }

    async def scenario(rt, chat_a, chat_b, stdout):
        provider_a, provider_b = _providers(rt, chat_a, chat_b)
        events_b = await _dashboard_turn(
            rt, provider_b, chat_b, stdout, _with([_sdk("s-a", retry)])
        )
        events_a = await _dashboard_turn(rt, provider_a, chat_a, stdout, _answer())
        return events_a, events_b

    events_a, events_b = _run(tmp_path, scenario)
    notices_a = [e.harness_notice for e in events_a if e.kind == EVENT_HARNESS_NOTICE]
    assert notices_a == [RetryNotice(attempt=1, max_retries=10, reason="overloaded", status=529)]
    assert not [e for e in events_b if e.kind == EVENT_HARNESS_NOTICE]


def test_a_workflows_agents_are_cards_of_their_own_chat_only(tmp_path):
    async def scenario(rt, chat_a, chat_b, stdout):
        provider_a, provider_b = _providers(rt, chat_a, chat_b)
        events_b = await _dashboard_turn(
            rt, provider_b, chat_b, stdout, _with(_workflow_runs("s-a"))
        )
        events_a = await _dashboard_turn(rt, provider_a, chat_a, stdout, _answer())
        live = (raw_sdk_tasks.live_card_ids(provider_a), raw_sdk_tasks.live_card_ids(provider_b))
        card = next(iter(live[0]), "")
        refusals = (
            chat_a.native_child_resume_refusal(f"native:{card}"),
            chat_b.native_child_resume_refusal(f"native:{card}"),
        )
        counts = (chat_a.report_native_children(None), chat_b.report_native_children(None))
        return events_a, events_b, live, refusals, counts

    events_a, events_b, live, refusals, counts = _run(tmp_path, scenario)
    rosters_a = [e.subagents for e in events_a if e.kind == EVENT_SUBAGENT_LIST]
    assert rosters_a and len(rosters_a[-1]) == 1
    assert not [e for e in events_b if e.kind == EVENT_SUBAGENT_LIST]
    # The running agent's card is A's: kept open, not resumable, counted, for A alone.
    assert len(live[0]) == 1 and live[1] == frozenset()
    assert refusals[0] is not None and refusals[1] is None
    assert counts == (1, 0)


def test_a_turn_the_dashboard_did_not_mark_draws_nothing_and_leaves_it_waiting(tmp_path):
    async def scenario(rt, chat_a, chat_b, stdout):
        provider_a, _ = _providers(rt, chat_a, chat_b)
        unmarked = await _turn(rt, chat_a, stdout, _with(_background_shell_ends("s-a")))
        marked = await _dashboard_turn(rt, provider_a, chat_a, stdout, _answer())
        return unmarked, marked

    unmarked, marked = _run(tmp_path, scenario)
    assert not [e for e in unmarked if e.kind == EVENT_HARNESS_NOTICE]
    assert [e for e in marked if e.kind == EVENT_HARNESS_NOTICE]


def test_ending_a_session_closes_the_cards_its_background_work_kept_open(tmp_path):
    closed: list[str] = []

    async def scenario(rt, chat_a, chat_b, stdout):
        provider_a, provider_b = _providers(rt, chat_a, chat_b)
        raw_sdk_tasks.on_cards_lost(provider_a, lambda: closed.append("a"))
        raw_sdk_tasks.on_cards_lost(provider_b, lambda: closed.append("b"))
        await _dashboard_turn(rt, provider_a, chat_a, stdout, _with(_workflow_runs("s-a")))
        await rt.terminate_session("s-a")

    _run(tmp_path, scenario)
    assert closed == ["a"]


class _State:
    """The fields ``ClaudeHarness.end_session`` and ``after_session_start`` touch."""

    def __init__(self, session_id: str | None = None) -> None:
        self._session_id = session_id
        self._raw_sdk = raw_sdk_tasks.RawSdkState()

    async def _discard_settings_seed(self) -> None:
        return None

    def _reset_settings_seed_state(self) -> None:
        return None

    def bind_started(self, session_id: str, _version: str, **_kw: Any) -> None:
        self._session_id = session_id

    async def _pin_starting_mode(self, _resp: Any) -> None:
        return None


def test_a_reload_keeps_the_dashboard_mark_and_the_workflow_cards_seen():
    """An MCP change reloads the session: its ended state hands the dashboard's mark
    and the card ids seen to the new state of the same id, as the per-session client
    keeps both on one object; another session's new state gets nothing."""
    import asyncio

    from kiro_crew.acp.harness.claude import ClaudeHarness

    host = ClaudeHarness()
    ended = _State("s-1")
    ended._raw_sdk.dashboard_turn = True
    ended._raw_sdk.seen_cards["card-1"] = None

    async def nothing(*_a: Any, **_k: Any) -> None:
        return None

    async def scenario() -> tuple[_State, _State]:
        await host.end_session(ended)
        reloaded, other = _State(), _State()
        for state, sid in ((reloaded, "s-1"), (other, "s-2")):
            await host.after_session_start(
                state, sid, {}, send_request=nothing, close_session=nothing
            )
        return reloaded, other

    reloaded, other = asyncio.run(scenario())
    assert reloaded._raw_sdk.dashboard_turn is True
    assert list(reloaded._raw_sdk.seen_cards) == ["card-1"]
    assert other._raw_sdk.dashboard_turn is False and not other._raw_sdk.seen_cards
