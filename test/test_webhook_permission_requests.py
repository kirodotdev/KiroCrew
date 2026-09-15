"""Webhook turns must ANSWER tool permission requests, not sit on them.

``_run_hook_inner`` consumed only ``EVENT_TEXT_CHUNK`` / ``EVENT_COMPLETE``, so
an ``EVENT_PERMISSION_REQUEST`` was never approved or rejected: the provider
waited for a decision that never came, the turn idled into the
``_run_hook_agent`` timeout, and the watchdog reported it as cancelled by the
user. These tests drive the real ``_run_hook_inner`` with a fake client whose
stream yields a permission request followed by a complete event and lock in
the headless contract shared with every other non-interactive runner:

- deny by default (no hook gate, or a gate verdict that would ASK a user on an
  interactive surface) — ``reject_tool`` awaited exactly once with the
  request id, and the denial SEL-audited with ``source="webhook"`` BEFORE the
  wire call;
- approve only on the gate's affirmative ``TOOL_AUTO_APPROVE``;
- the turn still returns promptly with its text instead of hitting the
  timeout path.
"""

from __future__ import annotations

import asyncio

from kiro_crew import name_grant
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    AcpEvent,
    TurnUsage,
)
from kiro_crew.dashboard.handlers import usage
from kiro_crew.dashboard.handlers.hooks import (
    _EVENT_PERMISSION_REQUEST_KIND,
    _run_hook_inner,
)
from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, TOOL_DENY, ToolHookResult

_REQUEST_ID = "perm-req-9790"

# Far above any plausible test runtime, far below the 300s production
# timeout: a turn that answers the request finishes in milliseconds, so
# tripping this bound means the request was left unanswered.
_PROMPT_BOUND_S = 10


def _permission_event(**overrides: object) -> AcpEvent:
    defaults: dict = dict(
        kind=EVENT_PERMISSION_REQUEST,
        title="Run a tool",
        tool_kind="execute",
        request_id=_REQUEST_ID,
    )
    defaults.update(overrides)
    return AcpEvent(**defaults)


class _FakeClient:
    """Stand-in for the ACP client: one permission request, then complete."""

    def __init__(self, permission_event: AcpEvent | None = None) -> None:
        self.approved: list[str | int] = []
        self.rejected: list[str | int] = []
        self._permission_event = permission_event or _permission_event()
        # read_effective_agent / _resolve_model walk the wrapper chain for these.
        self._agent = "kirocrew"
        self._model = "claude-test"

    async def stream(self, _message: str):
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="hello ")
        yield self._permission_event
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="world")
        yield AcpEvent(kind=EVENT_COMPLETE, usage=TurnUsage(duration_ms=0, credits=1.0))

    async def approve_tool(self, request_id, *, always: bool = False) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id) -> None:
        self.rejected.append(request_id)


class _FakeSessions:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    async def get_or_create(self, _session_key: str, agent: str | None = None):
        # is_new=False so _run_hook_inner skips context building (no embed pool).
        return (self._client, False, False)

    def record_success(self, _session_key: str) -> None:
        pass


class _FakeGate:
    def __init__(self, result: ToolHookResult | Exception) -> None:
        self._result = result
        self.calls: list[dict] = []

    def on_tool_call(self, tool_name: str, **kwargs) -> ToolHookResult:
        self.calls.append({"tool_name": tool_name, **kwargs})
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeContextBuilder:
    """Only what the permission branch reads; falsy-safe conversation_log."""

    conversation_log = None

    def __init__(self, gate: _FakeGate) -> None:
        self.hooks = gate


class _FakeState:
    def __init__(self, client: _FakeClient, context_builder=None) -> None:
        self.sessions = _FakeSessions(client)
        self.context_builder = context_builder


class _RecordingSel:
    def __init__(self) -> None:
        self.tool_rows: list[dict] = []

    def log_tool_invocation(self, **kwargs) -> None:
        self.tool_rows.append(kwargs)

    def __getattr__(self, _name):  # any other SEL surface: ignore
        return lambda *a, **k: None


