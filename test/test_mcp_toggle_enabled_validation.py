"""The three MCP toggle routes honour only a real boolean ``enabled``.

``POST /api/mcp/toggle``, ``/api/mcp/toggle-tool`` and ``/api/mcp/toggle-all``
wrote ``body.get("enabled", True)`` into ``mcp.json`` as given. A JSON client
sending the string ``"false"`` for an off switch hit the truthy branch, so the
server (or tool) was turned ON while the route answered ``{"ok": true}``. The
routes now refuse a non-boolean with the 400 ``invalid_enabled`` the cron,
updates and webhook toggle routes already use, and an absent field still means
``true``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from handlers_mcp_coverage_helpers import sandbox_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import _payload, _read_global, _request, _write_global

from kiro_crew.dashboard.handlers import mcp as mcp_mod

pytestmark = pytest.mark.asyncio

# (id, handler, the body that should be refused)
_NON_BOOL = ["false", "true", 1, 0, None, [False], "yes"]


def _toggle_body(handler_id: str, enabled: Any) -> dict[str, Any]:
    if handler_id == "toggle":
        return {"name": "srv", "enabled": enabled}
    if handler_id == "toggle-tool":
        return {"server": "srv", "tool": "t", "enabled": enabled}
    return {"enabled": enabled}  # toggle-all


_HANDLERS = {
    "toggle": mcp_mod.api_mcp_toggle,
    "toggle-tool": mcp_mod.api_mcp_toggle_tool,
    "toggle-all": mcp_mod.api_mcp_toggle_all,
}


@pytest.mark.parametrize("handler_id", list(_HANDLERS))
@pytest.mark.parametrize("value", _NON_BOOL)
async def test_non_boolean_enabled_is_refused_and_changes_nothing(
    sandbox: SimpleNamespace, handler_id: str, value: Any
) -> None:
    # The server starts enabled (no `disabled` flag). A non-boolean `enabled`
    # must not reach the write: the 400 lands before any mutation.
    _write_global(sandbox, {"srv": {"command": "x"}})
    resp = await _HANDLERS[handler_id](_request(_toggle_body(handler_id, value)))
    payload = _payload(resp)
    assert resp.status == 400, payload
    assert payload.get("code") == "invalid_enabled", payload
    assert isinstance(payload.get("error"), str) and payload["error"], payload
    # Nothing was written to the global config and no agent sync ran, so the
    # server is exactly as enabled as it was.
    assert "disabled" not in _read_global(sandbox)["srv"]
    assert sandbox.synced == []
    assert sandbox.batched == []


@pytest.mark.parametrize("handler_id", list(_HANDLERS))
async def test_real_boolean_false_disables(sandbox: SimpleNamespace, handler_id: str) -> None:
    # A real boolean still applies: enabled=False writes disabled=true.
    _write_global(sandbox, {"srv": {"command": "x"}})
    resp = await _HANDLERS[handler_id](_request(_toggle_body(handler_id, False)))
    assert resp.status == 200, _payload(resp)
    srv = _read_global(sandbox)["srv"]
    if handler_id == "toggle-tool":
        # toggle-tool records the tool under disabledTools rather than a server flag.
        assert "t" in srv.get("disabledTools", [])
    else:
        assert srv.get("disabled") is True


@pytest.mark.parametrize("handler_id", ["toggle", "toggle-all"])
async def test_absent_enabled_means_true(sandbox: SimpleNamespace, handler_id: str) -> None:
    # An omitted `enabled` keeps the on default these routes always assumed: a
    # server that was disabled is re-enabled (the `disabled` flag is cleared).
    _write_global(sandbox, {"srv": {"command": "x", "disabled": True}})
    body = {"name": "srv"} if handler_id == "toggle" else {}
    resp = await _HANDLERS[handler_id](_request(body))
    assert resp.status == 200, _payload(resp)
    assert _read_global(sandbox)["srv"].get("disabled") in (False, None)
