"""The agent-written ``__tool_use_purpose`` is never a tool identity.

``build_permission_event`` fills ``tool_purpose`` from the
model's ``rawInput.__tool_use_purpose``. The auto-improvement runner's
allowlist substring-matches a tool's identity, so if the purpose stood in for a
missing ``kind``, a kindless write whose purpose says "Read the module" would
pass a ``["Read", "Grep", "Glob"]`` allowlist.

These tests drive the REAL permission builder with a kiro-shaped frame (no
``toolCall.kind``, no earlier ``tool_call``) and then the runner's own
permission branch.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from kiro_crew.acp._dispatch import build_permission_event
from kiro_crew.acp.types import EVENT_COMPLETE, JsonRpcMessage
from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as R

_PURPOSE = "Read the module and apply the fix"


def _kindless_write_with_a_read_purpose():
    params = {
        "sessionId": "s",
        "toolCall": {
            "toolCallId": "t1",
            "title": "Writing /repo/src/x.py",
            "rawInput": {
                "__tool_use_purpose": _PURPOSE,
                "path": "/repo/src/x.py",
                "command": "create",
                "file_text": "x",
            },
        },
        "options": [
            {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
            {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
        ],
    }
    ev, _ = build_permission_event(
        JsonRpcMessage.from_dict(
            {"jsonrpc": "2.0", "id": 7, "method": "session/request_permission", "params": params}
        )
    )
    # Preconditions: this is the shape the bug needs. If the builder ever
    # stops reporting the purpose, the test below would pass vacuously.
    assert ev.tool_kind == ""
    assert ev.tool_purpose == _PURPOSE
    return ev


class _Provider:
    def __init__(self, events):
        self._events = list(events)
        self.approved: list = []
        self.rejected: list = []

    async def start(self) -> None:
        return None

    def stream(self, prompt):
        events = self._events

        class _It:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if not events:
                    raise StopAsyncIteration
                return events.pop(0)

        return _It()

    async def approve_tool(self, rid) -> bool:
        self.approved.append(rid)
        return True

    async def reject_tool(self, rid) -> None:
        self.rejected.append(rid)

    async def shutdown(self) -> None:
        return None


@pytest.fixture
def quiet_audit(monkeypatch):
    """Accept the runner's audit writes without touching the real log."""

    class _Sel:
        def log_tool_invocation(self, **kw) -> None:
            return None

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())


@pytest.mark.asyncio
async def test_a_purpose_never_passes_the_read_only_allowlist(monkeypatch, quiet_audit):
    seen_kinds: list = []

    def _gov(ev, **kw):
        seen_kinds.append(kw.get("tool_kind"))
        return ""

    monkeypatch.setattr(R, "_governance_denial", _gov)
    ev = _kindless_write_with_a_read_purpose()
    provider = _Provider([ev, SimpleNamespace(kind=EVENT_COMPLETE)])
    runner = R.SessionAgentRunner()
    res = await runner._run_async(
        "prompt",
        factory=lambda key, **k: provider,
        cwd="/tmp/wt",
        append_system=None,
        timeout_s=30.0,
        t0=time.monotonic(),
        allowed_tools=["Read", "Grep", "Glob"],
    )
    assert res.ok is True
    assert provider.approved == [], "the agent's purpose text was read as a Read tool"
    assert provider.rejected == [ev.request_id]
    assert _PURPOSE not in seen_kinds, "the governance gate was told the purpose is the tool"


def test_governance_gate_never_takes_the_purpose_as_the_tool_kind(monkeypatch):
    seen: dict = {}

    class _Manager:
        def __init__(self, cfg):
            pass

        def on_tool_call(self, name, **kw):
            seen["name"] = name
            seen.update(kw)
            return SimpleNamespace(action="allow", reason="")

    monkeypatch.setattr(
        R, "KiroCrewConfig", SimpleNamespace(load=lambda: SimpleNamespace(hooks={}))
    )
    monkeypatch.setattr(R, "hooks_config_from_config_dict", lambda d: d)
    monkeypatch.setattr(R, "HookManager", _Manager)

    ev = _kindless_write_with_a_read_purpose()
    assert R._governance_denial(ev, session_key="s", agent="a") == ""
    assert seen.get("tool_kind") != _PURPOSE
    assert _PURPOSE not in (seen.get("name") or "")
