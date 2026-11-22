"""The owner's grant-request routes: only the owner lists and answers the requests apps
made through local authorization, and an app cannot answer its own."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from jsonschema import Draft202012Validator
from test_local_authorization import ASK, ISSUER, REDIRECT, VERIFIER, Clock
from test_shared_crew_teammate_owner_access import ANA, GUEST_USER, _publish

from kiro_crew.dashboard import local_authorization as la
from kiro_crew.dashboard.routes import crew_api
from kiro_crew.dashboard.routes import local_authorization as oauth_routes

CONTRACT = json.loads(
    (Path(__file__).parent / "fixtures" / "crew_api_owner_openapi.json").read_text(encoding="utf-8")
)
OWNER = "U_OWNER"
AS_OWNER = {"X-Test-User": OWNER}
AS_APP = {"X-Test-User": "notely", "X-Test-App": "notely"}
LIST = "/v1/crew/owner/grant-requests"


def _valid(name: str, body: Any) -> None:
    schema = {"$ref": f"#/components/schemas/{name}", "components": CONTRACT["components"]}
    Draft202012Validator(schema).validate(body)


def _decision(request_id: str) -> str:
    return f"{LIST}/{request_id}/decision"


@web.middleware
async def _identity(request: web.Request, handler: Any) -> web.StreamResponse:
    """Stands in for token auth: who the caller is, as token auth would have set it."""
    request["user"] = request.headers.get("X-Test-User", "")
    request["app"] = request.headers.get("X-Test-App", "")
    if teammate := request.headers.get("X-Test-Teammate"):
        _publish(request, teammate)
    return await handler(request)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest_asyncio.fixture
async def client(tmp_path, clock, monkeypatch):
    store = la.LocalAuthorization(tmp_path / "local-grants" / "state.json", b"k" * 32, clock)
    audit = Mock(spec=["log_api_access"])
    monkeypatch.setattr(la, "gateway_local_authorization", lambda: store)
    monkeypatch.setattr(la, "sel", lambda: audit)
    monkeypatch.setattr(oauth_routes, "sel", lambda: audit)
    app = web.Application(middlewares=[_identity])
    state = _make_state(tmp_path)
    state.owner_id = OWNER
    app["state"] = state
    crew_api.register(app)
    oauth_routes.register(app)
    async with TestClient(TestServer(app)) as test_client:
        test_client.store = store
        test_client.audit = audit
        yield test_client


async def _ask(client) -> str:
    asked = await client.get("/oauth/authorize", params=ASK, headers={"Accept": "application/json"})
    assert asked.status == 202
    return str((await asked.json())["request_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [AS_APP, {"X-Test-User": "123456789"}, {}])
async def test_only_the_owner_reaches_the_routes_and_an_app_cannot_answer(client, caller):
    request_id = await _ask(client)
    listed = await client.get(LIST, headers=caller)
    answered = await client.post(
        _decision(request_id), json={"decision": "approve"}, headers=caller
    )
    for refused in (listed, answered):
        body = await refused.json()
        assert refused.status == 403
        _valid("Error", body)
        assert body["error"]["code"] == "owner_only"
    assert [r["status"] for r in client.store.pending()] == ["pending"]


@pytest.mark.asyncio
@pytest.mark.parametrize("teammate", ["relay", "ssm", "tailscale"])
@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_teammates_cannot_list_or_decide_local_grants(
    client, monkeypatch, teammate, decision
):
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    request_id = await _ask(client)
    denial_audit = Mock(spec=["log_api_access"])
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: denial_audit)
    caller = {"X-Test-Teammate": teammate}
    listed = await client.get(LIST, headers=caller)
    answered = await client.post(_decision(request_id), json={"decision": decision}, headers=caller)
    for response in (listed, answered):
        body = await response.json()
        assert response.status == 403
        _valid("Error", body)
        assert body["error"]["code"] == "owner_only"
        assert body["error"]["type"] == "permission_error"
        assert body["error"]["request_id"] == response.headers["X-Request-Id"]
        assert response.headers["Crew-API-Version"]
    rows = [call.kwargs for call in denial_audit.log_api_access.call_args_list]
    assert [(row["operation"], row["outcome"]) for row in rows] == [
        ("crew_api.grant_requests.list", "denied"),
        ("crew_api.grant_requests.decide", "denied"),
    ]
    assert [r["request_id"] for r in client.store.pending()] == [request_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_the_decision_row_names_the_host_owner_who_answered(client, decision):
    request_id = await _ask(client)
    client.audit.reset_mock()
    answered = await client.post(
        _decision(request_id), json={"decision": decision}, headers=AS_OWNER
    )
    assert answered.status == 200
    client.audit.log_api_access.assert_called_once_with(
        caller=OWNER,
        operation="local_auth.decide",
        outcome="approved" if decision == "approve" else "denied",
        source="local-authorization",
        resources=f"request={request_id}",
        critical=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("teammate", "subject"),
    [("relay", f"relay:{ANA}"), ("ssm", GUEST_USER), ("tailscale", "tailnet:uid:42")],
)
async def test_a_refused_teammate_leaves_one_row_naming_them(
    client, monkeypatch, teammate, subject
):
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    request_id = await _ask(client)
    denial_audit = Mock(spec=["log_api_access"])
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: denial_audit)
    client.audit.reset_mock()
    answered = await client.post(
        _decision(request_id), json={"decision": "approve"}, headers={"X-Test-Teammate": teammate}
    )
    assert answered.status == 403
    client.audit.log_api_access.assert_not_called()
    denial_audit.log_api_access.assert_called_once()
    row = denial_audit.log_api_access.call_args.kwargs
    assert (row["operation"], row["outcome"]) == ("crew_api.grant_requests.decide", "denied")
    assert row["caller"] == subject


@pytest.mark.asyncio
@pytest.mark.parametrize("forwarded", [False, True])
async def test_the_owner_lists_and_approves_and_the_app_gets_what_was_given(
    client, clock, forwarded
):
    caller = {**AS_OWNER, **({"X-Forwarded-For": "203.0.113.8"} if forwarded else {})}
    request_id = await _ask(client)
    listed = await client.get(LIST, headers=caller)
    body = await listed.json()
    assert listed.status == 200
    _valid("GrantRequestList", body)
    assert listed.headers["X-Request-Id"].startswith("req_")
    row = body["data"][0]
    assert (row["id"], row["client_id"], row["verified"]) == (request_id, "notely-desktop", False)
    assert row["namespace"] == "default"
    assert row["requested_scopes"] == ["responses:write", "conversations:read"]

    decision = {"decision": "approve", "scopes": ["conversations:read"]}
    answered = await client.post(_decision(request_id), json=decision, headers=caller)
    approved = await answered.json()
    assert answered.status == 200
    _valid("GrantRequest", approved)
    assert approved["status"] == "approved"
    assert (await (await client.get(LIST, headers=AS_OWNER)).json())["data"] == []

    clock.now += la.POLL_INTERVAL_SECS
    again = {**ASK, "request_id": request_id}
    sent = await client.get("/oauth/authorize", params=again, allow_redirects=False)
    code = parse_qs(urlsplit(sent.headers["Location"]).query)["code"][0]
    form = {
        "grant_type": "authorization_code",
        "client_id": "notely-desktop",
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
    }
    tokens = await (await client.post("/oauth/token", data=form)).json()
    credential = client.store.verify(tokens["access_token"])
    assert credential is not None and credential.scopes == {"conversations:read"}


@pytest.mark.asyncio
async def test_a_denial_and_a_second_answer(client, clock):
    request_id = await _ask(client)
    denied = await client.post(_decision(request_id), json={"decision": "deny"}, headers=AS_OWNER)
    assert denied.status == 200 and (await denied.json())["status"] == "denied"
    twice = await client.post(_decision(request_id), json={"decision": "approve"}, headers=AS_OWNER)
    body = await twice.json()
    assert twice.status == 409
    _valid("Error", body)
    assert body["error"]["type"] == "conflict_error"
    assert body["error"]["code"] == "grant_request_already_resolved"
    clock.now += la.POLL_INTERVAL_SECS
    sent = await client.get(
        "/oauth/authorize", params={**ASK, "request_id": request_id}, allow_redirects=False
    )
    assert "error=access_denied" in sent.headers["Location"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        pytest.param(
            {"decision": "approve", "scopes": ["bots:chat"]}, 400, "invalid_scope", id="wider"
        ),
        pytest.param({"decision": "approve", "scopes": []}, 400, "invalid_scope", id="empty"),
        pytest.param({"decision": "maybe"}, 400, "invalid_request", id="bad-decision"),
        pytest.param(
            {"decision": "approve", "scopes": "all"}, 400, "invalid_request", id="bad-scopes"
        ),
        pytest.param(["approve"], 400, "invalid_request", id="not-an-object"),
    ],
)
async def test_a_bad_answer_is_refused_and_the_request_keeps_waiting(client, body, status, code):
    request_id = await _ask(client)
    answered = await client.post(_decision(request_id), json=body, headers=AS_OWNER)
    error = (await answered.json())["error"]
    assert (answered.status, error["code"]) == (status, code)
    assert [r["status"] for r in client.store.pending()] == ["pending"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["malformed", "full", "write"])
async def test_a_record_that_cannot_take_the_answer_is_503_not_a_bad_scope(
    client, tmp_path, monkeypatch, failure
):
    request_id = await _ask(client)
    path = tmp_path / "local-grants" / "state.json"
    if failure == "malformed":
        path.write_text("{not json", encoding="utf-8")
    elif failure == "full":
        monkeypatch.setattr(la, "_STATE_MAX_BYTES", len(path.read_bytes()) + 1)
    else:
        monkeypatch.setattr(la, "atomic_write", Mock(side_effect=OSError("disk unavailable")))
    before = path.read_bytes()
    client.audit.reset_mock()
    answered = await client.post(
        _decision(request_id), json={"decision": "approve"}, headers=AS_OWNER
    )
    body = await answered.json()
    assert answered.status == 503
    _valid("Error", body)
    assert body["error"]["code"] == "instance_unavailable"
    assert path.read_bytes() == before
    if failure != "write":
        # Refused before the audit: no row says approved for an answer that was not saved.
        client.audit.log_api_access.assert_not_called()


@pytest.mark.asyncio
async def test_a_decision_body_is_bounded_and_a_scope_given_twice_is_refused(client):
    request_id = await _ask(client)
    padded = {"decision": "approve", "scopes": ["conversations:read"] * 1000}
    too_large = await client.post(_decision(request_id), json=padded, headers=AS_OWNER)
    assert too_large.status == 400
    assert (await too_large.json())["error"]["code"] == "invalid_request"
    twice = {"decision": "approve", "scopes": ["conversations:read"] * 2}
    answered = await client.post(_decision(request_id), json=twice, headers=AS_OWNER)
    assert answered.status == 400
    assert (await answered.json())["error"]["code"] == "invalid_scope"
    assert [r["status"] for r in client.store.pending()] == ["pending"]


@pytest.mark.asyncio
async def test_an_unknown_or_expired_request_is_404(client, clock):
    unknown = await client.post(
        _decision("greq_nope"), json={"decision": "approve"}, headers=AS_OWNER
    )
    body = await unknown.json()
    assert unknown.status == 404
    assert (body["error"]["type"], body["error"]["code"]) == ("not_found_error", "not_found")
    request_id = await _ask(client)
    clock.now += la.REQUEST_TTL_SECS
    expired = await client.post(
        _decision(request_id), json={"decision": "approve"}, headers=AS_OWNER
    )
    assert expired.status == 404


@pytest.mark.asyncio
async def test_the_metadata_says_the_owner_answers_through_the_owner_routes(client):
    metadata = await (await client.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["crew_consent_modes"] == ["owner_api"]


def _one(request_id: str) -> str:
    return f"{LIST}/{request_id}"


def _grant(grant_id: str) -> str:
    return f"/v1/crew/owner/grants/{grant_id}"


async def _approved_tokens(client, clock, scopes=None) -> tuple[str, dict]:
    """A request the owner approved and the app redeemed: its id and the app's tokens."""
    request_id = await _ask(client)
    decision = {"decision": "approve", **({"scopes": scopes} if scopes else {})}
    answered = await client.post(_decision(request_id), json=decision, headers=AS_OWNER)
    assert answered.status == 200
    clock.now += la.POLL_INTERVAL_SECS
    sent = await client.get(
        "/oauth/authorize", params={**ASK, "request_id": request_id}, allow_redirects=False
    )
    code = parse_qs(urlsplit(sent.headers["Location"]).query)["code"][0]
    form = {
        "grant_type": "authorization_code",
        "client_id": ASK["client_id"],
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
    }
    return request_id, await (await client.post("/oauth/token", data=form)).json()


