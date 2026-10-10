"""F150 — internal-auth bypass fix on POST /api/chat/mode and siblings.

Verifies that ``deny_session_approval_caller`` in
``src/kiro_crew/dashboard/chat_handlers.py`` now gates internal-auth callers:

* Bare internal-auth (no session key) is refused on every operation.
* A verified ``cron:`` key is admitted only on ``chat_mode``; refused on every
  other operation (``chat_slot_approve``, ``approval_resolve``).
* The dashboard owner and app-token paths are unaffected.
* ``cron_script.set_session_mode`` (the one real cron path) still sets its
  own slot's mode via the handler.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_state(tmp_path):
    from kiro_crew.dashboard.state import DashboardState
    from kiro_crew.history import ConversationLog

    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


def _internal_req(path: str = "/api/chat/mode", *, session_key: str = "") -> web.Request:
    """A bare internal-auth request — no app claim, no user."""
    app = web.Application()
    app["state"] = MagicMock()
    headers = {}
    if session_key:
        headers["X-Session-Key"] = session_key
    req = make_mocked_request("POST", path, app=app, headers=headers)
    req["internal_auth"] = True
    return req


def _cron_req(
    path: str = "/api/chat/mode",
    job_id: str = "test-job-1",
) -> web.Request:
    """Internal-auth + attested cron:<id> session key (peer_verified=True)."""
    app = web.Application()
    app["state"] = MagicMock()
    req = make_mocked_request(
        "POST",
        path,
        app=app,
        headers={"X-Session-Key": f"cron:{job_id}"},
    )
    req["internal_auth"] = True
    req["peer_verified"] = True
    return req


def _owner_req(path: str = "/api/chat/mode") -> web.Request:
    """Dashboard-owner request — no internal auth."""
    app = web.Application()
    app["state"] = MagicMock()
    req = make_mocked_request("POST", path, app=app)
    req["app"] = ""
    req["user"] = "local-app"
    return req


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _mock_sel(_floor_monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    recorder = SimpleNamespace(log_api_access=lambda **kw: None)
    _floor_monkeypatch.setattr(chat_handlers, "sel", lambda: recorder)
    _floor_monkeypatch.setattr("kiro_crew.sel.sel", lambda: recorder)


# ---------------------------------------------------------------------------
# 1. Bare internal-auth is refused on chat_mode
# ---------------------------------------------------------------------------


def test_bare_internal_auth_refused_on_chat_mode() -> None:
    """deny_session_approval_caller refuses internal_auth=True with no session key."""
    from kiro_crew.dashboard.chat_handlers import deny_session_approval_caller

    req = _internal_req()
    result = _run(deny_session_approval_caller(req, "chat_mode"))
    assert result is not None, "Bare internal-auth must be refused on chat_mode"
    assert result.status == 403


# ---------------------------------------------------------------------------
# 2. Bare internal-auth is refused on chat_slot_approve
# ---------------------------------------------------------------------------


def test_bare_internal_auth_refused_on_chat_slot_approve() -> None:
    """deny_session_approval_caller refuses bare internal-auth on chat_slot_approve."""
    from kiro_crew.dashboard.chat_handlers import deny_session_approval_caller

    req = _internal_req()
    result = _run(deny_session_approval_caller(req, "chat_slot_approve"))
    assert result is not None
    assert result.status == 403


# ---------------------------------------------------------------------------
# 3. Bare internal-auth is refused on approval_resolve operation
# ---------------------------------------------------------------------------


def test_bare_internal_auth_refused_on_approval_resolve() -> None:
    """deny_session_approval_caller refuses bare internal-auth on approval_resolve."""
    from kiro_crew.dashboard.chat_handlers import deny_session_approval_caller

    req = _internal_req()
    result = _run(deny_session_approval_caller(req, "approval_resolve"))
    assert result is not None
    assert result.status == 403


# ---------------------------------------------------------------------------
# 4. Non-owner (no auth at all) is refused on chat_mode
# ---------------------------------------------------------------------------


def test_non_owner_refused_on_chat_mode() -> None:
    """deny_session_approval_caller refuses a caller with no owner identity."""
    from kiro_crew.dashboard.chat_handlers import deny_session_approval_caller
    from kiro_crew.dashboard.state import DashboardState

    app = web.Application()
    state_mock = MagicMock(spec=DashboardState)
    state_mock.owner_id = ""
    app["state"] = state_mock
    req = make_mocked_request("POST", "/api/chat/mode", app=app)
    # No internal_auth, no app, no user — pure anonymous.

    result = _run(deny_session_approval_caller(req, "chat_mode"))
    assert result is not None
    assert result.status == 403


# ---------------------------------------------------------------------------
# 5. Verified cron: key is admitted on chat_mode only
# ---------------------------------------------------------------------------


def test_verified_cron_key_passes_on_chat_mode() -> None:
    """A verified cron: key passes deny_session_approval_caller on chat_mode."""
    from kiro_crew.dashboard.chat_handlers import deny_session_approval_caller

    req = _cron_req()
    result = _run(deny_session_approval_caller(req, "chat_mode"))
    assert result is None, (
        "Verified cron: key must be admitted on chat_mode; "
        f"got {result.status if result else None}"
    )


# ---------------------------------------------------------------------------
# 6. Verified cron: key is refused on chat_slot_approve
# ---------------------------------------------------------------------------


def test_verified_cron_key_refused_on_chat_slot_approve() -> None:
    """A cron: key is NOT admitted on chat_slot_approve — only chat_mode."""
    from kiro_crew.dashboard.chat_handlers import deny_session_approval_caller

    req = _cron_req()
    result = _run(deny_session_approval_caller(req, "chat_slot_approve"))
    assert result is not None, "Verified cron: key must be refused on chat_slot_approve"
    assert result.status == 403


# ---------------------------------------------------------------------------
# 7. Verified cron: key is refused on approval_resolve
# ---------------------------------------------------------------------------


def test_verified_cron_key_refused_on_approval_resolve() -> None:
    """A cron: key is NOT admitted on approval_resolve — only chat_mode."""
    from kiro_crew.dashboard.chat_handlers import deny_session_approval_caller

    req = _cron_req()
    result = _run(deny_session_approval_caller(req, "approval_resolve"))
    assert result is not None
    assert result.status == 403


# ---------------------------------------------------------------------------
# 8. Owner is admitted on chat_mode
# ---------------------------------------------------------------------------


def test_owner_passes_on_chat_mode() -> None:
    """The dashboard owner is admitted on chat_mode."""
    from kiro_crew.dashboard.chat_handlers import deny_session_approval_caller
    from kiro_crew.dashboard.state import DashboardState

    app = web.Application()
    state_mock = MagicMock(spec=DashboardState)
    state_mock.owner_id = ""
    app["state"] = state_mock
    req = make_mocked_request("POST", "/api/chat/mode", app=app)
    req["app"] = ""
    req["user"] = "local-app"

    result = _run(deny_session_approval_caller(req, "chat_mode"))
    assert result is None, f"Owner must be admitted; got {result}"


# ---------------------------------------------------------------------------
# 9. Owner is admitted on chat_slot_approve
# ---------------------------------------------------------------------------


def test_owner_passes_on_chat_slot_approve() -> None:
    """The dashboard owner is admitted on chat_slot_approve."""
    from kiro_crew.dashboard.chat_handlers import deny_session_approval_caller
    from kiro_crew.dashboard.state import DashboardState

    app = web.Application()
    state_mock = MagicMock(spec=DashboardState)
    state_mock.owner_id = ""
    app["state"] = state_mock
    req = make_mocked_request("POST", "/api/chat/slots/s1/approve", app=app)
    req["app"] = ""
    req["user"] = "local-app"

    result = _run(deny_session_approval_caller(req, "chat_slot_approve"))
    assert result is None, f"Owner must be admitted on chat_slot_approve; got {result}"


# ---------------------------------------------------------------------------
# 10. cron_script.set_session_mode path: cron still sets its own slot's mode
#
# Drives the FULL handler (api_chat_mode) with a TestClient, an internal-request
# middleware that sets internal_auth + peer_verified, and a real cron-slot state.
# This is the same shape as test_chat_mode_cron.py so the test proves the
# handler path ScriptContext._post("/api/chat/mode", ...) uses is still open.
# ---------------------------------------------------------------------------


@web.middleware
async def _internal_middleware(request: web.Request, handler):
    request["internal_auth"] = True
    request["peer_verified"] = True
    return await handler(request)


_CRON_KEY = "cron:test-job-99"


def _cron_slot_from_header(req: web.Request) -> str:
    """Test stand-in for cron_slot_creator: reads attestation from header."""
    if req.get("internal_auth") is not True:
        return ""
    key = req.headers.get("X-Session-Key", "")
    return key if key.startswith("cron:") else ""


async def _cron_slot_from_header_async(req: web.Request) -> str:
    return _cron_slot_from_header(req)


@pytest.mark.asyncio
async def test_cron_set_session_mode_still_works(tmp_path, monkeypatch) -> None:
    """cron_script.set_session_mode path: an attested cron: key sets its own slot's mode.

    Verifies end-to-end that the handler still returns 200 + {"ok": True}
    for a cron caller with a verified cron: key on chat_mode, after the F150
    fix that refuses bare internal-auth.
    """
    from kiro_crew.dashboard.chat_handlers import api_chat_mode
    from kiro_crew.dashboard.state import SlotOrigin
    from kiro_crew.safety_override import reset_singleton

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    reset_singleton()

    state = _make_state(tmp_path)
    state.owner_id = ""
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()

    # A slot the cron created.
    slot = state.get_or_create_slot("cron-slot-1", origin=SlotOrigin.CRON)
    slot._created_by = _CRON_KEY

    # Stand in for cron_slot_creator so it reads from the header (no token sig).
    from kiro_crew.dashboard import chat_handlers as ch

    monkeypatch.setattr(ch, "cron_slot_creator", _cron_slot_from_header_async)

    app = web.Application(middlewares=[_internal_middleware])
    app["state"] = state
    app.router.add_post("/api/chat/mode", api_chat_mode)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/mode",
            json={"slot": "cron-slot-1", "mode": "trust"},
            headers={"X-Session-Key": _CRON_KEY},
        )
        status = resp.status
        body = await resp.json()

    reset_singleton()

    assert status == 200, f"Expected 200, got {status}: {body}"
    assert body.get("ok") is True, f"Expected ok=True, got {body}"
    assert slot._trust is True, "Slot must have trust mode set"
