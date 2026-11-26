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

from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.crew_teams import TEAM_MEMBERS_MAX
from kiro_crew.dashboard import crew_api_grant_requests as grant_requests
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


GRANTS = "/v1/crew/owner/grants"


@pytest.mark.asyncio
async def test_the_owner_lists_the_grants_that_work_now(client, clock):
    empty = await client.get(GRANTS, headers=AS_OWNER)
    assert empty.status == 200
    assert await empty.json() == {
        "object": "list",
        "data": [],
        "first_id": None,
        "last_id": None,
        "has_more": False,
    }
    _, first = await _approved_tokens(client, clock, ["conversations:read"])
    clock.now += 10
    _, second = await _approved_tokens(client, clock)
    # Asked and approved, never redeemed: not a grant.
    waiting = await _ask(client)
    await client.post(_decision(waiting), json={"decision": "approve"}, headers=AS_OWNER)

    listed = await client.get(GRANTS, headers=AS_OWNER)
    body = await listed.json()
    assert listed.status == 200
    _valid("GrantList", body)
    assert listed.headers["X-Request-Id"].startswith("req_")
    assert listed.headers["Crew-API-Version"]
    assert [g["id"] for g in body["data"]] == [first["grant_id"], second["grant_id"]]
    assert (body["first_id"], body["last_id"], body["has_more"]) == (
        first["grant_id"],
        second["grant_id"],
        False,
    )
    row = body["data"][0]
    assert row == {
        "id": first["grant_id"],
        "object": "grant",
        "kind": "local",
        "namespace": "default",
        "client_id": ASK["client_id"],
        "app": {"name": row["app"]["name"], "icon_url": None},
        "scopes": ["conversations:read"],
        "bots": {"mode": "selected", "ids": []},
        "status": "active",
        "created_at": row["created_at"],
        "expires_at": row["created_at"] + la.REFRESH_MAX_SECS,
    }
    # No token, code or digest is in the answer.
    text = json.dumps(body)
    for secret in (first["access_token"], first["refresh_token"], VERIFIER):
        assert secret not in text

    paged = await client.get(GRANTS, params={"limit": "1"}, headers=AS_OWNER)
    page = await paged.json()
    assert [g["id"] for g in page["data"]] == [first["grant_id"]] and page["has_more"] is True
    rest = await client.get(GRANTS, params={"after": first["grant_id"]}, headers=AS_OWNER)
    assert [g["id"] for g in (await rest.json())["data"]] == [second["grant_id"]]

    # A revoked grant is not listed, under any status.
    await client.delete(_grant(first["grant_id"]), headers=AS_OWNER)
    for params in ({}, {"status": "active"}):
        after = await (await client.get(GRANTS, params=params, headers=AS_OWNER)).json()
        assert [g["id"] for g in after["data"]] == [second["grant_id"]]
    for status in ("revoked", "paused", "throttled"):
        other = await client.get(GRANTS, params={"status": status}, headers=AS_OWNER)
        assert other.status == 200 and (await other.json())["data"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"status": "gone"},
        {"limit": "0"},
        {"limit": "101"},
        {"limit": "1e1"},
        {"limit": "٣"},
        {"after": "grt_nope"},
    ],
)
async def test_a_bad_grants_query_is_400(client, clock, params):
    await _approved_tokens(client, clock)
    refused = await client.get(GRANTS, params=params, headers=AS_OWNER)
    body = await refused.json()
    assert refused.status == 400
    _valid("Error", body)
    assert body["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller",
    [AS_APP, {"X-Test-User": "123456789"}, {}, {"X-Test-Teammate": "relay"}],
    ids=["app-token", "another-user", "no-credential", "teammate"],
)
async def test_only_the_host_owner_lists_grants(client, clock, monkeypatch, caller):
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    _, tokens = await _approved_tokens(client, clock)
    denial_audit = Mock(spec=["log_api_access"])
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: denial_audit)
    # The store is not read for a refused caller.
    monkeypatch.setattr(client.store, "grants", Mock(side_effect=AssertionError("read")))
    refused = await client.get(GRANTS, headers=caller)
    body = await refused.json()
    assert refused.status == 403
    _valid("Error", body)
    assert (body["error"]["type"], body["error"]["code"]) == ("permission_error", "owner_only")
    assert set(body) == {"error"} and tokens["grant_id"] not in json.dumps(body)
    rows = [call.kwargs for call in denial_audit.log_api_access.call_args_list]
    assert rows and all(row["outcome"] == "denied" for row in rows)
    if "X-Test-Teammate" in caller:
        assert [row["operation"] for row in rows] == ["crew_api.grants.list"]