def _drive(monkeypatch, gate_action: str | None, *, gate_result=None, event=None):
    """Run one webhook turn; return (client, sel, gate, result_text)."""
    sel = _RecordingSel()
    # _sel() in handlers/hooks.py resolves through the handlers package's
    # late-binding sel() exactly for this patch point.
    import kiro_crew.dashboard.handlers as handlers_pkg

    monkeypatch.setattr(handlers_pkg, "sel", lambda: sel)
    # Keep the usage row off the real shards (same seam the turn-duration
    # tests intercept).
    monkeypatch.setattr(usage, "_write_token_record", lambda _record, _now: None)

    client = _FakeClient(event)
    if gate_result is None and gate_action is not None:
        gate_result = ToolHookResult(action=gate_action)
    gate = _FakeGate(gate_result) if gate_result is not None else None
    builder = _FakeContextBuilder(gate) if gate is not None else None
    result_text = asyncio.run(
        asyncio.wait_for(
            _run_hook_inner(_FakeState(client, builder), "hook:test:9790", "go", None),
            timeout=_PROMPT_BOUND_S,
        )
    )
    return client, sel, gate, result_text


def _denial_rows(sel: _RecordingSel) -> list[dict]:
    return [r for r in sel.tool_rows if r.get("outcome") == "denied"]


def test_no_gate_rejects_audits_and_still_returns_text(monkeypatch):
    """Deny by default: no hook gate -> audit the denial, reject, keep going."""
    client, sel, _gate, result_text = _drive(monkeypatch, gate_action=None)

    assert client.rejected == [_REQUEST_ID], "reject_tool must be awaited exactly once"
    assert client.approved == []

    rows = _denial_rows(sel)
    assert len(rows) == 1, "exactly one SEL denial row"
    row = rows[0]
    assert row["source"] == "webhook"
    assert row["request_id"] == _REQUEST_ID
    assert row["tool_name"] == "Run a tool"

    # The turn completed promptly (wait_for above) and kept its text.
    assert result_text == "hello world"


def test_gate_deny_is_audited_then_rejected(monkeypatch):
    """A TOOL_DENY verdict rejects, with the denial attributed to the gate."""
    client, sel, gate, result_text = _drive(monkeypatch, gate_action=TOOL_DENY)

    assert client.rejected == [_REQUEST_ID]
    assert client.approved == []
    assert len(gate.calls) == 1
    # The gate got the security-relevant fields, not just the display title.
    assert gate.calls[0]["session_key"] == "hook:test:9790"
    assert gate.calls[0]["tool_kind"] == "execute"

    rows = _denial_rows(sel)
    assert len(rows) == 1
    assert rows[0]["error"] == "hook_deny"
    assert rows[0]["source"] == "webhook"
    assert result_text == "hello world"


def test_gate_would_ask_fails_closed(monkeypatch):
    """TOOL_ALLOW means "ask the user" interactively; headless it must deny."""
    client, sel, _gate, _text = _drive(monkeypatch, gate_action=TOOL_ALLOW)

    assert client.rejected == [_REQUEST_ID]
    assert client.approved == []
    rows = _denial_rows(sel)
    assert len(rows) == 1
    assert rows[0]["error"] == "no_interactive_approver"


def test_gate_auto_approve_approves_once_and_audits(monkeypatch):
    """The gate's affirmative auto-approve is honoured and SEL-audited."""
    client, sel, _gate, result_text = _drive(monkeypatch, gate_action=TOOL_AUTO_APPROVE)

    assert client.approved == [_REQUEST_ID], "approve_tool must be awaited exactly once"
    assert client.rejected == []
    assert _denial_rows(sel) == []

    approvals = [r for r in sel.tool_rows if r.get("outcome") == "auto_approved"]
    assert len(approvals) == 1
    assert approvals[0]["source"] == "webhook"
    assert approvals[0]["request_id"] == _REQUEST_ID
    assert result_text == "hello world"


def test_gate_exception_denies_and_still_answers(monkeypatch):
    """A raising gate must not leave the request unanswered (the stall)."""
    client, sel, _gate, result_text = _drive(
        monkeypatch, None, gate_result=RuntimeError("gate blew up")
    )

    assert client.rejected == [_REQUEST_ID]
    assert client.approved == []
    rows = _denial_rows(sel)
    assert len(rows) == 1
    assert rows[0]["error"] == "gate_error"
    assert result_text == "hello world"


