"""A batch-guard deferral on the LLM-helpers transport is not a security block.

``approve_tool`` answers both the batch write guard and the security floor with
``False``. Only the floor may reach a cron's verdict as "blocked by the security
gate"; a guard deferral the model retried one step later did its work.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.sel as sel_mod
from kiro_crew.hooks import ToolHookResult
from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission, stream_and_collect
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.slack.gateway import _GateTally

_GUARD_REASON = "Blocked by the batch guard: the file write in this same step has not finished."


def _event(request_id: str) -> LLMEvent:
    params = {"path": "notes.txt"}
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="@kirocrew-core/artifact_list",
        request_id=request_id,
        tool_kind="other",
        tool_input=json.dumps(params),
        raw_tool_params=params,
        raw_params_trusted=True,
        mcp_identity_trusted=True,
        mcp_server_name="kirocrew-core",
        tool_name="artifact_list",
    )


class _Provider:
    """Refuses the request ids in *refused*; keeps a guard reason for *deferred*."""

    def __init__(self, *, refused: set[str], deferred: set[str], events: list[LLMEvent]):
        self._refused = refused
        self._kept = {rid: _GUARD_REASON for rid in deferred}
        self._events = events
        self.approved: list[str] = []

    async def approve_tool(self, request_id: str) -> bool:
        if request_id in self._refused:
            return False
        self.approved.append(request_id)
        return True

    async def reject_tool(self, request_id: str) -> None:
        return None

    def take_batch_refusal(self, request_id: str) -> str | None:
        return self._kept.pop(request_id, None)

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        for event in self._events:
            yield event
        yield LLMEvent(kind=EVENT_COMPLETE, text="")


def _sel_rows() -> tuple[MagicMock, list[dict]]:
    rows: list[dict] = []
    stub = MagicMock()
    stub.log_tool_invocation.side_effect = lambda **kw: rows.append(kw)
    return stub, rows


async def _run_cron_turn(provider: _Provider) -> _GateTally:
    tally = _GateTally()
    stub, _rows = _sel_rows()
    with patch.object(sel_mod, "sel", lambda: stub):
        await stream_and_collect(
            provider,  # type: ignore[arg-type]
            "q",
            approval_policy=ToolApprovalPolicy.AUTO_APPROVE,
            retry_transient=False,
            on_tool_gate=tally.note,
        )
    return tally


@pytest.mark.asyncio
async def test_a_deferral_retried_next_step_reports_no_security_block() -> None:
    provider = _Provider(refused={"r1"}, deferred={"r1"}, events=[_event("r1"), _event("r2")])
    tally = await _run_cron_turn(provider)
    assert provider.approved == ["r2"]
    assert tally.refused == 0
    assert tally.refused_calls == []
    assert not tally.partially_blocked
    assert not tally.all_blocked


@pytest.mark.asyncio
async def test_a_transport_floor_refusal_is_still_a_security_block() -> None:
    provider = _Provider(refused={"r1"}, deferred=set(), events=[_event("r1"), _event("r2")])
    tally = await _run_cron_turn(provider)
    assert tally.refused == 1
    assert [title for title, _rule in tally.refused_calls] == ["@\u200bkirocrew-core/artifact_list"]
    assert tally.partially_blocked


async def _resolve_one(provider: _Provider, policy: ToolApprovalPolicy, hooks=None) -> dict:
    stub, rows = _sel_rows()
    decisions: list[tuple[str, str, str]] = []
    with patch.object(sel_mod, "sel", lambda: stub):
        approved = await _resolve_permission(
            provider,  # type: ignore[arg-type]
            _event("r1"),
            policy,
            hooks,
            on_decision=lambda o, m, r: decisions.append((o, m, r)),
        )
    assert approved is False
    assert len(rows) == 1, rows
    assert decisions == [(rows[0]["outcome"], rows[0]["metadata"]["mechanism"], "")]
    return rows[0]


def _auto_approving_hooks() -> MagicMock:
    hooks = MagicMock()
    hooks.on_tool_call = MagicMock(return_value=ToolHookResult.auto_approve())
    hooks.effective_denied_regexes = MagicMock(return_value=[])
    return hooks


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_granted", [False, True], ids=["default", "hook_grant"])
async def test_the_audit_row_names_the_guard_not_the_floor(hook_granted: bool) -> None:
    provider = _Provider(refused={"r1"}, deferred={"r1"}, events=[])
    if hook_granted:
        row = await _resolve_one(provider, ToolApprovalPolicy.HOOK_BASED, _auto_approving_hooks())
    else:
        row = await _resolve_one(provider, ToolApprovalPolicy.AUTO_APPROVE)
    assert row["outcome"] == "rejected_batch_guard"
    assert row["metadata"]["mechanism"] == "batch_write_guard"


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_granted", [False, True], ids=["default", "hook_grant"])
async def test_a_floor_refusal_keeps_the_transport_floor_row(hook_granted: bool) -> None:
    provider = _Provider(refused={"r1"}, deferred=set(), events=[])
    if hook_granted:
        row = await _resolve_one(provider, ToolApprovalPolicy.HOOK_BASED, _auto_approving_hooks())
    else:
        row = await _resolve_one(provider, ToolApprovalPolicy.AUTO_APPROVE)
    assert row["outcome"] == "rejected_transport_floor"
    assert row["metadata"]["mechanism"] == "always_deny_transport"
