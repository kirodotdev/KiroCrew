"""The self-authenticating webhooks' CSRF exemption, and Teams route hardening.

``POST /api/messaging/teams`` and ``POST /api/hooks/agent`` are the two routes in
the product an unauthenticated external caller can reach. The Bot Framework
Connector posts to the first from Microsoft's servers holding nothing but a JWT;
CI runners, review bots and deploy pipelines post to the second holding nothing
but a webhook bearer token. Three things make that safe, and each is pinned here
because none of them is visible from the handler:

1. **The CSRF Origin check is skipped for the TEAMS path, POST only.** The Bot
   Framework Connector is server-to-server and sends neither ``Origin`` nor
   ``Referer``, and ``check_origin`` accepts a header-less request only from a
   loopback peer — so without the exemption the route 403s in the topology that
   exposes the gateway directly, before its own credential is ever examined. The
   set is defined once (``token_auth.CSRF_EXEMPT_EXACT_METHODS``) and read by the
   single middleware factory both entrypoints build from, so it cannot be granted
   on one server and withheld on the other. `/api/hooks/agent` shares the shape and
   has the TOKEN bypass, but is deliberately NOT Origin-exempt: no reported failure
   named it, and a perimeter exemption is harder to withdraw than to add.
2. **The method scope is load-bearing, and on the hook path it closes a live
   collision.** The literal ``agent`` also matches the ``{hook_id}`` wildcard of
   the PUT/DELETE ``/api/hooks/{hook_id}`` CRUD routes, whose handler
   authenticates by dashboard token alone, so an unscoped entry would strip the
   CSRF barrier off those two methods as well.
3. **The Teams route throttles failed auth and caps the body** before delegating,
   so an anonymous flood cannot buy an unbounded buffer per request. Note the
   throttle's own scope: it is SKIPPED for a proxied request (see
   ``is_proxied_request``), which is normal in two of the three documented
   topologies, so it is not what bounds the JWKS refetch -- that damper lives next
   to the refetch in ``JwtValidator._get_signing_key`` and is pinned in
   ``test_teams_client.py``. The hook route's own equivalent
   (``webhooks.auth_throttle``) is covered by ``test_webhooks_auth_surface.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew import webhooks
from kiro_crew.dashboard import server as server_mod
from kiro_crew.dashboard import token_auth
from kiro_crew.dashboard.handlers import messaging
from kiro_crew.dashboard.token_auth import (
    AGENT_HOOK_PATH,
    TEAMS_WEBHOOK_PATH,
    is_csrf_exempt,
)
from kiro_crew.teams.client import TEAMS_ACTIVITY_REQUEST_KEY, TEAMS_MAX_ACTIVITY_BYTES


class _FakeContent:
    """Minimal stand-in for ``aiohttp.StreamReader`` exposing ``iter_chunked``."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def iter_chunked(self, n: int):
        for i in range(0, len(self._data), n):
            yield self._data[i : i + n]


