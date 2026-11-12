"""The Crew API Owner routes (``/v1/crew/owner/*``) against their contract and their twins.

``test/fixtures/crew_api_owner_openapi.json`` describes every Owner route the gateway
registers. These tests keep three things in step:

* the routes registered under ``/v1/crew/owner/`` and the paths the file describes;
* each route's success and error bodies and headers and the file's schemas;
* each route's success body and its ``/api/*`` twin's, for the same state.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from chat_test_helpers import _make_state
from jsonschema import Draft202012Validator

from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.dashboard import crew_api_owner
from kiro_crew.dashboard import routes as routes_pkg
from kiro_crew.dashboard.handlers import members as m

CONTRACT = json.loads(
    (Path(__file__).parent / "fixtures" / "crew_api_owner_openapi.json").read_text(encoding="utf-8")
)
MEMBERS = "/v1/crew/owner/members"
CREW = "code-reviewer"
OWNER = "U_OWNER"


def _validator(ref: str) -> Draft202012Validator:
    """A validator for one ``#/components/...`` schema of the contract file."""
    return Draft202012Validator({"$ref": ref, "components": CONTRACT["components"]})


def _owner_routes() -> dict[tuple[str, str], Any]:
    app = web.Application()
    routes_pkg.register_all(app)
    return {
        (route.method, route.resource.canonical): route.handler
        for route in app.router.routes()
        if route.resource is not None
        and route.method != "HEAD"
        and route.resource.canonical.startswith(crew_api_owner.OWNER_PATH_PREFIX + "/")
    }


def _fake_cfg() -> SimpleNamespace:
    return SimpleNamespace(
        agents={CREW: KiroCrewAgentConfig(kiro_agent="kirocrew", description="d", model="m1")},
        default_agent=CREW,
        memory_stores={},
        workspaces={"default": SimpleNamespace(dir="workspace")},
        default_workspace="default",
        degraded_sections=frozenset(),
    )


def _request(state: Any, path: str, *, app_claim: str = "", user: str = OWNER, headers=None):
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("GET", path, app=app, headers=headers or {})
    req["app"] = app_claim
    req["user"] = user
    return req


def _owner_state(tmp_path: Path) -> Any:
    state = _make_state(tmp_path)
    state.owner_id = OWNER
    return state


async def _call(handler, req) -> tuple[int, Any, Any]:
    with patch.object(m.KiroCrewConfig, "load", return_value=_fake_cfg()):
        resp = await handler(req)
    return resp.status, json.loads(resp.body), resp.headers


def _members_route():
    return _owner_routes()[("GET", MEMBERS)]


def test_every_owner_route_is_described_and_every_described_route_is_served():
    served = set(_owner_routes())
    described = {
        (method.upper(), path)
        for path, item in CONTRACT["paths"].items()
        for method in item
        if path.startswith(crew_api_owner.OWNER_PATH_PREFIX + "/")
    }
    assert served == described


def test_each_owner_route_wraps_the_handler_of_the_twin_it_names():
    app = web.Application()
    routes_pkg.register_all(app)
    table = {
        (route.method, route.resource.canonical): route.handler
        for route in app.router.routes()
        if route.resource is not None
    }
    for path, item in CONTRACT["paths"].items():
        for method, operation in item.items():
            twin_method, twin_path = operation["x-crew-wraps"].split(" ", 1)
            wrapped = table[(method.upper(), path)]
            assert wrapped.__wrapped__ is table[(twin_method, twin_path)]


@pytest.mark.asyncio
async def test_the_roster_matches_the_contract(tmp_path):
    status, body, headers = await _call(_members_route(), _request(_owner_state(tmp_path), MEMBERS))
    assert status == 200
    _validator("#/components/schemas/MemberRoster").validate(body)
    assert [row["name"] for row in body["members"]] == [CREW]
    header_schemas = CONTRACT["components"]["headers"]
    for name in ("X-Request-Id", "Crew-API-Version"):
        Draft202012Validator(header_schemas[name]["schema"]).validate(headers[name])


