"""The runner starts a first message's embedding before its session-start build.

The session-start build ranks the memory it starts with against the first message,
and that message's embedding is the slow step of the build. ``_run_chat`` starts it
early, on the recall pool: before ``get_or_create`` when the turn has to start the
session itself (so it runs beside ``session/new``), and again just before every
session-start build (so the build only ever joins it and waits within its
deadline). On the real embedder that second start is a cache hit, or joins the
first inference. A message the turn rewrites before its build (an ``@prompt``
body, drained notices or context put in front of it) is embedded only there.

Each test drives the REAL runner with a mocked session manager and builder, and a
fake ``embed_startup_query`` that records what it was asked to embed. Ordering is
proved by signals, not durations: a session start, or a build, that may only finish
once the embedding has begun. The ``wait_for`` bounds only name a hang.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state, drain_background_tasks

from kiro_crew.config.memory_sections import STARTUP_RECALL_TIMEOUT_MAX_SECS
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard import chat_runner
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.skills import SkillsLoader

MESSAGE = "Summarize the deploy checklist."


def _turn_state(tmp_path, monkeypatch, *, registered: bool, is_new=True, resumed=False):
    """A DashboardState whose session manager, provider and builder are mocks.

    *registered* is what ``has_session`` answers before the claim: False is a turn
    that starts its session itself, True one whose session an eager spawn (or an
    earlier turn) already registered.
    """
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "workspace"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    state = _make_state(tmp_path, context_builder=builder)
    state.context_builder.build_message = MagicMock(return_value=("task", None))
    state.context_builder.ensure_store = AsyncMock(return_value=object())
    provider = MagicMock()

    async def stream(*args, **kwargs):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done.")
        yield LLMEvent(kind=EVENT_COMPLETE)

    provider.stream = stream
    state.sessions.get_or_create = AsyncMock(return_value=(provider, is_new, resumed))
    # ``sessions`` is a MagicMock, so every predicate below would otherwise read
    # as a truthy mock.
    state.sessions.has_session = MagicMock(return_value=registered)
    state.sessions.resumable_hint = MagicMock(return_value=False)
    state.sessions.consume_replay_suppression = MagicMock(return_value=False)
    state.sessions.consume_needs_reinjection = MagicMock(return_value=False)
    state.sessions.record_failure = AsyncMock()
    monkeypatch.setattr(chat_runner, "title_then_refresh", AsyncMock())
    monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock())
    return state, provider


def _record_embeds(monkeypatch, on_embed=None) -> list[str]:
    """Replace the early embed with a recorder; *on_embed* runs on its worker thread."""
    embedded: list[str] = []

    def fake_embed(builder, text, *, workspace, memory_store):
        embedded.append(text)
        if on_embed is not None:
            on_embed()
        return True

    monkeypatch.setattr(chat_runner, "embed_startup_query", fake_embed)
    return embedded


async def _turn(state, slot_name: str, message: str = MESSAGE) -> None:
    slot = state.get_or_create_slot(slot_name)
    slot.append("user", message)
    await asyncio.wait_for(chat_runner._run_chat(state, slot, message), 60)
    await asyncio.wait_for(drain_background_tasks(state), 30)


@pytest.mark.asyncio
async def test_a_cold_start_embeds_the_message_while_the_session_starts(tmp_path, monkeypatch):
    """Nothing registered: the embed starts before ``get_or_create`` returns."""
    state, provider = _turn_state(tmp_path, monkeypatch, registered=False)
    loop = asyncio.get_running_loop()
    embedding = asyncio.Event()
    embedded = _record_embeds(monkeypatch, lambda: loop.call_soon_threadsafe(embedding.set))

    async def starting_session(*args, **kwargs):
        # The session finishes starting only once the embedding has begun, so a
        # runner that embeds after startup never gets past this line.
        await asyncio.wait_for(embedding.wait(), 30)
        return provider, True, False

    state.sessions.get_or_create = AsyncMock(side_effect=starting_session)
    await _turn(state, "cold-start")

    # Beside session/new, then again just ahead of the build, which the real
    # embedder answers from its cache or by joining the first inference.
    assert embedded == [MESSAGE, MESSAGE]
    state.context_builder.build_message.assert_called_once()
    assert state.context_builder.build_message.call_args.args[:2] == (MESSAGE, True)


@pytest.mark.asyncio
async def test_a_cold_start_embeds_only_the_text_its_build_reads(tmp_path, monkeypatch):
    """An ``@prompt`` body replaces the message after the claim: only it is embedded."""
    state, _provider = _turn_state(tmp_path, monkeypatch, registered=False)
    embedded = _record_embeds(monkeypatch)
    expanded = "Walk the release checklist one step at a time."

    async def expand(message, _state, _slot, *, project=None):
        return expanded, "ok"

    monkeypatch.setattr(chat_runner, "_expand_prompt_mention_off_loop", expand)
    await _turn(state, "rewritten", "@release-checklist")

    state.context_builder.build_message.assert_called_once()
    assert state.context_builder.build_message.call_args.args[:2] == (expanded, True)
    assert embedded == [expanded]


@pytest.mark.parametrize(
    "message, failures, pending, rewritten",
    [
        pytest.param(MESSAGE, [], [], False, id="plain"),
        pytest.param("@release-checklist", [], [], True, id="prompt-mention"),
        pytest.param(MESSAGE, ["[sub-agent result timed out]"], [], True, id="drained-failures"),
        pytest.param(
            MESSAGE, [], [{"source": "app", "content": "a note"}], True, id="pending-context"
        ),
    ],
)
def test_a_message_the_turn_rewrites_is_recognised_before_the_claim(
    message, failures, pending, rewritten
):
    slot = SimpleNamespace(_pending_subagent_failures=failures, _pending_context=pending)
    assert chat_runner._message_rewritten_before_build(slot, message) is rewritten


def test_the_early_embed_outlasts_the_longest_wait_a_build_can_join() -> None:
    """A queued early embed is dropped at its own deadline, and a build that joined
    it would then rank by keyword however much of its own wait was left."""
    assert chat_runner._STARTUP_QUERY_PREWARM_SECS > STARTUP_RECALL_TIMEOUT_MAX_SECS


@pytest.mark.asyncio
async def test_a_prewarmed_session_embeds_the_message_before_its_build(tmp_path, monkeypatch):
    """A session an eager spawn registered: the embed starts before the build."""
    state, _provider = _turn_state(tmp_path, monkeypatch, registered=True)
    entered = threading.Event()
    embedded = _record_embeds(monkeypatch, entered.set)
    built: list[bool] = []

    def build(*args, **kwargs):
        # On the embed pool. The early embed runs on the recall pool, so the build
        # can wait for it without holding the worker that embed needs.
        built.append(entered.wait(30))
        return ("task", None)

    state.context_builder.build_message = MagicMock(side_effect=build)
    await _turn(state, "prewarmed")

    assert built == [True], "the build ran without an early embed of its first message"
    assert embedded == [MESSAGE]


@pytest.mark.parametrize(
    "registered, resumable, is_new, resumed",
    [
        pytest.param(True, False, False, False, id="warm-turn"),
        pytest.param(True, False, True, True, id="natively-resumed"),
        pytest.param(False, True, True, True, id="resumable-cold-start"),
    ],
)
@pytest.mark.asyncio
async def test_a_turn_that_builds_no_session_start_memory_embeds_nothing(
    tmp_path, monkeypatch, registered, resumable, is_new, resumed
):
    """No session-start build, or one that restores its transcript: no early embed."""
    state, _provider = _turn_state(
        tmp_path, monkeypatch, registered=registered, is_new=is_new, resumed=resumed
    )
    state.sessions.resumable_hint = MagicMock(return_value=resumable)
    embedded = _record_embeds(monkeypatch)
    await _turn(state, "no-startup-memory")

    # The positive signal first: the turn reached its build, and every task it
    # started has finished, so an early embed would already be recorded.
    state.context_builder.build_message.assert_called_once()
    assert embedded == []


_PERSISTENT = SimpleNamespace(member_id=None, memory_mode="persistent")


@pytest.mark.parametrize(
    "builder, text, slot_mode, execution, applies",
    [
        pytest.param(object(), MESSAGE, "persistent", None, True, id="persistent-chat"),
        pytest.param(object(), MESSAGE, "persistent", _PERSISTENT, True, id="v1-execution"),
        pytest.param(object(), MESSAGE, "temporary", None, False, id="temporary-chat"),
        pytest.param(
            object(),
            MESSAGE,
            "persistent",
            SimpleNamespace(member_id=None, memory_mode="temporary"),
            False,
            id="temporary-execution",
        ),
        pytest.param(
            object(),
            MESSAGE,
            "persistent",
            SimpleNamespace(member_id="mate-1", memory_mode="persistent"),
            False,
            id="private-member",
        ),
        pytest.param(object(), "   ", "persistent", None, False, id="blank-message"),
        pytest.param(None, MESSAGE, "persistent", None, False, id="no-builder"),
    ],
)
def test_the_gate_skips_turns_whose_build_reads_no_learned_memory(
    tmp_path, builder, text, slot_mode, execution, applies
):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("gated")
    slot.memory_mode = slot_mode
    assert chat_runner._startup_query_prewarm_applies(builder, slot, text, execution) is applies