class _FakeRequest(dict):
    """Request double: a MutableMapping (as ``web.Request`` is) plus the few
    attributes the middleware and the handler read."""

    def __init__(
        self,
        *,
        method: str = "POST",
        path: str = TEAMS_WEBHOOK_PATH,
        remote: str = "203.0.113.9",
        headers: dict[str, str] | None = None,
        body: bytes = b"{}",
        content_length: int | None = None,
        app: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.method = method
        self.path = path
        self.remote = remote
        self.headers = headers or {}
        self.content = _FakeContent(body)
        self.content_length = len(body) if content_length is None else content_length
        self.app = app if app is not None else {"allowed_origins": {"http://localhost:5476"}}


async def _reached(request: Any) -> web.Response:
    return web.Response(text="reached")


# ── 1. The exemption set itself ──


class TestCsrfExemptSet:
    def test_only_the_teams_webhook_is_enrolled(self) -> None:
        """The whole map, not just its one entry: a SECOND path arriving silently is
        the failure this asserts against. Every member relaxes a security control on
        a route an anonymous caller can reach, so growing the set must go red here
        and be argued rather than merely reviewed.

        `/api/hooks/agent` is deliberately absent even though it shares the shape and
        IS in the token-auth bypass: no reported failure named it, its own proxy
        topologies already work, and a perimeter exemption is much harder to withdraw
        once a caller depends on it than to add later with its own cause."""
        assert token_auth.CSRF_EXEMPT_EXACT_METHODS == {
            TEAMS_WEBHOOK_PATH: frozenset({"POST"}),
        }

    def test_the_agent_hook_is_not_csrf_exempt(self) -> None:
        """Skipping the cookie gate and skipping the Origin check are two grants.

        The hook path has the first (it predates this change) and must not silently
        acquire the second: that is a widening of an unrelated route's perimeter.
        """
        assert AGENT_HOOK_PATH in token_auth._BYPASS_EXACT_METHODS
        assert AGENT_HOOK_PATH not in token_auth.CSRF_EXEMPT_EXACT_METHODS
        assert is_csrf_exempt(AGENT_HOOK_PATH, "POST") is False

    def test_the_path_is_spelled_once(self) -> None:
        """Both middleware exemptions on the Teams route key off the same constant,
        or one control ends up pointed at a route the other is not."""
        assert TEAMS_WEBHOOK_PATH in token_auth._BYPASS_EXACT_METHODS
        assert TEAMS_WEBHOOK_PATH in token_auth.CSRF_EXEMPT_EXACT_METHODS

    @pytest.mark.parametrize("path,route_module", [(TEAMS_WEBHOOK_PATH, "messaging")])
    def test_the_constant_names_a_route_the_app_registers(
        self, path: str, route_module: str
    ) -> None:
        """Guards each constant against drifting off the real route table: an
        exemption on a path nobody serves is silent, and so is the reverse."""
        import importlib

        routes = importlib.import_module(f"kiro_crew.dashboard.routes.{route_module}")
        app = web.Application()
        routes.register(app)
        methods = {
            route.method
            for route in app.router.routes()
            if getattr(route.resource, "canonical", "") == path
        }
        assert methods, f"{path} is not registered"
        assert token_auth.CSRF_EXEMPT_EXACT_METHODS[path] <= methods

    @pytest.mark.parametrize("method", ["PUT", "DELETE", "GET", "PATCH", "HEAD"])
    def test_predicate_is_method_scoped(self, method: str) -> None:
        assert is_csrf_exempt(TEAMS_WEBHOOK_PATH, "POST") is True
        assert is_csrf_exempt(TEAMS_WEBHOOK_PATH, method) is False

    def test_predicate_rejects_unknown_paths(self) -> None:
        assert is_csrf_exempt("/api/chat", "POST") is False
        # Exact-path matching, not a prefix: a sibling hook id is a different
        # route, and the trailing-slash spelling is not the registered one.
        assert is_csrf_exempt("/api/hooks/other", "POST") is False
        assert is_csrf_exempt(AGENT_HOOK_PATH + "/", "POST") is False


# ── 2. End-to-end through the real middleware ──


class TestCsrfMiddlewareHonoursTheExemption:
    """Driven through ``_make_csrf_middleware``, because asserting the constant
    alone would pass even if the middleware ignored it."""

    @pytest.fixture(autouse=True)
    def _no_sel(self, monkeypatch: pytest.MonkeyPatch):
        """Record denial audits instead of writing to the real security log."""
        audits: list[str] = []

        async def _fake_audit(caller: str, request: Any, error: str) -> None:
            audits.append(error)

        monkeypatch.setattr(server_mod, "_audit_denied", _fake_audit)
        return audits

    @pytest.mark.asyncio
    async def test_connector_post_without_origin_reaches_the_handler(self) -> None:
        """The defect this exemption fixes: no Origin, no Referer, non-loopback
        peer — a direct Connector POST at a public hostname."""
        mw = server_mod._make_csrf_middleware("dashboard_user")
        resp = await mw(_FakeRequest(), _reached)
        assert resp.text == "reached"

    @pytest.mark.asyncio
    async def test_a_foreign_origin_on_the_webhook_is_also_admitted(self) -> None:
        """The exemption skips the check entirely rather than widening the allowed
        origin set: the handler's JWT is the credential, and a cross-origin page
        cannot obtain one."""
        mw = server_mod._make_csrf_middleware("dashboard_user")
        req = _FakeRequest(headers={"Origin": "https://evil.example"})
        resp = await mw(req, _reached)
        assert resp.text == "reached"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
    async def test_other_methods_on_the_same_path_still_face_the_check(
        self, method: str, _no_sel: list[str]
    ) -> None:
        mw = server_mod._make_csrf_middleware("dashboard_user")
        with pytest.raises(web.HTTPForbidden):
            await mw(_FakeRequest(method=method), _reached)
        assert _no_sel and "CSRF check failed" in _no_sel[0]

    @pytest.mark.asyncio
    async def test_a_hook_post_without_origin_is_still_refused(self) -> None:
        """The hook route is NOT enrolled, and this is what that means end to end.

        Same shape a CI runner produces -- no Origin, no Referer, non-loopback peer --
        and it 403s before ``_verify_hook_token`` sees the bearer token, exactly as it
        does on main. Enrolling it would be a widening of an unrelated route's
        perimeter, so it belongs to a change whose own cause names it.
        """
        mw = server_mod._make_csrf_middleware("dashboard_user")
        with pytest.raises(web.HTTPForbidden):
            await mw(_FakeRequest(path=AGENT_HOOK_PATH), _reached)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["PUT", "DELETE"])
    async def test_hook_crud_methods_on_the_same_path_still_403_and_audit(
        self, method: str, _no_sel: list[str]
    ) -> None:
        """The method scope driven end to end, which is the half the constant
        cannot prove. ``agent`` also matches the ``{hook_id}`` wildcard of
        PUT/DELETE ``/api/hooks/{hook_id}``, whose handler authenticates by
        dashboard token alone — so those keep facing the Origin check, and the
        refusal is still recorded rather than silently dropped."""
        mw = server_mod._make_csrf_middleware("dashboard_user")
        with pytest.raises(web.HTTPForbidden):
            await mw(_FakeRequest(method=method, path=AGENT_HOOK_PATH), _reached)
        assert _no_sel and "CSRF check failed" in _no_sel[0]

    @pytest.mark.asyncio
    async def test_an_ordinary_api_post_is_still_checked(self, _no_sel: list[str]) -> None:
        """The exemption must not become a hole for the rest of the API."""
        mw = server_mod._make_csrf_middleware("dashboard_user")
        with pytest.raises(web.HTTPForbidden):
            await mw(_FakeRequest(path="/api/chat"), _reached)
        assert _no_sel

    @pytest.mark.asyncio
    async def test_the_exempt_path_writes_no_denial_audit(self, _no_sel: list[str]) -> None:
        """An allowed request is not a denial. The carve-out relies on
        ``sel_audit_middleware`` (registered inner to this one) for its record,
        matching how the Host barrier's PROBE_PATHS exemption behaves."""
        mw = server_mod._make_csrf_middleware("dashboard_user")
        await mw(_FakeRequest(), _reached)
        assert _no_sel == []

    @pytest.mark.asyncio
    async def test_safe_methods_are_untouched(self) -> None:
        mw = server_mod._make_csrf_middleware("dashboard_user")
        resp = await mw(_FakeRequest(method="GET", path="/api/sessions"), _reached)
        assert resp.text == "reached"


