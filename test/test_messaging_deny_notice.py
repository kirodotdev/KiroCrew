"""Every HOST deny on the messaging surfaces steers the in-band notice first.

A rejected permission reaches the model as kiro-cli's fixed "User denied tool
execution". The dashboard chat runner and ``llm_helpers`` steer the real reason
into the running turn before answering; this pins the same property on the three
surfaces where a person is watching a reply that would otherwise silently give
up: the native Slack handler (``slack/handler.py``), the channel agent stream
(``channel.py``) and the channel-neutral ``messaging.TurnDriver``.

Two halves, mirroring ``test_llm_helpers_deny_notice.py``:

* a SOURCE-LEVEL guard per surface that enumerates every ``reject_tool(`` in the
  module with a per-site verdict -- host deny (steered), genuine USER rejection
  (bare: kiro-cli's wording is the truth there and "NOT a user action" would be a
  lie), or teardown cleanup (bare: it answers the wire for a site whose own
  reject a cancellation skipped) -- and fails when a site is missing from the
  enumeration or carries the wrong verdict;
* BEHAVIOURAL tests, one per host-deny reason, driving the real surface with a
  provider double recording steer/reject ORDER. Order is the mechanism: the
  steer must be written while the permission request is still unanswered,
  because that is what proves the turn is in flight and gets the notice queued
  instead of dropped.

A third property rides along: one decision, one SEL row. A cancellation inside
the steer answers the wire through an orphan reject, and that reject audits ONLY
for a site whose caller audits after the wire (the Slack approval-timeout arm,
the TurnDriver's decider path); an audit-first site already has its row.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.channel as channel_mod
import kiro_crew.slack.handler as h
from kiro_crew import llm_helpers, permission_floor
from kiro_crew.acp.types import AcpEvent
from kiro_crew.channel import _APPROVAL_FIELD_MAX_CHARS, _stream_task
from kiro_crew.constants import DENY_CAUSE_APPROVAL_OVERSIZE
from kiro_crew.deny_notice import _DENY_CAUSE_TEXT, build_refusal_steer_notice
from kiro_crew.hooks import TOOL_ALLOW, TOOL_DENY, ToolHookResult
from kiro_crew.messaging import (
    APPROVAL_AUTO,
    APPROVAL_INTERACTIVE,
    TransportCapabilities,
    TurnDriver,
)
from kiro_crew.messaging import driver as driver_mod
from kiro_crew.messaging.dispatch import build_tool_gate
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST

_GENERIC = "User denied tool execution"
_TAG = "[Kiro Crew host notice]"
#: Only the POLICY cause appends class-specific remediation; its guidance line
#: is the fingerprint that must be absent from every non-policy notice.
_POLICY_GUIDANCE = "allowed alternative"
_POLICY_CLAUSE = "safety policy"
_SURFACE_CLAUSE = "tool policy of the surface"
_TIMEOUT_CLAUSE = "approval prompt expired unanswered"
_OVERSIZE_CLAUSE = "too long for this channel to show in full"

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


def _assert_steered_then_rejected(calls: list[str], steered: list[str], *fragments: str) -> str:
    assert calls == ["steer", "reject"], calls
    (notice,) = steered
    assert notice.startswith(_TAG)
    assert _GENERIC in notice, "the notice must name the string it is correcting"
    assert "NOT a user action" in notice
    for fragment in fragments:
        assert fragment in notice, (fragment, notice)
    return notice


# ── The new cause ─────────────────────────────────────────────────────────────


class TestTheOversizeCause:
    def test_it_is_in_the_shared_table_with_the_invariant_half(self):
        assert DENY_CAUSE_APPROVAL_OVERSIZE in _DENY_CAUSE_TEXT
        out = build_refusal_steer_notice(
            "Running: ls", "this command is 900 characters", cause=DENY_CAUSE_APPROVAL_OVERSIZE
        )
        assert _GENERIC in out and "NOT a user action" in out
        assert _OVERSIZE_CLAUSE in out
        assert "900 characters" in out

    def test_it_tells_the_model_to_split_and_offers_no_remediation(self):
        # The action was never judged: the fix is the model's own (split the
        # request), so the policy remediation would answer a question nobody asked.
        out = build_refusal_steer_notice("Running: ls", "why", cause=DENY_CAUSE_APPROVAL_OVERSIZE)
        assert "split the request" in out
        assert _POLICY_GUIDANCE not in out
        assert _POLICY_CLAUSE not in out


# ── Slack handler ─────────────────────────────────────────────────────────────


class _SlackSteerProvider:
    """``FakeProvider``-shaped double that also advertises the steer channel."""

    def __init__(self, events, *, supports_steer: bool = True) -> None:
        self._events = events
        self.approved: list = []
        self.rejected: list = []
        self.calls: list[str] = []
        self.steered: list[str] = []
        self.supports_steer = supports_steer
        self.supports_refusal_steer = supports_steer

    async def stream(self, message, timeout=120.0):
        for event in self._events:
            yield event
        yield AcpEvent(kind=EVENT_COMPLETE)

    async def steer(self, message: str) -> bool:
        self.calls.append("steer")
        self.steered.append(message)
        return True

    async def approve_tool(self, request_id, option_id="allow_once"):
        self.calls.append("approve")
        self.approved.append(request_id)

    async def reject_tool(self, request_id):
        self.calls.append("reject")
        self.rejected.append(request_id)

    async def start(self):
        pass

    async def shutdown(self):
        pass

    def context_usage_pct(self):
        return 0.0


@pytest.fixture()
def slack_harness():
    from test_slack_handler_more_coverage import FakeSessions, _Builder

    from conftest import MockSlackClient

    h._pending_approvals.clear()
    h._linked_approvals.clear()
    h._trusted_sessions.clear()
    yield MockSlackClient, FakeSessions, _Builder
    h._pending_approvals.clear()
    h._linked_approvals.clear()
    h._trusted_sessions.clear()


async def _slack_hook_deny(slack_harness, provider, reason="sensitive path: ~/.aws/credentials"):
    MockSlackClient, FakeSessions, _Builder = slack_harness
    builder = _Builder(ToolHookResult(action=TOOL_DENY, reason=reason))
    await h.handle_message(
        MockSlackClient(),
        FakeSessions(provider),
        "C1",
        "go",
        None,
        "m1",
        "U1",
        approval_mode=h.APPROVAL_INTERACTIVE,
        context_builder=builder,
    )


@pytest.mark.asyncio
async def test_slack_hook_deny_steers_the_hooks_reason_before_rejecting(slack_harness):
    provider = _SlackSteerProvider(
        [AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq2", title="Write secrets.txt")]
    )
    await _slack_hook_deny(slack_harness, provider)
    assert provider.rejected == ["rq2"] and provider.approved == []
    _assert_steered_then_rejected(
        provider.calls,
        provider.steered,
        "Write secrets.txt",
        "sensitive path",
        _POLICY_CLAUSE,
    )


@pytest.mark.asyncio
async def test_slack_hook_deny_audits_before_the_steer(slack_harness, monkeypatch):
    order: list[str] = []
    sel_mock = MagicMock()
    sel_mock.log_tool_invocation.side_effect = lambda **kw: order.append("audit")
    monkeypatch.setattr(h, "sel", lambda: sel_mock)

    class _Ordered(_SlackSteerProvider):
        async def steer(self, message):
            order.append("steer")
            return await super().steer(message)

        async def reject_tool(self, request_id):
            order.append("reject")
            await super().reject_tool(request_id)

    provider = _Ordered([AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq2", title="W")])
    await _slack_hook_deny(slack_harness, provider)
    assert order[:3] == ["audit", "steer", "reject"], order


@pytest.mark.asyncio
async def test_slack_backend_without_steer_only_rejects(slack_harness):
    provider = _SlackSteerProvider(
        [AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq2", title="W")],
        supports_steer=False,
    )
    await _slack_hook_deny(slack_harness, provider)
    assert provider.calls == ["reject"]


@pytest.mark.asyncio
async def test_slack_agent_authored_reason_is_redacted_before_it_reaches_the_model(slack_harness):
    provider = _SlackSteerProvider(
        [AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq2", title="W")]
    )
    secret = "AKIAIOSFODNN7EXAMPLE"
    await _slack_hook_deny(slack_harness, provider, reason=f"matched key {secret} in the command")
    (notice,) = provider.steered
    assert secret not in notice
    assert "matched key" in notice


@pytest.mark.asyncio
class _StalledSlack(_SlackSteerProvider):
    async def steer(self, message):
        await asyncio.sleep(3600)
        return True


async def _cancel_slack_steer(monkeypatch, *, audited: bool) -> tuple[_StalledSlack, MagicMock]:
    """Cancel the Slack helper mid-steer and return the provider and SEL double."""
    sel_mock = MagicMock()
    monkeypatch.setattr(h, "sel", lambda: sel_mock)
    provider = _StalledSlack([])
    event = SimpleNamespace(title="W", request_id="rq9")
    task = asyncio.ensure_future(
        h._steer_host_deny(provider, event, "why", cause="policy", audited=audited)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(5):
        await asyncio.sleep(0)
    assert provider.rejected == ["rq9"]
    assert not h._orphan_rejects
    return provider, sel_mock


@pytest.mark.asyncio
async def test_slack_cancelled_mid_steer_still_answers_the_wire(monkeypatch):
    # The helper's cancellation arm: a stranded permission request wedges the
    # backend, so the reject is scheduled and stepped while the caller unwinds.
    await _cancel_slack_steer(monkeypatch, audited=True)


@pytest.mark.asyncio
async def test_slack_audited_site_cancelled_mid_steer_writes_no_second_sel_row(monkeypatch):
    # The hook deny wrote its SEL row BEFORE the steer; the orphan reject must
    # not write another for the same decision (the ledger is append-only).
    _provider, sel_mock = await _cancel_slack_steer(monkeypatch, audited=True)
    sel_mock.log_tool_invocation.assert_not_called()


@pytest.mark.asyncio
async def test_slack_after_wire_site_cancelled_mid_steer_audits_once(monkeypatch):
    # The approval-timeout arm's caller audits after the wire, which the
    # cancellation skips; the orphan reject must write that one row itself.
    _provider, sel_mock = await _cancel_slack_steer(monkeypatch, audited=False)
    assert sel_mock.log_tool_invocation.call_count == 1
    kw = sel_mock.log_tool_invocation.call_args.kwargs
    assert kw["outcome"] == "rejected" and kw["request_id"] == "rq9"


# ── channel.py ────────────────────────────────────────────────────────────────


def _make_agent():
    return SimpleNamespace(
        id="a1",
        role="dev",
        agent_name="dev",
        session_key="channel:test",
        _approval_future=None,
        _trusted_commands=set(),
        _trusted_bases=set(),
        _pending_approval_command="",
    )


def _make_channel(agent, *, trusted: bool = False, decide: str | None = None):
    """Channel stub; ``decide`` resolves the approval card as that human decision,
    ``None`` leaves the card unanswered (the timeout path)."""
    ch = SimpleNamespace(id="c1", trusted=trusted, members={})
    ch._broadcast = MagicMock()

    async def _post(*args, **kw):
        if kw.get("msg_type") == "approval" and decide is not None:
            if agent._approval_future is not None and not agent._approval_future.done():
                agent._approval_future.set_result(decide)

    ch.post = AsyncMock(side_effect=_post)
    return ch


class _ChannelClient:
    def __init__(self, events, *, supports_steer: bool = True) -> None:
        self._events = events
        self.calls: list[str] = []
        self.steered: list[str] = []
        self.supports_steer = supports_steer
        self.supports_refusal_steer = supports_steer

    async def stream(self, message):
        for ev in self._events:
            yield ev

    async def steer(self, message: str) -> bool:
        self.calls.append("steer")
        self.steered.append(message)
        return True

    async def approve_tool(self, request_id):
        self.calls.append("approve")

    async def reject_tool(self, request_id):
        self.calls.append("reject")


def _perm(*, title="", text="", tool_input="", is_shell=False):
    cmd = None
    if is_shell and tool_input:
        cmd = json.loads(tool_input).get("command")
    return SimpleNamespace(
        kind=EVENT_PERMISSION_REQUEST,
        text=text,
        title=title,
        request_id=7,
        tool_input=tool_input,
        is_shell=is_shell,
        tool_input_redacted=False,
        shell_command=cmd,
    )


def _done():
    return SimpleNamespace(kind=EVENT_COMPLETE)


@pytest.fixture()
def channel_sel(monkeypatch):
    sel_mock = MagicMock()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_mock)
    return sel_mock


@pytest.mark.asyncio
async def test_channel_blocked_messaging_tool_steers_the_surface_cause(channel_sel):
    client = _ChannelClient([_perm(text="send_message (kirocrew-core)", title=""), _done()])
    agent = _make_agent()
    await _stream_task(agent, _make_channel(agent, trusted=True), client, "hi")
    notice = _assert_steered_then_rejected(
        client.calls, client.steered, "send_message", "channel posts", _SURFACE_CLAUSE
    )
    assert _POLICY_GUIDANCE not in notice


@pytest.mark.asyncio
async def test_channel_gate_deny_steers_the_gates_reason(channel_sel, monkeypatch):
    monkeypatch.setattr(
        permission_floor,
        "refusal_for",
        lambda event, **kw: "Blocked by security policy: sensitive path ~/.ssh/id_rsa",
    )
    client = _ChannelClient([_perm(title="Read ~/.ssh/id_rsa"), _done()])
    agent = _make_agent()
    await _stream_task(agent, _make_channel(agent, trusted=True), client, "hi")
    _assert_steered_then_rejected(
        client.calls, client.steered, "Read ~/.ssh/id_rsa", "sensitive path", _POLICY_CLAUSE
    )


@pytest.mark.asyncio
async def test_channel_over_bound_card_steers_the_oversize_cause(channel_sel):
    long_cmd = "echo " + "x" * (_APPROVAL_FIELD_MAX_CHARS + 50)
    client = _ChannelClient(
        [_perm(title="Run", tool_input=json.dumps({"command": long_cmd}), is_shell=True), _done()]
    )
    agent = _make_agent()
    ch = _make_channel(agent, decide="approved")
    await _stream_task(agent, ch, client, "hi")
    notice = _assert_steered_then_rejected(
        client.calls, client.steered, str(_APPROVAL_FIELD_MAX_CHARS), _OVERSIZE_CLAUSE
    )
    assert "split the request" in notice
    assert _POLICY_GUIDANCE not in notice
    # The reader's own notice was still posted; the two audiences each get theirs.
    assert any("Approval refused" in str(c.args[1]) for c in ch.post.call_args_list)


@pytest.mark.asyncio
async def test_channel_expired_card_steers_the_timeout_cause(channel_sel, monkeypatch):
    monkeypatch.setattr(channel_mod, "_APPROVAL_TIMEOUT_SECS", 0.01)
    client = _ChannelClient([_perm(title="Write notes.md"), _done()])
    agent = _make_agent()
    await _stream_task(agent, _make_channel(agent, decide=None), client, "hi")
    notice = _assert_steered_then_rejected(
        client.calls, client.steered, "Write notes.md", "went unanswered", _TIMEOUT_CLAUSE
    )
    assert _POLICY_GUIDANCE not in notice


@pytest.mark.asyncio
async def test_channel_human_deny_gets_no_notice(channel_sel):
    # The reader clicked Deny: kiro-cli's wording is the truth, and the same
    # reject line must stay bare for it.
    client = _ChannelClient([_perm(title="Write notes.md"), _done()])
    agent = _make_agent()
    await _stream_task(agent, _make_channel(agent, decide="rejected"), client, "hi")
    assert client.calls == ["reject"]


@pytest.mark.asyncio
async def test_channel_backend_without_steer_only_rejects(channel_sel):
    client = _ChannelClient(
        [_perm(text="send_message (kirocrew-core)"), _done()], supports_steer=False
    )
    agent = _make_agent()
    await _stream_task(agent, _make_channel(agent, trusted=True), client, "hi")
    assert client.calls == ["reject"]


@pytest.mark.asyncio
async def test_channel_audits_before_the_steer(channel_sel):
    order: list[str] = []
    channel_sel.log_tool_invocation.side_effect = lambda **kw: order.append("audit")

    class _Ordered(_ChannelClient):
        async def steer(self, message):
            order.append("steer")
            return await super().steer(message)

        async def reject_tool(self, request_id):
            order.append("reject")
            await super().reject_tool(request_id)

    client = _Ordered([_perm(text="send_message (kirocrew-core)"), _done()])
    agent = _make_agent()
    await _stream_task(agent, _make_channel(agent, trusted=True), client, "hi")
    assert order[:3] == ["audit", "steer", "reject"], order


@pytest.mark.asyncio
async def test_channel_cancelled_mid_steer_still_answers_the_wire():
    class _Stalled(_ChannelClient):
        async def steer(self, message):
            await asyncio.sleep(3600)
            return True

    client = _Stalled([])
    event = SimpleNamespace(title="W", text="", request_id=9)
    task = asyncio.ensure_future(channel_mod._steer_host_deny(client, event, "why", cause="policy"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(5):
        await asyncio.sleep(0)
    assert client.calls == ["reject"]
    assert not llm_helpers._orphan_rejects


# ── messaging.TurnDriver ──────────────────────────────────────────────────────


class _Renderer(Renderer):
    def __init__(self) -> None:
        super().__init__(TransportCapabilities())

    async def on_text_chunk(self, text):
        pass

    async def on_thinking(self, text):
        pass

    async def on_tool_call(self, tool_call_id, title, tool_kind="", tool_purpose=""):
        pass

    async def on_prompt_choice(
        self, options, request_id, tool_title="", tool_purpose="", tool_input=""
    ):
        pass

    async def on_compaction(self, pct):
        pass

    async def on_done(self, stop_reason=""):
        pass

    async def on_steer_consumed(self, summary=""):
        pass


class _DriverProvider:
    def __init__(self, events, *, supports_steer: bool = True) -> None:
        self._events = events
        self.calls: list[str] = []
        self.steered: list[str] = []
        self.rejected: list = []
        self.supports_steer = supports_steer
        self.supports_refusal_steer = supports_steer

    async def stream(self, message):
        for ev in self._events:
            yield ev

    async def steer(self, message: str) -> bool:
        self.calls.append("steer")
        self.steered.append(message)
        return True

    async def approve_tool(self, request_id, *, always=False):
        self.calls.append("approve")

    async def reject_tool(self, request_id):
        self.calls.append("reject")
        self.rejected.append(request_id)


def _driver_events():
    return [
        AcpEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id="rq1",
            title="fs_write",
            options=[{"id": "approve"}],
        ),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]


async def _run_driver(provider, **kw):
    return await TurnDriver(provider, _Renderer(), **kw).run("hello")


@pytest.mark.asyncio
async def test_driver_deny_all_tools_steers_the_surface_cause():
    p = _DriverProvider(_driver_events())
    await _run_driver(p, approval_mode=APPROVAL_AUTO, deny_all_tools=True)
    notice = _assert_steered_then_rejected(
        p.calls, p.steered, "fs_write", "other than its operator", _SURFACE_CLAUSE
    )
    assert _POLICY_GUIDANCE not in notice


@pytest.mark.asyncio
async def test_driver_gate_deny_steers_the_gates_reason_when_it_carries_one():
    def gate(ev):
        return "deny"

    gate.last_deny_reason = "Blocked by security policy: write-protected config"  # type: ignore
    p = _DriverProvider(_driver_events())
    await _run_driver(p, approval_mode=APPROVAL_AUTO, tool_gate=gate)
    _assert_steered_then_rejected(
        p.calls, p.steered, "fs_write", "write-protected config", _POLICY_CLAUSE
    )


@pytest.mark.asyncio
async def test_driver_gate_deny_names_the_gate_when_it_carries_no_reason():
    p = _DriverProvider(_driver_events())
    await _run_driver(p, approval_mode=APPROVAL_AUTO, tool_gate=lambda ev: "deny")
    _assert_steered_then_rejected(p.calls, p.steered, "PreToolUse security gate", _POLICY_CLAUSE)


@pytest.mark.asyncio
async def test_driver_human_deny_through_the_decider_gets_no_notice():
    async def _say_no(event) -> bool:
        return False

    p = _DriverProvider(_driver_events())
    await _run_driver(p, approval_mode=APPROVAL_INTERACTIVE, decider=_say_no)
    assert p.calls == ["reject"]


@pytest.mark.asyncio
async def test_driver_backend_without_steer_only_rejects():
    p = _DriverProvider(_driver_events(), supports_steer=False)
    await _run_driver(p, approval_mode=APPROVAL_AUTO, deny_all_tools=True)
    assert p.calls == ["reject"]


@pytest.mark.asyncio
async def test_driver_audits_before_the_steer(monkeypatch):
    order: list[str] = []
    sel_mock = MagicMock()
    sel_mock.log_api_access.side_effect = lambda **kw: order.append("audit")
    monkeypatch.setattr("kiro_crew.messaging.driver.sel", lambda: sel_mock)

    class _Ordered(_DriverProvider):
        async def steer(self, message):
            order.append("steer")
            return await super().steer(message)

        async def reject_tool(self, request_id):
            order.append("reject")
            await super().reject_tool(request_id)

    p = _Ordered(_driver_events())
    await _run_driver(p, approval_mode=APPROVAL_AUTO, tool_gate=lambda ev: "deny")
    assert order[:3] == ["audit", "steer", "reject"], order


class _StalledDriverProvider(_DriverProvider):
    async def steer(self, message):
        await asyncio.sleep(3600)
        return True


async def _cancel_driver_steer(monkeypatch, *, audited: bool) -> MagicMock:
    sel_mock = MagicMock()
    monkeypatch.setattr("kiro_crew.messaging.driver.sel", lambda: sel_mock)
    p = _StalledDriverProvider([])
    drv = TurnDriver(p, _Renderer(), approval_mode=APPROVAL_AUTO)
    event = SimpleNamespace(title="W", request_id="rq9")
    task = asyncio.ensure_future(
        drv._steer_host_deny(event, "why", cause="policy", audited=audited)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(5):
        await asyncio.sleep(0)
    assert p.rejected == ["rq9"]
    assert not driver_mod._orphan_rejects
    return sel_mock


@pytest.mark.asyncio
async def test_driver_audited_site_cancelled_mid_steer_writes_no_second_sel_row(monkeypatch):
    sel_mock = await _cancel_driver_steer(monkeypatch, audited=True)
    sel_mock.log_api_access.assert_not_called()


@pytest.mark.asyncio
async def test_driver_after_wire_site_cancelled_mid_steer_audits_once(monkeypatch):
    sel_mock = await _cancel_driver_steer(monkeypatch, audited=False)
    assert sel_mock.log_api_access.call_count == 1
    assert "cancelled_mid_steer" in sel_mock.log_api_access.call_args.kwargs["resources"]


def test_build_tool_gate_leaves_the_hooks_reason_on_the_gate():
    hooks = MagicMock()
    hooks.on_tool_call = MagicMock(
        return_value=ToolHookResult(action=TOOL_DENY, reason="denylisted: rm -rf")
    )
    gate = build_tool_gate(SimpleNamespace(hooks=hooks), session_key="s", agent="a")
    assert gate.last_deny_reason == ""
    assert gate(SimpleNamespace(title="rm -rf /")) == "deny"
    assert gate.last_deny_reason == "denylisted: rm -rf"
    hooks.on_tool_call.return_value = ToolHookResult(action=TOOL_ALLOW)
    assert gate(SimpleNamespace(title="ls")) == ""
    assert gate.last_deny_reason == "", "an allow must not leave a stale deny reason behind"


# ── Source-level guards: one enumeration per surface ─────────────────────────


@dataclass(frozen=True)
class _Site:
    """One ``reject_tool(`` site, identified by a fingerprint near it."""

    fingerprint: str
    verdict: str  # "host" (steered) | "user" (bare) | "cleanup" (bare) | "mixed"
    #: For "mixed": the guard line the steer must sit under, so a human's Deny on
    #: the same reject stays bare while the host's timeout is explained.
    steer_guard: str = ""
    #: False only for a site whose audit row is written by its caller after
    #: the wire (the Slack approval-timeout arm); every other steered site
    #: audits before the steer.
    audit_first: bool = True


@dataclass(frozen=True)
class _Surface:
    path: str
    steer: str
    audit: str
    sites: tuple[_Site, ...]
    #: Non-awaited spellings that are the cancellation fallback INSIDE the steer
    #: helper -- they answer the wire for a site whose own reject the
    #: cancellation skipped, so they are not deny sites of their own.
    orphan_spellings: tuple[str, ...] = field(default_factory=tuple)
    #: Lines a steer may sit above its reject (the steer is the statement
    #: immediately before the reject; the slack absorbs black wrapping and a
    #: one-line guard).
    window: int = 12
    #: Lines the audit call may sit above its reject: the audit opens first and
    #: its metadata plus the wrapped steer can span a dozen lines.
    audit_window: int = 45


_SURFACES = {
    "slack": _Surface(
        path="slack/handler.py",
        steer=r"^\s*await _steer_host_deny\(",
        audit=r"^\s*sel\(\)\.log_tool_invocation\(",
        sites=(
            # handle_message: the PreToolUse hook said deny -- a host verdict.
            _Site('error="hook_deny"', "host"),
            # _reject_orphaned_tool: teardown answers a wire the fallback arms
            # re-raised past; not a decision of its own.
            _Site("Failed to reject orphaned tool", "cleanup"),
            # _request_approval: the prompt expired unanswered -- a host decline
            # (steered under the same claim gate as the reject).
            _Site("Only the claim winner answers the wire", "host", audit_first=False),
            # handle_interaction: the person clicked Deny.
            _Site("pending.provider.reject_tool(pending.request_id)", "user"),
        ),
        # The timeout arm's steer and reject sit under two ``if claimed:``
        # guards with the audited= comment and the claim-winner comment between.
        window=20,
    ),
    "channel": _Surface(
        path="channel.py",
        steer=r"^\s*await _steer_host_deny\(",
        audit=r"^\s*sel\(\)\.log_tool_invocation\(",
        sites=(
            _Site('outcome="rejected_blocked_tool"', "host"),
            _Site('outcome="rejected_hook_deny"', "host"),
            _Site('outcome="rejected_over_bound_title"', "host"),
            # One reject line, two provenances: the card expired (host) or the
            # reader clicked Deny (user). The steer sits under the timeout flag.
            _Site("outcome=decision,", "mixed", steer_guard="if _approval_timed_out:"),
        ),
    ),
    "driver": _Surface(
        path="messaging/driver.py",
        steer=r"^\s*await self\._steer_host_deny\(",
        audit=r"^\s*sel\(\)\.log_api_access\(",
        sites=(
            _Site("reason=untrusted_sender", "host"),
            _Site("reason=hook_deny", "host"),
            # The decider said no. ``_steer_deny_cause`` steers ONLY when the
            # decider recorded a host cause (an expired prompt); a human's Deny
            # stays bare. Its audit row is written by the caller after the wire.
            _Site("await self._steer_deny_cause(event)", "mixed", steer_guard="<decider>"),
        ),
        orphan_spellings=("ensure_future(self.provider.reject_tool(",),
    ),
}

_REJECT = re.compile(r"^\s*await [\w.]+\.reject_tool\(")


def _lines(surface: _Surface) -> list[str]:
    return (_SRC / surface.path).read_text(encoding="utf-8").splitlines()


def _reject_sites(lines: list[str]) -> list[int]:
    return [i for i, line in enumerate(lines) if _REJECT.match(line)]


def _site_for(surface: _Surface, lines: list[str], i: int, previous: int = -1) -> _Site:
    # Never read past the PREVIOUS reject: two adjacent sites must not share a
    # fingerprint window.
    span = "\n".join(lines[max(0, i - surface.audit_window, previous + 1) : i + 6])
    hits = [s for s in surface.sites if s.fingerprint in span]
    assert len(hits) == 1, (
        f"{surface.path}:{i + 1}: a reject_tool site must match exactly one enumerated "
        f"fingerprint (matched {[s.fingerprint for s in hits]}); a new site needs its "
        "own per-site verdict here, not a wider marker"
    )
    return hits[0]


def _walk(surface: _Surface, lines: list[str]) -> list[tuple[int, _Site]]:
    out: list[tuple[int, _Site]] = []
    previous = -1
    for i in _reject_sites(lines):
        out.append((i, _site_for(surface, lines, i, previous)))
        previous = i
    return out


def _last_match(pattern: str, lines: list[str], lo: int, hi: int) -> int:
    rx = re.compile(pattern)
    hits = [j for j in range(lo, hi) if rx.match(lines[j])]
    return hits[-1] if hits else -1


@pytest.mark.parametrize("name", sorted(_SURFACES))
class TestEveryHostDenyOnTheMessagingSurfacesSteersFirst:
    """Coverage checkable from the source, not asserted in a PR body."""

    def test_the_scan_finds_every_reject_the_source_contains(self, name):
        surface = _SURFACES[name]
        src = (_SRC / surface.path).read_text(encoding="utf-8")
        for spelling in surface.orphan_spellings:
            assert src.count(spelling) == 1, spelling
        textual = src.count(".reject_tool(") - len(surface.orphan_spellings)
        found = len(_reject_sites(src.splitlines()))
        assert found == len(surface.sites) == textual, (found, len(surface.sites), textual)

    def test_every_site_carries_exactly_one_verdict(self, name):
        surface = _SURFACES[name]
        lines = _lines(surface)
        seen = [site.fingerprint for _i, site in _walk(surface, lines)]
        assert sorted(seen) == sorted(s.fingerprint for s in surface.sites)

    def test_every_host_deny_is_preceded_by_the_steer(self, name):
        surface = _SURFACES[name]
        lines = _lines(surface)
        bare: list[int] = []
        for i, site in _walk(surface, lines):
            if site.verdict != "host":
                continue
            if _last_match(surface.steer, lines, max(0, i - surface.window), i) < 0:
                bare.append(i + 1)
        assert not bare, (
            f"{surface.path}: these host denies hand the model kiro-cli's generic 'user "
            f"denied' with nothing to correct it -- steer first: lines {bare}"
        )

    def test_user_rejections_and_cleanup_are_not_steered(self, name):
        surface = _SURFACES[name]
        lines = _lines(surface)
        for i, site in _walk(surface, lines):
            if site.verdict not in ("user", "cleanup"):
                continue
            window = lines[max(0, i - surface.window) : i]
            assert not any(re.match(surface.steer, line) for line in window), (
                f"{surface.path}:{i + 1}: a genuine user rejection (or a teardown reject) "
                "must not claim it was not one"
            )

    def test_mixed_sites_steer_only_under_their_host_guard(self, name):
        surface = _SURFACES[name]
        lines = _lines(surface)
        for i, site in _walk(surface, lines):
            if site.verdict != "mixed":
                continue
            if site.steer_guard == "<decider>":
                # The steer is inside _steer_deny_cause, gated on the decider's
                # recorded host cause; pin that gate rather than a source line.
                src = "\n".join(lines)
                assert "if cause != DENY_CAUSE_APPROVAL_TIMEOUT:\n            return" in src
                continue
            steer = _last_match(surface.steer, lines, max(0, i - surface.window), i)
            assert steer >= 0, f"{surface.path}:{i + 1}: the host half is not steered"
            assert lines[steer - 1].strip() == site.steer_guard, (
                f"{surface.path}:{steer + 1}: the steer must sit under `{site.steer_guard}` "
                "so the human's Deny on the same reject stays bare"
            )

    def test_every_steered_site_audits_before_the_steer(self, name):
        surface = _SURFACES[name]
        lines = _lines(surface)
        late: list[int] = []
        previous = -1
        for i, site in _walk(surface, lines):
            steer = _last_match(surface.steer, lines, max(0, i - surface.window), i)
            if steer < 0 or not site.audit_first:
                previous = i
                continue
            # Never borrow the PREVIOUS site's audit: the floor is the line
            # after the last reject, so two adjacent sites cannot share one.
            floor = max(0, i - surface.audit_window, previous + 1)
            audit = _last_match(surface.audit, lines, floor, i)
            if audit < 0 or not audit < steer:
                late.append(i + 1)
            previous = i
        assert not late, (
            f"{surface.path}: the SEL row must be written before the steer and the reject, "
            f"or a stalled pipe cancels the coroutine with the decision unaudited: {late}"
        )

    def test_every_steer_names_its_cause_explicitly(self, name):
        surface = _SURFACES[name]
        lines = _lines(surface)
        unnamed: list[int] = []
        for i, line in enumerate(lines):
            if not re.match(surface.steer, line):
                continue
            block = "\n".join(lines[i : i + surface.window])
            if "cause=DENY_CAUSE_" not in block and "cause=cause" not in block:
                unnamed.append(i + 1)
        assert not unnamed, unnamed

    def test_every_steer_declares_whether_its_site_audited_first(self, name):
        # The orphan reject on cancellation audits ONLY for a site whose caller
        # audits after the wire; an audit-first site would otherwise gain a
        # second SEL row for one decision. The flag must match the site's real
        # audit order, which the previous test measured.
        surface = _SURFACES[name]
        if name == "channel":
            # channel reuses llm_helpers' helper, whose orphan reject never
            # audits (every site on both surfaces is audit-first), so it
            # carries no flag by construction.
            assert channel_mod._steer_host_deny is llm_helpers._steer_host_deny
            return
        lines = _lines(surface)
        wrong: list[int] = []
        for i, site in _walk(surface, lines):
            steer = _last_match(surface.steer, lines, max(0, i - surface.window), i)
            if steer < 0:
                continue
            block = "\n".join(lines[steer : steer + surface.window])
            expected = f"audited={site.audit_first}"
            if expected not in block:
                wrong.append((i + 1, expected))
        # The decider path steers inside _steer_deny_cause, not at the reject.
        if name == "driver":
            src = "\n".join(lines)
            assert (
                "audited=False"
                in src.split("async def _steer_host_deny", 1)[0].rsplit(
                    "async def _steer_deny_cause", 1
                )[1]
            )
        assert not wrong, wrong


def test_the_local_helpers_delegate_to_the_shared_one():
    # One spelling of the notice for the whole codebase (see
    # test_approval_timeout_cause.TestTheBoundedSteerSpellings): each surface's
    # helper may only redact and forward, never build or send on its own. The
    # channel has no helper of its own: it reuses llm_helpers' trio.
    import inspect

    assert channel_mod._steer_host_deny is llm_helpers._steer_host_deny
    helpers = (
        h._steer_host_deny,
        llm_helpers._steer_host_deny,
        driver_mod.TurnDriver._steer_host_deny,
    )
    for fn in helpers:
        src = inspect.getsource(fn)
        assert "steer_refusal_notice(" in src, fn
        assert "cause=cause" in src, fn
        assert ".steer(" not in src, fn
        assert inspect.signature(fn).parameters["cause"].default is inspect.Parameter.empty
    for fn in (h._steer_host_deny, driver_mod.TurnDriver._steer_host_deny):
        assert inspect.signature(fn).parameters["audited"].default is inspect.Parameter.empty
