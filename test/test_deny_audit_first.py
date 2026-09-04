"""Audit-first ordering on deny paths (SEL write precedes wire I/O).

Every deny path answers the permission request over the ACP stdin pipe and
records the decision to SEL. The pipe write is unbounded: a backend that stops
reading stdin blocks ``reject_tool`` -> ``_send_response`` -> ``stdin.drain()``
until the turn deadline cancels the coroutine. When the SEL write is sequenced
AFTER that await, cancellation destroys the audit record: the permission
decision was made, acted on locally, and never audited.

The invariant these tests pin (issue #8621): **the SEL audit write must
precede any wire I/O for that decision** — record the decision first, then
attempt delivery. The steer is wire I/O too, so the audit precedes it as well.

Two layers, mirroring TestEveryHostDenyCallSiteIsWired in
test_refusal_inband_notice.py:

* behavioral — a stalled client plus a real ``task.cancel()`` proves the audit
  record survives turn-deadline cancellation at each deny helper;
* source-scan — every ``await client.reject_tool(`` site in the runner must
  have its ``sel().log_tool_invocation(`` textually before it, bounded to the
  site's own window, so a future deny path cannot re-open the gap by putting
  the audit back after the wire await.

Failure semantics on the flip side (audit recorded, delivery cancelled) are
the issue's own chosen direction: an audit row for a rejection the backend
never received is recoverable noise; a delivered rejection with no audit row
is an unmeetable compliance guarantee.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from unittest import mock

import pytest

import kiro_crew.dashboard.chat_runner as chat_runner
from kiro_crew.dashboard.chat_runner import (
    _reject_hook_blocked,
    _reject_hook_error,
    _reject_invalid_tool,
)


class _Slot:
    key = "slot-1"
    agent = "tester"

    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def append(self, kind: str, content: str, *_args, **_kwargs) -> None:
        self.rows.append((kind, content))


class _Event:
    request_id = "req-1"
    tool_call_id = "call-1"
    title = "bash"
    tool_kind = "execute"


class _StalledRejectClient:
    """A backend that answered nothing since the decision: reject stalls."""

    supports_steer = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def steer(self, message: str) -> bool:
        self.calls.append("steer")
        return True

    async def reject_tool(self, request_id) -> None:
        self.calls.append("reject")
        await asyncio.Event().wait()  # stdin.drain() against a stopped reader


class _StalledSteerClient(_StalledRejectClient):
    """The stall can hit the FIRST wire await of the path: the steer."""

    async def steer(self, message: str) -> bool:
        self.calls.append("steer")
        await asyncio.Event().wait()
        return True


class _HealthyClient(_StalledRejectClient):
    """Records order; nothing stalls."""

    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    async def steer(self, message: str) -> bool:
        self._order.append("steer")
        return True

    async def reject_tool(self, request_id) -> None:
        self._order.append("reject")


async def _cancelled_at_the_turn_deadline(coro) -> None:
    """Run ``coro`` until it parks on the stalled pipe, then cancel it."""
    task = asyncio.get_running_loop().create_task(coro)
    for _ in range(20):
        await asyncio.sleep(0)
    assert not task.done(), "the double never stalled -- harness rotted"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class TestAuditSurvivesStalledPipe:
    """Attack: decision made, pipe dead, coroutine cancelled -- audit must exist."""

    @pytest.mark.asyncio
    async def test_hook_blocked_audit_lands_before_the_stalled_reject(self):
        with mock.patch.object(chat_runner, "sel") as sel_factory:
            audit = sel_factory.return_value
            await _cancelled_at_the_turn_deadline(
                _reject_hook_blocked(
                    _StalledRejectClient(),
                    _Slot(),
                    _Event(),
                    session_key="s",
                    pre_hook_results=["BLOCKED: unsafe shell pattern"],
                    refusal_reasons=[],
                    refusal_notices=None,
                )
            )
            assert audit.log_tool_invocation.called, (
                "the permission decision was made and the coroutine cancelled at "
                "the turn deadline, but no SEL record exists -- the audit write is "
                "sequenced after the unbounded wire await"
            )
            kwargs = audit.log_tool_invocation.call_args.kwargs
            assert kwargs["outcome"] == "hook_blocked"
            assert kwargs["request_id"] == "req-1"

    @pytest.mark.asyncio
    async def test_invalid_tool_audit_lands_before_the_stalled_reject(self):
        with mock.patch.object(chat_runner, "sel") as sel_factory:
            audit = sel_factory.return_value
            await _cancelled_at_the_turn_deadline(
                _reject_invalid_tool(
                    _StalledRejectClient(),
                    _Slot(),
                    _Event(),
                    session_key="s",
                    error=ValueError("tool name failed validation"),
                    refusal_reasons=[],
                    refusal_notices=None,
                )
            )
            assert audit.log_tool_invocation.called
            assert audit.log_tool_invocation.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_hook_error_audit_lands_before_the_stalled_reject(self):
        with mock.patch.object(chat_runner, "sel") as sel_factory:
            audit = sel_factory.return_value
            await _cancelled_at_the_turn_deadline(
                _reject_hook_error(
                    _StalledRejectClient(),
                    _Slot(),
                    _Event(),
                    session_key="s",
                    error="hook raised",
                    refusal_reasons=[],
                    refusal_notices=None,
                )
            )
            assert audit.log_tool_invocation.called
            assert audit.log_tool_invocation.call_args.kwargs["outcome"] == "hook_error"

    @pytest.mark.asyncio
    async def test_audit_lands_even_when_the_steer_is_the_await_that_stalls(self):
        # The steer is wire I/O for the same decision and runs FIRST on notice
        # paths, so audit-after-reject and audit-between would both lose here.
        with mock.patch.object(chat_runner, "sel") as sel_factory:
            audit = sel_factory.return_value
            await _cancelled_at_the_turn_deadline(
                _reject_hook_blocked(
                    _StalledSteerClient(),
                    _Slot(),
                    _Event(),
                    session_key="s",
                    pre_hook_results=["BLOCKED: unsafe shell pattern"],
                    refusal_reasons=[],
                    refusal_notices=[],
                )
            )
            assert audit.log_tool_invocation.called, (
                "the stall hit the steer (the decision's first wire await) and the "
                "audit record is missing -- the SEL write must precede ALL wire I/O"
            )


class TestHealthyPathUnchanged:
    """Control: with a live pipe the reorder changes ordering only, not effects."""

    @pytest.mark.asyncio
    async def test_audit_first_then_steer_then_reject_and_effects_intact(self):
        order: list[str] = []
        with mock.patch.object(chat_runner, "sel") as sel_factory:
            audit = sel_factory.return_value
            audit.log_tool_invocation.side_effect = lambda **_: order.append("audit")
            slot = _Slot()
            reasons: list[tuple[str, str]] = []
            notices: list[str] = []
            await _reject_hook_blocked(
                _HealthyClient(order),
                slot,
                _Event(),
                session_key="s",
                pre_hook_results=["BLOCKED: unsafe shell pattern"],
                refusal_reasons=reasons,
                refusal_notices=notices,
            )
        assert order == ["audit", "steer", "reject"]
        assert audit.log_tool_invocation.call_count == 1
        # Every downstream effect of the deny is preserved.
        assert reasons and reasons[0][0] == "bash"
        assert notices, "the in-band notice list must still be fed"
        # The steer's own inject row may precede the blocked row; the deny's
        # visible row must still exist regardless.
        assert any(kind == "tool" for kind, _ in slot.rows)

    @pytest.mark.asyncio
    async def test_benign_no_new_deny_and_audit_record_shape_unchanged(self):
        # The reorder must not invent denies or alter the record's fields.
        with mock.patch.object(chat_runner, "sel") as sel_factory:
            audit = sel_factory.return_value
            order: list[str] = []
            await _reject_invalid_tool(
                _HealthyClient(order),
                _Slot(),
                _Event(),
                session_key="s",
                error=ValueError("bad name"),
                refusal_reasons=[],
                refusal_notices=None,
            )
        kwargs = audit.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "bash"
        assert kwargs["tool_kind"] == "execute"
        assert kwargs["error"] == "validation_failed: bad name"
        assert order == ["reject"], "fallback-only callers still deny exactly once"


class TestEveryDenySiteAuditsBeforeTheWire:
    """Source-level guard over ALL deny sites, inline ones included.

    The direct tests above cover the three helpers; the runner also denies
    inline (hook TOOL_DENY, batch cascade, interactive rejection) inside the
    turn coroutine where a unit test cannot reach. Same doctrine as
    TestEveryHostDenyCallSiteIsWired: the coverage claim must be checkable.
    For each ``await client.reject_tool(`` site, its decision's
    ``sel().log_tool_invocation(`` must appear BEFORE it within the site's own
    window (bounded by the nearest preceding wire answer or function def, so
    one site's audit cannot vouch for another's).
    """

    RUNNER = pathlib.Path(__file__).resolve().parents[1] / "src/kiro_crew/dashboard/chat_runner.py"
    REJECT = re.compile(r"^\s*await client\.reject_tool\(")
    BOUNDARY = re.compile(
        r"^\s*(?:await client\.approve_tool\(|await client\.reject_tool\(|async def )"
    )
    AUDIT = "log_tool_invocation("

    def _lines(self) -> list[str]:
        return self.RUNNER.read_text(encoding="utf-8").splitlines()

    def _reject_sites(self, lines: list[str]) -> list[int]:
        return [i for i, line in enumerate(lines) if self.REJECT.match(line)]

    def test_the_scan_finds_every_reject_site(self):
        # Count-matched against a plain textual count so a call-shape drift
        # cannot silently drop a site out of the ordering assertion below.
        lines = self._lines()
        textual = sum("await client.reject_tool(" in line for line in lines)
        found = len(self._reject_sites(lines))
        assert found == textual, (
            f"the site scan found {found} of {textual} reject sites -- its regex "
            "no longer matches every call shape"
        )
        assert found >= 6, f"expected the known deny sites, saw {found}"

    def test_every_reject_site_has_its_audit_before_the_wire(self):
        lines = self._lines()
        unaudited: list[int] = []
        for site in self._reject_sites(lines):
            audited = False
            for j in range(site - 1, -1, -1):
                if self.AUDIT in lines[j]:
                    audited = True
                    break
                if self.BOUNDARY.match(lines[j]):
                    break
            if not audited:
                unaudited.append(site + 1)
        assert not unaudited, (
            "these deny sites answer the wire before their SEL audit write -- a "
            "stalled reader cancels the coroutine and the decision is never "
            f"audited (issue #8621 invariant): lines {unaudited}"
        )