def test_auto_approve_is_name_grant_verified_headless(monkeypatch):
    """A withheld name grant denies: no approver exists to downgrade to."""
    refusal = name_grant.Refusal(code="shadowed", detail="PATH leads elsewhere")

    async def _refuse(_event):
        return refusal

    monkeypatch.setattr(name_grant, "refusal_for_event", _refuse)
    client, sel, _gate, _text = _drive(monkeypatch, TOOL_AUTO_APPROVE)

    assert client.rejected == [_REQUEST_ID], "the withheld grant must deny"
    assert client.approved == []
    rows = _denial_rows(sel)
    assert len(rows) == 1
    assert rows[0]["error"] == "name_grant_headless_reject"
    # The decline itself is audited too (name_grant.log_decline through _sel).
    declines = [r for r in sel.tool_rows if (r.get("metadata") or {}).get("reason") == "name_grant"]
    assert len(declines) == 1
    assert declines[0]["source"] == "webhook"


def test_low_fidelity_child_auto_approve_is_denied(monkeypatch):
    """A child event with unverified security context cannot ride a
    title-derived auto-approve; headless the downgrade is deny."""
    event = _permission_event(sub_session_id="child-1")
    assert event.child_low_fidelity, "premise: this event is low-fidelity"
    client, sel, _gate, _text = _drive(monkeypatch, TOOL_AUTO_APPROVE, event=event)

    assert client.rejected == [_REQUEST_ID]
    assert client.approved == []
    rows = _denial_rows(sel)
    assert len(rows) == 1
    assert rows[0]["error"] == "child_low_fidelity"


def test_identity_grant_covers_verified_child(monkeypatch):
    """The identity-keyed grant still stands for a child whose MCP identity is
    verified: both sides of the match are non-agent-authored."""
    event = _permission_event(
        sub_session_id="child-1",
        mcp_server_name="approved-server",
        tool_name="list_things",
        mcp_identity_trusted=True,
    )
    assert event.child_mcp_identity_trusted, "premise: identity verified"
    client, sel, _gate, _text = _drive(
        monkeypatch,
        None,
        gate_result=ToolHookResult(action=TOOL_AUTO_APPROVE, identity_grant=True),
        event=event,
    )

    assert client.approved == [_REQUEST_ID]
    assert client.rejected == []
    assert _denial_rows(sel) == []


def test_unauditable_auto_approve_is_denied(monkeypatch):
    """Audit-or-deny: an auto-approve whose SEL write raises must not run."""
    sel_calls: list[dict] = []

    class _RaisingSel(_RecordingSel):
        def log_tool_invocation(self, **kwargs) -> None:
            sel_calls.append(kwargs)
            if kwargs.get("outcome") == "auto_approved":
                raise OSError("audit disk full")

    import kiro_crew.dashboard.handlers as handlers_pkg

    raising = _RaisingSel()
    monkeypatch.setattr(handlers_pkg, "sel", lambda: raising)
    monkeypatch.setattr(usage, "_write_token_record", lambda _record, _now: None)

    client = _FakeClient()
    builder = _FakeContextBuilder(_FakeGate(ToolHookResult(action=TOOL_AUTO_APPROVE)))
    result_text = asyncio.run(
        asyncio.wait_for(
            _run_hook_inner(_FakeState(client, builder), "hook:test:9790", "go", None),
            timeout=_PROMPT_BOUND_S,
        )
    )

    assert client.rejected == [_REQUEST_ID], "unaudited approval must become a reject"
    assert client.approved == []
    assert result_text == "hello world"
    # The auto-approve audit was ATTEMPTED with critical=True before the wire.
    attempted = [c for c in sel_calls if c.get("outcome") == "auto_approved"]
    assert attempted and attempted[0].get("critical") is True
    # The decision itself is still recorded: a best-effort denial row names
    # the audit failure so the permission decision cannot vanish from SEL.
    denials = [c for c in sel_calls if c.get("outcome") == "denied"]
    assert len(denials) == 1
    assert denials[0].get("error") == "audit_write_failed"


def test_permission_kind_constant_matches_the_wire_vocabulary():
    """The handler spells the event kind locally (the agent-sdk boundary gate
    forbids adding an ACP import edge); this pin breaks if the vocabulary moves."""
    assert _EVENT_PERMISSION_REQUEST_KIND == EVENT_PERMISSION_REQUEST
