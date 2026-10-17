"""A failed completion's held retry against every automatic drain of an idle tab.

A completion turn that fails before the model read it queues one retry row and
holds it, so the retry is not walked straight back into the failure. The hold
is on that row, in the shared drain (``queue_drain._start_next_queued_turn``):
a new delivery reaching the idle tab runs its own row, the stall kick runs the
rows behind the retry, and nothing waits for a person. It is released when a
turn on the tab ends without failing (that turn's exit drain then runs the
retry), or when a person sends, presses Run now or presses Stop.

Every test drives the real turn, the real drain and the real delivery path; only
the provider is scripted: a prompt carrying the first child's announce fails
before any token, every other prompt ends normally.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_runner_coverage_helpers import _complete
from test_completion_retry_ownership import (
    COMPLETION,
    SECOND_COMPLETION,
    USER_MESSAGE,
    _bounded_real_drain,
    _completion_rows,
    _run_started_turns,
    _state,
)

from kiro_crew.acp.client import AcpError
from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.dashboard import session_deliver as sd
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.chat_utils_slots import effective_session_key
from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

CRON_TEXT = '[Cron notification from "nightly"]\nstill red\n[End of cron notification]'
APP_TEXT = '[MCP app message from "srv/tool"]\nhello\n[End of MCP app message]'


def _scripted_client(prompts: list[str]) -> AsyncMock:
    """Fails every prompt carrying the first child's announce; ends every other one."""
    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.set_model = AsyncMock()
    client.served_model = ""
    client.client = None

    async def _stream(msg):
        prompts.append(msg)
        if COMPLETION in msg:
            raise AcpError("provider rejected the prompt", transient=False)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done.")
        yield _complete()

    client.stream = _stream
    client.stream_command = _stream
    return client


def _sent(prompts: list[str]) -> list[str]:
    labels = (
        ("first", COMPLETION),
        ("second", SECOND_COMPLETION),
        ("cron", "still red"),
        ("app", "hello"),
        ("user", USER_MESSAGE),
    )
    return [next(label for label, text in labels if text in prompt) for prompt in prompts]


async def _held_retry(tmp_path, monkeypatch):
    """A tab whose completion turn failed: its retry row is queued and held."""
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    slot._titled = True
    prompts: list[str] = []
    state.sessions.get_or_create = AsyncMock(return_value=(_scripted_client(prompts), True, False))
    started = _bounded_real_drain(monkeypatch)
    discarded = MagicMock()
    await _run_chat(
        state,
        slot,
        COMPLETION,
        _turn_actor="subagent",
        _on_consumed=MagicMock(),
        _on_discarded=discarded,
    )
    await _run_started_turns(state)
    # The failed turn's own hand-off started nothing: the retry is held.
    assert _sent(prompts) == ["first"]
    assert [row["content"] for row in _completion_rows(slot)] == [COMPLETION]
    return state, slot, prompts, started, discarded


async def _deliver_completion(state, slot) -> None:
    """A second child's completion reaches the idle tab through the gateway's route."""
    from kiro_crew.slack.gateway import GatewayOrchestrator
    from kiro_crew.subagent import SubagentInfo

    gateway = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gateway.dashboard_state = state
    gateway.subagent_mgr = None
    gateway.sessions = None
    info = SubagentInfo(id="second-child", task="t", parent_session_key=f"dashboard:{slot.key}")
    info.done = True
    info.result = "another report"
    await GatewayOrchestrator._route_subagent_to_tab(
        gateway,
        info,
        announce=SECOND_COMPLETION,
        body="another report",
        title="second-child finished",
        sub_meta={},
        carried=(),
        _flush_only=True,
        _injection_slot=slot,
        _injection_slot_name=slot.key,
        parent_key=f"dashboard:{slot.key}",
    )


async def _deliver(kind: str, state, slot) -> None:
    if kind == "sub-agent":
        await _deliver_completion(state, slot)
    elif kind == "cron":
        assert await sd.deliver(state, slot, CRON_TEXT, sd.cron("nightly")) == sd.DELIVERED_TURN
    else:
        assert await sd.deliver(state, slot, APP_TEXT, sd.app("srv/tool")) == sd.DELIVERED_TURN


