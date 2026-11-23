"""How a Crew API request (``/v1/``) authenticates: the ``Authorization`` header.

Pins that a ``/v1/`` path takes a bearer credential and verifies it, that ``?token=``
is refused there, that the dashboard's own cookie still works on ``/v1/`` and that
``/api/`` and the frozen ``/v1/chat/completions`` read their credential as before. With
a tailnet allowlist, a bearer meets the forwarded-peer resolution and its denials as the
cookie does; the daemon's ``whois`` is mocked at ``tailnet._run_json_detail``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_request,
    _session_token,
    clear_nonces,
)

from kiro_crew.dashboard import crew_api_auth, tailnet
from kiro_crew.dashboard.tailnet import TailnetTrust
from kiro_crew.dashboard.token_auth import (
    generate_token,
    revoke_access_cookie,
    token_auth_middleware,
)

MODELS = "/v1/models"
COOKIE = "mc_token_5476"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _send(**request_fields):
    """The middleware's answer, and the claims the handler got (``None`` if refused)."""
    from aiohttp import web

    reached: list = []

    async def _handler(request):
        reached.append(request)
        return web.Response(text="ok")

    resp = await token_auth_middleware(port=5476)(_make_request(**request_fields), _handler)
    return resp, (_claims(reached[0]) if reached else None)


def _claims(request) -> dict:
    """What the middleware published on the mock request (``request[key] = value``)."""
    return {call.args[0]: call.args[1] for call in request.__setitem__.call_args_list}


def _assert_refused_401(resp, reached) -> None:
    assert reached is None
    assert resp.status == 401
    assert resp.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
    assert json.loads(resp.body)["error"]["type"] == "authentication_error"


@pytest.mark.asyncio
async def test_a_valid_bearer_session_reaches_the_handler_and_mints_no_cookie() -> None:
    resp, reached = await _send(path=MODELS, headers=_bearer(_session_token("owner")))
    assert resp.status == 200
    assert reached["user"] == "owner"
    assert reached["is_dashboard_user"] is True
    assert reached["auth_from_query_token"] is False
    assert not resp.cookies


@pytest.mark.asyncio
async def test_no_credential_is_401() -> None:
    _assert_refused_401(*await _send(path=MODELS))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential",
    [
        pytest.param("not-a-token", id="malformed"),
        pytest.param("", id="empty"),
        pytest.param("eyJzdWIiOiJvd25lciJ9.AAAA", id="bad-signature"),
    ],
)
async def test_a_wrong_bearer_is_401(credential) -> None:
    _assert_refused_401(*await _send(path=MODELS, headers=_bearer(credential)))


@pytest.mark.asyncio
async def test_a_tampered_session_is_401() -> None:
    payload, signature = _session_token("owner").split(".")
    forged = payload + "." + signature[:-2] + ("AA" if signature[-2:] != "AA" else "BB")
    _assert_refused_401(*await _send(path=MODELS, headers=_bearer(forged)))


@pytest.mark.asyncio
async def test_a_one_time_link_is_not_a_bearer_credential() -> None:
    _assert_refused_401(*await _send(path=MODELS, headers=_bearer(generate_token("owner"))))


@pytest.mark.asyncio
async def test_a_logged_out_session_is_401() -> None:
    token = _session_token("owner")
    assert revoke_access_cookie(token) is True
    _assert_refused_401(*await _send(path=MODELS, headers=_bearer(token)))


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [MODELS, "/v1/responses", "/v1/crew/owner/bots"])
async def test_a_query_token_on_the_crew_api_is_401_even_when_valid(path) -> None:
    link, session = generate_token("owner"), _session_token("owner")
    _assert_refused_401(*await _send(path=path, query={"token": link}))
    # Refused as a request, whatever else it carries.
    _assert_refused_401(*await _send(path=path, query={"token": link}, headers=_bearer(session)))
    _assert_refused_401(*await _send(path=path, query={"token": ""}, cookies={COOKIE: session}))


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["Bearer", "Bearer not-a-token", "bearer "])
async def test_a_bearer_header_that_fails_is_not_rescued_by_the_cookie(header) -> None:
    resp, reached = await _send(
        path=MODELS, headers={"Authorization": header}, cookies={COOKIE: _session_token("owner")}
    )
    _assert_refused_401(resp, reached)


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic b3duZXI6eA=="}])
async def test_the_dashboard_cookie_still_works_on_the_crew_api(headers) -> None:
    """Also behind a proxy that adds its own ``Basic`` header to every request."""
    resp, reached = await _send(
        path=MODELS, headers=headers, cookies={COOKIE: _session_token("owner")}
    )
    assert resp.status == 200
    assert reached["user"] == "owner"


