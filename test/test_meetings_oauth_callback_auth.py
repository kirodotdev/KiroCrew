"""The Meetings OAuth callback reaches its handler without a dashboard token.

The consent leg of the calendar sign-in ends with the PROVIDER redirecting the
user's browser to ``GET /api/apps/meetings/calendar/oauth/callback``. From the
Electron shell that browser is the OS default (``window.open`` is forwarded to
``shell.openExternal``), which carries no dashboard cookie and no token, so on
the ordinary token gate every desktop sign-in ended on a 403 JSON page and no
token was ever stored. The route therefore sits on
``token_auth._BYPASS_EXACT_METHODS`` -- the METHOD-SCOPED bypass map, GET only --
and its admission is the handler's own single-use ``state`` check.

These tests pin that contract in both directions, and pin the spelling in
``token_auth`` to the one the app registers, because the two are written in
different modules on purpose (the dashboard keeps no import of an app package)
and a drift between them would fail only at the provider, with a message that
does not name the cause.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend.routes import calendar as calroutes
from kiro_crew.dashboard import token_auth
from kiro_crew.dashboard.token_auth import token_auth_middleware

CALLBACK = token_auth.MEETINGS_OAUTH_CALLBACK_PATH


class TestTheExemption:
    def test_spelling_matches_the_route_the_app_registers(self):
        """The middleware constant IS the path the start handler puts in the redirect URI."""
        assert CALLBACK == f"{k.API_BASE}{calroutes.OAUTH_CALLBACK_PATH}"

    def test_get_only(self):
        assert token_auth._BYPASS_EXACT_METHODS.get(CALLBACK) == frozenset({"GET"})

    def test_not_on_the_path_only_bypass(self):
        """A path-only entry would open every method on the path, not just the redirect."""
        assert CALLBACK not in token_auth._BYPASS_EXACT
        assert not any(CALLBACK.startswith(p) for p in token_auth._BYPASS_PREFIXES)

    def test_the_other_calendar_routes_stay_gated(self):
        """The exemption is the single callback path, not the calendar surface."""
        for suffix in (
            "/calendar",
            "/calendar/sync",
            "/calendar/providers",
            "/calendar/credentials",
            "/calendar/credentials/forget",
            "/calendar/oauth/start",
            "/calendar/oauth",
        ):
            path = f"{k.API_BASE}{suffix}"
            assert path not in token_auth._BYPASS_EXACT, path
            assert path not in token_auth._BYPASS_EXACT_METHODS, path


class TestThroughTheMiddleware:
    """Drive the real middleware with no credential at all.

    Asserting the constant alone would pass even if the middleware ignored the
    method scope.
    """

    @staticmethod
    async def _handler(request: web.Request) -> web.Response:
        return web.Response(text="reached")

    @staticmethod
    def _request(method: str, path: str = CALLBACK):
        req = MagicMock(spec=web.Request)
        req.path = path
        req.query = {"state": "not-checked-here", "code": "x"}
        req.cookies = {}
        req.remote = "127.0.0.1"  # the OS browser on the same machine
        req.headers = {}
        req.method = method
        return req

    @pytest.mark.asyncio
    async def test_a_tokenless_get_reaches_the_handler(self):
        resp = await token_auth_middleware()(self._request("GET"), self._handler)
        assert resp.status == 200
        assert resp.text == "reached"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
    async def test_other_methods_are_denied(self, method: str):
        resp = await token_auth_middleware()(self._request(method), self._handler)
        assert resp.status != 200
        assert resp.text != "reached"

    @pytest.mark.asyncio
    async def test_the_start_route_next_door_still_needs_a_token(self):
        """A prefix or a typo widening the grant would show up here."""
        start = f"{k.API_BASE}/calendar/oauth/start"
        for method in ("GET", "POST"):
            resp = await token_auth_middleware()(self._request(method, start), self._handler)
            assert resp.status != 200, f"{method} {start} bypassed the gate"