@pytest.mark.asyncio
async def test_the_roster_is_the_same_on_both_routes(tmp_path):
    state = _owner_state(tmp_path)
    _s, legacy, _h = await _call(m.api_members, _request(state, "/api/members"))
    _s, owner, _h = await _call(_members_route(), _request(state, MEMBERS))
    _s, legacy_again, _h = await _call(m.api_members, _request(state, "/api/members"))
    assert owner == legacy == legacy_again


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_claim", "user", "code"),
    [
        pytest.param("someapp", "someapp", "owner_only", id="app-token"),
        pytest.param("", "123456789", "owner_only", id="non-owner-dashboard-user"),
    ],
)
async def test_a_refusal_is_a_contract_error_and_reads_nothing(tmp_path, app_claim, user, code):
    req = _request(_owner_state(tmp_path), MEMBERS, app_claim=app_claim, user=user)

    def _no_read(*_a, **_k):
        raise AssertionError("a refused caller must not reach the roster")

    with (
        patch.object(m.KiroCrewConfig, "load", side_effect=_no_read),
        patch.object(m, "load_config_with_content_stamp", side_effect=_no_read),
    ):
        resp = await _members_route()(req)
    body = json.loads(resp.body)
    assert resp.status == 403
    _validator("#/components/schemas/Error").validate(body)
    assert body["error"]["code"] == code
    assert body["error"]["type"] == "permission_error"
    assert body["error"]["request_id"] == resp.headers["X-Request-Id"]


@pytest.mark.asyncio
async def test_an_app_token_refusal_is_written_to_the_audit_trail(tmp_path):
    """An app that reaches the route (its permissions.api names it) is audited."""
    req = _request(_owner_state(tmp_path), MEMBERS, app_claim="someapp", user="someapp")
    sel = MagicMock()
    with patch("kiro_crew.sel.sel", return_value=sel):
        resp = await _members_route()(req)
    assert resp.status == 403
    assert json.loads(resp.body)["error"]["code"] == "owner_only"
    sel.log_api_access.assert_called_once()
    assert sel.log_api_access.call_args.kwargs["outcome"] == "denied"


@pytest.mark.asyncio
async def test_a_handler_error_becomes_a_contract_error():
    async def _failing(_request):
        return web.json_response({"error": "roster unavailable", "code": "roster_busy"}, status=503)

    route = crew_api_owner.owner_route(_failing, "test.fail")
    req = make_mocked_request("GET", MEMBERS)
    req["app"] = ""
    with patch.object(crew_api_owner, "require_owner_dashboard_request", return_value=None):
        resp = await route(req)
    body = json.loads(resp.body)
    assert resp.status == 503
    _validator("#/components/schemas/Error").validate(body)
    assert body["error"] == {
        "type": "server_error",
        "code": "roster_busy",
        "message": "roster unavailable",
        "param": None,
        "request_id": resp.headers["X-Request-Id"],
    }


@pytest.mark.asyncio
async def test_a_401_from_the_owner_gate_carries_a_bearer_challenge():
    denied = web.json_response({"error": "sign in", "code": "stale_session_reauth"}, status=401)
    route = crew_api_owner.owner_route(_members_handler_unused, "test.denied")
    req = make_mocked_request("GET", MEMBERS)
    req["app"] = ""
    with patch.object(crew_api_owner, "require_owner_dashboard_request", return_value=denied):
        resp = await route(req)
    assert resp.status == 401
    assert json.loads(resp.body)["error"]["code"] == "stale_session_reauth"
    assert resp.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'


async def _members_handler_unused(_request):
    raise AssertionError("a refused caller must not reach the handler")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("supplied", "kept"),
    [
        pytest.param("req_client-123", True, id="req-shaped"),
        pytest.param("not-a-request-id", False, id="wrong-prefix"),
        pytest.param("req_" + "x" * 65, False, id="too-long"),
    ],
)
async def test_a_req_shaped_request_id_is_kept_and_any_other_replaced(tmp_path, supplied, kept):
    req = _request(_owner_state(tmp_path), MEMBERS, headers={"X-Request-Id": supplied})
    _s, _b, headers = await _call(_members_route(), req)
    assert (headers["X-Request-Id"] == supplied) is kept
    assert headers["X-Request-Id"].startswith("req_")


def test_the_bot_prune_gate_holds_the_owner_roster_like_its_twin():
    from kiro_crew.dashboard import server as server_mod

    assert server_mod._crewmate_prune_gate_holds_path("/api/members")
    assert server_mod._crewmate_prune_gate_holds_path(MEMBERS)
