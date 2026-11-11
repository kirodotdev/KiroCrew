"""The Context tab's "Prompt as sent": recorded by the runner, read by the owner only.

The read side is driven over a real crew log, so the join from the tab's turn ordinal
to the composer's turn number is the ``usage`` fold's own; the write side drives the
real runner with a provider whose delivery is the real ``EssentialDelivery``.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, drain_background_tasks
from crew_log_drain import assert_drained

from kiro_crew import prompt_trace
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
from kiro_crew.context import ContextBuilder
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.handlers.prompt_as_sent import api_prompt_as_sent
from kiro_crew.essential_delivery import EssentialDelivery
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.skills import SkillsLoader

SLOT = "chat-prompt"
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    _floor_monkeypatch.setattr(prompt_trace, "_sessions", OrderedDict())
    _floor_monkeypatch.setattr(prompt_trace, "_total", 0)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    yield
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()


def _turn(unit: str, number: int, text: str) -> None:
    """One composed and completed turn of *unit*, sent as *text*."""
    crew_log_emit.on_context_composed(unit, number, blocks={"your_message": len(text)})
    crew_log_emit.on_turn_completed(unit, number, model="m", input_tokens=1, output_tokens=1)
    prompt_trace.record(unit, number, text)


def _app(user: str, request_app: str = "") -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id="")

    @web.middleware
    async def stamp(request, handler):
        request["app"] = request_app
        request["user"] = user
        return await handler(request)

    app.middlewares.append(stamp)
    app.router.add_get("/api/telemetry/prompt-as-sent", api_prompt_as_sent)
    return app


async def _get(turn: int, *, user: str = "local-app", request_app: str = ""):
    async with TestClient(TestServer(_app(user, request_app))) as client:
        resp = await client.get(f"/api/telemetry/prompt-as-sent?slot={SLOT}&turn={turn}")
        return resp.status, await resp.json()


@pytest.fixture
def sel_calls(monkeypatch):
    import kiro_crew.sel as sel_mod

    calls: list[dict] = []
    monkeypatch.setattr(
        sel_mod, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: calls.append(kw))
    )
    return calls


@pytest.mark.asyncio
async def test_the_owner_reads_the_selected_turn_with_secrets_masked(sel_calls):
    crew_log_emit.on_session_opened("unit-a", slot=SLOT)
    _turn("unit-a", 4, f"rules first, then the ask: use {TOKEN}")
    assert_drained()
    # The tab's turn 1 is the composer's turn 4: joined through the fold.
    status, body = await _get(1)
    assert status == 200
    assert body["text"] == "rules first, then the ask: use [REDACTED: credential]"
    assert (await _get(2))[1]["code"] == "not_kept"


@pytest.mark.asyncio
async def test_a_non_owner_session_and_an_app_are_refused(sel_calls):
    crew_log_emit.on_session_opened("unit-a", slot=SLOT)
    _turn("unit-a", 1, "private words")
    assert_drained()
    status, body = await _get(1, user="presigned-link-guest")
    assert (status, body["code"]) == (403, "owner_only")
    status, body = await _get(1, request_app="acme-app")
    assert (status, body["code"]) == (404, "not_found")
    assert any(c["caller"] == "acme-app" and c["outcome"] == "denied" for c in sel_calls)


@pytest.mark.asyncio
async def test_an_earlier_session_never_shows_a_later_one_with_the_same_turn_number(sel_calls):
    crew_log_emit.on_session_opened("unit-a", slot=SLOT)
    _turn("unit-a", 2, "first conversation")
    # The slot starts over on a new session, whose turn numbers start again.
    crew_log_emit.on_session_opened("unit-b", slot=SLOT)
    _turn("unit-b", 2, "second conversation")
    assert_drained()
    assert (await _get(2))[1]["text"] == "second conversation"
    assert (await _get(1))[1]["code"] == "not_kept"


def _runner_state(tmp_path, monkeypatch):
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "workspace"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    state = _make_state(tmp_path, context_builder=builder)
    state.context_builder.build_message = MagicMock(return_value=("the prompt", None))
    state.context_builder.ensure_store = AsyncMock(return_value=object())
    provider = MagicMock()
    provider.session_id = "unit-runner"
    provider.essential_delivery = EssentialDelivery()

    async def send(message):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done.")
        yield LLMEvent(kind=EVENT_COMPLETE)

    def stream(message, *args, **kwargs):
        return provider.essential_delivery.stream(message, send, lambda: "incarnation")

    provider.stream = stream
    state.sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    state.sessions.consume_replay_suppression = MagicMock(return_value=False)
    state.sessions.consume_needs_reinjection = MagicMock(return_value=False)
    state.sessions.record_failure = AsyncMock()
    monkeypatch.setattr(chat_runner, "title_then_refresh", AsyncMock())
    monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock())
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("memory_mode", "key_says_restricted"),
    [("persistent", False), ("incognito", True), ("incognito", False)],
)
async def test_the_runner_keeps_a_turn_and_never_a_restricted_ones(
    tmp_path, monkeypatch, memory_mode, key_says_restricted
):
    state = _runner_state(tmp_path, monkeypatch)
    # A channel-born slot's effective key can miss the key-based check: the slot's
    # own mode still refuses it.
    monkeypatch.setattr(chat_runner, "is_restricted_session_key", lambda *_a: key_says_restricted)
    slot = state.get_or_create_slot(SLOT)
    slot.memory_mode = memory_mode
    slot.append("user", "the prompt")
    await asyncio.wait_for(chat_runner._run_chat(state, slot, "the prompt"), 30)
    await asyncio.wait_for(drain_background_tasks(state), 30)
    kept = [text for _, text in prompt_trace._sessions.get("unit-runner", [])]
    assert kept == (["the prompt"] if memory_mode == "persistent" else [])


@pytest.mark.asyncio
async def test_a_prompt_over_the_cap_answers_too_long_and_keeps_no_text(sel_calls, caplog):
    crew_log_emit.on_session_opened("unit-a", slot=SLOT)
    _turn("unit-a", 1, "kept words")
    with caplog.at_level("INFO", logger="kiro_crew.prompt_trace"):
        _turn("unit-a", 2, "y" * (prompt_trace.MAX_PROMPT_CHARS + 1))
    assert_drained()
    status, body = await _get(2)
    assert (status, body["code"]) == (404, "too_long")
    assert (await _get(1))[1]["text"] == "kept words"
    # The marker holds a turn slot and no characters.
    assert prompt_trace._total == len("kept words")
    lines = [r.getMessage() for r in caplog.records if r.name == "kiro_crew.prompt_trace"]
    assert len(lines) == 1 and "turn 2 of unit-a" in lines[0] and "yyy" not in lines[0]


def test_naming_a_turn_again_drops_the_earlier_attempt_before_it_sends():
    prompt_trace.record("unit-b", 2, "turn two")
    prompt_trace.record("unit-b", 3, "first attempt")
    prompt_trace.record("unit-c", 1, "another session")
    # A regenerate takes turn 3 again and then fails before it reaches the send.
    prompt_trace.forget_from("unit-b", 3)
    assert prompt_trace.read("unit-b", 3) is None
    assert prompt_trace.read("unit-b", 2) == "turn two"
    assert prompt_trace._total == len("turn two") + len("another session")
    # Dropping turns is not a write: unit-b stays the least recently written.
    assert list(prompt_trace._sessions) == ["unit-b", "unit-c"]
    prompt_trace.forget_from("unit-b", 1)
    assert list(prompt_trace._sessions) == ["unit-c"]
    assert prompt_trace._total == len("another session")


def test_evicting_a_session_logs_which_cap_without_its_text(monkeypatch, caplog):
    monkeypatch.setattr(prompt_trace, "MAX_SESSIONS", 2)
    prompt_trace.record("unit-a", 1, "oldest words")
    prompt_trace.record("unit-b", 1, "middle words")
    with caplog.at_level("INFO", logger="kiro_crew.prompt_trace"):
        prompt_trace.record("unit-c", 1, "newest words")
    assert list(prompt_trace._sessions) == ["unit-b", "unit-c"]
    lines = [r.getMessage() for r in caplog.records if r.name == "kiro_crew.prompt_trace"]
    assert lines == ["prompt_trace: unit-a no longer kept: over MAX_SESSIONS"]


@pytest.mark.asyncio
async def test_a_slash_command_at_a_rewound_turn_number_leaves_no_older_text(tmp_path, monkeypatch):
    state = _runner_state(tmp_path, monkeypatch)
    sent: list[str] = []

    async def stream_command(command):
        sent.append(command)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Listed.")
        yield LLMEvent(kind=EVENT_COMPLETE)

    provider, _new, _resumed = await state.sessions.get_or_create()
    provider.stream_command = stream_command
    # Kiro CLI runs its own commands, so the runner forwards this one to it.
    monkeypatch.setattr(
        chat_runner, "_slash_command_backend", AsyncMock(return_value=ACP_BACKEND_KIRO)
    )
    slot = state.get_or_create_slot(SLOT)
    slot.memory_mode = "persistent"
    slot.append("user", "the prompt")
    await asyncio.wait_for(chat_runner._run_chat(state, slot, "the prompt"), 30)
    await asyncio.wait_for(drain_background_tasks(state), 30)
    [(turn, kept)] = prompt_trace._sessions["unit-runner"]
    assert kept == "the prompt"
    # A rewind back to before that turn, then a slash command takes its number.
    del slot.messages[:]
    slot.append("user", "/tools")
    await asyncio.wait_for(chat_runner._run_chat(state, slot, "/tools"), 30)
    await asyncio.wait_for(drain_background_tasks(state), 30)
    assert sent == ["/tools"]
    assert prompt_trace.read("unit-runner", turn) is None