@pytest.mark.asyncio
async def test_another_scheme_is_no_credential() -> None:
    session = _session_token("owner")
    _assert_refused_401(*await _send(path=MODELS, headers={"Authorization": f"Basic {session}"}))


@pytest.mark.asyncio
async def test_the_middleware_asks_the_verifier_and_obeys_it(monkeypatch) -> None:
    """The gate verifies: what ``verify_bearer`` says decides, not the header's presence."""
    seen: list[str] = []

    async def _refuse(credential):
        seen.append(credential)
        return False, "", "refused by the verifier", ""

    monkeypatch.setattr(crew_api_auth, "verify_bearer", _refuse)
    _assert_refused_401(*await _send(path=MODELS, headers=_bearer(_session_token("owner"))))
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_an_app_token_in_the_header_is_still_held_to_its_scope(monkeypatch) -> None:
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_app_api_allowlist", lambda name: ())
    token = generate_token("file-explorer", app="file-explorer")
    resp, reached = await _send(path="/v1/crew/owner/bots", headers=_bearer(token))
    assert reached is None
    assert resp.status == 403
    assert json.loads(resp.body)["error"]["code"] == "owner_only"


@pytest.mark.asyncio
async def test_an_app_session_bearer_takes_no_address_pin(monkeypatch) -> None:
    """An app's session sent as a bearer, with no verified peer, keeps no address pin."""
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_app_api_allowlist", lambda name: ("/v1/responses",))
    token = generate_token("file-explorer", app="file-explorer", session=True)
    assert not _ta._state.has_binding(token)
    resp, reached = await _send(path="/v1/responses", headers=_bearer(token))
    assert (resp.status, reached["app"]) == (200, "file-explorer")
    assert not _ta._state.has_binding(token)
    resp, reached = await _send(path="/v1/responses", headers=_bearer(token), remote="127.0.0.2")
    assert (resp.status, reached["app"]) == (200, "file-explorer")
    assert not _ta._state.has_binding(token)


@pytest.mark.asyncio
async def test_the_dashboard_routes_do_not_read_the_authorization_header() -> None:
    session = _session_token("owner")
    resp, reached = await _send(path="/api/status", headers=_bearer(session))
    assert (resp.status, reached) == (403, None)
    resp, reached = await _send(path="/api/status", query={"token": generate_token("owner")})
    assert resp.status == 200 and reached is not None


@pytest.mark.asyncio
async def test_the_frozen_chat_completions_route_keeps_its_query_token() -> None:
    resp, reached = await _send(
        path="/v1/chat/completions", method="POST", query={"token": generate_token("owner")}
    )
    assert resp.status == 200 and reached is not None


ALICE, MALLORY, NOBODY = "100.64.0.11", "100.64.0.13", "100.64.0.99"
ALLOWLIST = TailnetTrust(trust_identity=True, allowed_logins=("alice@example.com",))
UNREADABLE = TailnetTrust(trust_identity=True, identity_unknown=True)


@pytest.fixture
def _tailnet(_floor_monkeypatch):
    """A tailnet daemon whose ``whois`` names Alice and Mallory and nobody else."""
    import kiro_crew.dashboard.token_auth as ta

    names = {ALICE: "alice@example.com", MALLORY: "mallory@example.com"}

    def whois(argv: list[str]):
        login = names.get(argv[-1])
        if login is None:
            return None, False
        return {"Node": {"Name": f"{login}.ts.net."}, "UserProfile": {"LoginName": login}}, False

    _floor_monkeypatch.delenv("KIROCREW_SHARED_CREW_PREVIEW", raising=False)
    _floor_monkeypatch.setattr(tailnet, "IS_POSIX", True)
    _floor_monkeypatch.setattr(tailnet, "_run_json_detail", MagicMock(side_effect=whois))
    tailnet._whois_cache.clear()
    ta._state.clear_all()
    yield
    tailnet._whois_cache.clear()
    ta._state.clear_all()


