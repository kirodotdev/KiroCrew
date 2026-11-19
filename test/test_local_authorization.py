"""Local authorization: an app on this machine gets a scoped, revocable token only after
the owner agrees (``dashboard/local_authorization.py`` and its four routes)."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import local_authorization as la
from kiro_crew.dashboard.routes import local_authorization as routes
from kiro_crew.dashboard.token_auth_paths import _BYPASS_EXACT_METHODS, _exact_method_bypass

VERIFIER = "v" * 43
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)
REDIRECT = "http://127.0.0.1:53124/callback"
ISSUER = "http://127.0.0.1:5476"
ASK = {
    "response_type": "code",
    "client_id": "notely-desktop",
    "client_name": "Notely",
    "redirect_uri": REDIRECT,
    "scope": "responses:write conversations:read",
    "state": "xyz",
    "code_challenge": CHALLENGE,
    "code_challenge_method": "S256",
}


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> la.LocalAuthorization:
    return la.LocalAuthorization(tmp_path / "local-grants" / "state.json", b"k" * 32, clock)


def _pending(store: la.LocalAuthorization, **changes: str) -> str:
    kind, view = store.authorize({**ASK, **changes})
    assert kind == "pending"
    return str(view["request_id"])


def _code(store: la.LocalAuthorization, clock: Clock, request_id: str) -> str:
    clock.now += la.POLL_INTERVAL_SECS
    kind, location = store.authorize({**ASK, "request_id": request_id})
    assert kind == "redirect"
    query = parse_qs(urlsplit(location).query)
    assert location.startswith(REDIRECT) and query["state"] == ["xyz"]
    return query["code"][0]


def _tokens(
    store: la.LocalAuthorization, clock: Clock, scopes: list[str] | None = None
) -> dict[str, Any]:
    request_id = _pending(store)
    store.decide(request_id, True, scopes)
    code = _code(store, clock, request_id)
    return store.exchange_code("notely-desktop", code, REDIRECT, VERIFIER, ISSUER)


def test_without_the_owners_consent_there_is_no_code_and_no_token(store, clock):
    request_id = _pending(store)
    clock.now += la.POLL_INTERVAL_SECS
    kind, view = store.authorize({**ASK, "request_id": request_id})
    assert (kind, view["status"]) == ("pending", "pending")
    with pytest.raises(la.AuthError) as refused:
        store.exchange_code("notely-desktop", "lc_guess", REDIRECT, VERIFIER, ISSUER)
    assert refused.value.error == "invalid_grant"


def test_after_consent_the_app_gets_a_token_for_the_scopes_it_was_given(store, clock):
    tokens = _tokens(store, clock)
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 300
    assert tokens["scope"] == "responses:write conversations:read"
    credential = store.verify(tokens["access_token"])
    assert credential is not None
    assert credential.client_id == "notely-desktop"
    assert credential.grant_id == tokens["grant_id"]
    assert credential.namespace == "default"
    assert credential.allows("responses:write") and credential.allows("conversations:read")


def test_a_denied_request_sends_the_app_back_with_access_denied(store, clock):
    request_id = _pending(store)
    store.decide(request_id, False)
    clock.now += la.POLL_INTERVAL_SECS
    kind, location = store.authorize({**ASK, "request_id": request_id})
    assert kind == "redirect"
    assert parse_qs(urlsplit(location).query) == {"error": ["access_denied"], "state": ["xyz"]}


def test_a_scope_outside_the_grant_is_refused(store, clock):
    tokens = _tokens(store, clock, scopes=["conversations:read"])
    credential = store.verify(tokens["access_token"])
    assert credential is not None and credential.scopes == {"conversations:read"}
    assert not credential.allows("responses:write")
    assert not credential.allows("grants:manage")
    with pytest.raises(la.AuthError) as widened:
        store.refresh("notely-desktop", tokens["refresh_token"], "responses:write", ISSUER)
    assert widened.value.error == "invalid_scope"


def test_the_owner_cannot_give_more_than_was_asked_and_unknown_scopes_never_wait(store):
    request_id = _pending(store)
    with pytest.raises(ValueError):
        store.decide(request_id, True, ["bots:chat"])
    kind, location = store.authorize({**ASK, "scope": "responses:write grants:manage"})
    assert kind == "redirect" and "error=invalid_scope" in location
    assert [r["request_id"] for r in store.pending()] == [request_id]


def test_a_token_is_refused_once_its_grant_is_revoked(store, clock):
    by_owner = _tokens(store, clock)
    assert store.revoke_grant(by_owner["grant_id"]) is True
    assert store.verify(by_owner["access_token"]) is None
    with pytest.raises(la.AuthError):
        store.refresh("notely-desktop", by_owner["refresh_token"], "", ISSUER)

    by_app = _tokens(store, clock)
    store.revoke_token(by_app["refresh_token"])
    assert store.verify(by_app["access_token"]) is None
    store.revoke_token("lr_unknown")  # an unknown token changes nothing and does not raise


def test_expired_tokens_codes_and_requests_are_refused(store, clock):
    tokens = _tokens(store, clock)
    clock.now += la.ACCESS_TTL_SECS
    assert store.verify(tokens["access_token"]) is None
    fresh = store.refresh("notely-desktop", tokens["refresh_token"], "", ISSUER)
    assert store.verify(fresh["access_token"]) is not None
    clock.now += la.REFRESH_IDLE_SECS
    with pytest.raises(la.AuthError):
        store.refresh("notely-desktop", fresh["refresh_token"], "", ISSUER)

    request_id = _pending(store)
    store.decide(request_id, True)
    code = _code(store, clock, request_id)
    clock.now += la.CODE_TTL_SECS
    with pytest.raises(la.AuthError):
        store.exchange_code("notely-desktop", code, REDIRECT, VERIFIER, ISSUER)

    stale = _pending(store)
    clock.now += la.REQUEST_TTL_SECS
    with pytest.raises(KeyError):
        store.decide(stale, True)


def test_an_app_cannot_answer_its_own_request(store, clock):
    request_id = _pending(store)
    clock.now += la.POLL_INTERVAL_SECS
    answer = {"decision": "approve", "status": "approved", "granted_scopes": ASK["scope"]}
    kind, view = store.authorize({**ASK, **answer, "request_id": request_id})
    assert (kind, view["status"]) == ("pending", "pending")
    # A second program that knows the request id but not the first one's parameters.
    clock.now += la.POLL_INTERVAL_SECS
    with pytest.raises(la.AuthError):
        store.authorize({**ASK, "code_challenge": "w" * 43, "request_id": request_id})
    # An answered request cannot be answered again.
    store.decide(request_id, False)
    with pytest.raises(RuntimeError):
        store.decide(request_id, True)


def test_a_code_needs_the_asking_apps_verifier_and_works_once(store, clock):
    request_id = _pending(store)
    store.decide(request_id, True)
    code = _code(store, clock, request_id)
    for client_id, redirect_uri, verifier in (
        ("other-app", REDIRECT, VERIFIER),
        ("notely-desktop", REDIRECT + "/x", VERIFIER),
        ("notely-desktop", REDIRECT, "w" * 43),
    ):
        with pytest.raises(la.AuthError):
            store.exchange_code(client_id, code, redirect_uri, verifier, ISSUER)
    tokens = store.exchange_code("notely-desktop", code, REDIRECT, VERIFIER, ISSUER)
    with pytest.raises(la.AuthError):
        store.exchange_code("notely-desktop", code, REDIRECT, VERIFIER, ISSUER)
    assert store.verify(tokens["access_token"]) is None  # the reuse revoked the grant


def test_a_refresh_token_changes_on_use_and_a_replayed_one_revokes_the_grant(store, clock):
    first = _tokens(store, clock)
    second = store.refresh("notely-desktop", first["refresh_token"], "", ISSUER)
    assert second["refresh_token"] != first["refresh_token"]
    assert second["grant_id"] == first["grant_id"]
    with pytest.raises(la.AuthError):
        store.refresh("notely-desktop", first["refresh_token"], "", ISSUER)
    assert store.verify(second["access_token"]) is None


def test_requests_and_grants_store_the_namespace_and_only_digests(store, clock, tmp_path):
    waiting = _pending(store, client_id="second-app")
    tokens = _tokens(store, clock)
    text = (tmp_path / "local-grants" / "state.json").read_text(encoding="utf-8")
    record = json.loads(text)
    assert record["requests"][waiting]["namespace"] == "default"
    assert record["grants"][tokens["grant_id"]]["namespace"] == "default"
    assert record["grants"][tokens["grant_id"]]["kind"] == "local"
    for secret in (tokens["access_token"], tokens["refresh_token"], VERIFIER):
        assert secret not in text


def test_a_restart_keeps_grants_and_pending_requests(store, clock, tmp_path):
    tokens = _tokens(store, clock)
    waiting = _pending(store)
    restarted = la.LocalAuthorization(tmp_path / "local-grants" / "state.json", b"k" * 32, clock)
    assert restarted.verify(tokens["access_token"]) is not None
    assert [r["request_id"] for r in restarted.pending()] == [waiting]
    restarted.decide(waiting, True)
    code = _code(restarted, clock, waiting)
    assert restarted.exchange_code("notely-desktop", code, REDIRECT, VERIFIER, ISSUER)["grant_id"]


def test_a_forged_altered_or_foreign_token_is_refused(store, clock, tmp_path):
    tokens = _tokens(store, clock)
    head, body, signature = tokens["access_token"].split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["scope"] = " ".join(la.SCOPES)
    widened = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    other_key = la.LocalAuthorization(tmp_path / "local-grants" / "state.json", b"z" * 32, clock)
    for token in (f"{head}.{widened}.{signature}", f"{head}.{body}", "", "a.b.c", "é.é.é"):
        assert store.verify(token) is None
    assert other_key.verify(tokens["access_token"]) is None


def test_an_unreadable_record_grants_nothing(store, clock, tmp_path):
    tokens = _tokens(store, clock)
    (tmp_path / "local-grants" / "state.json").write_text("{not json", encoding="utf-8")
    assert store.verify(tokens["access_token"]) is None
    with pytest.raises(ValueError):
        store.authorize(ASK)


@pytest.mark.parametrize(
    "changes",
    [
        {"redirect_uri": "http://example.com/callback"},
        {"redirect_uri": "https://127.0.0.1:1/cb"},
        {"redirect_uri": "http://127.0.0.1/cb"},
        {"redirect_uri": "http://user@127.0.0.1:1/cb"},
        {"redirect_uri": "http://localhost:1/cb"},
        {"code_challenge_method": "plain"},
        {"code_challenge": "short"},
        {"response_type": "token"},
        {"client_id": ""},
        {"state": ""},
    ],
)
def test_a_bad_request_is_refused_and_never_redirected(store, changes):
    with pytest.raises(la.AuthError) as refused:
        store.authorize({**ASK, **changes})
    assert (refused.value.error, refused.value.status) == ("invalid_request", 400)
    assert store.pending() == []


def test_waiting_requests_are_capped_and_asking_again_too_soon_is_slowed(store, clock):
    first = _pending(store)
    with pytest.raises(la.AuthError) as soon:
        store.authorize({**ASK, "request_id": first})
    assert (soon.value.error, soon.value.status) == ("slow_down", 429)
    for _ in range(la.MAX_PENDING_PER_CLIENT - 1):
        _pending(store)
    with pytest.raises(la.AuthError) as full:
        store.authorize(ASK)
    assert full.value.status == 429
    clock.now += la.REQUEST_TTL_SECS  # expired requests free their place
    _pending(store)


# ── the routes ────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client(store, monkeypatch):
    monkeypatch.setattr(la, "gateway_local_authorization", lambda: store)
    monkeypatch.setattr(routes, "_audit", lambda *a: None)
    app = web.Application()
    routes.register(app)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


@pytest.mark.asyncio
async def test_an_app_goes_from_request_to_token_over_http(client, store, clock):
    metadata = await (await client.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["token_endpoint"].endswith("/oauth/token")
    assert metadata["scopes_supported"] == list(la.SCOPES)

    json_only = {"Accept": "application/json"}
    asked = await client.get("/oauth/authorize", params=ASK, headers=json_only)
    pending = await asked.json()
    assert asked.status == 202 and pending["status"] == "pending" and pending["interval"] == 2
    page = await client.get("/oauth/authorize", params={**ASK, "client_id": "<b>x"})
    assert page.status == 200 and "<b>x" not in await page.text()

    store.decide(pending["request_id"], True)
    clock.now += la.POLL_INTERVAL_SECS
    again = {**ASK, "request_id": pending["request_id"]}
    sent = await client.get("/oauth/authorize", params=again, allow_redirects=False)
    assert sent.status == 302
    code = parse_qs(urlsplit(sent.headers["Location"]).query)["code"][0]

    form = {
        "grant_type": "authorization_code",
        "client_id": "notely-desktop",
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
    }
    answered = await client.post("/oauth/token", data=form)
    tokens = await answered.json()
    assert answered.status == 200 and answered.headers["Cache-Control"] == "no-store"
    assert store.verify(tokens["access_token"]) is not None
    reused = await client.post("/oauth/token", data=form)
    assert reused.status == 400 and (await reused.json())["error"] == "invalid_grant"

    assert (await client.post("/oauth/revoke", data={})).status == 400
    assert (await client.post("/oauth/revoke", data={"token": "lr_unknown"})).status == 200
    other = await client.post("/oauth/token", data={"grant_type": "password"})
    assert (await other.json())["error"] == "unsupported_grant_type"


@pytest.mark.asyncio
async def test_a_request_that_came_through_an_entry_gets_404(client, store):
    forwarded = {"X-Forwarded-For": "203.0.113.7"}
    for method, path in (
        ("GET", "/.well-known/oauth-authorization-server"),
        ("GET", "/oauth/authorize"),
        ("POST", "/oauth/token"),
        ("POST", "/oauth/revoke"),
    ):
        answer = await client.request(method, path, params=ASK, headers=forwarded)
        assert answer.status == 404, path
        assert (await answer.json())["error"] == "not_found"
    assert store.pending() == []


def test_token_auth_lets_through_one_method_per_route_and_no_owner_route():
    for path, method in (
        ("/.well-known/oauth-authorization-server", "GET"),
        ("/oauth/authorize", "GET"),
        ("/oauth/token", "POST"),
        ("/oauth/revoke", "POST"),
    ):
        assert _BYPASS_EXACT_METHODS[path] == frozenset({method})
    assert not _exact_method_bypass("/oauth/authorize", "POST")
    assert not any(path.startswith("/v1/") for path in _BYPASS_EXACT_METHODS)
