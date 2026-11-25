"""What a header a proxy might add is worth on a Crew API request (``/v1/``): nothing.

A request reaches the gateway's loopback listener either from a program on the same
machine or from a proxy on it that forwards for a caller elsewhere. Both look alike
at the socket. These tests pin that the credential alone decides who the caller is:
a forwarding header, a header that names a person and a relay header a client wrote
each leave a request with no credential at 401, leave a non-owner credential a
non-owner, and leave the owner's bearer the owner's.

The socket tests run the relay-path rule, the client-sent relay-header strip, the
entry gate and token auth, in that order and without the barriers that sit between
the entry gate and token auth in the real chain, before the real ``/v1/models``
route, over a loopback connection.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_request,
    _session_token,
    clear_nonces,
)

import kiro_crew.dashboard.token_auth as ta
from kiro_crew.dashboard import crew_api_models
from kiro_crew.dashboard.entry_gate import entry_gate_middleware
from kiro_crew.dashboard.kiro_relay.ingress import relay_ingress_middleware
from kiro_crew.dashboard.origin import is_direct_local_request, is_proxied_request
from kiro_crew.dashboard.relay_headers import relay_header_strip_middleware
from kiro_crew.dashboard.routes import crew_api
from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware

MODELS = "/v1/models"
OWNER = "owner"

#: Headers a tunnel or reverse proxy adds, each claiming the caller is this machine.
_FORWARDING = [
    pytest.param({"X-Forwarded-For": "127.0.0.1"}, id="x-forwarded-for"),
    pytest.param({"Forwarded": "for=127.0.0.1;proto=https"}, id="forwarded"),
    pytest.param({"X-Real-IP": "127.0.0.1"}, id="x-real-ip"),
    pytest.param({"X-Forwarded-Host": "localhost"}, id="x-forwarded-host"),
    pytest.param({"X-Forwarded-Proto": "https"}, id="x-forwarded-proto"),
]

#: Headers that name a person, as an authenticating proxy or a relay would write them.
_NAMES_THE_OWNER = [
    pytest.param({"X-Forwarded-User": OWNER}, id="x-forwarded-user"),
    pytest.param({"X-Forwarded-Email": "owner@example.com"}, id="x-forwarded-email"),
    pytest.param({"X-Remote-User": OWNER}, id="x-remote-user"),
    pytest.param({"X-Auth-Request-User": OWNER}, id="x-auth-request-user"),
    pytest.param({"X-Kiro-Entry-User": OWNER}, id="entry-user"),
    pytest.param({"X-Kiro-Relay-User": OWNER}, id="relay-user"),
    pytest.param(
        {"X-Kiro-Relay-User": OWNER, "X-Kiro-Relay-Handover": "guessed"},
        id="relay-user-and-handover",
    ),
    pytest.param(
        {"X-Forwarded-For": "127.0.0.1", "X-Forwarded-User": OWNER},
        id="forwarded-for-and-user",
    ),
]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def gateway(monkeypatch):
    """A client of the real ``/v1/models`` route behind the front of the real chain."""
    read: list[str] = []

    def _catalog():
        read.append("catalog")
        return []

    monkeypatch.setattr(crew_api_models, "usable_models", _catalog)
    # The app is allowed the path, so the refusal it gets is the owner gate's own.
    monkeypatch.setattr(ta, "_app_api_allowlist", lambda name: (MODELS,))
    app = web.Application(
        middlewares=[
            relay_ingress_middleware(strict_internal_paths=frozenset()),
            relay_header_strip_middleware,
            entry_gate_middleware,
            token_auth_middleware(),
        ]
    )
    app["state"] = SimpleNamespace(owner_id=OWNER)
    crew_api.register(app)
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        client.catalog_reads = read
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", _FORWARDING + _NAMES_THE_OWNER)
async def test_a_header_with_no_credential_is_401(gateway, headers) -> None:
    resp = await gateway.get(MODELS, headers=headers)
    assert resp.status == 401
    assert resp.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
    assert (await resp.json())["error"]["type"] == "authentication_error"
    assert gateway.catalog_reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", _FORWARDING + _NAMES_THE_OWNER)
async def test_a_header_beside_a_forged_bearer_is_401(gateway, headers) -> None:
    token = _session_token(OWNER)
    forged = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    resp = await gateway.get(MODELS, headers={**_bearer(forged), **headers})
    assert resp.status == 401
    assert gateway.catalog_reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", _NAMES_THE_OWNER)
async def test_a_header_naming_the_owner_does_not_make_an_app_the_owner(gateway, headers) -> None:
    app_token = generate_token("local-app", app="file-explorer")
    resp = await gateway.get(MODELS, headers={**_bearer(app_token), **headers})
    error = (await resp.json())["error"]
    assert resp.status == 403
    assert (error["type"], error["code"]) == ("permission_error", "owner_only")
    assert gateway.catalog_reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", _FORWARDING + _NAMES_THE_OWNER)
async def test_the_owners_bearer_is_served_whatever_header_rides_beside_it(
    gateway, headers
) -> None:
    resp = await gateway.get(MODELS, headers={**_bearer(_session_token(OWNER)), **headers})
    assert resp.status == 200
    assert await resp.json() == {"object": "list", "data": []}
    assert "Set-Cookie" not in resp.headers
    assert gateway.catalog_reads == ["catalog"]


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", _NAMES_THE_OWNER)
async def test_the_caller_is_the_credentials_subject_not_the_one_a_header_names(headers) -> None:
    reached: list = []

    async def _handler(request):
        reached.append(request)
        return web.Response(text="ok")

    named = {name: value.replace(OWNER, "mallory") for name, value in headers.items()}
    request = _make_request(path=MODELS, headers={**_bearer(_session_token(OWNER)), **named})
    resp = await token_auth_middleware(port=5476)(request, _handler)
    assert resp.status == 200
    published = {call.args[0]: call.args[1] for call in reached[0].__setitem__.call_args_list}
    assert published["user"] == OWNER
    assert published["app"] == ""


@pytest.mark.parametrize("headers", _FORWARDING)
def test_a_forwarding_header_naming_loopback_is_forwarded_and_never_local(headers) -> None:
    request = _make_request(path=MODELS, headers=headers)
    request.get = lambda key, default=None: default
    assert is_direct_local_request(request) is False
    assert is_proxied_request(request) is True


@pytest.mark.parametrize("headers", _NAMES_THE_OWNER[:4])
def test_a_header_naming_a_person_does_not_mark_a_request_forwarded(headers) -> None:
    """Only the forwarding headers say a proxy is in the path; a name says nothing."""
    request = _make_request(path=MODELS, headers=headers)
    request.get = lambda key, default=None: default
    assert is_direct_local_request(request) is True
    assert is_proxied_request(request) is False
