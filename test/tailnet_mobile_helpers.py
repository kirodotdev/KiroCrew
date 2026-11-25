"""Shared request and daemon stand-ins for the tailnet mobile-access handler tests.

A dedicated ``*_helpers.py`` imported by bare name, per the repo convention (see
``guide_route_helpers``): no ``test_*`` module imports another.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.dashboard.handlers import tailnet_mobile
from kiro_crew.dashboard.tailnet import DaemonProbe

_PORT = 5476
_HOST = "desk.tail-abc.ts.net"
_OWNER = "owner@example.com"


def _probe(
    *,
    name: str = _HOST,
    login: str = "owner@example.com",
    installed: bool = True,
    reachable: bool = True,
    logged_in: bool = True,
    https_enabled: bool | None = True,
    detail: str = "",
    stopped: bool = False,
    refused_cli: str = "",
) -> DaemonProbe:
    return DaemonProbe(
        name=name,
        installed=installed,
        reachable=reachable,
        logged_in=logged_in,
        detail=detail,
        login=login,
        stopped=stopped,
        refused_cli=refused_cli,
        https_enabled=https_enabled,
    )


def _request(
    *,
    restricted: bool = False,
    port: int = _PORT,
    body: object = None,
    app_identity: str | None = "",
    user: str | None = _OWNER,
    tailnet_host: str = "",
    query_token: str = "",
    cookie_token: str = "",
    auth_token: str | None = None,
):
    """Minimal stand-in for the aiohttp request these handlers actually touch.

    ``app_identity`` models what the auth middleware writes into ``request["app"]``:
    ``""`` for a dashboard user (the default here), an app name for an app token,
    and ``None`` for the key being ABSENT, i.e. the middleware never ran. A real
    ``web.Request`` is a MutableMapping, which is why ``.get`` is implemented
    rather than only the ``.app`` attribute.

    ``user`` models the token subject the middleware resolved. It defaults to the
    configured owner so the success paths read normally; pass a different id to
    model a NON-owner dashboard user (a messaging-channel user who was handed a
    presigned dashboard link), and ``None`` for no resolved subject at all.
    ``tailnet_host`` models the name the RUNNING server trusted at startup. Empty
    (the default) means the fixed allowlist does not carry the resolvable name,
    so ``_derive_step`` reports ``restart_gateway``. Ready paths must set it.
    ``query_token`` / ``cookie_token`` model the raw credential material.
    ``auth_token`` models what the middleware published as the VALIDATED
    credential (``request["auth_token"]``): defaults to ``query_token or
    cookie_token`` (the normal middleware-prefer-query behaviour), but pass
    ``auth_token=cookie_token`` explicitly to model the fallback path where the
    query token was invalid and the middleware fell back to the cookie. Both
    ``query_token`` and ``cookie_token`` empty (the default) exercises the
    fail-closed no-readable-token path.
    """

    class _Req:
        def __init__(self) -> None:
            self.app = {
                "port": port,
                "state": SimpleNamespace(owner_id="owner@example.com"),
                "tailnet_host": tailnet_host,
                "tailnet_resolved_at": 1 if tailnet_host else 0,
            }
            self.remote = "127.0.0.1"
            self.headers: dict[str, str] = {}
            self.query: dict[str, str] = {"token": query_token} if query_token else {}
            self.cookies: dict[str, str] = (
                {f"mc_token_{port}": cookie_token} if cookie_token else {}
            )
            self._items: dict[str, object] = {}
            if app_identity is not None:
                self._items["app"] = app_identity
            if user is not None:
                self._items["user"] = user
            # Publish the validated credential, mirroring token_auth middleware.
            _auth = auth_token if auth_token is not None else (query_token or cookie_token)
            if _auth:
                self._items["auth_token"] = _auth

        def get(self, key: str, default: object = None) -> object:
            return self._items.get(key, default)

        def __contains__(self, key: str) -> bool:
            # A real web.Request is a MutableMapping, so authorization predicates
            # legitimately use `"app" in request` to tell an ABSENT key (middleware
            # never ran) from an empty one (a dashboard user). Without this the
            # stand-in raises TypeError and the gate under test never runs.
            return key in self._items

        def __getitem__(self, key: str) -> object:
            return self._items[key]

        async def json(self):
            if body is None:
                raise ValueError("no body")
            return body

    req = _Req()
    if restricted:
        req.app["state"] = SimpleNamespace(owner_id="owner@example.com", _restricted=True)
    return req


@pytest.fixture(name="_unrestricted")
def unrestricted_fixture():
    with patch.object(tailnet_mobile, "_is_restricted_session", return_value=False) as m:
        yield m


@pytest.fixture(name="_quiet_audit")
def quiet_audit_fixture():
    with patch.object(tailnet_mobile, "_audit") as m:
        yield m
