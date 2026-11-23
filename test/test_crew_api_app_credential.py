"""An app credential on the Crew API: recognised, and refused by tier.

An app credential is the access token local authorization issues for a grant the owner
approved. Token auth recognises a valid one on a Crew API path (``/v1/`` except the
frozen ``/v1/chat/completions``) and answers ``403 owner_only`` there: no route serves
an app yet. One that does not verify stays a 401. Outside the Crew API the
``Authorization`` header is not read, so the credential opens nothing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_request,
    _session_token,
    clear_nonces,
)

from kiro_crew.dashboard import crew_api_auth
from kiro_crew.dashboard import local_authorization as la
from kiro_crew.dashboard.crew_api_errors import CREDENTIAL_HINT
from kiro_crew.dashboard.handlers.source_providers import is_host_owner_dashboard_request
from kiro_crew.dashboard.kiro_relay.caller import RELAY_CALLER_KEY, RelayCaller, RelayCallerKind
from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware

VERIFIER = "v" * 43
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)
REDIRECT = "http://127.0.0.1:53124/callback"
ISSUER = "http://127.0.0.1:5476"
CLIENT = "notely-desktop"
COOKIE = "mc_token_5476"
KEY = b"k" * 32

OWNER_ROUTES = [
    ("GET", "/v1/crew/owner/bots"),
    ("GET", "/v1/crew/owner/grant-requests"),
    ("GET", "/v1/crew/owner/grant-requests/lgr_0123456789abcdef"),
    ("POST", "/v1/crew/owner/grant-requests/lgr_0123456789abcdef/decision"),
    ("DELETE", "/v1/crew/owner/grants/grt_0123456789abcdef"),
    ("GET", "/v1/crew/owner/approvals"),
    ("GET", "/v1/crew/owner/approvals/apr_0123456789abcdef"),
    ("POST", "/v1/crew/owner/approvals/apr_0123456789abcdef/decision"),
]
#: Every Crew API path an app credential is tried on: the Owner routes, the Public
#: route that serves the owner alone, and a path no route answers.
CREW_API_ROUTES = [*OWNER_ROUTES, ("GET", "/v1/models"), ("POST", "/v1/responses")]


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def audit(monkeypatch) -> Mock:
    """The audit trail of the app credential check and of local authorization."""
    audit = Mock(spec=["log_api_access"])
    monkeypatch.setattr(crew_api_auth, "sel", lambda: audit)
    monkeypatch.setattr(la, "sel", lambda: Mock(spec=["log_api_access"]))
    return audit


@pytest.fixture
def store(tmp_path: Path, clock: Clock, audit, monkeypatch) -> la.LocalAuthorization:
    """The gateway's local authorization, on a record of this test's own."""
    store = la.LocalAuthorization(tmp_path / "local-grants" / "state.json", KEY, clock)
    monkeypatch.setattr(la, "gateway_local_authorization", lambda: store)
    return store


def _grant(store: la.LocalAuthorization, clock: Clock, client_id: str = CLIENT) -> dict:
    """The tokens of a grant the owner approved for *client_id*."""
    ask = {
        "response_type": "code",
        "client_id": client_id,
        "client_name": "Notely",
        "redirect_uri": REDIRECT,
        "scope": "responses:write conversations:read",
        "state": "xyz",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
    }
    kind, view = store.authorize(ask)
    assert kind == "created"
    request_id = str(view["request_id"])
    store.decide(request_id, True, None, caller="U_HOST_OWNER")
    clock.now += la.POLL_INTERVAL_SECS
    kind, location = store.authorize({**ask, "request_id": request_id})
    assert kind == "redirect"
    code = parse_qs(urlsplit(location).query)["code"][0]
    return store.exchange_code(client_id, code, REDIRECT, VERIFIER, ISSUER)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _send(**request_fields):
    """The middleware's answer, the request it judged, and whether a handler ran."""
    reached: list = []

    async def _handler(request):
        reached.append(request)
        return web.Response(text="ok")

    request = _make_request(**request_fields)
    resp = await token_auth_middleware(port=5476)(request, _handler)
    return resp, request, bool(reached)


def _published(request) -> dict:
    """What token auth set on the mock request (``request[key] = value``)."""
    return {call.args[0]: call.args[1] for call in request.__setitem__.call_args_list}


def _error(resp) -> dict:
    return json.loads(resp.body)["error"]


