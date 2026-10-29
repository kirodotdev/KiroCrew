"""A completion the gateway injects into a channel or cron parent triggers no skill.

``_inject_subagent_into_channel_parent`` (a Slack, Telegram or Discord parent
with no dashboard tab) and ``_finish_subagent_cron_or_notify`` (a ``cron:``
parent) build the parent's turn from the completion announcement. That text
repeats the sub-agent's task and result: the parent's own work coming back, not
a person's request. The dashboard runner already passes ``""`` for a completion;
without it here, the matcher read the announcement like a message and injected
full skill bodies on every completion.

Each test drives the real route with ``run_in_embed_pool`` swapped for a
recorder, then renders the recorded call through a real builder with a skill
the announcement's words trigger, so the assertion is on the assembled prompt.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.slack.gateway as gw
from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader
from kiro_crew.subagent import SubagentInfo

pytestmark = pytest.mark.usefixtures("close_skills_loaders")

#: The skill body; its presence in the prompt means the skill was selected.
_BODY = "RELEASE CHECKLIST: tag, build, publish."
#: A trigger the announcement below carries.
_TRIGGER = "release checklist"
_ANNOUNCE = "[Subagent completion event] child-1 finished: ran the release checklist."

#: Ceiling for a wait that only fails on a hang, so a regression fails by name.
_HANG_CEILING_S = 30


class _Recorder:
    """Stands in for ``run_in_embed_pool``: keeps the ``build_message`` call."""

    def __init__(self) -> None:
        self.args: tuple[Any, ...] = ()
        self.kwargs: dict[str, Any] = {}

    async def __call__(self, _fn: Any, *args: Any, **kwargs: Any) -> tuple[str, None]:
        self.args, self.kwargs = args, kwargs
        return "MSG", None


def _gateway() -> Any:
    gateway = gw.GatewayOrchestrator.__new__(gw.GatewayOrchestrator)
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, True))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.cancel_current = AsyncMock()
    gateway.sessions = sessions
    # A namespace, not a mock: the store resolver reads attributes a mock would
    # invent. ``build_message`` itself is reached through the recorder.
    gateway.ctx_builder = SimpleNamespace(hooks=object(), build_message=None)
    gateway.conv_log = None
    gateway.dashboard_state = None
    gateway.slack = None
    gateway._owner_id = ""
    gateway._cron_injecting = {}
    gateway.cron_svc = None
    gateway.subagent_mgr = MagicMock()
    gateway.subagent_mgr.running = []
    gateway.subagent_mgr.queued_count_for_async = AsyncMock(return_value=0)
    gateway._channel_reply_link = MagicMock(return_value=None)
    gateway._deliver_channel_reply = AsyncMock()
    gateway._inject_with_retry = AsyncMock(return_value="reply")
    return gateway


async def _route_call(monkeypatch: pytest.MonkeyPatch, route: str) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(gw, "run_in_embed_pool", recorder)
    gateway = _gateway()
    key = "cron:job-1" if route == "cron" else "telegram:chat-1"
    info = SubagentInfo(id="child-1", task="run the release", parent_session_key=key)
    info.result = "done"
    info.done = True
    if route == "cron":
        # A sibling still running keeps the route's after-injection session
        # reset out of this test.
        gateway.subagent_mgr.running = [
            SubagentInfo(id="child-2", task="more", parent_session_key=key)
        ]
        inject = gateway._finish_subagent_cron_or_notify
    else:
        inject = gateway._inject_subagent_into_channel_parent
    with patch("kiro_crew.slack.gateway._MAX_INJECT_ATTEMPTS", 1):
        await asyncio.wait_for(
            inject(info, announce=_ANNOUNCE, body="b", title="t", parent_key=key),
            _HANG_CEILING_S,
        )
    assert recorder.args, "build_message was never reached"
    return recorder


def _builder(tmp_path: Path) -> ContextBuilder:
    skills = tmp_path / "skills"
    target = skills / "release"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        f"---\nname: release\ndescription: Cut a release\ntriggers: {_TRIGGER}\n---\n{_BODY}\n",
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


def test_the_announcement_alone_would_trigger_the_skill(tmp_path):
    # The cases below are meaningful only while matching the announcement
    # itself selects the skill.
    assert _BODY in _render(_builder(tmp_path), _ANNOUNCE, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["channel", "cron"])
async def test_completion_injection_triggers_no_skill(monkeypatch, tmp_path, route):
    call = await _route_call(monkeypatch, route)
    assert call.args[0] == _ANNOUNCE
    assert call.kwargs.get("skill_trigger_text") == ""
    rendered = _render(_builder(tmp_path), call.args[0], call.kwargs.get("skill_trigger_text"))
    assert _BODY not in rendered
