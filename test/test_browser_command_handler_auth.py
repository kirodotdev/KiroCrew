"""Auth guard for ``POST /api/browser/command``.

The handler is a MACHINE-only strict route reached by the ``browser`` MCP tool
over the AF_UNIX internal-API socket, where ``request.remote`` is empty. It must
gate on ``request["internal_auth"] is True`` ALONE (mirroring
``api_computer_use_invoke``) -- an added ``is_loopback(request.remote)`` re-assert
would 403 every unix-socket op with ``loopback_only``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import messaging as mod


@pytest.mark.asyncio
async def test_missing_internal_auth_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_sel", lambda: MagicMock())
    req = make_mocked_request("POST", "/api/browser/command")
    resp = await mod.api_browser_command(req)
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "loopback_only"


@pytest.mark.asyncio
async def test_non_loopback_remote_passes_the_guard_when_internal_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The guard must depend ONLY on internal_auth, never on request.remote.
    # make_mocked_request's remote is not a loopback IP, so under the old
    # `is_loopback(request.remote)` re-assert this would have 403'd even with the
    # secret; now it proceeds and only the empty body trips op_required.
    monkeypatch.setattr(mod, "_sel", lambda: MagicMock())
    req = make_mocked_request("POST", "/api/browser/command")
    req["internal_auth"] = True
    req._read_bytes = b"{}"  # short-circuits request.json() -> reaches op check
    resp = await mod.api_browser_command(req)
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "op_required"
