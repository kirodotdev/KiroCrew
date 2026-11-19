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
from test_local_authorization import ASK, REDIRECT, VERIFIER, Clock
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
