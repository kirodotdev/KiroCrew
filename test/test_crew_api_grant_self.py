"""``GET /v1/crew/grant``: an app reads its own grant, and only its own.

Two layers. Token auth is driven with the app credential a real local authorization
issued, to show the route admits it and what it publishes for the handler. The handler
is then called with exactly what token auth published, on the same record.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
import test_crew_api_app_credential as kit
import yaml
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from jsonschema import Draft202012Validator
from test_crew_api_app_credential import (
    CLIENT,
    KEY,
    _assert_401,
    _bearer,
    _error,
    _grant,
    _published,
    _send,
)
from token_auth_helpers import clear_nonces  # noqa: F401 -- autouse fixture

from kiro_crew.dashboard import crew_api_auth
from kiro_crew.dashboard import crew_api_grant_self as grant_self
from kiro_crew.dashboard import local_authorization as la
from kiro_crew.dashboard.crew_api_scopes import CREDENTIAL_KEY, ROUTES

# The fixtures of the app credential tests, registered here under their own names.
audit, clock, store = kit.audit, kit.clock, kit.store

PATH = "/v1/crew/grant"
OTHER = "calendar-helper"
_COMPONENTS = yaml.safe_load(
    (
        Path(__file__).resolve().parents[1]
        / "docs/request-for-change/assets/crew-api/components.yaml"
    ).read_text(encoding="utf-8")
)["components"]
#: The contract's ``GrantSelf`` properties: nothing else may be answered.
SPEC_FIELDS = frozenset(_COMPONENTS["schemas"]["GrantSelf"]["properties"])


def _valid(body: Any) -> None:
    schema = {"$ref": "#/components/schemas/GrantSelf", "components": _COMPONENTS}
    Draft202012Validator(schema).validate(body)


def _request(path: str = PATH, headers: dict[str, str] | None = None) -> web.Request:
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id="U_HOST_OWNER")
    return make_mocked_request("GET", path, app=app, headers=headers or {})


def _as_app(request: web.Request, credential: la.LocalCredential) -> web.Request:
    """*request* as token auth publishes a verified app credential on it."""
    request["user"] = f"app:{credential.grant_id}"
    request["app"] = f"local-grant:{credential.grant_id}"
    request["is_dashboard_user"] = False
    request[CREDENTIAL_KEY] = credential
    return request


def _credential(store: la.LocalAuthorization, tokens: dict) -> la.LocalCredential:
    found = store.verify(tokens["access_token"])
    assert found is not None
    return found


async def _read(request: web.Request) -> tuple[int, Any]:
    response = await grant_self.api_grant_self(request)
    assert response.headers["X-Request-Id"].startswith("req_")
    assert response.headers["Crew-API-Version"] == "1.0"
    return response.status, json.loads(response.body)


# ── token auth admits the app credential on this route ───────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["responses:write", "artifacts:read"])
async def test_token_auth_admits_any_app_credential_and_publishes_its_own_grant(
    store, clock, audit, scope
) -> None:
    tokens = _grant(store, clock, scope=scope)
    resp, request, ran = await _send(path=PATH, headers=_bearer(tokens["access_token"]))
    assert (resp.status, ran) == (200, True)
    row = audit.log_api_access.call_args.kwargs
    assert (row["outcome"], row["error"]) == ("allowed", "")
    assert row["resources"] == f"{PATH} client_id={CLIENT}"
    published = _published(request)
    assert published[CREDENTIAL_KEY].grant_id == tokens["grant_id"]
    assert published["is_dashboard_user"] is False


def test_the_route_is_one_an_app_is_served_on_and_needs_no_scope() -> None:
    assert grant_self.ROUTE_KEY in crew_api_auth.APP_SERVED_ROUTES
    assert PATH not in crew_api_auth.OWNER_ONLY_PUBLIC_ROUTES
    assert ROUTES[grant_self.ROUTE_KEY].scopes == frozenset({"any"})


@pytest.mark.asyncio
async def test_a_revoked_grants_token_is_401_before_the_handler(store, clock) -> None:
    tokens = _grant(store, clock)
    assert store.revoke_grant(tokens["grant_id"], caller="U_HOST_OWNER") is True
    resp, _, ran = await _send(path=PATH, headers=_bearer(tokens["access_token"]))
    _assert_401(resp, ran)


@pytest.mark.asyncio
async def test_an_app_credential_through_a_forwarder_is_401_here_too(store, clock) -> None:
    tokens = _grant(store, clock)
    headers = {**_bearer(tokens["access_token"]), "X-Forwarded-For": "203.0.113.7"}
    resp, request, ran = await _send(path=PATH, headers=headers)
    _assert_401(resp, ran)
    assert "app" not in _published(request)


@pytest.mark.asyncio
async def test_a_path_that_names_a_grant_is_not_this_route(store, clock) -> None:
    mine = _grant(store, clock)
    other = _grant(store, clock, client_id=OTHER)
    resp, _, ran = await _send(
        path=f"{PATH}/{other['grant_id']}", headers=_bearer(mine["access_token"])
    )
    assert (resp.status, ran, _error(resp)["code"]) == (403, False, "owner_only")


# ── the handler: the caller's own grant, in the contract's shape ─────────────


@pytest.mark.asyncio
async def test_an_app_reads_its_own_grant_in_the_contract_shape(store, clock) -> None:
    tokens = _grant(store, clock, scope="responses:write conversations:read")
    status, body = await _read(_as_app(_request(), _credential(store, tokens)))
    assert status == 200
    _valid(body)
    assert body == {
        "id": tokens["grant_id"],
        "object": "grant",
        "client_id": CLIENT,
        "kind": "local",
        "scopes": ["responses:write", "conversations:read"],
        "entitlements": [],
        "bots": {"mode": "selected", "ids": []},
        "status": "active",
        "pause": None,
        # The grant is made when the code is exchanged, the clock's last step.
        "created_at": int(clock.now),
    }


@pytest.mark.asyncio
async def test_the_answer_holds_only_contract_fields_and_no_secret(store, clock) -> None:
    tokens = _grant(store, clock)
    response = await grant_self.api_grant_self(_as_app(_request(), _credential(store, tokens)))
    body = json.loads(response.body)
    assert set(body) <= SPEC_FIELDS
    # None of what the owner's view adds, and nothing of the record's own.
    assert not {"namespace", "app", "expires_at", "quota"} & set(body)
    text = response.body.decode("utf-8")
    for secret in (tokens["access_token"], tokens["refresh_token"], KEY.decode("ascii")):
        assert secret not in text
    record = (store._path).read_text(encoding="utf-8")
    for digest in (v for v in json.loads(record)["grants"][tokens["grant_id"]].values()):
        if isinstance(digest, str) and len(digest) == 64:
            assert digest not in text


@pytest.mark.asyncio
async def test_two_apps_each_read_only_their_own_grant(store, clock) -> None:
    first = _grant(store, clock)
    second = _grant(store, clock, client_id=OTHER, scope="artifacts:read")
    for mine, theirs, client in ((first, second, CLIENT), (second, first, OTHER)):
        response = await grant_self.api_grant_self(_as_app(_request(), _credential(store, mine)))
        body = json.loads(response.body)
        assert (response.status, body["id"], body["client_id"]) == (200, mine["grant_id"], client)
        text = response.body.decode("utf-8")
        assert theirs["grant_id"] not in text
        assert (OTHER if client == CLIENT else CLIENT) not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "steer",
    [
        pytest.param(lambda other: (f"{PATH}?grant_id={other}", {}), id="query grant_id"),
        pytest.param(lambda other: (f"{PATH}?id={other}&client_id={OTHER}", {}), id="query id"),
        pytest.param(lambda other: (PATH, {"X-Grant-Id": other}), id="header"),
        pytest.param(lambda other: (PATH, {"X-Crew-Grant": other, "X-Client-Id": OTHER}), id="two"),
    ],
)
async def test_nothing_in_the_request_changes_which_grant_is_read(store, clock, steer) -> None:
    mine = _grant(store, clock)
    other = _grant(store, clock, client_id=OTHER)
    path, headers = steer(other["grant_id"])
    status, body = await _read(_as_app(_request(path, headers), _credential(store, mine)))
    assert (status, body["id"], body["client_id"]) == (200, mine["grant_id"], CLIENT)


# ── a caller with no grant of its own ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("user", ["U_HOST_OWNER", "U_TEAMMATE"], ids=["owner", "teammate"])
async def test_a_first_party_session_has_no_grant_and_is_404(store, clock, user) -> None:
    _grant(store, clock)
    request = _request()
    request["user"], request["app"], request["is_dashboard_user"] = user, "", True
    store.grants = Mock(side_effect=AssertionError("no grant is read for a session"))
    status, body = await _read(request)
    assert (status, body["error"]["type"], body["error"]["code"]) == (
        404,
        "not_found_error",
        "not_found",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True], ids=["audited", "audit fails"])
async def test_a_caller_with_neither_mark_is_refused_and_the_refusal_is_audited(
    store, clock, monkeypatch, broken
) -> None:
    """An installed app's token whose manifest names the path reaches the handler so."""
    _grant(store, clock)
    store.grants = Mock(side_effect=AssertionError("no grant is read for an unknown caller"))
    trail = Mock(spec=["log_api_access"])
    if broken:
        trail.log_api_access.side_effect = OSError("audit disk")
    monkeypatch.setattr(grant_self, "sel", lambda: trail)
    request = _request()
    request["user"], request["app"] = "U_HOST_OWNER", "notes-app"
    status, body = await _read(request)
    # A failed audit write changes no answer.
    assert (status, body["error"]["code"]) == (403, "owner_only")
    assert trail.log_api_access.call_args.kwargs == {
        "caller": "U_HOST_OWNER",
        "operation": "crew_api.grant.read",
        "outcome": "denied",
        "source": "crew-api",
        "resources": PATH,
        "error": "owner_only",
    }