def _assert_401(resp, ran: bool) -> None:
    assert ran is False
    assert resp.status == 401
    assert resp.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
    error = _error(resp)
    assert (error["type"], error["code"]) == ("authentication_error", "invalid_token")
    assert error["message"].endswith(CREDENTIAL_HINT)


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _resigned(store: la.LocalAuthorization, token: str, *, header=None, **claims) -> str:
    """*token* with its header or claims changed, signed with the store's own key."""
    head, body, _ = token.split(".")
    if header is not None:
        head = _b64(header)
    if claims:
        padded = body + "=" * (-len(body) % 4)
        body = _b64({**json.loads(base64.urlsafe_b64decode(padded)), **claims})
    return f"{head}.{body}.{store._sign(f'{head}.{body}')}"


# ── a valid app credential: known caller, refused by tier ────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), CREW_API_ROUTES)
async def test_a_valid_app_credential_is_403_owner_only_on_every_crew_api_path(
    store, clock, audit, method, path
) -> None:
    tokens = _grant(store, clock)
    resp, request, ran = await _send(
        path=path, method=method, headers=_bearer(tokens["access_token"])
    )
    assert ran is False
    assert resp.status == 403
    assert "WWW-Authenticate" not in resp.headers
    error = _error(resp)
    assert (error["type"], error["code"]) == ("permission_error", "owner_only")
    assert CREDENTIAL_HINT not in error["message"]
    row = audit.log_api_access.call_args.kwargs
    assert audit.log_api_access.call_count == 1
    assert row["caller"] == f"app:{tokens['grant_id']}"
    assert (row["outcome"], row["error"]) == ("denied", "owner_only")
    assert row["resources"] == f"{path} client_id={CLIENT}"


@pytest.mark.asyncio
async def test_the_caller_published_for_an_app_credential_is_never_the_owner(store, clock) -> None:
    tokens = _grant(store, clock)
    _, request, _ = await _send(path="/v1/models", headers=_bearer(tokens["access_token"]))
    published = _published(request)
    assert published["user"] == f"app:{tokens['grant_id']}"
    assert published["app"] == f"local-grant:{tokens['grant_id']}"
    assert published["is_dashboard_user"] is False
    # The owner gate every Owner route runs refuses that caller, whoever the owner is.
    marked = {**published, "app": published["app"]}
    for owner_id in ("", published["user"]):
        probe = Mock()
        probe.app = {"state": Mock(owner_id=owner_id)}
        probe.get = marked.get
        probe.__contains__ = lambda self, key: key in marked
        probe.__getitem__ = lambda self, key: marked[key]
        assert is_host_owner_dashboard_request(probe) is False


@pytest.mark.asyncio
async def test_an_owner_cookie_beside_an_app_credential_changes_nothing(store, clock) -> None:
    tokens = _grant(store, clock)
    cookie = {COOKIE: _session_token("owner")}
    resp, _, ran = await _send(
        path="/v1/crew/owner/bots", headers=_bearer(tokens["access_token"]), cookies=cookie
    )
    assert (resp.status, ran) == (403, False)
    forged = tokens["access_token"][:-2] + ("AA" if tokens["access_token"][-2:] != "AA" else "BB")
    resp, _, ran = await _send(path="/v1/crew/owner/bots", headers=_bearer(forged), cookies=cookie)
    _assert_401(resp, ran)


# ── a credential that does not verify is no app credential: 401 ──────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["expired", "other-key", "typ", "alg-none", "revoked", "client-id", "tampered", "no-grant"],
)
async def test_an_app_token_that_does_not_verify_is_401(
    store, clock, audit, tmp_path, case
) -> None:
    tokens = _grant(store, clock)
    token = tokens["access_token"]
    if case == "expired":
        clock.now += la.ACCESS_TTL_SECS + 1
    elif case == "other-key":
        other = la.LocalAuthorization(tmp_path / "other" / "state.json", b"x" * 32, clock)
        token = _grant(other, clock)["access_token"]
    elif case == "typ":
        token = _resigned(store, token, header={"alg": "HS256", "typ": "JWT"})
    elif case == "alg-none":
        head = _b64({"alg": "none", "typ": la.TOKEN_TYP})
        token = f"{head}.{token.split('.')[1]}."
    elif case == "revoked":
        assert store.revoke_grant(tokens["grant_id"], caller="U_HOST_OWNER") is True
    elif case == "client-id":
        token = _resigned(store, token, client_id="another-app")
    elif case == "tampered":
        token = token[:-2] + ("AA" if token[-2:] != "AA" else "BB")
    elif case == "no-grant":
        token = _resigned(store, token, grant_id="grt_0000000000000000")
    for path in ("/v1/crew/owner/bots", "/v1/models"):
        resp, request, ran = await _send(path=path, headers=_bearer(token))
        _assert_401(resp, ran)
        assert "app" not in _published(request)
    audit.log_api_access.assert_not_called()


