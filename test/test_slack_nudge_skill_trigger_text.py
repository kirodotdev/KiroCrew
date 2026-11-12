"""A Slack-thread auto-nudge matches skill triggers on the loop's own message.

``_fire_slack_inner_nudge`` puts Kiro Crew's ``[auto-nudge cycle N]`` header
(and, on a capped loop, the patrol budget line) before the loop's message.
Unless ``build_message`` gets ``skill_trigger_text``, triggers match that whole
text, so a skill whose trigger is a word of the header loads into every cycle.

Each test drives the real nudge with ``run_in_embed_pool`` swapped for a
recorder, then renders the recorded call through a real builder with such a
skill installed, so the assertion is on the assembled prompt text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from slack_gateway_coverage_helpers import _loop, _slack_nudge_orchestrator

import kiro_crew.slack.gateway as gw
from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

pytestmark = pytest.mark.usefixtures("close_skills_loaders")

#: The skill body; its presence in the prompt means the skill was selected.
_BODY = "HEADER-ONLY PROCEDURE: never selected by the nudge cycle header."
#: Words of the cycle header that the loop message below never uses.
_TRIGGER = "nudge cycle"
#: The loop's own message, which does not name the trigger.
_MESSAGE = "check the deploy queue"


class _Recorder:
    """Stands in for ``run_in_embed_pool``: keeps the ``build_message`` call."""

    def __init__(self) -> None:
        self.args: tuple[Any, ...] = ()
        self.kwargs: dict[str, Any] = {}

    async def __call__(self, _fn: Any, *args: Any, **kwargs: Any) -> tuple[str, None]:
        self.args, self.kwargs = args, kwargs
        return "MSG", None


def _builder(tmp_path: Path) -> ContextBuilder:
    skills = tmp_path / "skills"
    target = skills / "header-only"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        "---\nname: header-only\ndescription: Selected only by a header word\n"
        f"triggers: {_TRIGGER}\n---\n{_BODY}\n",
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


def _render(builder: ContextBuilder, call: _Recorder, trigger_text: str | None) -> str:
    rendered, _ = builder.build_message(
        call.args[0], call.args[1], None, skill_trigger_text=trigger_text
    )
    return rendered


async def _nudge_call(
    monkeypatch: pytest.MonkeyPatch, message: str, wake_message: str | None = None
) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(gw, "run_in_embed_pool", recorder)
    monkeypatch.setattr(gw, "stream_and_collect", AsyncMock(return_value="done"))
    await _slack_nudge_orchestrator()._fire_slack_nudge(_loop(message=message), wake_message)
    assert recorder.args, "build_message was never reached"
    return recorder


@pytest.mark.asyncio
async def test_the_header_alone_loads_no_skill(monkeypatch, tmp_path):
    call = await _nudge_call(monkeypatch, _MESSAGE)
    # Control: matched as a whole, the delivered text selects the skill.
    assert _BODY in _render(_builder(tmp_path / "control"), call, None)
    assert call.kwargs.get("skill_trigger_text") == _MESSAGE
    assert _BODY not in _render(_builder(tmp_path), call, call.kwargs["skill_trigger_text"])


@pytest.mark.asyncio
async def test_a_message_naming_the_trigger_still_loads_the_skill(monkeypatch, tmp_path):
    call = await _nudge_call(monkeypatch, "report each nudge cycle you skipped")
    assert _BODY in _render(_builder(tmp_path), call, call.kwargs.get("skill_trigger_text"))


@pytest.mark.asyncio
async def test_a_monitor_wake_keeps_matching_its_whole_text(monkeypatch):
    # Nothing is prepended to a monitor wake, so its text is matched as before.
    call = await _nudge_call(monkeypatch, "keep checking", wake_message="[Monitor wake]")
    assert call.args[0] == "[Monitor wake]"
    assert call.kwargs.get("skill_trigger_text") is None
