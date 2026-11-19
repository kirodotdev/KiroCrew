"""A sub-agent turn the backend ends because the HOST refused a call resumes.

Codex offers one reject option for a shell command (``cancel``), and codex-rs
answers it by aborting the whole turn. opencode answers a reject with its stock
"user rejected permission" text and ends the turn ``end_turn`` with nothing
after it. The run sends the refusal as the next prompt on the same session, a
bounded number of times, so the model learns why. A turn with no host refusal,
one after a person's no, a clean turn the model went on with after the refusal,
and a clean turn on another harness, end as they ended.
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
    EVENT_TOOL_CALL,
    LLMEvent,
)
from kiro_crew.subagent import SubagentInfo, SubagentManager

pytestmark = pytest.mark.usefixtures("close_subagent_managers")

_REASON = "Blocked by security policy: heredoc body"
#: A permission request in a ``_Provider.script``.
_ASK = object()


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

    *cancel_turns* prompts each ask for a tool and end with *stop_reason*
    (``end_turn`` on opencode is opencode's shape); every later prompt answers
    and ends normally.
    """

    supports_refusal_steer = False

    def __init__(
        self, cancel_turns: int, stop_reason: str = "cancelled", *, opencode: bool = False
    ) -> None:
        self.is_opencode_backend = opencode
        self.prompts: list[str] = []
        self.rejected = 0
        #: Permission requests each cancelled turn makes before it ends.
        self.ask = 1
        #: Text the model writes after the refusal, before the turn ends.
        self.after = ""
        #: When set, what each refused turn streams instead, in order: ``_ASK``
        #: for a permission request, or an event.
        self.script: list | None = None
        self._cancel_turns = cancel_turns
        self._stop_reason = stop_reason
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
            for _ in range(self.ask if self.script is None else 0):
                yield _request()
            for item in self.script or ():
                yield _request() if item is _ASK else item
            if self.after:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=self.after)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=self._stop_reason)
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
        patch(
            "kiro_crew.subagent_manager.run.permission_pre_tool_block",
            AsyncMock(return_value=None),
        ),
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


@pytest.mark.asyncio
async def test_a_clean_turn_ended_on_a_host_refusal_resumes_with_the_reason():
    # opencode's shape: the reject closes the call with its stock text and the
    # turn ends ``end_turn`` with nothing after it.
    provider = _Provider(cancel_turns=1, stop_reason="end_turn", opencode=True)
    info = await _run(provider, ToolHookResult(action=TOOL_DENY, reason=_REASON))
    assert provider.rejected == 1
    assert len(provider.prompts) == 2, provider.prompts
    assert _REASON in provider.prompts[1]
    assert "done after rephrasing" in (info.result or "")
    info._tombstone.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_clean_turn_resumes_are_bounded():
    resume_max = deny_notice.REFUSED_TURN_RESUME_MAX
    provider = _Provider(cancel_turns=resume_max + 5, stop_reason="end_turn", opencode=True)
    await _run(provider, ToolHookResult(action=TOOL_DENY, reason=_REASON))
    assert len(provider.prompts) == resume_max + 1, provider.prompts


@pytest.mark.asyncio
async def test_a_clean_turn_the_model_went_on_with_is_not_resumed():
    # The model read the refusal and answered in the same turn: its answer stands.
    provider = _Provider(cancel_turns=1, stop_reason="end_turn", opencode=True)
    provider.after = "I will do this step myself."
    info = await _run(provider, ToolHookResult(action=TOOL_DENY, reason=_REASON))
    assert len(provider.prompts) == 1, provider.prompts
    assert "I will do this step myself." in (info.result or "")


def _tool_call() -> LLMEvent:
    return LLMEvent(  # type: ignore[call-arg]
        kind=EVENT_TOOL_CALL, title="Read file", tool_call_id="t2", tool_name="fs_read"
    )


@pytest.mark.asyncio
async def test_a_clean_turn_the_model_went_on_with_a_tool_call_is_not_resumed():
    # The model moved on to another tool after the refusal: no text, but progress.
    provider = _Provider(cancel_turns=1, stop_reason="end_turn", opencode=True)
    provider.script = [_ASK, _tool_call()]
    await _run(provider, ToolHookResult(action=TOOL_DENY, reason=_REASON))
    assert len(provider.prompts) == 1, provider.prompts


@pytest.mark.asyncio
async def test_a_later_refusal_left_unanswered_still_resumes():
    # Progress after the first refusal does not cover the second one, which
    # the turn then ended on.
    provider = _Provider(cancel_turns=1, stop_reason="end_turn", opencode=True)
    provider.script = [_ASK, _tool_call(), _ASK]
    info = await _run(provider, ToolHookResult(action=TOOL_DENY, reason=_REASON))
    assert provider.rejected == 2
    assert len(provider.prompts) == 2, provider.prompts
    assert _REASON in provider.prompts[1]
    assert "done after rephrasing" in (info.result or "")


@pytest.mark.asyncio
async def test_a_clean_turn_on_another_harness_is_not_resumed():
    # Other harnesses go on after a reject, so a clean ending there is the
    # model's own; only opencode ends the turn on the reject itself.
    provider = _Provider(cancel_turns=1, stop_reason="end_turn")
    await _run(provider, ToolHookResult(action=TOOL_DENY, reason=_REASON))
    assert len(provider.prompts) == 1, provider.prompts