@pytest.mark.asyncio
async def test_an_app_token_in_the_query_is_401_like_any_query_credential(store, clock) -> None:
    token = _grant(store, clock)["access_token"]
    resp, request, ran = await _send(path="/v1/models", query={"token": token})
    _assert_401(resp, ran)
    assert "app" not in _published(request)


# ── only from this machine ───────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"X-Forwarded-For": "203.0.113.7"}, id="x-forwarded-for"),
        pytest.param({"Forwarded": "for=203.0.113.7"}, id="forwarded"),
        pytest.param({"X-Real-IP": "203.0.113.7"}, id="x-real-ip"),
    ],
)
async def test_an_app_credential_through_a_forwarder_is_401(store, clock, audit, extra) -> None:
    tokens = _grant(store, clock)
    headers = {**_bearer(tokens["access_token"]), **extra}
    resp, request, ran = await _send(path="/v1/crew/owner/bots", headers=headers)
    _assert_401(resp, ran)
    assert "app" not in _published(request)
    row = audit.log_api_access.call_args.kwargs
    assert (row["outcome"], row["error"]) == ("denied", "not-direct-local")


@pytest.mark.asyncio
async def test_an_app_credential_the_relay_delivered_is_401(store, clock, audit) -> None:
    tokens = _grant(store, clock)
    request = _make_request(path="/v1/models", headers=_bearer(tokens["access_token"]))
    relayed = RelayCaller(kiro_user_id="kiro-user.owner-0001", kind=RelayCallerKind.OWNER)
    request.get = lambda key, default=None: relayed if key == RELAY_CALLER_KEY else default
    # ``None``: not an app credential here, so the caller's own 401 answers it.
    assert await crew_api_auth.refuse_app_credential(request, tokens["access_token"]) is None
    assert "app" not in _published(request)
    assert audit.log_api_access.call_args.kwargs["error"] == "not-direct-local"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/crew/owner/bots", "/v1/models", "/v1/responses"])
async def test_a_forwarded_valid_credential_is_answered_exactly_as_a_forged_one(
    store, clock, path
) -> None:
    """Nothing in the answer says the forwarded credential was a live one."""
    valid = _grant(store, clock)["access_token"]
    forged = valid[:-2] + ("AA" if valid[-2:] != "AA" else "BB")
    sent = {"X-Forwarded-For": "203.0.113.7", "X-Request-Id": "req_same0001"}
    answers = []
    for token in (valid, forged):
        resp, request, ran = await _send(path=path, headers={**_bearer(token), **sent})
        _assert_401(resp, ran)
        assert "app" not in _published(request)
        answers.append((resp.status, resp.body, sorted(resp.headers.items())))
    assert answers[0] == answers[1]


@pytest.mark.asyncio
async def test_an_app_credential_from_another_address_is_401(store, clock) -> None:
    tokens = _grant(store, clock)
    resp, request, ran = await _send(
        path="/v1/models", headers=_bearer(tokens["access_token"]), remote="203.0.113.7"
    )
    assert (resp.status, ran) == (401, False)
    assert "app" not in _published(request)


# ── outside the Crew API the header is not read ──────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/ws", "/api/notifications/push", "/api/status", "/v1/chat/completions"]
)
async def test_an_app_credential_opens_nothing_outside_the_crew_api(
    store, clock, monkeypatch, path
) -> None:
    tokens = _grant(store, clock)
    asked = Mock(wraps=crew_api_auth.refuse_app_credential)
    monkeypatch.setattr(crew_api_auth, "refuse_app_credential", asked)
    with_credential, request, ran = await _send(
        path=path, method="POST", headers=_bearer(tokens["access_token"])
    )
    without, _, _ = await _send(path=path, method="POST")
    assert ran is False
    assert with_credential.status == without.status == 403
    assert with_credential.body == without.body
    assert not {"user", "app"} & set(_published(request))
    asked.assert_not_called()


