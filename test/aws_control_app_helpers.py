"""Shared helpers for the ``test_aws_control_app*`` tests.

The route table builder, the mocked owner request and its payload reader, the guarded-surface
patches, and the autouse account-snapshot reset that every part runs under.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod
from kiro_crew.apps.builtins.aws_control.backend import routes as routes_mod

BASE = "/api/apps/aws-control"


def _registered() -> dict[tuple[str, str], object]:
    app = web.Application()
    routes_mod.register_routes(app)
    return {
        (route.method, str(route.resource.canonical)[len(BASE) :]): route.handler
        for route in app.router.routes()
        if str(route.resource.canonical).startswith(BASE)
        # add_get auto-registers a HEAD twin; the contract names the real verbs.
        and route.method != "HEAD"
    }


def _request(
    method: str,
    path: str,
    *,
    owner: bool = True,
    app_claim: str = "",
    match_info: dict | None = None,
    headers: dict | None = None,
) -> web.Request:
    """A real (mocked) aiohttp request carrying dashboard-owner identity.

    ``is_owner_dashboard_request`` reads ``request.app["state"].owner_id`` and
    the middleware-set ``app``/``user`` keys, so a real Application with a
    state object is attached — a duck-typed stub would make every ``get``
    truthy and take the owner branch unconditionally.
    """
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id="owner-1")
    kwargs: dict = {"app": app}
    if match_info is not None:
        kwargs["match_info"] = match_info
    if headers is not None:
        kwargs["headers"] = headers
    req = make_mocked_request(method, f"{BASE}{path}", **kwargs)
    req["app"] = app_claim
    req["user"] = "owner-1" if owner else "someone-else"
    return req


def _payload(response: web.StreamResponse) -> dict:
    raw = response.body  # type: ignore[attr-defined]
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


@pytest.fixture(autouse=True)
def _fresh_snapshot():
    accounts_mod.invalidate_cache()
    yield
    accounts_mod.invalidate_cache()


ACCOUNT = "111122223333"


def _enabled_owner_env():
    """Patches shared by every guarded-surface test: app on, account resolvable.

    Includes a live-probe stub RESOLVING TO THE REQUESTED ACCOUNT — the
    stale-mapping guard re-verifies profile->account on every target
    resolution, so an unpatched probe would 409 every guarded test.
    """
    return (
        mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
        mock.patch.object(
            routes_mod.accounts_mod,
            "resolve_account_profile",
            AsyncMock(return_value=("prof", "us-west-2")),
        ),
        mock.patch.object(
            routes_mod.aws_consent,
            "probe_identity",
            AsyncMock(return_value=aws_consent.Identity(ok=True, account=ACCOUNT)),
        ),
    )
