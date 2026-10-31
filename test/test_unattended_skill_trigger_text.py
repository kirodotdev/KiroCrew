"""Heartbeat and Slack auto-nudge turns match skill triggers on their own text.

Both turns prepend Kiro Crew's words before ``build_message``: the heartbeat
puts the HEARTBEAT_KEEP reminder ahead of every task, and a Slack auto-nudge
puts the ``[auto-nudge cycle N]`` header (and, on a capped loop, the patrol
budget line) ahead of the loop's message. Unless ``build_message`` gets
``skill_trigger_text``, triggers match that whole text, so a skill whose
trigger is a word of the prefix loads into every such turn whatever it asks.

Each test drives the real turn with ``run_in_embed_pool`` swapped for a
recorder, then renders the recorded call through a real builder with such a
skill installed, so the assertion is on the assembled prompt text.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_gateway_coverage_helpers import _loop, _make_orchestrator

import kiro_crew.slack.gateway as gw
from kiro_crew.autonudge_service.model import nudge_cycle_header
from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader
from kiro_crew.slack.gateway_runtime.heartbeat_turns import _HEARTBEAT_KEEP_INJECTION

pytestmark = pytest.mark.usefixtures("close_skills_loaders")

#: The skill body; its presence in the prompt means the skill was selected.
_BODY = "PREFIX-ONLY PROCEDURE: never selected by Kiro Crew's own words."
#: Words of the heartbeat reminder that no task below uses.
_HEARTBEAT_TRIGGER = "heartbeat keyword"
#: Words of the nudge cycle header that no loop message below uses.
_NUDGE_TRIGGER = "nudge cycle"


class _Recorder:
    """Stands in for ``run_in_embed_pool``: keeps the ``build_message`` call."""

    def __init__(self) -> None:
        self.args: tuple[Any, ...] = ()
        self.kwargs: dict[str, Any] = {}

    async def __call__(self, _fn: Any, *args: Any, **kwargs: Any) -> tuple[str, None]:
        self.args, self.kwargs = args, kwargs
        return "MSG", None


def _builder(tmp_path: Path, trigger: str) -> ContextBuilder:
    skills = tmp_path / "skills"
    target = skills / "prefix-only"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        "---\nname: prefix-only\ndescription: Selected only by a prefix word\n"
        f"triggers: {trigger}\n---\n{_BODY}\n",
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


def _render(builder: ContextBuilder, call: _Recorder) -> str:
    rendered, _ = builder.build_message(
        call.args[0],
        call.args[1],
        None,
        skill_trigger_text=call.kwargs.get("skill_trigger_text"),
    )
    return rendered


# ── heartbeat ────────────────────────────────────────────────────────────


def _heartbeat_orchestrator() -> Any:
    orch = gw.GatewayOrchestrator.__new__(gw.GatewayOrchestrator)
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    sessions.reset = AsyncMock()
    sessions.release = MagicMock()
    orch.sessions = sessions
    ctx_builder = MagicMock()
    ctx_builder.hooks = MagicMock()
    # The heartbeat's store resolver reads the builder's conversation log, and an
    # auto-created mock attribute is not one.
    ctx_builder.conversation_log = None
    orch.ctx_builder = ctx_builder
    orch.consolidator = None
    orch._heartbeat_approval = AsyncMock(return_value=True)
    orch._deliver_result = AsyncMock()
    return orch


async def _heartbeat_call(monkeypatch: pytest.MonkeyPatch, task: str) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(gw, "run_in_embed_pool", recorder)
    monkeypatch.setattr(gw, "stream_and_collect", AsyncMock(return_value="done"))
    started: dict[str, Any] = {}

    async def _fake_start(self: Any) -> None:
        started["on_task"] = self._on_task

    monkeypatch.setattr(gw.HeartbeatService, "start", _fake_start)
    await _heartbeat_orchestrator()._init_heartbeat()
    await started["on_task"](task, "")
    assert recorder.args, "build_message was never reached"
    return recorder


def test_the_heartbeat_reminder_carries_the_trigger_words():
    # The heartbeat cases are meaningful only while the reminder holds these words.
    for word in _HEARTBEAT_TRIGGER.split():
        assert word in _HEARTBEAT_KEEP_INJECTION.lower()


@pytest.mark.asyncio
async def test_heartbeat_triggers_read_the_task(monkeypatch):
    task = "summarize yesterday's deploys"
    call = await _heartbeat_call(monkeypatch, task)
    assert call.args[0] == _HEARTBEAT_KEEP_INJECTION + task
    assert call.kwargs.get("skill_trigger_text") == task


@pytest.mark.asyncio
async def test_heartbeat_reminder_loads_no_skill(monkeypatch, tmp_path):
    call = await _heartbeat_call(monkeypatch, "summarize yesterday's deploys")
    assert _BODY not in _render(_builder(tmp_path, _HEARTBEAT_TRIGGER), call)


@pytest.mark.asyncio
async def test_heartbeat_task_naming_the_trigger_still_loads_the_skill(monkeypatch, tmp_path):
    call = await _heartbeat_call(monkeypatch, "check the heartbeat keyword list")
    assert _BODY in _render(_builder(tmp_path, _HEARTBEAT_TRIGGER), call)


# ── Slack auto-nudge ─────────────────────────────────────────────────────


def _slack_nudge_orchestrator() -> Any:
    orch = _make_orchestrator()
    sessions = MagicMock()
    sessions.is_busy = MagicMock(return_value=False)
    sessions.get_channel = MagicMock(return_value="C123")
    sessions.get_thread = MagicMock(return_value="111.222")
    sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    sessions.cancel_current = AsyncMock()
    sessions.release = MagicMock()
    orch.sessions = sessions
    orch.slack = MagicMock()
    orch.slack.post_message = AsyncMock()
    # A namespace, not a mock: the store resolver reads attributes a mock would
    # invent. ``build_message`` itself is reached through the recorder.
    orch.ctx_builder = SimpleNamespace(hooks=object(), build_message=None)
    orch.conv_log = None
    orch.autonudge_svc = None
    return orch


async def _nudge_call(
    monkeypatch: pytest.MonkeyPatch, message: str, wake_message: str | None = None
) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(gw, "run_in_embed_pool", recorder)
    monkeypatch.setattr(gw, "stream_and_collect", AsyncMock(return_value="done"))
    await _slack_nudge_orchestrator()._fire_slack_nudge(_loop(message=message), wake_message)
    assert recorder.args, "build_message was never reached"
    return recorder


def test_the_nudge_header_carries_the_trigger_words():
    # The nudge cases are meaningful only while the header holds these words.
    header = nudge_cycle_header(_loop())
    for word in _NUDGE_TRIGGER.split():
        assert word in header


@pytest.mark.asyncio
async def test_nudge_triggers_read_the_loop_message(monkeypatch):
    message = "check the deploy queue"
    call = await _nudge_call(monkeypatch, message)
    assert call.args[0].startswith(nudge_cycle_header(_loop()))
    assert call.kwargs.get("skill_trigger_text") == message


@pytest.mark.asyncio
async def test_nudge_header_loads_no_skill(monkeypatch, tmp_path):
    call = await _nudge_call(monkeypatch, "check the deploy queue")
    assert _BODY not in _render(_builder(tmp_path, _NUDGE_TRIGGER), call)


@pytest.mark.asyncio
async def test_nudge_message_naming_the_trigger_still_loads_the_skill(monkeypatch, tmp_path):
    call = await _nudge_call(monkeypatch, "report each nudge cycle you skipped")
    assert _BODY in _render(_builder(tmp_path, _NUDGE_TRIGGER), call)


@pytest.mark.asyncio
async def test_monitor_wake_keeps_matching_its_whole_text(monkeypatch):
    # Nothing is prepended to a monitor wake, so its text is matched as before.
    call = await _nudge_call(monkeypatch, "keep checking", wake_message="[Monitor wake]")
    assert call.args[0] == "[Monitor wake]"
    assert call.kwargs.get("skill_trigger_text") is None