@pytest.mark.asyncio
async def test_a_request_says_how_it_was_answered_and_which_grant_it_led_to(client, clock):
    request_id = await _ask(client)
    waiting = await client.get(_one(request_id), headers=AS_OWNER)
    body = await waiting.json()
    assert waiting.status == 200
    _valid("GrantRequest", body)
    assert (body["id"], body["status"]) == (request_id, "pending")
    assert (body["decided_at"], body["decided_by"], body["grant_id"]) == (None, None, None)
    assert "granted_scopes" not in body
    assert (await (await client.get(LIST, headers=AS_OWNER)).json())["data"] == [body]

    decided_at = int(clock.now)
    request_id, tokens = await _approved_tokens(client, clock, ["conversations:read"])
    read = await client.get(_one(request_id), headers=AS_OWNER)
    body = await read.json()
    assert read.status == 200
    _valid("GrantRequest", body)
    # The app redeemed its code; to the owner the request is still an approved one.
    assert body["status"] == "approved"
    assert body["requested_scopes"] == ["responses:write", "conversations:read"]
    assert body["granted_scopes"] == ["conversations:read"]
    assert body["decided_at"] == decided_at
    assert body["decided_by"] == {"kind": "owner", "via": "api"}
    assert body["grant_id"] == tokens["grant_id"]
    assert not {"code_hash", "code_challenge", "state"} & set(body)


