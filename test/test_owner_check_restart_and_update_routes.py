"""Who may call ``POST /api/sessions/restart``, ``/api/update/cancel`` and ``/api/update/simulate``.

Each route is judged once per caller kind:

- the owner's dashboard session runs the handler and gets its usual answer;
- a dashboard session that is not the owner's is refused ``403 owner_only``;
- a dashboard session signed before an owner was configured is refused
  ``401 stale_session_reauth``;
- an app token and an internal caller are refused on the two update routes,
  and are left to their own controls on the restart route.

No session is reset and no update step runs: the restart work and the update
progress state are doubles, and a refused request must not reach them.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard import handlers
from kiro_crew.dashboard.handlers import _shared
from kiro_crew.dashboard.handlers import source_providers as source
from kiro_crew.dashboard.handlers.sessions_parts import restart

OWNER = "U_OWNER"

# name -> (user claim, app claim; ``None`` leaves the claim absent)
_CALLERS: dict[str, tuple[str | None, str | None]] = {
    "owner": (OWNER, ""),
    "non_owner_dashboard": ("U_OTHER", ""),
    "stale_session": ("local-startup", ""),
    "app_token": ("app:some-app", "some-app"),
    "internal": (None, None),
}

_REFUSALS = {
    "non_owner_dashboard": (403, "owner_only"),
    "stale_session": (401, source.STALE_OWNER_SESSION_CODE),
    "app_token": (403, "owner_only"),
    "internal": (403, "owner_only"),
}


@pytest.fixture(autouse=True)
def _audit(_floor_monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """The security-event log is a double; a refusal's record is read from it."""
    audit = MagicMock()
    _floor_monkeypatch.setattr("kiro_crew.sel.sel", lambda: audit)
    return audit


@pytest.fixture
def reset_all(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stand in for the restart work, so no session or MCP config is touched."""
    reset = AsyncMock(return_value=3)
    monkeypatch.setattr(restart, "_reset_all_sessions", reset)
    monkeypatch.setattr(restart, "sync_discovered_servers", lambda: ["one"])
    # The module-level guard asks whether an internal caller is a private member.
    monkeypatch.setattr(_shared, "internal_memory_scope", AsyncMock(return_value=(None, None)))
    return reset


def _request(caller: str, body: object = None) -> web.Request:
    user, app_claim = _CALLERS[caller]
    app = web.Application()
    state = MagicMock()
    state.owner_id = OWNER
    state._background_tasks = set()
    app["state"] = state
    request = make_mocked_request("POST", "/x", app=app)
    if user is not None:
        request["user"] = user
    if app_claim is not None:
        request["app"] = app_claim
    request.json = AsyncMock(return_value={} if body is None else body)  # type: ignore[method-assign]
    return request


def _json(resp: web.StreamResponse) -> dict:
    assert isinstance(resp, web.Response) and resp.body is not None
    return json.loads(bytes(resp.body).decode())


def _assert_refused(resp: web.StreamResponse, caller: str) -> None:
    status, code = _REFUSALS[caller]
    assert resp.status == status
    assert _json(resp)["code"] == code


# ── POST /api/update/cancel ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_cancel_answers_the_owner_as_before() -> None:
    request = _request("owner")
    resp = await handlers.api_update_cancel(request)
    assert resp.status == 200
    assert _json(resp) == {"ok": True}
    state = request.app["state"]
    assert state.clear_update_progress.call_count == 2
    state.push_update_progress.assert_called_once_with("failed", "Update cancelled by user")


@pytest.mark.parametrize("caller", sorted(_REFUSALS))
@pytest.mark.asyncio
async def test_update_cancel_refuses_every_other_caller(caller: str, _audit: MagicMock) -> None:
    request = _request(caller)
    resp = await handlers.api_update_cancel(request)
    _assert_refused(resp, caller)
    state = request.app["state"]
    state.clear_update_progress.assert_not_called()
    state.push_update_progress.assert_not_called()
    assert _audit.log_api_access.call_args.kwargs["operation"] == "update.cancel"
    assert _audit.log_api_access.call_args.kwargs["outcome"] == "denied"


# ── POST /api/update/simulate ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_simulate_answers_the_owner_as_before() -> None:
    # ``reject`` is the simulator's own synchronous branch: it answers without
    # starting the background walk, so nothing is left running.
    request = _request("owner", {"reject": True, "reject_message": "nope"})
    resp = await handlers.api_update_simulate(request)
    assert resp.status == 409
    assert _json(resp) == {"error": "nope"}


@pytest.mark.parametrize("caller", sorted(_REFUSALS))
@pytest.mark.asyncio
async def test_update_simulate_refuses_every_other_caller(caller: str, _audit: MagicMock) -> None:
    request = _request(caller, {"delay": 0})
    resp = await handlers.api_update_simulate(request)
    _assert_refused(resp, caller)
    state = request.app["state"]
    request.json.assert_not_awaited()  # type: ignore[attr-defined]
    state.push_update_progress.assert_not_called()
    assert state._background_tasks == set()
    assert _audit.log_api_access.call_args.kwargs["operation"] == "update.simulate"


# ── POST /api/sessions/restart ───────────────────────────────────────────────

_RESTART_OK = {"ok": True, "sessions_reset": 3, "mcp_synced": 1, "mcp_sync_ok": True}


@pytest.mark.parametrize("caller", ["owner", "app_token", "internal"])
@pytest.mark.asyncio
async def test_sessions_restart_answers_these_callers_as_before(
    caller: str, reset_all: AsyncMock, _audit: MagicMock
) -> None:
    """The owner runs it; an app token and an internal caller are not judged here."""
    resp = await handlers.api_sessions_restart(_request(caller))
    assert resp.status == 200
    assert _json(resp) == _RESTART_OK
    reset_all.assert_awaited_once()
    _audit.log_api_access.assert_not_called()


@pytest.mark.parametrize("caller", ["non_owner_dashboard", "stale_session"])
@pytest.mark.asyncio
async def test_sessions_restart_refuses_a_dashboard_session_that_is_not_the_owner(
    caller: str, reset_all: AsyncMock, _audit: MagicMock
) -> None:
    resp = await handlers.api_sessions_restart(_request(caller))
    _assert_refused(resp, caller)
    reset_all.assert_not_awaited()
    assert _audit.log_api_access.call_args.kwargs["operation"] == "api_sessions_restart"
    assert _audit.log_api_access.call_args.kwargs["outcome"] == "denied"
