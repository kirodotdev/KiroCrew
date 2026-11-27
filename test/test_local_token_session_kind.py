"""``GET /api/token/local?kind=session`` mints a session, for a client with no cookie.

The token is the one ``kirocrew token --bearer`` prints. It is accepted where a
session is (the cookie path, and ``Authorization: Bearer`` on the Crew API) and
refused as a ``?token=`` link; without the parameter the mint is the link it always was.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from dashboard_handlers_core_coverage_helpers import fake_sel_fixture  # noqa: F401
from dashboard_handlers_core_coverage_helpers import _req
from token_auth_helpers import clear_nonces  # noqa: F401 -- autouse, registers by import

from kiro_crew import member_memory_auth as auth
from kiro_crew.dashboard.crew_api_errors import CREDENTIAL_HINT
from kiro_crew.dashboard.handlers import core as core_mod
from kiro_crew.dashboard.token_auth import validate_token
from kiro_crew.dashboard.token_auth_tokens import LINK_NOT_A_SESSION, validate_token_with_app

_PEER_PID = 4242


@pytest.fixture
def owner_process(monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
    monkeypatch.setattr(auth, "_request_peer_pid", lambda _request: _PEER_PID)
    monkeypatch.setattr(
        auth.platform_compat,
        "get_process_start_id",
        lambda pid: "synthetic-start" if pid == _PEER_PID else None,
    )
    monkeypatch.setattr(auth, "_verified_host_process", lambda pid: pid == _PEER_PID)
    monkeypatch.setattr(auth, "_gateway_spawned_app_backend", lambda _pid: None)


async def _mint(query: dict, secret: str = "right"):
    return await core_mod.api_token_local(
        _req(
            app={"local_secret": "right", "state": SimpleNamespace(owner_id="owner-1")},
            headers={"X-Local-Secret": secret},
            query=query,
        )
    )


@pytest.mark.asyncio
async def test_kind_session_mints_a_session_that_is_not_a_link(fake_sel, owner_process) -> None:
    resp = await _mint({"kind": "session", "ttl": "1h"})
    body = json.loads(resp.body)
    assert (resp.status, body["expires_in"]) == (200, 3600)
    assert validate_token_with_app(body["token"], use_session_exp=True)[:2] == (True, "owner-1")
    assert validate_token(body["token"])[0] is False


@pytest.mark.asyncio
async def test_without_the_parameter_the_mint_is_still_a_link(fake_sel, owner_process) -> None:
    token = json.loads((await _mint({})).body)["token"]
    assert validate_token(token)[:2] == (True, "owner-1")
    assert validate_token_with_app(token, use_session_exp=True)[2] == LINK_NOT_A_SESSION


@pytest.mark.asyncio
async def test_a_session_is_refused_at_the_same_gates_as_a_link(fake_sel, owner_process) -> None:
    resp = await _mint({"kind": "session"}, secret="wrong")
    assert (resp.status, json.loads(resp.body)["code"]) == (403, "invalid_secret")


def _record_binds(monkeypatch, ta) -> list:
    bound: list = []
    bind = ta.bind_token_peer

    def record(*args, **kwargs):
        bound.append((args, kwargs))
        return bind(*args, **kwargs)

    monkeypatch.setattr(ta, "bind_token_peer", record)
    return bound


@pytest.mark.asyncio
async def test_local_session_bearer_first_use_keeps_its_peer_and_expiry(
    fake_sel, owner_process, monkeypatch
) -> None:
    """An empty binding map (what a restart leaves): the bearer's first request pins it."""
    from token_auth_helpers import _make_request, _ok_handler

    from kiro_crew.dashboard import token_auth as ta

    token = json.loads((await _mint({"kind": "session", "ttl": "1h"})).body)["token"]
    expiry = json.loads(ta._b64url_decode(token.split(".")[0]))["session_exp"]
    bound = _record_binds(monkeypatch, ta)
    middleware = ta.token_auth_middleware(port=5476)
    fields = {"path": "/v1/models", "headers": {"Authorization": f"Bearer {token}"}}
    assert not ta._state.has_binding(token)
    first = await middleware(_make_request(**fields), _ok_handler)
    assert first.status == 200
    assert bound == [((token, "ip:127.0.0.1", expiry), {"proxied": False})]
    assert ta.check_token_peer(token, "ip:127.0.0.1") == (True, "")
    replay = await middleware(_make_request(remote="127.0.0.2", **fields), _ok_handler)
    assert replay.status == 401
    assert json.loads(replay.body)["error"]["message"] == f"IP mismatch {CREDENTIAL_HINT}"
    again = await middleware(_make_request(**fields), _ok_handler)
    assert again.status == 200
    assert len(bound) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["cookie", "stale-link-cookie"])
async def test_local_session_cookie_first_use_stays_unbound(
    fake_sel, owner_process, monkeypatch, transport
) -> None:
    """The same session as a cookie, with no verified peer, roams as an ordinary cookie."""
    from token_auth_helpers import _make_request, _ok_handler

    from kiro_crew.dashboard import token_auth as ta

    token = json.loads((await _mint({"kind": "session", "ttl": "1h"})).body)["token"]
    bound = _record_binds(monkeypatch, ta)
    middleware = ta.token_auth_middleware(port=5476)
    fields: dict = {"path": "/v1/models", "cookies": {"mc_token_5476": token}}
    if transport == "stale-link-cookie":
        fields.update(path="/api/status", query={"token": "expired-link"})
    assert not ta._state.has_binding(token)
    first = await middleware(_make_request(**fields), _ok_handler)
    assert first.status == 200
    assert bound == []
    assert not ta._state.has_binding(token)
    elsewhere = await middleware(_make_request(remote="127.0.0.2", **fields), _ok_handler)
    assert elsewhere.status == 200
    assert bound == []


@pytest.mark.asyncio
async def test_an_app_backend_is_refused_a_session_and_still_gets_its_link(
    fake_sel, owner_process, monkeypatch
) -> None:
    """The refusal is for ``kind=session`` alone, and nothing is signed for it."""
    monkeypatch.setattr(auth, "_verified_host_process", lambda _pid: False)
    monkeypatch.setattr(
        auth, "_gateway_spawned_app_backend", lambda pid: "demo-app" if pid == _PEER_PID else None
    )
    signed: list = []
    sign = core_mod.generate_token

    def record(*args, **kwargs):
        signed.append(kwargs)
        return sign(*args, **kwargs)

    monkeypatch.setattr(core_mod, "generate_token", record)
    refused = await _mint({"kind": "session"})
    assert (refused.status, json.loads(refused.body)["code"]) == (
        403,
        "session_kind_refused_for_app",
    )
    assert signed == []
    row = fake_sel.log_api_access.call_args.kwargs
    assert (row["outcome"], row["resources"]) == ("denied", "session-kind-app-backend app=demo-app")
    link = await _mint({})
    assert link.status == 200
    assert validate_token(json.loads(link.body)["token"])[:2] == (True, "owner-1")
    assert len(signed) == 1