@pytest.mark.asyncio
async def test_a_denied_request_reads_denied_and_gives_no_scopes(client):
    request_id = await _ask(client)
    await client.post(_decision(request_id), json={"decision": "deny"}, headers=AS_OWNER)
    body = await (await client.get(_one(request_id), headers=AS_OWNER)).json()
    _valid("GrantRequest", body)
    assert (body["status"], body["grant_id"]) == ("denied", None)
    assert body["decided_by"] == {"kind": "owner", "via": "api"}
    assert "granted_scopes" not in body


@pytest.mark.asyncio
async def test_a_request_is_readable_as_long_as_the_record_keeps_it(client, clock):
    unknown = await client.get(_one("greq_nope"), headers=AS_OWNER)
    assert unknown.status == 404
    assert (await unknown.json())["error"]["code"] == "not_found"
    waited = await _ask(client)
    request_id, tokens = await _approved_tokens(client, clock)
    clock.now += la.REQUEST_TTL_SECS + la.CODE_TTL_SECS
    # Nobody answered the first in time: it is gone. The second is kept by its grant.
    assert (await client.get(_one(waited), headers=AS_OWNER)).status == 404
    assert (await client.get(_one(request_id), headers=AS_OWNER)).status == 200
    clock.now += la.REFRESH_MAX_SECS
    assert (await client.get(_one(request_id), headers=AS_OWNER)).status == 404
    assert (await client.delete(_grant(tokens["grant_id"]), headers=AS_OWNER)).status == 404