@pytest.mark.asyncio
async def test_grants_that_cannot_be_read_are_503(client, clock, monkeypatch):
    await _approved_tokens(client, clock)
    monkeypatch.setattr(client.store, "_load", Mock(side_effect=OSError("disk")))
    unreadable = await client.get(GRANTS, headers=AS_OWNER)
    body = await unreadable.json()
    assert unreadable.status == 503
    assert body["error"]["code"] == "instance_unavailable"


# ── the Bots an app asks for, and the Bots the owner gives ───────────────────

BOTS_ASK = {**ASK, "bots": ["scout", "planner"]}


@pytest.fixture
def bots(monkeypatch) -> list[str]:
    """The Bots this Kiro Crew has, as the decision reads them."""
    existing = ["scout", "planner", "writer"]

    async def _existing() -> dict[str, str | None]:
        # Each with a member id of its own.
        return {name: f"{name}-1" for name in existing}

    monkeypatch.setattr(grant_requests, "_existing_bots", _existing)
    return existing


async def _ask_for_bots(client) -> str:
    asked = await client.get(
        "/oauth/authorize", params=BOTS_ASK, headers={"Accept": "application/json"}
    )
    assert asked.status == 202
    return str((await asked.json())["request_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [["scout", "planner"], ["dr. eggbot", "助手"]])
async def test_the_owner_reads_the_bots_asked_for_and_gives_fewer(client, clock, bots, names):
    bots.extend(names)
    ask = {**ASK, "bots": names}
    asked = await client.get("/oauth/authorize", params=ask, headers={"Accept": "application/json"})
    assert asked.status == 202
    request_id = (await asked.json())["request_id"]
    listed = (await (await client.get(LIST, headers=AS_OWNER)).json())["data"]
    assert listed[0]["requested_bots"] == names
    _valid("GrantRequest", listed[0])

    given = names if names[0] == "dr. eggbot" else names[1:]
    decision = {"decision": "approve", "bots": given}
    _valid("GrantRequestDecision", decision)
    answered = await client.post(_decision(request_id), json=decision, headers=AS_OWNER)
    body = await answered.json()
    assert answered.status == 200
    assert body["granted_bots"] == given
    _valid("GrantRequest", body)
    read = await (await client.get(_one(request_id), headers=AS_OWNER)).json()
    assert (read["requested_bots"], read["granted_bots"]) == (names, given)

    clock.now += la.POLL_INTERVAL_SECS
    sent = await client.get(
        "/oauth/authorize", params={**ask, "request_id": request_id}, allow_redirects=False
    )
    code = parse_qs(urlsplit(sent.headers["Location"]).query)["code"][0]
    form = {
        "grant_type": "authorization_code",
        "client_id": ASK["client_id"],
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
    }
    tokens = await (await client.post("/oauth/token", data=form)).json()
    grants = (await (await client.get(GRANTS, headers=AS_OWNER)).json())["data"]
    assert [(g["id"], g["bots"]) for g in grants] == [
        (tokens["grant_id"], {"mode": "selected", "ids": given})
    ]
    _valid("Grant", grants[0])


@pytest.mark.asyncio
async def test_an_approval_that_names_no_bot_gives_none(client, bots):
    request_id = await _ask_for_bots(client)
    answered = await client.post(
        _decision(request_id), json={"decision": "approve"}, headers=AS_OWNER
    )
    assert answered.status == 200
    assert (await answered.json())["granted_bots"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "given",
    [
        ["writer"],  # a Bot of this Kiro Crew that the app did not ask for
        ["scout", "scout"],
        "scout",
        [1],
    ],
)
async def test_a_bot_not_asked_for_or_given_twice_is_refused(client, bots, given):
    request_id = await _ask_for_bots(client)
    answered = await client.post(
        _decision(request_id), json={"decision": "approve", "bots": given}, headers=AS_OWNER
    )
    assert answered.status == 400
    assert (await answered.json())["error"]["code"] == "invalid_request"
    assert [r["status"] for r in client.store.pending()] == ["pending"]


@pytest.mark.asyncio
async def test_a_bot_this_kiro_crew_does_not_have_is_refused_at_the_decision(client, bots):
    request_id = await _ask_for_bots(client)
    bots.remove("planner")
    answered = await client.post(
        _decision(request_id),
        json={"decision": "approve", "bots": ["scout", "planner"]},
        headers=AS_OWNER,
    )
    assert answered.status == 400
    assert (await answered.json())["error"]["code"] == "invalid_request"
    assert [r["status"] for r in client.store.pending()] == ["pending"]
    # The Bots that do exist can still be given.
    answered = await client.post(
        _decision(request_id), json={"decision": "approve", "bots": ["scout"]}, headers=AS_OWNER
    )
    assert (answered.status, (await answered.json())["granted_bots"]) == (200, ["scout"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("known_name", "unknown_name"),
    [("scout", "no-such-bot"), ("dr. eggbot", "no such bot"), ("助手", "不存在")],
)
async def test_asking_never_says_whether_a_bot_exists(
    client, clock, bots, monkeypatch, known_name, unknown_name
):
    """The answer to the app is the same for a Bot this Kiro Crew has and one it lacks."""
    bots.append(known_name)
    assert unknown_name not in bots
    roster = Mock(side_effect=AssertionError)
    monkeypatch.setattr(grant_requests, "_existing_bots", roster)
    monkeypatch.setattr(
        "aiohttp.web_response.rfc822_formatted_time", lambda: "Tue, 24 Nov 2026 00:00:00 GMT"
    )
    monkeypatch.setattr(la.secrets, "token_urlsafe", lambda n: "A" * 16)
    monkeypatch.setattr(la.secrets, "choice", lambda alphabet: alphabet[0])
    answers = []
    for index, name in enumerate((known_name, unknown_name)):
        store = la.LocalAuthorization(
            client.store._path.with_name(f"s{index}.json"), b"k" * 32, clock
        )
        monkeypatch.setattr(la, "gateway_local_authorization", lambda store=store: store)
        asked = await client.get(
            "/oauth/authorize",
            params={**ASK, "bots": name},
            headers={"Accept": "application/json"},
        )
        answers.append((asked.status, await asked.read(), sorted(asked.headers.items())))
        clock.now += la.POLL_INTERVAL_SECS
        again = await client.get(
            "/oauth/authorize",
            params={**ASK, "bots": name, "request_id": "greq_" + "A" * 16},
            headers={"Accept": "application/json"},
        )
        answers.append((again.status, await again.read(), sorted(again.headers.items())))
        clock.now -= la.POLL_INTERVAL_SECS
    known, known_again, unknown, unknown_again = answers
    assert known[0] == known_again[0] == 202
    assert known == unknown and known_again == unknown_again
    roster.assert_not_called()


@pytest.mark.asyncio
async def test_a_decision_exceeding_the_body_limit_is_refused(client, bots):
    request_id = await _ask_for_bots(client)
    padded = {"decision": "approve", "bots": ["scout"] * 1000}
    answered = await client.post(_decision(request_id), json=padded, headers=AS_OWNER)
    assert answered.status == 400
    assert (await answered.json())["error"]["message"] == (
        "the body is missing its length or too large"
    )
    assert [r["status"] for r in client.store.pending()] == ["pending"]


@pytest.mark.asyncio
async def test_a_roster_that_cannot_be_read_gives_no_bot(client, monkeypatch):
    async def _unreadable() -> set[str]:
        raise OSError("config unreadable")

    monkeypatch.setattr(grant_requests, "_existing_bots", _unreadable)
    request_id = await _ask_for_bots(client)
    answered = await client.post(
        _decision(request_id), json={"decision": "approve", "bots": ["scout"]}, headers=AS_OWNER
    )
    assert answered.status == 503
    assert [r["status"] for r in client.store.pending()] == ["pending"]
    # A decision that gives no Bot does not read the roster.
    answered = await client.post(
        _decision(request_id), json={"decision": "approve"}, headers=AS_OWNER
    )
    assert answered.status == 200


@pytest.mark.asyncio
async def test_a_denial_reads_no_roster_and_gives_no_bot(client, monkeypatch):
    monkeypatch.setattr(grant_requests, "_existing_bots", Mock(side_effect=AssertionError))
    request_id = await _ask_for_bots(client)
    answered = await client.post(
        _decision(request_id), json={"decision": "deny", "bots": ["scout"]}, headers=AS_OWNER
    )
    body = await answered.json()
    assert (answered.status, body["status"]) == (200, "denied")
    assert "granted_bots" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "given",
    [
        ["sco\x1bt"],
        ["x" * (la.MEMBER_NAME_MAX_CHARS + 1)],
        [str(i) for i in range(TEAM_MEMBERS_MAX + 1)],
    ],
)
async def test_malformed_decision_bots_are_refused_before_the_roster(client, monkeypatch, given):
    request_id = await _ask_for_bots(client)
    roster = Mock(side_effect=AssertionError)
    monkeypatch.setattr(grant_requests, "_existing_bots", roster)
    body = {"decision": "approve", "bots": given}
    # Short names keep the cap-plus-one case below the independent body limit.
    assert len(json.dumps(body).encode("utf-8")) <= grant_requests._BODY_MAX
    answered = await client.post(_decision(request_id), json=body, headers=AS_OWNER)
    assert answered.status == 400
    error = (await answered.json())["error"]
    assert error["code"] == "invalid_request"
    assert error["message"] == "bots is malformed"
    assert [r["status"] for r in client.store.pending()] == ["pending"]
    roster.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("answered", [False, True])
@pytest.mark.parametrize("given", [["scout"], ["sco\x1bt"], ["scout", "scout"]])
async def test_decision_checks_request_before_bots_or_roster(client, monkeypatch, answered, given):
    request_id = "greq_missing"
    if answered:
        request_id = await _ask_for_bots(client)
        client.store.decide(request_id, False, caller=OWNER)
    roster = Mock(side_effect=OSError("roster unavailable"))
    monkeypatch.setattr(grant_requests, "_existing_bots", roster)
    response = await client.post(
        _decision(request_id), json={"decision": "approve", "bots": given}, headers=AS_OWNER
    )
    assert response.status == (409 if answered else 404)
    assert (await response.json())["error"]["code"] == (
        "grant_request_already_resolved" if answered else "not_found"
    )
    roster.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["*", "agents"])
async def test_a_degraded_config_cannot_confirm_the_default_bot(client, monkeypatch, section):
    config = grant_requests.KiroCrewConfig(
        agents={"default": KiroCrewAgentConfig()},
        _degraded_sections=frozenset({section}),
    )
    monkeypatch.setattr(grant_requests.KiroCrewConfig, "load", lambda: config)
    _, view = client.store.authorize(ASK, ["default"])
    before = client.store._path.read_bytes()
    response = await client.post(
        _decision(view["request_id"]),
        json={"decision": "approve", "bots": ["default"]},
        headers=AS_OWNER,
    )
    assert response.status == 503
    assert (await response.json())["error"]["code"] == "instance_unavailable"
    assert client.store._path.read_bytes() == before
    assert client.store.request(view["request_id"])["status"] == "pending"


@pytest.mark.asyncio
async def test_a_healthy_config_confirms_only_dispatchable_bots(monkeypatch):
    config = grant_requests.KiroCrewConfig(
        agents={
            "default": KiroCrewAgentConfig(),
            "助手": KiroCrewAgentConfig(member_id="helper-2"),
            "bad\x1bname": KiroCrewAgentConfig(member_id="bad-1"),
        },
        default_agent="default",
    )
    monkeypatch.setattr(grant_requests.KiroCrewConfig, "load", lambda: config)
    # Each Bot a turn can name, with the member id its record holds.
    assert await grant_requests._existing_bots() == {"default": "", "助手": "helper-2"}


@pytest.mark.asyncio
async def test_an_approval_keeps_which_bot_each_given_name_is(client, clock, monkeypatch):
    """The decision route hands ``decide`` the identities of the read that checked the Bots."""

    async def _existing() -> dict[str, str | None]:
        return {"scout": "", "planner": "planner-7", "writer": "writer-1"}

    monkeypatch.setattr(grant_requests, "_existing_bots", _existing)
    request_id = await _ask_for_bots(client)
    given = ["scout", "planner"]
    answered = await client.post(
        _decision(request_id), json={"decision": "approve", "bots": given}, headers=AS_OWNER
    )
    body = await answered.json()
    assert (answered.status, body["granted_bots"]) == (200, given)
    # Kept with the request, for the grant made from it, and in no answer.
    stored = json.loads(client.store._path.read_text(encoding="utf-8"))["requests"][request_id]
    assert stored["granted_bot_member_ids"] == {"scout": "", "planner": "planner-7"}
    assert not [key for key in stored if "default" in key]
    read = await (await client.get(_one(request_id), headers=AS_OWNER)).json()

    def _assert_no_member_ids(answer: Any) -> None:
        if isinstance(answer, dict):
            for key, value in answer.items():
                assert "member_id" not in key
                _assert_no_member_ids(value)
        elif isinstance(answer, list):
            for value in answer:
                _assert_no_member_ids(value)
        else:
            assert answer not in ("planner-7", "writer-1")

    for answer in (body, read):
        _assert_no_member_ids(answer)


@pytest.mark.asyncio
async def test_an_expired_answered_request_is_not_found_while_its_grant_lives(
    client, clock, monkeypatch
):
    request_id, tokens = await _approved_tokens(client, clock)
    clock.now = client.store.request(request_id)["expires_at"]
    assert client.store.request(request_id)["status"] == "approved"
    assert client.store.grants()[0]["grant_id"] == tokens["grant_id"]
    roster = Mock(side_effect=AssertionError)
    monkeypatch.setattr(grant_requests, "_existing_bots", roster)
    response = await client.post(
        _decision(request_id), json={"decision": "approve", "bots": ["scout"]}, headers=AS_OWNER
    )
    assert response.status == 404
    assert (await response.json())["error"]["code"] == "not_found"
    with pytest.raises(la.UnknownRequest):
        client.store.decide(request_id, True, caller=OWNER)
    roster.assert_not_called()


@pytest.mark.asyncio
async def test_an_unrequested_missing_bot_is_refused_before_reading_the_roster(client, monkeypatch):
    request_id = await _ask_for_bots(client)
    roster = Mock(side_effect=AssertionError)
    monkeypatch.setattr(grant_requests, "_existing_bots", roster)
    response = await client.post(
        _decision(request_id),
        json={"decision": "approve", "bots": ["missing"]},
        headers=AS_OWNER,
    )
    assert response.status == 400
    error = (await response.json())["error"]
    assert error["code"] == "invalid_request"
    assert error["message"] == "bots must be a subset of the requested bots"
    assert client.store.request(request_id)["status"] == "pending"
    roster.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_null_bots_is_refused_but_omitting_bots_gives_none(client, monkeypatch):
    request_id = await _ask_for_bots(client)
    roster = Mock(side_effect=AssertionError)
    monkeypatch.setattr(grant_requests, "_existing_bots", roster)
    response = await client.post(
        _decision(request_id), json={"decision": "approve", "bots": None}, headers=AS_OWNER
    )
    assert response.status == 400
    error = (await response.json())["error"]
    assert error["code"] == "invalid_request"
    assert error["message"] == "bots must be a list of strings"
    assert client.store.request(request_id)["status"] == "pending"
    response = await client.post(
        _decision(request_id), json={"decision": "approve"}, headers=AS_OWNER
    )
    assert response.status == 200
    assert (await response.json())["granted_bots"] == []
    roster.assert_not_called()