# ── the hint that ends every 401 ─────────────────────────────────────────────


def test_the_hint_names_the_command_and_holds_no_port_and_no_value() -> None:
    assert "`kirocrew token --bearer`" in CREDENTIAL_HINT
    assert not re.search(r"\d", CREDENTIAL_HINT)
    assert not re.search(r"[A-Za-z0-9_-]{20,}", CREDENTIAL_HINT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_fields",
    [
        pytest.param({}, id="no-credential"),
        pytest.param({"headers": {"Authorization": "Bearer not-a-token"}}, id="wrong-bearer"),
        pytest.param({"headers": {"Authorization": "Bearer"}}, id="empty-bearer"),
        pytest.param({"query": {"token": "anything"}}, id="query-token"),
        pytest.param({"headers": {"Authorization": "Bearer a.b.c"}}, id="jwt-shaped"),
    ],
)
@pytest.mark.parametrize("path", ["/v1/models", "/v1/crew/owner/bots", "/v1/responses"])
async def test_every_401_on_the_crew_api_ends_with_the_hint(store, request_fields, path) -> None:
    resp, _, ran = await _send(path=path, **request_fields)
    _assert_401(resp, ran)


@pytest.mark.asyncio
async def test_a_one_time_link_as_bearer_gets_the_hint_too(store) -> None:
    resp, _, ran = await _send(path="/v1/models", headers=_bearer(generate_token("owner")))
    _assert_401(resp, ran)


# ── nothing of the credential is written down ────────────────────────────────


@pytest.mark.asyncio
async def test_the_credential_is_in_no_audit_row_no_log_line_and_no_answer(
    store, clock, audit, caplog
) -> None:
    tokens = _grant(store, clock)
    token = tokens["access_token"]
    caplog.set_level(logging.DEBUG)
    answers = []
    for headers in (_bearer(token), {**_bearer(token), "X-Forwarded-For": "203.0.113.7"}):
        resp, _, _ = await _send(path="/v1/crew/owner/bots", headers=headers)
        answers.append(resp.body.decode() + repr(dict(resp.headers)))
    written = repr(audit.log_api_access.call_args_list) + caplog.text + "".join(answers)
    for piece in (token, token[:12], token[-12:], tokens["refresh_token"]):
        assert piece not in written


# ── the calling program's own name never becomes an identity ─────────────────


def test_audit_text_drops_control_characters_and_is_capped() -> None:
    hostile = "a\r\nb\x00\x1b[31mc\x7f\u2028" + "z" * 200
    cleaned = crew_api_auth._audit_text(hostile)
    assert cleaned.startswith("ab[31mc")
    assert len(cleaned) == 64
    assert all(" " <= c <= "~" for c in cleaned)


@pytest.mark.asyncio
async def test_a_hostile_client_id_reaches_the_audit_row_cleaned_and_no_identity(
    store, clock, audit, monkeypatch
) -> None:
    tokens = _grant(store, clock)
    hostile = "evil\r\ncaller=owner outcome=allowed\x00" + "x" * 300
    real = store.verify

    def _verify(token):
        found = real(token)
        return la.LocalCredential(
            client_id=hostile, grant_id=found.grant_id, scopes=found.scopes, namespace="default"
        )

    monkeypatch.setattr(store, "verify", _verify)
    resp, request, ran = await _send(path="/v1/models", headers=_bearer(tokens["access_token"]))
    assert (resp.status, ran) == (403, False)
    row = audit.log_api_access.call_args.kwargs
    assert "\n" not in row["resources"] and "\r" not in row["resources"]
    assert "\x00" not in row["resources"]
    assert len(row["resources"]) <= len("/v1/models client_id=") + 64
    published = _published(request)
    for identity in (row["caller"], published["user"], published["app"]):
        assert identity.endswith(tokens["grant_id"])
        assert "evil" not in identity


@pytest.mark.asyncio
async def test_a_failing_grant_record_refuses_as_401(store, clock, monkeypatch) -> None:
    tokens = _grant(store, clock)

    def _broken():
        raise OSError("record unreadable")

    monkeypatch.setattr(la, "gateway_local_authorization", _broken)
    resp, request, ran = await _send(path="/v1/models", headers=_bearer(tokens["access_token"]))
    _assert_401(resp, ran)
    assert "app" not in _published(request)