def test_both_servers_install_the_shared_csrf_barrier() -> None:
    """Wiring pin, mirroring the Host-barrier pin: BOTH entrypoints must build the
    CSRF barrier from the shared factory, so the exemption set is a single
    decision. An inline copy could drop or widen it on one server only."""
    import inspect

    for func, name in (
        (server_mod.start_dashboard, "start_dashboard"),
        (server_mod.start_api_server, "start_api_server"),
    ):
        src = inspect.getsource(func)
        assert "_make_csrf_middleware(" in src, f"{name} no longer uses the shared CSRF factory"
        assert "async def csrf_middleware" not in src, (
            f"{name} re-introduced an inline CSRF middleware; keep the shared "
            "factory as the single exemption point"
        )


def test_the_middleware_reads_the_shared_predicate() -> None:
    """The exemption must be a lookup in the owning module, never a path literal
    re-typed into ``server.py`` (which is how the two controls drift apart)."""
    import inspect

    src = inspect.getsource(server_mod._make_csrf_middleware)
    assert "is_csrf_exempt(" in src
    assert TEAMS_WEBHOOK_PATH not in src
    assert AGENT_HOOK_PATH not in src


# ── 3. Route-level hardening ──


class _State:
    def __init__(self, handler: Any = None) -> None:
        self.teams_on_activity = handler