@pytest.mark.asyncio
async def test_the_owner_revokes_a_grant_and_its_tokens_stop_working(client, clock):
    _, tokens = await _approved_tokens(client, clock)
    assert client.store.verify(tokens["access_token"]) is not None
    client.audit.reset_mock()
    revoked = await client.delete(_grant(tokens["grant_id"]), headers=AS_OWNER)
    body = await revoked.json()
    assert revoked.status == 200
    _valid("Deleted", body)
    assert body == {"id": tokens["grant_id"], "object": "grant", "deleted": True}
    assert revoked.headers["X-Request-Id"].startswith("req_")
    client.audit.log_api_access.assert_called_once_with(
        caller=OWNER,
        operation="local_auth.revoke",
        outcome="revoked",
        source="local-authorization",
        resources=f"grant={tokens['grant_id']}",
        critical=True,
    )
    assert client.store.verify(tokens["access_token"]) is None
    with pytest.raises(la.AuthError):
        client.store.refresh(ASK["client_id"], tokens["refresh_token"], "", ISSUER)

    # A second call answers the same and writes nothing more.
    client.audit.reset_mock()
    again = await client.delete(_grant(tokens["grant_id"]), headers=AS_OWNER)
    assert again.status == 200 and await again.json() == body
    client.audit.log_api_access.assert_not_called()


