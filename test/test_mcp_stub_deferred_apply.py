"""``POST /api/mcp-gateway/servers/stub``: approvals that defer the apply, and the one apply.

A dialog approving several servers records each approval with ``defer_apply`` and then
sends ``{"apply": true}`` once, so the broker restarts and the open chats reset once
for the whole dialog instead of once per approval.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from handlers_mcp_coverage_helpers import _effective_stubs, _payload, _request

from kiro_crew.dashboard.handlers import mcp as mcp_mod

_APPLIED = {"applied": True, "restart_required": False, "stub_servers": ["ok-mcp"]}


@pytest.mark.asyncio
async def test_a_deferred_approval_is_recorded_without_an_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.config.loader import config_path

    monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
    apply = AsyncMock(return_value=_APPLIED)
    resp = await mcp_mod.api_mcp_gateway_set_stub(
        _request(
            {"name": "ok-mcp", "stub": True, "defer_apply": True},
            state=SimpleNamespace(_mcp_gateway_apply_stub=apply),
        )
    )
    assert resp.status == 200
    assert _payload(resp) == {
        "ok": True,
        "name": "ok-mcp",
        "stub": True,
        "applied": False,
        "restart_required": False,
        "apply_deferred": True,
    }
    apply.assert_not_awaited()
    saved = json.loads(config_path().read_text(encoding="utf-8"))
    assert _effective_stubs(saved["mcp_gateway"]) == ["ok-mcp"]


@pytest.mark.asyncio
async def test_the_apply_runs_once_as_an_approval_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.config.loader import config_path

    monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
    apply = AsyncMock(return_value=_APPLIED)
    state = SimpleNamespace(_mcp_gateway_apply_stub=apply)
    for name in ("ok-mcp", "two-mcp"):
        resp = await mcp_mod.api_mcp_gateway_set_stub(
            _request({"name": name, "stub": True, "defer_apply": True}, state=state)
        )
        assert resp.status == 200
    before = config_path().read_text(encoding="utf-8")

    resp = await mcp_mod.api_mcp_gateway_set_stub(_request({"apply": True}, state=state))

    assert resp.status == 200
    assert _payload(resp) == {"ok": True, **_APPLIED}
    apply.assert_awaited_once_with(launch_approved=True)
    assert config_path().read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_the_apply_with_no_gateway_wired_reports_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
    resp = await mcp_mod.api_mcp_gateway_set_stub(
        _request({"apply": True}, state=SimpleNamespace())
    )
    assert resp.status == 200
    assert _payload(resp) == {"ok": True, "applied": False, "restart_required": True}


@pytest.mark.asyncio
async def test_a_failed_apply_is_500(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
    apply = AsyncMock(side_effect=RuntimeError("broker down"))
    resp = await mcp_mod.api_mcp_gateway_set_stub(
        _request({"apply": True}, state=SimpleNamespace(_mcp_gateway_apply_stub=apply))
    )
    assert resp.status == 500
    assert _payload(resp)["code"] == "mcp_apply_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,code",
    [
        ({"apply": False}, "apply_invalid"),
        ({"apply": True, "name": "ok-mcp", "stub": True}, "apply_invalid"),
        ({"name": "ok-mcp", "stub": True, "defer_apply": "yes"}, "defer_apply_invalid"),
        ({"name": "ok-mcp", "stub": False, "defer_apply": True}, "defer_apply_invalid"),
        ({"names": ["ok-mcp"], "stub": False, "defer_apply": True}, "defer_apply_invalid"),
    ],
)
async def test_an_invalid_apply_or_deferral_is_400(
    monkeypatch: pytest.MonkeyPatch, body: dict, code: str
) -> None:
    monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
    apply = AsyncMock(return_value=_APPLIED)
    resp = await mcp_mod.api_mcp_gateway_set_stub(
        _request(body, state=SimpleNamespace(_mcp_gateway_apply_stub=apply))
    )
    assert resp.status == 400
    assert _payload(resp)["code"] == code
    apply.assert_not_awaited()