async def _served_over_a_socket(trust, *, forwarded_for="", bearer="", cookie=""):
    """``(status, body)`` of ``GET /v1/models`` through the real middleware on loopback."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def _models(request):
        return web.json_response({"user": request.get("user")})

    app = web.Application(middlewares=[token_auth_middleware(tailnet_trust=trust)])
    app.router.add_get(MODELS, _models)
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    try:
        headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else {}
        if bearer:
            headers.update(_bearer(bearer))
        if cookie:
            headers["Cookie"] = f"mc_token_{client.port}={cookie}"
        resp = await client.get(MODELS, headers=headers)
        return resp.status, await resp.text()
    finally:
        await client.close()


def _refusal(status: int, body: str) -> tuple[int, str]:
    """The status and message of a Crew API refusal, without its per-request id."""
    return status, (json.loads(body)["error"]["message"] if status != 200 else body)


@pytest.mark.asyncio
async def test_a_bearer_from_a_login_off_the_allowlist_is_refused_as_the_cookie_is(_tailnet):
    session = _session_token("owner")
    by_cookie = await _served_over_a_socket(ALLOWLIST, forwarded_for=MALLORY, cookie=session)
    by_bearer = await _served_over_a_socket(ALLOWLIST, forwarded_for=MALLORY, bearer=session)
    assert _refusal(*by_cookie) == (401, "tailnet login not allowed")
    assert _refusal(*by_bearer) == _refusal(*by_cookie)


@pytest.mark.asyncio
async def test_a_bearer_from_an_unattributed_peer_is_refused_when_the_allowlist_is_unreadable(
    _tailnet,
) -> None:
    session = _session_token("owner")
    by_cookie = await _served_over_a_socket(UNREADABLE, forwarded_for=NOBODY, cookie=session)
    by_bearer = await _served_over_a_socket(UNREADABLE, forwarded_for=NOBODY, bearer=session)
    assert _refusal(*by_cookie) == (401, "tailnet login not allowed")
    assert _refusal(*by_bearer) == _refusal(*by_cookie)


@pytest.mark.asyncio
async def test_a_bearer_from_an_allowlisted_login_is_served(_tailnet) -> None:
    status, body = await _served_over_a_socket(
        ALLOWLIST, forwarded_for=ALICE, bearer=_session_token("owner")
    )
    assert status == 200 and json.loads(body)["user"] == "owner"


@pytest.mark.asyncio
async def test_a_bearer_on_loopback_with_no_forwarding_is_served(_tailnet) -> None:
    status, body = await _served_over_a_socket(ALLOWLIST, bearer=_session_token("owner"))
    assert status == 200 and json.loads(body)["user"] == "owner"


@pytest.mark.asyncio
async def test_a_request_with_no_credential_resolves_no_peer(_tailnet, monkeypatch) -> None:
    import kiro_crew.dashboard.token_auth as ta

    asked: list = []

    async def _resolve(request, trust):
        asked.append(request.path)
        return None

    monkeypatch.setattr(ta, "resolve_forwarded_peer", _resolve)
    status, _ = await _served_over_a_socket(ALLOWLIST, forwarded_for=MALLORY)
    assert status == 401
    assert asked == []
    # The same request with a bearer is resolved: the spy sees it.
    await _served_over_a_socket(ALLOWLIST, forwarded_for=MALLORY, bearer=_session_token("owner"))
    assert asked == [MODELS]


def test_reading_the_header() -> None:
    assert crew_api_auth.bearer_credential(_make_request()) is None
    for header, expected in [
        ("Bearer abc", "abc"),
        ("bearer  abc ", "abc"),
        ("Bearer", ""),
        ("Basic abc", None),
        ("Token abc", None),
    ]:
        request = _make_request(headers={"Authorization": header})
        assert crew_api_auth.bearer_credential(request) == expected, header


@pytest.mark.asyncio
async def test_bearer_peer_refusal_matches_an_exchanged_cookie() -> None:
    link = generate_token("owner")
    exchange, _ = await _send(path="/api/status", query={"token": link})
    cookie = exchange.cookies[COOKIE].value
    bearer = _session_token("owner")
    assert (await _send(path=MODELS, headers=_bearer(bearer)))[0].status == 200
    by_cookie, _ = await _send(path=MODELS, cookies={COOKIE: cookie}, remote="127.0.0.2")
    by_bearer, _ = await _send(path=MODELS, headers=_bearer(bearer), remote="127.0.0.2")
    assert by_cookie.status == by_bearer.status == 401
    assert json.loads(by_cookie.body)["error"]["message"] == "IP mismatch"
    assert json.loads(by_bearer.body)["error"]["message"] == "IP mismatch"
    assert (await _send(path=MODELS, cookies={COOKIE: cookie}))[0].status == 200


@pytest.mark.asyncio
async def test_bearer_first_use_binds_verified_identity_until_signed_expiry(
    _tailnet, monkeypatch
) -> None:
    import kiro_crew.dashboard.token_auth as ta

    token = _session_token("owner", ttl_seconds=3600)  # flake-ok: signed lifetime, not a wait
    expiry = json.loads(ta._b64url_decode(token.split(".")[0]))["session_exp"]
    bound = []
    bind = ta.bind_token_peer

    def record(*args, **kwargs):
        bound.append((args, kwargs))
        return bind(*args, **kwargs)

    monkeypatch.setattr(ta, "bind_token_peer", record)
    trust = TailnetTrust(
        trust_identity=True,
        allowed_logins=("alice@example.com", "mallory@example.com"),
        pin_scope="login",
    )
    assert (await _served_over_a_socket(trust, forwarded_for=ALICE, bearer=token))[0] == 200
    assert bound == [((token, "ts:login:alice@example.com", expiry), {"proxied": False})]
    assert _refusal(*await _served_over_a_socket(trust, forwarded_for=MALLORY, bearer=token)) == (
        401,
        "peer identity mismatch",
    )
    assert (await _served_over_a_socket(trust, forwarded_for=ALICE, bearer=token))[0] == 200
    assert len(bound) == 1


@pytest.mark.asyncio
async def test_app_scope_uses_the_public_owner_policy(monkeypatch) -> None:
    import kiro_crew.dashboard.token_auth as ta

    monkeypatch.setattr(ta, "_app_api_allowlist", lambda name: ())
    # A further owner-only Public route needs no second entry in the scope gate.
    monkeypatch.setitem(crew_api_auth.OWNER_ONLY_PUBLIC_ROUTES, "/v1/future", "test.future")
    token = generate_token("local-app", app="file-explorer")
    for path in crew_api_auth.OWNER_ONLY_PUBLIC_ROUTES:
        resp, reached = await _send(path=path, headers=_bearer(token))
        assert reached is None
        assert resp.status == 403
        assert "WWW-Authenticate" not in resp.headers
        assert resp.headers["Crew-API-Version"] == "1.0"
        assert json.loads(resp.body) == {
            "error": {
                "type": "permission_error",
                "code": "owner_only",
                "message": "app token not permitted for this endpoint",
                "param": None,
                "request_id": resp.headers["X-Request-Id"],
            }
        }
    _assert_refused_401(*await _send(path=MODELS, headers=_bearer("invalid")))
    assert (await _send(path=MODELS, headers=_bearer(_session_token("owner"))))[0].status == 200


@pytest.mark.asyncio
async def test_owner_openapi_routes_refuse_out_of_scope_apps(monkeypatch) -> None:
    from pathlib import Path

    import yaml

    import kiro_crew.dashboard.token_auth as ta

    monkeypatch.setattr(ta, "_app_api_allowlist", lambda name: ())
    spec = (
        Path(__file__).resolve().parents[1]
        / "docs/request-for-change/assets/crew-api/crew-owner.yaml"
    )
    paths = yaml.safe_load(spec.read_text(encoding="utf-8"))["paths"]
    token = generate_token("local-app", app="file-explorer")
    checked = []
    for path, operations in paths.items():
        for method, operation in operations.items():
            if not isinstance(operation, dict) or operation.get("x-crew-tier") != "owner":
                continue
            assert path.startswith("/v1/crew/owner/")
            resp, reached = await _send(path=path, method=method.upper(), headers=_bearer(token))
            assert reached is None, (method, path)
            assert resp.status == 403, (method, path)
            error = json.loads(resp.body)["error"]
            assert (error["type"], error["code"]) == ("permission_error", "owner_only")
            assert "WWW-Authenticate" not in resp.headers
            checked.append((method, path))
    assert checked


@pytest.mark.asyncio
async def test_the_registered_models_route_keeps_each_refusal_through_the_real_chain(
    monkeypatch,
) -> None:
    """The real ``/v1/models`` route behind the real token auth, over a loopback socket."""
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import kiro_crew.dashboard.token_auth as ta
    from kiro_crew.dashboard import crew_api_models
    from kiro_crew.dashboard.routes import crew_api

    read: list[str] = []

    def _catalog():
        read.append("catalog")
        return []

    monkeypatch.setattr(crew_api_models, "usable_models", _catalog)
    # The app is allowed the path, so the refusal it gets is the owner gate's own.
    monkeypatch.setattr(ta, "_app_api_allowlist", lambda name: (MODELS,))
    app = web.Application(middlewares=[token_auth_middleware()])
    app["state"] = SimpleNamespace(owner_id="owner")
    crew_api.register(app)
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        none = await client.get(MODELS)
        assert none.status == 401
        assert none.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'

        app_token = generate_token("local-app", app="file-explorer")
        refused = await client.get(MODELS, headers=_bearer(app_token))
        error = (await refused.json())["error"]
        assert refused.status == 403
        assert (error["type"], error["code"]) == ("permission_error", "owner_only")
        assert error["request_id"] == refused.headers["X-Request-Id"]
        assert "WWW-Authenticate" not in refused.headers
        assert read == []

        served = await client.get(MODELS, headers=_bearer(_session_token("owner")))
        assert served.status == 200
        assert await served.json() == {"object": "list", "data": []}
        assert served.headers["X-Request-Id"].startswith("req_")
        assert served.headers["Crew-API-Version"] == "1.0"
        assert "Set-Cookie" not in served.headers
        assert read == ["catalog"]
