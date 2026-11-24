"""The Crew API scope table: its rows are the spec's, and it admits nothing by default.

``crew_api_scopes.ROUTES`` is held equal to ``x-crew-tier``, ``x-crew-scope`` and
``x-crew-entitlement`` in the two spec files. ``refusal_for`` is tried on each kind of row,
and through token auth on every route: an app credential reaches no handler.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_crew_api_app_credential as kit
import yaml
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from test_crew_api_app_credential import CLIENT, _bearer, _error, _grant, _send
from token_auth_helpers import clear_nonces  # noqa: F401 -- autouse fixture

from kiro_crew.dashboard import crew_api_auth, crew_api_scopes
from kiro_crew.dashboard import local_authorization as la
from kiro_crew.dashboard.crew_api_errors import contract_error, is_crew_api_path, is_owner_path
from kiro_crew.dashboard.crew_api_scopes import (
    ANY,
    EMBED,
    OWNER,
    PUBLIC,
    ROUTES,
    RouteScope,
    refusal_for,
)
from kiro_crew.dashboard.routes import crew_api

# The fixtures of the app credential tests, registered here under their own names.
audit, clock, store = kit.audit, kit.clock, kit.store

SPEC_DIR = Path(__file__).resolve().parents[1] / "docs/request-for-change/assets/crew-api"
ALL_SCOPES = " ".join(la.SCOPES)
MODELS = ("GET", "/v1/models")
OWNER_ONLY_TODAY = crew_api_auth.OWNER_ONLY_PUBLIC_ROUTES


def _as_list(value: Any) -> list[str]:
    return [value] if isinstance(value, str) else list(value or ())


def _as_set(value: Any) -> frozenset[str]:
    return frozenset(_as_list(value))


def _spec_rows() -> dict[tuple[str, str], RouteScope]:
    """Every operation of the two spec files, as a table row."""
    rows: dict[tuple[str, str], RouteScope] = {}
    for name in ("crew-public.yaml", "crew-owner.yaml"):
        paths = yaml.safe_load((SPEC_DIR / name).read_text(encoding="utf-8"))["paths"]
        for path, item in paths.items():
            for method, operation in item.items():
                if isinstance(operation, dict) and "x-crew-tier" in operation:
                    rows[(method.upper(), path)] = RouteScope(
                        operation["x-crew-tier"],
                        _as_set(operation["x-crew-scope"]),
                        # In the spec's own order: the first is the one a refusal names.
                        tuple(_as_list(operation.get("x-crew-entitlement"))),
                    )
    return rows


def _concrete(pattern: str) -> str:
    """A path that *pattern* matches."""
    return pattern.replace("{", "").replace("}", "")


def _every_row_router() -> web.UrlDispatcher:
    """A router with a route for every row of the table."""

    async def handler(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    for method, pattern in ROUTES:
        app.router.add_route(method, pattern, handler)
    return app.router


# ── the table is the spec ────────────────────────────────────────────────────


def test_the_table_is_the_spec() -> None:
    assert dict(ROUTES) == _spec_rows()


def test_the_spec_marks_any_and_embed_the_way_the_table_reads_them() -> None:
    rows = _spec_rows()
    assert rows[("GET", "/v1/crew/info")].scopes == ANY
    assert rows[("POST", "/v1/crew/approvals/{approval_id}/decision")].scopes == EMBED
    assert {row.api_tier for row in rows.values()} == {PUBLIC, OWNER}
    # No real scope is spelled like a marker, so a marker can never be held.
    assert not (ANY | EMBED) & set(la.SCOPES)


def test_an_owner_row_is_an_owner_path_and_a_public_row_is_not() -> None:
    for (_method, pattern), row in ROUTES.items():
        assert is_owner_path(pattern) == (row.api_tier == OWNER), pattern


def test_every_route_the_gateway_registers_has_a_row() -> None:
    app = web.Application()
    crew_api.register(app)
    registered = {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
        if route.resource is not None and route.method != "HEAD"
    }
    assert registered and registered <= set(ROUTES)


# ── refusal_for: one decision, in the contract's order ─────────────────────────────


@pytest.mark.parametrize(
    ("key", "held", "code", "reason"),
    [
        (("POST", "/v1/responses"), {"responses:write"}, None, None),
        (("GET", "/v1/responses/{response_id}"), {"conversations:read"}, None, None),
        (("GET", "/v1/responses/{response_id}"), {"responses:write"}, None, None),
        (("GET", "/v1/crew/info"), set(), None, None),
        (("POST", "/v1/responses"), {"conversations:read"}, "insufficient_scope", None),
        (("POST", "/v1/responses"), set(), "insufficient_scope", None),
        (("POST", "/v1/responses"), {"any", "embed"}, "insufficient_scope", None),
        (
            ("POST", "/v1/crew/approvals/{approval_id}/decision"),
            set(la.SCOPES) | {"embed"},
            "approval_requires_first_party",
            None,
        ),
        (("GET", "/v1/crew/schedules"), {"schedules:manage"}, "entitlement_required", None),
        (("GET", "/v1/crew/owner/grants"), {"grants:manage"}, "owner_only", None),
        (("GET", "/v1/crew/unknown"), set(la.SCOPES), "owner_only", "no-row"),
        (("PUT", "/v1/responses"), {"responses:write"}, "owner_only", "no-row"),
        (("GET", ""), set(la.SCOPES), "owner_only", "no-row"),
    ],
)
def test_refusal_for(key, held, code, reason) -> None:
    refusal = refusal_for(key, held, ())
    assert (refusal and refusal.code) == code
    if refusal is not None:
        assert refusal.reason == (reason or code)
        assert refusal.audit == (f"{code}:{reason}" if reason else code)


def test_a_scope_refusal_names_the_scopes_of_which_one_is_enough() -> None:
    refusal = refusal_for(("GET", "/v1/responses/{response_id}"), set(), ())
    assert refusal is not None
    assert refusal.message == "needs conversations:read or responses:write"


def test_an_entitlement_refusal_names_the_first_entitlement_the_spec_lists() -> None:
    refusal = refusal_for(("POST", "/v1/crew/memory/recall"), {"memory:read"}, ())
    assert refusal is not None
    assert (refusal.entitlement, refusal.message) == ("memory.read", "needs memory.read")
    assert _error(refusal.response(make_mocked_request("POST", "/v1/crew/memory/recall")))[
        "entitlement"
    ] == ("memory.read")
    # No other refusal carries the field.
    scope = refusal_for(("POST", "/v1/responses"), set(), ())
    assert scope is not None and scope.entitlement == ""
    assert "entitlement" not in _error(scope.response(make_mocked_request("POST", "/v1/responses")))


def test_an_extra_field_never_replaces_a_fixed_field_of_the_error_body() -> None:
    resp = contract_error(
        403, "owner_only", "no", "req_fixed0001", None, {"code": "other", "entitlement": "x.y"}
    )
    error = _error(resp)
    assert (error["code"], error["request_id"], error["entitlement"]) == (
        "owner_only",
        "req_fixed0001",
        "x.y",
    )


def test_every_row_that_names_an_entitlement_is_refused_with_every_scope_held() -> None:
    named = [key for key, row in ROUTES.items() if row.entitlements]
    assert named
    for key in named:
        refusal = refusal_for(key, set(la.SCOPES) | ROUTES[key].scopes, ())
        assert refusal is not None and refusal.code == "entitlement_required", key


def test_the_owner_only_list_is_read_before_the_table() -> None:
    """``GET /v1/models`` is Public for any credential in the spec, and owner-only today."""
    assert ROUTES[MODELS] == RouteScope(PUBLIC, ANY)
    assert refusal_for(MODELS, set(), ()) is None
    refusal = refusal_for(MODELS, set(la.SCOPES), OWNER_ONLY_TODAY)
    assert refusal is not None and refusal.audit == "owner_only"


# ── through token auth: only a listed route serves an app credential ─────────


def test_the_routes_that_serve_an_app_credential() -> None:
    """The whole list, so a route joins it only by a change to this test."""
    assert crew_api_auth.APP_SERVED_ROUTES == {MODELS}
    for key in crew_api_auth.APP_SERVED_ROUTES:
        assert ROUTES[key].api_tier == PUBLIC and not ROUTES[key].entitlements
    assert crew_api_auth.app_owner_only_routes() == {"/v1/responses"}


@pytest.mark.asyncio
async def test_an_app_credential_holding_every_scope_reaches_only_the_listed_routes(
    store, clock, audit
) -> None:
    token = _grant(store, clock, scope=ALL_SCOPES)["access_token"]
    router = _every_row_router()
    for method, pattern in ROUTES:
        resp, _request, ran = await _send(
            router, path=_concrete(pattern), method=method, headers=_bearer(token)
        )
        served = (method, pattern) in crew_api_auth.APP_SERVED_ROUTES
        assert (resp.status, ran) == ((200, True) if served else (403, False)), (method, pattern)


@pytest.mark.asyncio
async def test_an_app_credential_reaches_only_the_listed_routes_the_gateway_registers(
    tmp_path, monkeypatch, store, clock, audit
) -> None:
    """Checked against the live router, so a route registered anywhere is covered."""
    from test_dashboard_server_startup_coverage import _dashboard

    async with _dashboard(tmp_path, monkeypatch) as (runner, _state, _spies):
        router = runner.app.router
        routes = sorted(
            {
                (route.method, route.resource.canonical)
                for route in router.routes()
                if route.resource is not None
                and route.method != "*"
                and is_crew_api_path(route.resource.canonical)
            }
        )
    assert ("GET", "/v1/models") in routes and ("GET", "/v1/crew/owner/bots") in routes
    token = _grant(store, clock, scope=ALL_SCOPES)["access_token"]
    for method, pattern in routes:
        resp, _request, ran = await _send(
            router, path=_concrete(pattern), method=method, headers=_bearer(token)
        )
        if (method, pattern) in crew_api_auth.APP_SERVED_ROUTES:
            assert (resp.status, ran) == (200, True), (method, pattern)
            continue
        assert (resp.status, ran) == (403, False), (method, pattern)
        assert _error(resp)["code"] == "owner_only", (method, pattern)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "pattern", "scope", "code", "why", "message"),
    [
        # The row admits it, and no route serves an app: refused, and the row says why.
        (
            "POST",
            "/v1/conversations",
            "responses:write",
            "owner_only",
            "owner_only:no-app-route",
            crew_api_auth.APP_CREDENTIAL_REFUSED,
        ),
        (
            "GET",
            "/v1/crew/info",
            "bots:read",
            "owner_only",
            "owner_only:no-app-route",
            crew_api_auth.APP_CREDENTIAL_REFUSED,
        ),
        (
            "GET",
            "/v1/conversations",
            "responses:write",
            "insufficient_scope",
            "insufficient_scope",
            "needs conversations:read",
        ),
        (
            "POST",
            "/v1/crew/approvals/{approval_id}/decision",
            ALL_SCOPES,
            "approval_requires_first_party",
            "approval_requires_first_party",
            "an app credential may not answer an approval",
        ),
        (
            "POST",
            "/v1/crew/workflows/runs",
            ALL_SCOPES,
            "entitlement_required",
            "entitlement_required",
            "needs workflows.run",
        ),
        (
            "GET",
            "/v1/crew/owner/grants",
            ALL_SCOPES,
            "owner_only",
            "owner_only",
            crew_api_auth.APP_CREDENTIAL_REFUSED,
        ),
    ],
)
async def test_the_answer_and_the_audit_row_come_from_the_row(
    store, clock, audit, method, pattern, scope, code, why, message
) -> None:
    tokens = _grant(store, clock, scope=scope)
    path = _concrete(pattern)
    resp, _request, ran = await _send(
        _every_row_router(), path=path, method=method, headers=_bearer(tokens["access_token"])
    )
    assert (resp.status, ran) == (403, False)
    error = _error(resp)
    assert (error["type"], error["code"], error["message"]) == ("permission_error", code, message)
    # The contract's error body: ``entitlement`` is on entitlement_required alone.
    named = {"entitlement": "workflows.run"} if code == "entitlement_required" else {}
    assert error == {
        "type": "permission_error",
        "code": code,
        "message": message,
        "param": None,
        "request_id": resp.headers["X-Request-Id"],
        **named,
    }
    # "Sign in again" goes out with the tier refusal alone: it cures no missing scope.
    assert resp.headers.get("X-Auth-Required") == ("true" if code == "owner_only" else None)
    assert audit.log_api_access.call_count == 1
    row = audit.log_api_access.call_args.kwargs
    assert (row["caller"], row["outcome"], row["error"]) == (
        f"app:{tokens['grant_id']}",
        "denied",
        why,
    )
    assert row["resources"] == f"{path} client_id={CLIENT}"


@pytest.mark.asyncio
async def test_the_verified_credential_is_on_the_request_for_require_scope(store, clock) -> None:
    tokens = _grant(store, clock)
    _resp, request, _ran = await _send(path="/v1/models", headers=_bearer(tokens["access_token"]))
    published = {call.args[0]: call.args[1] for call in request.__setitem__.call_args_list}
    credential = published[crew_api_scopes.CREDENTIAL_KEY]
    assert credential.grant_id == tokens["grant_id"]
    assert credential.scopes == {"responses:write", "conversations:read"}


# ── require_scope: what a handler that serves apps calls first ───────────────


def _request(scopes: set[str] | None = None, **published: Any) -> web.Request:
    """A request carrying *published*, and a verified credential when *scopes* is given."""
    request = make_mocked_request("POST", "/v1/responses")
    for key, value in published.items():
        request[key] = value
    if scopes is not None:
        request[crew_api_scopes.CREDENTIAL_KEY] = SimpleNamespace(scopes=frozenset(scopes))
    return request


KEY = ("GET", "/v1/responses/{response_id}")
APP = "local-grant:grt_1"


def test_require_scope_passes_a_first_party_session_to_the_routes_own_gate() -> None:
    session = _request(user="U_HOST_OWNER", app="", is_dashboard_user=True)
    assert crew_api_scopes.require_scope(session, ("GET", "/v1/crew/owner/grants"), ()) is None


@pytest.mark.parametrize("held", [{"conversations:read"}, {"responses:write", "bots:read"}])
def test_require_scope_admits_a_credential_holding_one_of_the_scopes(held) -> None:
    request = _request(held, app=APP, is_dashboard_user=False)
    assert crew_api_scopes.require_scope(request, KEY, ()) is None


@pytest.mark.parametrize(
    "published",
    [
        # Neither marker: nothing says who this is.
        {},
        {"user": "U_HOST_OWNER"},
        # An app identity with no verified credential behind it.
        {"app": APP, "is_dashboard_user": False},
        {"app": "notes", "is_dashboard_user": False},
        # Marked a session and an app at once.
        {"app": APP, "is_dashboard_user": True},
        # A truthy value that is not the marker token auth sets.
        {"app": "", "is_dashboard_user": "yes"},
        {"app": "", "is_dashboard_user": False},
        # The session marker with no app value set at all, or one that is not "".
        {"is_dashboard_user": True},
        {"app": None, "is_dashboard_user": True},
    ],
)
def test_require_scope_refuses_a_caller_it_cannot_place(published) -> None:
    resp = crew_api_scopes.require_scope(_request(**published), ("GET", "/v1/crew/info"), ())
    assert resp is not None and resp.status == 403
    assert _error(resp)["code"] == "owner_only"


def test_require_scope_judges_a_credential_even_beside_a_session_marker() -> None:
    request = _request({"bots:read"}, app="", is_dashboard_user=True)
    resp = crew_api_scopes.require_scope(request, KEY, ())
    assert resp is not None and _error(resp)["code"] == "insufficient_scope"


def test_require_scope_has_no_default_for_the_owner_only_list() -> None:
    with pytest.raises(TypeError):
        crew_api_scopes.require_scope(_request(set(), app=APP), MODELS)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("scopes", "key", "owner_only_today", "code"),
    [
        ({"bots:read"}, KEY, (), "insufficient_scope"),
        (set(), KEY, (), "insufficient_scope"),
        ({"responses:write"}, ("GET", "/v1/crew/owner/grants"), (), "owner_only"),
        ({"responses:write"}, ("GET", "/v1/unknown"), (), "owner_only"),
        (set(), MODELS, OWNER_ONLY_TODAY, "owner_only"),
    ],
)
def test_require_scope_refuses_with_the_contract_error(scopes, key, owner_only_today, code) -> None:
    request = _request(scopes, app=APP, is_dashboard_user=False)
    resp = crew_api_scopes.require_scope(request, key, owner_only_today)
    assert resp is not None and resp.status == 403
    error = _error(resp)
    assert (error["type"], error["code"]) == ("permission_error", code)
    assert resp.headers["X-Request-Id"] == error["request_id"]
    assert "WWW-Authenticate" not in resp.headers
