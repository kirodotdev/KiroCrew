"""Dashboard Stop and delete release the completion ownership a goal holds.

The helpers and the autouse SEL fixture are ``test_autonudge_handlers_cov80``'s.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from test_autonudge_handlers_cov80 import _body, _FakeSvc, _loop, _mk, _svc, sel_mock  # noqa: F401

from kiro_crew.dashboard.handlers import autonudge as h


@pytest.mark.asyncio
async def test_direct_user_delete_overrides_attached_child_work(
    monkeypatch: pytest.MonkeyPatch, sel_mock: MagicMock  # noqa: F811
) -> None:
    """The child-work fence applies to agent directives, never human Stop."""

    class _HostileSubagents:
        def running_agents_for(self, _session_key: str) -> list[str]:
            raise AssertionError("direct user Stop must not consult the agent fence")

    svc = _svc(monkeypatch, _FakeSvc([_loop("lp-1", "chat-5-555")]))
    request = _mk(
        "DELETE",
        "/api/autonudge/lp-1",
        match={"loop_id": "lp-1"},
        state=MagicMock(subagents=_HostileSubagents(), owner_id=""),
    )

    assert _body(await h.api_autonudge_delete(request)) == {"ok": True}
    assert svc.removed == ["lp-1"]
    assert sel_mock.log_tool_invocation.call_args.kwargs["outcome"] == "success"


@pytest.mark.asyncio
async def test_human_delete_releases_retained_completion_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful dashboard delete clears a failed one-shot completion fence."""
    subagents = MagicMock()
    state = SimpleNamespace(owner_id="", subagents=subagents)
    _svc(monkeypatch, _FakeSvc([_loop("lp-1", "chat-5-555")]))
    request = _mk(
        "DELETE",
        "/api/autonudge/lp-1",
        match={"loop_id": "lp-1"},
        state=state,
    )

    assert _body(await h.api_autonudge_delete(request)) == {"ok": True}
    subagents.release_completion_deliveries_for.assert_called_once_with("dashboard:chat-5-555")


@pytest.mark.asyncio
async def test_human_delete_releases_the_real_dashboard_ledger_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The release reaches the manager ledger keyed by the parent session key.

    A loop's binding is the bare slot key, while a child's
    ``parent_session_key`` is ``dashboard:<slot>``; releasing the binding as
    is would leave the fence held until restart.
    """
    from kiro_crew.subagent import SubagentManager

    manager = SubagentManager(sessions=MagicMock(), ctx_builder=None)  # type: ignore[arg-type]
    manager.retain_completion_delivery("dashboard:chat-5-555", "child-1")
    manager.retain_completion_delivery("dashboard:chat-6-666", "child-2")
    assert manager.terminal_delivery_inflight_for("dashboard:chat-5-555") is True
    _svc(monkeypatch, _FakeSvc([_loop("lp-1", "chat-5-555")]))
    request = _mk(
        "DELETE",
        "/api/autonudge/lp-1",
        match={"loop_id": "lp-1"},
        state=SimpleNamespace(owner_id="", subagents=manager),
    )

    assert _body(await h.api_autonudge_delete(request)) == {"ok": True}
    assert manager.terminal_delivery_inflight_for("dashboard:chat-5-555") is False
    assert manager.terminal_delivery_inflight_for("dashboard:chat-6-666") is True


@pytest.mark.parametrize(
    ("binding", "ledger_key"),
    [
        ("chat-5-555", "dashboard:chat-5-555"),
        ("dashboard:chat-5-555", "dashboard:chat-5-555"),
        ("slack:C1|1700000000.000100", "slack:C1|1700000000.000100"),
        ("discord:kirocrew:direct:42", "discord:kirocrew:direct:42"),
        ("", ""),
    ],
)
def test_completion_owner_key_inverts_the_loop_binding(binding: str, ledger_key: str) -> None:
    from kiro_crew.autonudge import binding_key_for

    assert h._completion_owner_key_for_binding(binding) == ledger_key
    if ledger_key:
        assert binding_key_for(ledger_key) == binding.removeprefix("dashboard:")
