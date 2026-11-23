"""A hook-gate refusal's audit row names the gate rule that refused the call.

``HookManager.judge`` stamps the ``GATE_TIERS`` row (or per-target rule) that
decided on ``ToolHookResult.tier``. Each surface's refusal row records it as
``metadata.rule_id``, the key ``failure_recurrence`` groups on, and never the
command it refused. A verdict no row decided adds no metadata.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from dashboard_approval_helpers import (
    _complete_event,
    _context_builder,
    _make_slot,
    _make_state,
    _patch_stats,
    _permission_event,
    _set_stream,
)

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig, ToolHookResult, hook_gate_kwargs
from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.sel import SecurityEventLog
from kiro_crew.tool_permission import Ask, HookGate, SubagentRows, TaskrunnerRows

_COMMAND = "rm -rf ./build"


def _verdict(tier: str) -> ToolHookResult:
    result = ToolHookResult(action=TOOL_DENY, reason=f"Blocked by security policy: {_COMMAND}")
    result.tier = tier
    return result


def _ask() -> Ask:
    event = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=f"Run: {_COMMAND}",
        tool_kind="execute",
        request_id="r1",
    )
    return Ask(event, None, "subagent:a1")


_CODECS = pytest.mark.parametrize(
    "codec",
    [SubagentRows("a1"), TaskrunnerRows("kirocrew", None, None)],
    ids=["subagent", "taskrunner"],
)


@_CODECS
def test_a_gate_refusal_row_names_the_rule_that_refused(codec):
    refusal = HookGate(lambda _event: _verdict("sensitive-bash")).judge(_ask())
    row = codec.refusal(_ask(), refusal)
    assert (row["outcome"], row["error"]) == ("denied", "hook_deny")
    assert row["metadata"]["rule_id"] == "sensitive-bash"
    assert _COMMAND not in json.dumps(row["metadata"])


@_CODECS
def test_a_verdict_no_rule_decided_adds_no_metadata(codec):
    refusal = HookGate(lambda _event: _verdict("")).judge(_ask())
    assert codec.refusal(_ask(), refusal).get("metadata") is None


class _Provider:
    def __init__(self) -> None:
        self.rejected: list[object] = []

    async def steer(self, _message: str) -> bool:
        return True

    async def reject_tool(self, request_id) -> None:
        self.rejected.append(request_id)


@pytest.mark.asyncio
async def test_a_background_refusal_row_names_the_real_gates_rule():
    # A shell call whose command was never recovered: the real gate's
    # deny-by-default row refuses it, and that row is what the audit names.
    event = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id="r1",
        title="List project files",
        tool_kind="execute",
        is_shell=True,
        shell_classified=True,
    )
    hooks = HookManager(HooksConfig())
    verdict = hooks.on_tool_call(event.title, **hook_gate_kwargs(event))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "unverifiable-shell")

    provider = _Provider()
    with patch("kiro_crew.sel.sel") as sel:
        ok = await _resolve_permission(provider, event, ToolApprovalPolicy.HOOK_BASED, hooks)
    assert ok is False and provider.rejected == ["r1"]
    (row,) = [c.kwargs for c in sel.return_value.log_tool_invocation.call_args_list]
    assert row["outcome"] == "denied"
    assert row["metadata"] == {"mechanism": "always_deny_hook", "rule_id": "unverifiable-shell"}


@pytest.mark.asyncio
async def test_a_dashboard_refusal_row_names_the_rule_that_refused(tmp_path):
    state, client = _make_state(tmp_path, context_builder=_context_builder(_verdict("exfil")))
    slot = _make_slot()
    _set_stream(client, [_permission_event(), _complete_event()])
    with (
        patch.object(SecurityEventLog, "_instance", None),
        patch.object(SecurityEventLog, "_initialized", False),
    ):
        audit = SecurityEventLog(base_dir=tmp_path / "sel", sync=True)

    with _patch_stats(), patch.object(chat_runner, "sel", return_value=audit):
        await _run_chat(state, slot, "hello")

    client.reject_tool.assert_called_once()
    rows = [r for r in audit.recent() if r["outcome"] == "denied"]
    assert [(r["error"], r["metadata"]) for r in rows] == [("hook_deny", {"rule_id": "exfil"})]
