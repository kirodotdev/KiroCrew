"""The relay viewer's gateway routes: the Crew list and the desktop app's token mint.

Off by default (404 ``RELAY_VIEWER_OFF``), the list is owner-only and carries no
token, the mint needs loopback plus the local secret (a session alone is refused),
neither route is reachable through the relay, and every answer is ``no-store``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from token_auth_helpers import _make_request, _ok_handler, clear_nonces  # noqa: F401

from kiro_crew.dashboard.handlers import relay_viewer as rv
from kiro_crew.dashboard.kiro_relay import limits
from kiro_crew.dashboard.kiro_relay.caller import RELAY_CALLER_KEY, RelayCaller, RelayCallerKind
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.member_memory_auth import LocalOwnerProvenance
from kiro_crew.relay_viewer import cps

SECRET = "local-secret-value"
BEARER = "bearer-sign-in-9f8e7d6c5b4a"
ACCESS = "access-token-1a2b3c4d5e6f"
EP = "a" * 24
OTHER = "b" * 24
CREW = cps.CrewEndpoint(
    endpoint_id=EP,
    crew_url=f"https://{EP}.relay.example.test",
    name="Build box",
    crew_type="PERSONAL",
    presence="CONNECTED",
    relay_connection_status="CONNECTED",
)
OWNER = RelayCaller(kiro_user_id="kiro-user.owner-0001", kind=RelayCallerKind.OWNER)


@pytest.fixture
def viewer(monkeypatch):
    """The viewer on, CPS faked, the owner and host-process gates passing."""
    state = SimpleNamespace(enabled=True, calls=[], owner=True, provenance=LocalOwnerProvenance())
    tunnel = SimpleNamespace(
        relay_viewer_cps_url="https://cps.example.test", relay_viewer_credential=""
    )

    def _tunnel():
        tunnel.relay_viewer_enabled = state.enabled
        return tunnel

    async def _bearer(ref):
        return BEARER

    async def _list(url, bearer):
        state.calls.append(("list", bearer))
        return (CREW,)

    async def _mint(url, bearer, endpoint_id):
        state.calls.append(("mint", endpoint_id))
        return cps.CrewAccessToken(ACCESS, 2000.0, 240.0, 1700.0)

    async def _owner(request, operation):
        return None if state.owner else web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(rv, "_load_tunnel", _tunnel)
    monkeypatch.setattr(rv, "_bearer", _bearer)
    monkeypatch.setattr(cps, "list_crew_endpoints", _list)
    monkeypatch.setattr(cps, "create_crew_access_token", _mint)
    monkeypatch.setattr(rv, "require_owner_dashboard_request", _owner)
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.local_owner_bootstrap_provenance",
        lambda _r: state.provenance,
    )
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: MagicMock())
    return state


def _client(relayed: bool = False) -> TestClient:
    @web.middleware
    async def _relay(request, handler):
        if relayed:
            request[RELAY_CALLER_KEY] = OWNER
        return await handler(request)

    app = web.Application(middlewares=[_relay])
    app["local_secret"] = SECRET
    app.router.add_get("/api/relay-viewer/crews", rv.api_relay_viewer_crews)
    app.router.add_post("/api/relay-viewer/token", rv.api_relay_viewer_token)
    return TestClient(TestServer(app))


async def _mint(client, headers=None, body=None):
    resp = await client.post(
        "/api/relay-viewer/token",
        json={"endpointId": EP} if body is None else body,
        headers={"X-Local-Secret": SECRET} if headers is None else headers,
    )
    await resp.read()  # kept, so the body is readable after the client closes
    return resp


@pytest.mark.asyncio
async def test_both_routes_answer_404_while_off(viewer) -> None:
    viewer.enabled = False
    async with _client() as client:
        for resp in (await client.get("/api/relay-viewer/crews"), await _mint(client)):
            assert resp.status == 404
            assert (await resp.json())["code"] == "RELAY_VIEWER_OFF"
            assert resp.headers["Cache-Control"] == "no-store"
    assert viewer.calls == []


@pytest.mark.asyncio
async def test_crews_lists_for_the_owner_with_no_token(viewer, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    async with _client() as client:
        resp = await client.get("/api/relay-viewer/crews")
        text = await resp.text()
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert (await resp.json())["crews"] == [
        {
            "endpointId": EP,
            "name": "Build box",
            "crewType": "PERSONAL",
            "presence": "CONNECTED",
            "relayConnectionStatus": "CONNECTED",
            "crewUrl": CREW.crew_url,
        }
    ]
    assert BEARER not in text and BEARER not in caplog.text


@pytest.mark.asyncio
async def test_crews_needs_the_owner(viewer) -> None:
    viewer.owner = False
    async with _client() as client:
        resp = await client.get("/api/relay-viewer/crews")
    assert resp.status == 403
    assert resp.headers["Cache-Control"] == "no-store"
    assert viewer.calls == []


@pytest.mark.asyncio
async def test_a_cps_refusal_passes_its_code_through(viewer, monkeypatch) -> None:
    async def _throttled(url, bearer):
        raise cps.CpsRefused(cps.THROTTLED, "slow down", status=429, retry_after=7.0)

    monkeypatch.setattr(cps, "list_crew_endpoints", _throttled)
    async with _client() as client:
        resp = await client.get("/api/relay-viewer/crews")
        body = await resp.json()
    assert (resp.status, body["code"], body["retryAfter"]) == (429, "THROTTLED", 7.0)
    assert resp.headers["Retry-After"] == "7"


@pytest.mark.asyncio
async def test_token_mints_for_main_with_the_local_secret(viewer, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    async with _client() as client:
        resp = await _mint(client)
        body = await resp.json()
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert body == {
        "accessToken": ACCESS,
        "expiresAt": 2000.0,
        "renewInSeconds": 240.0,
        "serverDate": 1700.0,
        "crewUrl": CREW.crew_url,
        "name": "Build box",
    }
    assert viewer.calls == [("list", BEARER), ("mint", EP)]
    assert ACCESS not in caplog.text and BEARER not in caplog.text


@pytest.mark.parametrize(
    "headers,code",
    [
        ({}, "invalid_secret"),
        ({"X-Local-Secret": "wrong"}, "invalid_secret"),
        # A dashboard session alone is not the local secret.
        ({"Cookie": "mc_token_5476=session-value"}, "invalid_secret"),
    ],
)
@pytest.mark.asyncio
async def test_token_needs_the_local_secret(viewer, headers, code) -> None:
    async with _client() as client:
        resp = await _mint(client, headers=headers)
    assert resp.status == 403
    assert (await resp.json())["code"] == code
    assert resp.headers["Cache-Control"] == "no-store"
    assert viewer.calls == []


@pytest.mark.asyncio
async def test_token_needs_loopback(viewer, monkeypatch) -> None:
    monkeypatch.setattr(rv, "is_loopback", lambda _h: False)
    monkeypatch.setattr(rv, "_unix_peer_is_self", lambda _r: False)
    async with _client() as client:
        resp = await _mint(client)
    assert resp.status == 403
    assert (await resp.json())["code"] == "loopback_only"
    assert viewer.calls == []


@pytest.mark.parametrize("provenance", [None, LocalOwnerProvenance(app="some-app")])
@pytest.mark.asyncio
async def test_token_needs_a_host_process_not_an_app(viewer, provenance) -> None:
    viewer.provenance = provenance
    async with _client() as client:
        resp = await _mint(client)
    assert resp.status == 403
    assert (await resp.json())["code"] == "member_owner_token_refused"
    assert viewer.calls == []


@pytest.mark.parametrize("body", [{}, {"endpointId": "NOT-AN-ID"}, ["x"]])
@pytest.mark.asyncio
async def test_token_refuses_a_bad_endpoint_without_calling_cps(viewer, body) -> None:
    async with _client() as client:
        resp = await _mint(client, body=body)
    assert resp.status == 400
    assert (await resp.json())["code"] == "INVALID_REQUEST"
    assert viewer.calls == []


@pytest.mark.asyncio
async def test_token_refuses_a_crew_not_in_the_list(viewer) -> None:
    async with _client() as client:
        resp = await _mint(client, body={"endpointId": OTHER})
    assert resp.status == 403
    assert (await resp.json())["code"] == "CREW_ACCESS_NOT_GRANTED"
    assert viewer.calls == [("list", BEARER)]


@pytest.mark.asyncio
async def test_crews_refuses_a_relayed_caller(viewer) -> None:
    async with _client(relayed=True) as client:
        resp = await client.get("/api/relay-viewer/crews")
    assert resp.status == 403
    assert resp.headers["Cache-Control"] == "no-store"
    assert viewer.calls == []


@pytest.mark.parametrize("path", ["/api/relay-viewer/crews", "/api/relay-viewer/token"])
@pytest.mark.parametrize("kind", list(RelayCallerKind))
def test_the_relay_limits_refuse_both_routes(path, kind) -> None:
    caller = RelayCaller(kiro_user_id="kiro-user.someone-0003", kind=kind)
    got = limits.remote_limit_for(path, {}, caller, strict_internal_paths=())
    assert got is limits.LOCAL_CREDENTIAL


@pytest.mark.asyncio
async def test_token_auth_bypass_is_post_only_and_crews_is_gated() -> None:
    mw = token_auth_middleware()
    post = await mw(_make_request(path="/api/relay-viewer/token", method="POST"), _ok_handler)
    assert post.status == 200
    for method, path in (("GET", "/api/relay-viewer/token"), ("GET", "/api/relay-viewer/crews")):
        resp = await mw(_make_request(path=path, method=method), _ok_handler)
        assert resp.status == 403


@pytest.mark.asyncio
async def test_sign_in_missing_is_signed_out(monkeypatch) -> None:
    from kiro_crew.auth.provider import KasAuthProvider, NotAuthenticated

    async def _none(self):
        raise NotAuthenticated("no stored Kiro credential")

    monkeypatch.setattr(KasAuthProvider, "current", _none)
    with pytest.raises(cps.CpsRefused) as exc:
        await rv._bearer("")
    assert exc.value.code == cps.SIGNED_OUT