@pytest.mark.asyncio
async def test_an_unknown_grant_is_404_and_writes_no_row(client):
    client.audit.reset_mock()
    missing = await client.delete(_grant("grt_nope"), headers=AS_OWNER)
    body = await missing.json()
    assert missing.status == 404
    _valid("Error", body)
    assert (body["error"]["type"], body["error"]["code"]) == ("not_found_error", "not_found")
    client.audit.log_api_access.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller",
    [AS_APP, {"X-Test-User": "123456789"}, {}, {"X-Test-Teammate": "relay"}],
    ids=["app-token", "another-user", "no-credential", "teammate"],
)
async def test_only_the_host_owner_reads_a_request_or_revokes_a_grant(
    client, clock, monkeypatch, caller
):
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    request_id, tokens = await _approved_tokens(client, clock)
    denial_audit = Mock(spec=["log_api_access"])
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: denial_audit)
    client.audit.reset_mock()
    read = await client.get(_one(request_id), headers=caller)
    revoked = await client.delete(_grant(tokens["grant_id"]), headers=caller)
    for refused in (read, revoked):
        body = await refused.json()
        assert refused.status == 403
        _valid("Error", body)
        assert (body["error"]["type"], body["error"]["code"]) == ("permission_error", "owner_only")
        assert set(body) == {"error"}
    # The grant still works, and the store audited nothing under the refused caller.
    assert client.store.verify(tokens["access_token"]) is not None
    client.audit.log_api_access.assert_not_called()
    rows = [call.kwargs for call in denial_audit.log_api_access.call_args_list]
    assert all(row["outcome"] == "denied" for row in rows)
    if "X-Test-Teammate" in caller:
        assert [row["operation"] for row in rows] == [
            "crew_api.grant_requests.get",
            "crew_api.grants.revoke",
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", ["record", "audit"])
async def test_a_revoke_that_cannot_be_recorded_is_503_and_revokes_nothing(
    client, clock, monkeypatch, fails
):
    request_id, tokens = await _approved_tokens(client, clock)
    with monkeypatch.context() as broken:
        if fails == "audit":
            broken.setattr(client.audit, "log_api_access", Mock(side_effect=OSError("audit")))
        else:
            broken.setattr(client.store, "_write", Mock(side_effect=OSError("disk")))
        revoked = await client.delete(_grant(tokens["grant_id"]), headers=AS_OWNER)
    body = await revoked.json()
    assert revoked.status == 503
    assert (body["error"]["type"], body["error"]["code"]) == (
        "server_error",
        "instance_unavailable",
    )
    assert client.store.verify(tokens["access_token"]) is not None

    monkeypatch.setattr(client.store, "_load", Mock(side_effect=OSError("disk")))
    unreadable = await client.get(_one(request_id), headers=AS_OWNER)
    assert unreadable.status == 503


def test_the_owner_view_keeps_the_stores_statuses_to_the_contracts_four():
    stored = {"pending", "approved", "denied", "code_issued", "exchanged"}
    shown = {la._OWNER_STATUS.get(status, status) for status in stored}
    assert shown == {"pending", "approved", "denied"}
