"""A single-agent cron turn matches skill triggers on the job's own prompt.

When a job carries acknowledged items, the cron turn appends Kiro Crew's own
``[User has seen and acknowledged ALL of the following ...]`` list to the prompt
before ``build_message``. Unless ``build_message`` gets ``skill_trigger_text``,
triggers match that whole text, so a skill whose trigger appears only in the
appended list loads into the run whatever the job asked for.

Each test drives the real cron callback with the builder's ``build_message``
recorded, then renders the recorded call through a real ``ContextBuilder`` with
such a skill installed, so the assertion is on the assembled prompt text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import _make_orchestrator, _mock_dashboard_state, _mock_sessions

from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

pytestmark = pytest.mark.usefixtures("close_skills_loaders")

#: The skill body; its presence in the prompt means the skill was selected.
_BODY = "ACKED-ONLY PROCEDURE: never selected by the acknowledged-items list."
#: A trigger whose words appear only in the acknowledged item below.
_TRIGGER = "rotate certificates"
#: The job's own prompt, which does not name the trigger.
_PROMPT = "summarize yesterday's deploys"
#: An item the user already acknowledged, naming the trigger.
_ACKED = "rotate certificates on the staging fleet"


def _builder(tmp_path: Path) -> ContextBuilder:
    skills = tmp_path / "skills"
    target = skills / "acked-only"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        "---\nname: acked-only\ndescription: Selected only by an acknowledged item\n"
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


def _job(prompt: str, acked: list[str]) -> MagicMock:
    job = MagicMock()
    job.execution_context = None
    job.member_id = ""
    job.memory_store = ""
    job.script = ""
    job.command = ""
    job.id = "j1"
    job.name = "test-job"
    job.message = prompt
    job.persistent_session = True
    job.agent_sequence = []
    job.agent_id = None
    job.channel = ""
    job.created_by = "U1"
    job.approval_mode = "auto"
    job.env = None
    job.acked_items = acked
    job.minimal_context = False
    job.silent = False
    job.thread_ts = None
    job.last_posted_hash = ""
    job.consecutive_dupes = 0
    job.last_posted_at = 0.0
    job.last_failure_hash = ""
    job.last_failure_at = 0.0
    job.consecutive_failures = 0
    return job


async def _cron_call(prompt: str, acked: list[str]) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Run one single-agent cron job and return its ``build_message`` call."""
    orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
    orch.sessions = _mock_sessions()
    orch.ctx_builder = MagicMock()
    orch.ctx_builder.build_message = MagicMock(return_value=("full msg", None))
    orch.ctx_builder.hooks = MagicMock()
    orch.subagent_mgr = MagicMock()
    orch.subagent_mgr.running = []
    orch.dashboard_state = _mock_dashboard_state()
    orch.slack = MagicMock()
    orch.slack.open_dm = AsyncMock(return_value="D_U1")
    orch.slack.post_blocks = AsyncMock(return_value="ts1")
    orch.slack.post_message = AsyncMock()

    with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
        mock_cs_inst = MagicMock()
        mock_cs_inst.start = AsyncMock()
        mock_cs_inst.start_reaper = MagicMock()
        mock_cs_inst.register_active_session_key = MagicMock()
        mock_cs_inst.clear_active_session_key = MagicMock()
        mock_cs.return_value = mock_cs_inst
        mock_cs.create = AsyncMock(return_value=mock_cs_inst)
        await orch._init_cron()
    callback = mock_cs.create.call_args[1]["on_job"]

    with (
        patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="cron result",
        ),
        patch(
            "kiro_crew.slack.gateway.build_cron_session_context",
            return_value=("cron:j1", prompt),
        ),
    ):
        await callback(_job(prompt, acked))

    assert orch.ctx_builder.build_message.call_args is not None, "build_message was never reached"
    args, kwargs = orch.ctx_builder.build_message.call_args
    return args, kwargs


def _render(builder: ContextBuilder, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    rendered, _ = builder.build_message(
        args[0],
        args[1],
        None,
        skill_trigger_text=kwargs.get("skill_trigger_text"),
    )
    return rendered


@pytest.mark.asyncio
async def test_the_acked_list_alone_would_trigger_the_skill(tmp_path: Path) -> None:
    """Control: matched as a whole, the delivered prompt selects the skill."""
    args, _ = await _cron_call(_PROMPT, [_ACKED])
    assert _ACKED in args[0], "the acknowledged item must reach the delivered prompt"
    rendered, _ = _builder(tmp_path).build_message(args[0], args[1], None)
    assert _BODY in rendered


@pytest.mark.asyncio
async def test_cron_triggers_read_the_jobs_prompt(tmp_path: Path) -> None:
    args, kwargs = await _cron_call(_PROMPT, [_ACKED])
    assert kwargs.get("skill_trigger_text") == _PROMPT
    assert _BODY not in _render(_builder(tmp_path), args, kwargs)


@pytest.mark.asyncio
async def test_a_prompt_naming_the_trigger_still_loads_the_skill(tmp_path: Path) -> None:
    prompt = "rotate certificates on the production fleet"
    args, kwargs = await _cron_call(prompt, [_ACKED])
    assert kwargs.get("skill_trigger_text") == prompt
    assert _BODY in _render(_builder(tmp_path), args, kwargs)


@pytest.mark.asyncio
async def test_a_job_with_no_acked_items_matches_its_prompt(tmp_path: Path) -> None:
    args, kwargs = await _cron_call(_PROMPT, [])
    assert args[0] == _PROMPT
    assert kwargs.get("skill_trigger_text") == _PROMPT
    assert _BODY not in _render(_builder(tmp_path), args, kwargs)