@pytest.mark.asyncio
async def test_an_answer_that_is_not_a_refusal_writes_no_row_of_its_own(
    store, clock, monkeypatch
) -> None:
    tokens = _grant(store, clock)
    trail = Mock(spec=["log_api_access"])
    monkeypatch.setattr(grant_self, "sel", lambda: trail)
    status, _ = await _read(_as_app(_request(), _credential(store, tokens)))
    assert status == 200
    request = _request()
    request["user"], request["app"], request["is_dashboard_user"] = "U_HOST_OWNER", "", True
    status, _ = await _read(request)
    assert status == 404
    trail.log_api_access.assert_not_called()


@pytest.mark.asyncio
async def test_a_grant_revoked_after_token_auth_passed_is_404(store, clock) -> None:
    tokens = _grant(store, clock)
    credential = _credential(store, tokens)
    assert store.revoke_grant(tokens["grant_id"], caller="U_HOST_OWNER") is True
    status, body = await _read(_as_app(_request(), credential))
    assert (status, body["error"]["code"]) == (404, "not_found")
    assert CLIENT not in json.dumps(body)


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_read_is_503_and_says_nothing_of_it(
    store, clock, caplog
) -> None:
    tokens = _grant(store, clock)
    credential = _credential(store, tokens)
    store.grants = Mock(side_effect=OSError("private disk detail"))
    with caplog.at_level("WARNING", logger=grant_self.logger.name):
        status, body = await _read(_as_app(_request(), credential))
    assert (status, body["error"]["code"]) == (503, "instance_unavailable")
    assert "private disk detail" not in json.dumps(body) + caplog.text
    assert tokens["grant_id"] in caplog.text