_DELIVERED = {"sub-agent": "second", "cron": "cron", "app": "app"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sub-agent", "cron", "app"])
async def test_a_new_delivery_to_an_idle_tab_runs_its_own_row_not_the_held_retry(
    tmp_path, monkeypatch, kind
):
    """Property 2: the idle-tab drain a delivery asks for starts that delivery."""
    state, slot, prompts, _started, discarded = await _held_retry(tmp_path, monkeypatch)

    await _deliver(kind, state, slot)
    await _run_started_turns(state)

    # The delivery's turn ended well, so its exit then ran the retry (property 4).
    assert _sent(prompts) == ["first", _DELIVERED[kind], "first"]
    discarded.assert_not_called()


@pytest.mark.asyncio
async def test_the_stall_kick_runs_the_rows_behind_a_held_retry(tmp_path, monkeypatch):
    """Property 3: a held retry strands nothing queued behind it."""
    state, slot, prompts, _started, discarded = await _held_retry(tmp_path, monkeypatch)
    slot.queue_append(USER_MESSAGE)

    await chat_runner.drain_idle_parent_queue_for_stall(state, effective_session_key(slot))
    await _run_started_turns(state)

    assert _sent(prompts) == ["first", "user", "first"]
    discarded.assert_not_called()


@pytest.mark.asyncio
async def test_a_turn_that_ends_well_releases_the_retry_to_its_exit_drain(tmp_path, monkeypatch):
    """Property 4: the delivery's turn ends without failing, and its exit runs the retry.

    The retry fails again, so its own turn holds the next retry: the two failures
    never run each other.
    """
    state, slot, prompts, _started, discarded = await _held_retry(tmp_path, monkeypatch)

    await _deliver("cron", state, slot)
    await _run_started_turns(state)

    assert _sent(prompts) == ["first", "cron", "first"]
    rows = _completion_rows(slot)
    assert [row["content"] for row in rows] == [COMPLETION]
    assert slot._held_completion_retries == {rows[0]["id"]}
    discarded.assert_not_called()


async def _the_kick_starts_nothing(state, slot, prompts) -> None:
    await chat_runner.drain_idle_parent_queue_for_stall(state, effective_session_key(slot))
    await _run_started_turns(state)
    assert _sent(prompts) == ["first"]


@pytest.mark.asyncio
async def test_run_now_releases_the_held_retry(tmp_path, monkeypatch):
    state, slot, prompts, _started, _discarded = await _held_retry(tmp_path, monkeypatch)
    await _the_kick_starts_nothing(state, slot, prompts)
    (retry,) = _completion_rows(slot)

    assert await chat_runner._start_next_queued_turn(state, slot, required_queue_id=retry["id"])
    await _run_started_turns(state)

    assert _sent(prompts) == ["first", "first"]


@pytest.mark.asyncio
async def test_stop_releases_the_held_retry(tmp_path, monkeypatch):
    state, slot, prompts, _started, _discarded = await _held_retry(tmp_path, monkeypatch)
    await _the_kick_starts_nothing(state, slot, prompts)

    await chat_handlers.stop_slot_turn(state, slot)
    assert slot._held_completion_retries == set()
    await chat_runner.drain_idle_parent_queue_for_stall(state, effective_session_key(slot))
    await _run_started_turns(state)

    assert _sent(prompts) == ["first", "first"]


@pytest.mark.asyncio
async def test_a_persons_send_releases_the_held_retry(tmp_path, monkeypatch):
    """The send's own turn ends well; its exit drain then runs the retry."""
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app

    state, slot, prompts, _started, _discarded = await _held_retry(tmp_path, monkeypatch)
    await _the_kick_starts_nothing(state, slot, prompts)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat", json={"slot": slot.key, "message": USER_MESSAGE})
        assert resp.status == 200
        await resp.read()
    await _run_started_turns(state)

    assert _sent(prompts) == ["first", "user", "first"]
