"""A sub-agent turn the backend cancels because the HOST refused a call resumes.

Codex offers one reject option for a shell command (``cancel``), and codex-rs
answers it by aborting the whole turn. The run sends the refusal as the next
prompt on the same session, a bounded number of times, so the model learns why.
A cancelled turn with no host refusal, or one after a person's no, ends
cancelled.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import deny_notice, subagent
from kiro_crew.agent_sdk.spec_hooks import TurnSpecHooks
from kiro_crew.execution_context import execution_for_store
from kiro_crew.hooks import TOOL_ALLOW, TOOL_DENY, ToolHookResult
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    LLMEvent,
)
from kiro_crew.subagent import SubagentInfo, SubagentManager

pytestmark = pytest.mark.usefixtures("close_subagent_managers")

_REASON = "Blocked by security policy: heredoc body"


def _request() -> LLMEvent:
    return LLMEvent(  # type: ignore[call-arg]
        kind=EVENT_PERMISSION_REQUEST,
        title="Run command",
        request_id="r1",
        tool_name="exec_command",
        is_shell=True,
        shell_classified=True,
        raw_params_trusted=True,
        raw_tool_params={"command": "cat <<EOF"},
    )


class _Provider:
    """Codex-shaped double: a refused call ends the turn ``cancelled``.

    *cancel_turns* prompts each ask for a tool and end cancelled; every later
    prompt answers and ends normally.
    """

    supports_refusal_steer = False

    def __init__(self, cancel_turns: int) -> None:
        self.prompts: list[str] = []
        self.rejected = 0
        #: Permission requests each cancelled turn makes before it ends.
        self.ask = 1
        self._cancel_turns = cancel_turns
        self.last_prompt_stats = None

    @property
    def cwd(self) -> str:
        return ""

    async def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def context_usage_pct(self) -> float:
        return 0.0

    async def stream(self, message: str):
        self.prompts.append(message)
        if len(self.prompts) <= self._cancel_turns:
            for _ in range(self.ask):
                yield _request()
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="cancelled")
            return
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done after rephrasing")
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    async def approve_tool(self, request_id) -> None:
        pass

    async def reject_tool(self, request_id) -> None:
        self.rejected += 1

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return MagicMock(name=name)


async def _run(provider: _Provider, hook: ToolHookResult) -> SubagentInfo:
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.get_approval_policy = MagicMock(return_value="ask")
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.release_subagent_runtime = AsyncMock()
    sessions._sessions = {}
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("msg", None))
    ctx.hooks.auto_approve_subagent_tools = False
    ctx.hooks.on_tool_call = MagicMock(return_value=hook)
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx, default_turn_limit=20)
    info = SubagentInfo(
        execution_context=execution_for_store(""),
        id="a1",
        task="t",
        parent_session_key="dashboard:chat-1",
    )
    manager._agents[info.id] = info
    manager._log_spawned(info)
    with (
        patch.object(subagent, "Stats"),
        patch.object(subagent, "sel", MagicMock()),
        patch.object(subagent, "update_state"),
        patch.object(subagent, "create_agent_folder", MagicMock()),
        patch.object(
            subagent,
            "turn_spec_hooks",
            AsyncMock(return_value=TurnSpecHooks([], None, False, False)),
        ),
        patch("kiro_crew.subagent_manager.run.permission_pre_tool_block", AsyncMock()),
        patch.object(manager, "_write_tombstone") as tombstone,
    ):
        await asyncio.wait_for(manager._run_inner(info, "subagent:a1"), 30)
    info._tombstone = tombstone  # type: ignore[attr-defined]
    return info


@pytest.mark.asyncio
async def test_a_turn_cancelled_by_a_host_refusal_resumes_with_the_reason():
    provider = _Provider(cancel_turns=1)
    info = await _run(provider, ToolHookResult(action=TOOL_DENY, reason=_REASON))
    assert provider.rejected == 1
    assert len(provider.prompts) == 2, provider.prompts
    resume = provider.prompts[1]
    assert _REASON in resume and "Run command" in resume
    assert "ended your previous turn" in resume
    assert "done after rephrasing" in (info.result or "")
    info._tombstone.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_resumes_are_bounded():
    # Read at call time, so a run without the fix fails here, not at collection.
    resume_max = deny_notice.REFUSED_TURN_RESUME_MAX
    provider = _Provider(cancel_turns=resume_max + 5)
    info = await _run(provider, ToolHookResult(action=TOOL_DENY, reason=_REASON))
    assert len(provider.prompts) == resume_max + 1
    info._tombstone.assert_called_once()  # type: ignore[attr-defined]
    assert info._tombstone.call_args.args[1] == "cancelled"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_cancelled_turn_without_a_host_refusal_is_not_resumed():
    provider = _Provider(cancel_turns=1)
    provider.ask = 0
    info = await _run(provider, ToolHookResult(action=TOOL_ALLOW))
    assert len(provider.prompts) == 1
    assert provider.rejected == 0
    info._tombstone.assert_called_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_persons_no_after_a_host_refusal_is_not_resumed():
    # The host refuses one call, then a person rejects the next one in the same
    # turn. The person's answer stands: the cancelled ending is not resumed.
    from kiro_crew import tool_permission

    answers = [
        tool_permission.Settled("refused", "hook_deny", "host", "policy", _REASON),
        tool_permission.Settled("rejected", "interactive_rejected", "person"),
    ]
    provider = _Provider(cancel_turns=1)
    provider.ask = 2
    with patch.object(tool_permission, "settle", AsyncMock(side_effect=answers)):
        info = await _run(provider, ToolHookResult(action=TOOL_DENY, reason=_REASON))
    assert len(provider.prompts) == 1, provider.prompts
    info._tombstone.assert_called_once()  # type: ignore[attr-defined]