class TestActivityRouteHardening:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch: pytest.MonkeyPatch):
        """The throttle is process-global module state, and ``_sel`` would
        otherwise write to the real security log."""
        webhooks._reset_auth_throttle()
        monkeypatch.setattr(messaging, "_sel", lambda: MagicMock())
        yield
        webhooks._reset_auth_throttle()

    @staticmethod
    def _request(**kwargs: Any) -> _FakeRequest:
        state = kwargs.pop("state", None)
        req = _FakeRequest(**kwargs)
        req.app = {"state": state}
        return req

    @pytest.mark.asyncio
    async def test_503_until_the_channel_is_wired(self) -> None:
        resp = await messaging.api_teams_activity(self._request(state=_State(None)))
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_a_valid_body_is_stashed_for_the_delegate(self) -> None:
        """``on_activity`` reads the stash, so the body is parsed exactly once and
        never past the cap."""
        seen: list[Any] = []

        async def _delegate(request: Any) -> web.Response:
            seen.append(request.get(TEAMS_ACTIVITY_REQUEST_KEY))
            return web.Response(status=200)

        req = self._request(state=_State(_delegate), body=b'{"type": "message"}')
        resp = await messaging.api_teams_activity(req)
        assert resp.status == 200
        assert seen == [{"type": "message"}]

    @pytest.mark.asyncio
    async def test_oversized_body_is_413_and_never_delegated(self) -> None:
        calls: list[int] = []

        async def _delegate(request: Any) -> web.Response:
            calls.append(1)
            return web.Response(status=200)

        req = self._request(
            state=_State(_delegate),
            body=b"{}",
            content_length=TEAMS_MAX_ACTIVITY_BYTES + 1,
        )
        resp = await messaging.api_teams_activity(req)
        assert resp.status == 413
        assert calls == []

    @pytest.mark.asyncio
    async def test_streamed_oversize_is_also_refused(self) -> None:
        """A chunked activity carries no Content-Length, so the cap has to hold on
        the incremental read too."""
        req = self._request(
            state=_State(_reached),
            body=b"x" * (TEAMS_MAX_ACTIVITY_BYTES + 1),
            content_length=None,
        )
        # Declared length is derived from the body here, so drop it to force the
        # streaming path the Connector's chunked POST takes.
        req.content_length = None
        resp = await messaging.api_teams_activity(req)
        assert resp.status == 413

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b""])
    async def test_a_malformed_body_still_reaches_the_token_check(self, body: bytes) -> None:
        """The ordering guarantee: no verdict derived from body CONTENT may precede
        the JWT check, so the route leaves the stash unset and lets
        ``on_activity`` answer after authenticating."""
        seen: list[Any] = []

        async def _delegate(request: Any) -> web.Response:
            seen.append(request.get(TEAMS_ACTIVITY_REQUEST_KEY))
            return web.Response(status=401)

        req = self._request(state=_State(_delegate), body=body)
        resp = await messaging.api_teams_activity(req)
        assert resp.status == 401
        assert seen == [None]

    @pytest.mark.asyncio
    async def test_repeated_401s_throttle_the_source(self) -> None:
        async def _unauthorized(request: Any) -> web.Response:
            return web.Response(status=401)

        state = _State(_unauthorized)
        for _ in range(webhooks._AUTH_FAIL_LIMIT):
            resp = await messaging.api_teams_activity(self._request(state=state))
            assert resp.status == 401

        calls: list[int] = []

        async def _counted(request: Any) -> web.Response:
            calls.append(1)
            return web.Response(status=401)

        state.teams_on_activity = _counted
        resp = await messaging.api_teams_activity(self._request(state=state))
        assert resp.status == 429
        assert resp.body is not None and b"auth_throttled" in resp.body
        # Throttled before delegating: the point is to avoid the JWKS fetch.
        assert calls == []

    @pytest.mark.asyncio
    async def test_throttle_is_per_source(self) -> None:
        async def _unauthorized(request: Any) -> web.Response:
            return web.Response(status=401)

        state = _State(_unauthorized)
        for _ in range(webhooks._AUTH_FAIL_LIMIT):
            await messaging.api_teams_activity(self._request(state=state))
        resp = await messaging.api_teams_activity(self._request(state=state, remote="198.51.100.4"))
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_an_accepted_activity_clears_the_failure_state(self) -> None:
        async def _unauthorized(request: Any) -> web.Response:
            return web.Response(status=401)

        state = _State(_unauthorized)
        for _ in range(webhooks._AUTH_FAIL_LIMIT - 1):
            await messaging.api_teams_activity(self._request(state=state))

        async def _ok(request: Any) -> web.Response:
            return web.Response(status=200)

        state.teams_on_activity = _ok
        await messaging.api_teams_activity(self._request(state=state))
        assert webhooks.auth_throttle_blocked("203.0.113.9") is False

        # The counter reset, so the next failure starts a fresh window rather
        # than tripping the block immediately.
        state.teams_on_activity = _unauthorized
        await messaging.api_teams_activity(self._request(state=state))
        assert webhooks.auth_throttle_blocked("203.0.113.9") is False

    @pytest.mark.asyncio
    async def test_a_400_from_the_delegate_is_neither_failure_nor_success(self) -> None:
        """A malformed payload arrives only after the JWT passed, so it is not an
        authentication failure — and it must not clear an attacker's counter
        either."""

        async def _unauthorized(request: Any) -> web.Response:
            return web.Response(status=401)

        state = _State(_unauthorized)
        for _ in range(webhooks._AUTH_FAIL_LIMIT - 1):
            await messaging.api_teams_activity(self._request(state=state))

        async def _bad_request(request: Any) -> web.Response:
            return web.Response(status=400)

        state.teams_on_activity = _bad_request
        await messaging.api_teams_activity(self._request(state=state))

        state.teams_on_activity = _unauthorized
        resp = await messaging.api_teams_activity(self._request(state=state))
        assert resp.status == 401
        assert webhooks.auth_throttle_blocked("203.0.113.9") is True
