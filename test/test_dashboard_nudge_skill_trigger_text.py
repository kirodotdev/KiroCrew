"""A dashboard auto-nudge cycle matches skill triggers on the loop's own message.

``_fire_dashboard_nudge`` prepends ``[auto-nudge cycle N]``, a capped loop's
``[patrol budget: ...]`` line and any ledger news to the loop's message. Those
are Kiro Crew's words, but the runner matched skill triggers on the whole
turn, so a skill triggered by "patrol budget" loaded on every capped cycle of
a loop that never asked for it. The fire path now hands ``_run_chat`` the
loop's message as ``_skill_trigger_text``, and ``_turn_build_prompt`` passes
it on to ``build_message``.

The fire tests read the keyword the runner received and render it through a
real builder with a skill the header triggers, so the assertion is on the
assembled prompt. The runner tests drive the REAL runner with a mocked
``build_message`` and read the keyword it was handed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state, drain_background_tasks

from kiro_crew.autonudge import NudgeLoop
from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard import chat_runner
from kiro_crew.memory import MemoryStore
from kiro_crew.monitoring.models import MonitorState
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.skills import SkillsLoader
from kiro_crew.slack import gateway as gw
from kiro_crew.testing.wait import finished

pytestmark = pytest.mark.usefixtures("close_skills_loaders")

#: The skill body; its presence in the prompt means the skill was selected.
_BODY = "BUDGET PLAYBOOK: renew before the cap."
#: Words the capped cycle header carries and the loop's message does not.
_TRIGGER = "patrol budget"
_LOOP_MESSAGE = "check the PR"

#: Ceiling for a wait that only fails on a hang, so a regression fails by name.
_HANG_CEILING_S = 30


def _loop() -> NudgeLoop:
    # A cap makes the header carry its ``[patrol budget: ...]`` line.
    return NudgeLoop(
        id="loop-abc",
        slot_key="chat-1-1785",
        message=_LOOP_MESSAGE,
        idle_secs=300,
        max_cycles=24,
        cycle_count=3,
    )


def _slot() -> MagicMock:
    slot = MagicMock()
    slot.key = "chat-1-1785"
    slot.running = False
    slot.is_closing = False
    slot.mode = ""
    slot.memory_mode = "persistent"
    slot._last_turn_structural_terminal = False
    slot._last_turn_structural_terminal_loop_id = ""
    slot._last_turn_structural_terminal_loop_gen = 0
    return slot


def _orchestrator() -> gw.GatewayOrchestrator:
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch.dashboard_state = SimpleNamespace(
        get_slot=MagicMock(return_value=_slot()),
        push_slots_update=MagicMock(),
        _background_tasks=set(),
        run_background_turn=MagicMock(side_effect=lambda _slot, coro: coro),
    )
    orch.autonudge_svc = MagicMock()
    orch.autonudge_svc.remove = AsyncMock()
    orch.autonudge_svc.monitor_dispatch_is_authorized = AsyncMock(return_value=True)
    orch._session_tasks = {}
    return orch


async def _authorize_monitor_turn(*_args: Any, **kwargs: Any) -> None:
    hook = kwargs.get("monitor_completion")
    if hook is not None and await hook.authorize():
        hook.mark_accepted()


async def _fire(loop: NudgeLoop, wake_message: str | None = None) -> AsyncMock:
    """Fire *loop* and return the ``_run_chat`` stand-in the turn reached."""
    orch = _orchestrator()
    spawned: list[asyncio.Task[Any]] = []

    def _spawn(_state: Any, _slot: Any, coro: Any, **_kw: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        spawned.append(task)
        return task

    run_chat = AsyncMock(side_effect=_authorize_monitor_turn)
    with (
        patch.object(gw, "spawn_guarded_turn", _spawn),
        patch("kiro_crew.dashboard.chat._run_chat", new=run_chat),
    ):
        await asyncio.wait_for(orch._fire_dashboard_nudge(loop, wake_message), _HANG_CEILING_S)
        await asyncio.wait_for(asyncio.gather(*spawned), _HANG_CEILING_S)
    run_chat.assert_awaited()
    return run_chat


def _builder(tmp_path: Path) -> ContextBuilder:
    skills = tmp_path / "skills"
    target = skills / "budget"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        f"---\nname: budget\ndescription: Renew a budget\ntriggers: {_TRIGGER}\n---\n{_BODY}\n",
        encoding="utf-8",
    )
    # The matcher is off at the shipped cap of 0; this case is about what it
    # matches once an operator turns it on.
    loader = SkillsLoader(
        skills_path=skills,
        install_builtins=False,
        config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
    )
    return ContextBuilder(memory=MemoryStore(workspace=tmp_path / "ws"), skills=loader)


def _render(builder: ContextBuilder, text: str, trigger_text: str | None) -> str:
    rendered, _ = builder.build_message(text, False, None, skill_trigger_text=trigger_text)
    return rendered


@pytest.mark.asyncio
async def test_the_cycle_header_alone_would_trigger_the_skill(tmp_path):
    # The case below is meaningful only while the delivered turn, matched as a
    # whole, selects the skill.
    tagged = (await _fire(_loop())).await_args.args[2]
    assert _TRIGGER in tagged
    assert _BODY in _render(_builder(tmp_path), tagged, None)


@pytest.mark.asyncio
async def test_a_plain_nudge_matches_the_loops_own_message(tmp_path):
    call = (await _fire(_loop())).await_args
    assert call.kwargs["_skill_trigger_text"] == _LOOP_MESSAGE
    rendered = _render(_builder(tmp_path), call.args[2], call.kwargs["_skill_trigger_text"])
    assert _BODY not in rendered


@pytest.mark.asyncio
async def test_a_monitor_wake_matches_its_own_text():
    # A structured wake carries no header the fire path prepends: its text is
    # the wake itself, so the runner matches it as it does any turn.
    loop = _loop()
    loop.monitor = MonitorState(
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        created_ts=1_000.0,
        last_wake_fingerprint="failure-a",
        wake_in_flight=True,
    )
    call = (await _fire(loop, "[Monitor wake]")).await_args
    assert call.kwargs["_skill_trigger_text"] is None


SLOT_NAME = "nudge-chat"
TEXT = "[auto-nudge cycle 4]\n[patrol budget: cycle 4/24]\ncheck the PR"


def _turn_state(tmp_path, monkeypatch):
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
    state.sessions.get_or_create = AsyncMock(return_value=(provider, False, True))
    state.sessions.consume_replay_suppression = MagicMock(return_value=False)
    state.sessions.consume_needs_reinjection = MagicMock(return_value=False)
    state.sessions.record_failure = AsyncMock()
    monkeypatch.setattr(chat_runner, "title_then_refresh", AsyncMock())
    monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock())
    return state


async def _runner_trigger_text(tmp_path, monkeypatch, **run_kwargs) -> str:
    state = _turn_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot(SLOT_NAME)
    slot.append("user", TEXT)
    await asyncio.wait_for(chat_runner._run_chat(state, slot, TEXT, **run_kwargs), _HANG_CEILING_S)
    await finished(drain_background_tasks(state), "draining the background tasks", timeout=10)
    state.context_builder.build_message.assert_called()
    return state.context_builder.build_message.call_args.kwargs["skill_trigger_text"]


@pytest.mark.asyncio
async def test_the_runner_hands_build_message_the_loops_message(tmp_path, monkeypatch):
    text = await _runner_trigger_text(
        tmp_path,
        monkeypatch,
        _directive_user_origin=False,
        _directive_self_wake=True,
        _skill_trigger_text=_LOOP_MESSAGE,
    )
    assert text == _LOOP_MESSAGE


@pytest.mark.asyncio
async def test_without_one_the_runner_matches_the_turn_text(tmp_path, monkeypatch):
    text = await _runner_trigger_text(tmp_path, monkeypatch)
    assert text == TEXT
