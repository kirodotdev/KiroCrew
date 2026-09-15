"""``/api/hooks/agent`` must be reachable by external callers, token-gated only.

A strict-internal entry would deny every non-loopback caller with 403 *before* the
handler's bearer check ran, making the webhook token layer unreachable and the
documented use case (a CI runner calling back) impossible. The endpoint instead
sits on ``token_auth._BYPASS_EXACT_METHODS`` — the METHOD-SCOPED bypass map —
alongside ``/api/messaging/teams``: both are self-authenticating external
webhooks, both POST-only. These tests pin that contract in both directions, plus
the failed-auth throttle that exposure requires.

The method scope is itself load-bearing. ``agent`` also matches the ``{hook_id}``
wildcard of the dashboard's hook CRUD routes (PUT/DELETE
``/api/hooks/{hook_id}``), whose handler authenticates on the dashboard token
alone, so a path-only bypass would hand those two methods to an anonymous caller.
"""

from __future__ import annotations

import pytest
from aiohttp import web

from kiro_crew import webhooks
from kiro_crew.dashboard import token_auth
from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import token_auth_middleware

HOOK_PATH = "/api/hooks/agent"


class TestAuthSurface:
    def test_not_strict_internal(self):
        """A strict-internal entry denies non-loopback before the bearer check."""
        assert HOOK_PATH not in _STRICT_INTERNAL_API_PATHS

    def test_on_middleware_bypass(self):
        assert token_auth._BYPASS_EXACT_METHODS.get(HOOK_PATH) == frozenset({"POST"})

    def test_not_on_the_path_only_bypass(self):
        """A path-only entry would also open PUT/DELETE on the same path."""
        assert HOOK_PATH not in token_auth._BYPASS_EXACT

    def test_teams_precedent_still_holds(self):
        """The bypass entry is justified by matching an existing self-auth webhook.

        Which is method-scoped for the same reason, so the shape a future
        self-authenticating webhook gets copied from is the safe one.
        """
        assert token_auth._BYPASS_EXACT_METHODS.get("/api/messaging/teams") == frozenset({"POST"})
        assert "/api/messaging/teams" not in token_auth._BYPASS_EXACT

    def test_other_mcp_paths_stay_strict(self):
        """Widening one path must not widen the rest of the internal surface."""
        for path in ("/api/send-message", "/api/outbox/notify", "/api/spawn-approve"):
            assert path not in token_auth._BYPASS_EXACT
            assert path not in token_auth._BYPASS_EXACT_METHODS

    def test_bypass_does_not_cover_the_dashboard_api(self):
        """The management endpoints stay dashboard-authed."""
        for path in ("/api/webhooks", "/api/webhooks/tokens", "/api/webhooks/test"):
            assert path not in token_auth._BYPASS_EXACT
            assert path not in token_auth._BYPASS_EXACT_METHODS


class TestBypassIsMethodScoped:
    """End-to-end through the middleware: only POST may skip the token gate.

    Asserting the constant alone would pass even if the middleware ignored the
    method scope, so these drive the real middleware with no credential at all.
    """

    @staticmethod
    async def _handler(request: web.Request) -> web.Response:
        return web.Response(text="reached")

    def _request(self, method: str, path: str = HOOK_PATH):
        from unittest.mock import MagicMock

        req = MagicMock(spec=web.Request)
        req.path = path
        req.query = {}
        req.cookies = {}
        req.remote = "203.0.113.9"  # non-loopback: an external webhook caller
        req.headers = {}
        req.method = method
        return req

    @pytest.mark.asyncio
    async def test_post_reaches_the_handler(self):
        """The webhook's own token check must be allowed to run."""
        resp = await token_auth_middleware()(self._request("POST"), self._handler)
        assert resp.status == 200
        assert resp.text == "reached"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["PUT", "DELETE", "GET", "PATCH"])
    async def test_other_methods_are_denied(self, method: str):
        """PUT/DELETE match api_hook_detail via {hook_id}; they need a token."""
        resp = await token_auth_middleware()(self._request(method), self._handler)
        assert resp.status != 200
        assert resp.text != "reached"

    @pytest.mark.asyncio
    async def test_teams_webhook_is_scoped_the_same_way(self):
        """The sibling self-auth webhook carries the same scope, POST only."""
        path = "/api/messaging/teams"
        resp = await token_auth_middleware()(self._request("POST", path), self._handler)
        assert resp.status == 200
        for method in ("PUT", "DELETE", "GET"):
            resp = await token_auth_middleware()(self._request(method, path), self._handler)
            assert resp.status != 200, f"{method} {path} bypassed the gate"


class TestAuthThrottle:
    @pytest.fixture(autouse=True)
    def _clean(self):
        webhooks._reset_auth_throttle()
        yield
        webhooks._reset_auth_throttle()

    def test_not_blocked_initially(self):
        assert webhooks.auth_throttle_blocked("10.0.0.1") is False

    def test_blocks_after_limit(self):
        src = "10.0.0.2"
        for _ in range(webhooks._AUTH_FAIL_LIMIT - 1):
            assert webhooks.record_auth_failure(src) is False
            assert webhooks.auth_throttle_blocked(src) is False
        assert webhooks.record_auth_failure(src) is True
        assert webhooks.auth_throttle_blocked(src) is True

    def test_block_expires(self):
        src = "10.0.0.3"
        t = 1_000_000.0
        for _ in range(webhooks._AUTH_FAIL_LIMIT):
            webhooks.record_auth_failure(src, now=t)
        assert webhooks.auth_throttle_blocked(src, now=t) is True
        assert webhooks.auth_throttle_blocked(
            src, now=t + webhooks._AUTH_FAIL_BLOCK + 1
        ) is False

    def test_window_rolls_over(self):
        """Slow, spread-out failures never trip the limit."""
        src = "10.0.0.4"
        t = 2_000_000.0
        for i in range(webhooks._AUTH_FAIL_LIMIT * 3):
            blocked = webhooks.record_auth_failure(
                src, now=t + i * (webhooks._AUTH_FAIL_WINDOW + 1)
            )
            assert blocked is False

    def test_success_clears_failures(self):
        src = "10.0.0.5"
        for _ in range(webhooks._AUTH_FAIL_LIMIT - 1):
            webhooks.record_auth_failure(src)
        webhooks.record_auth_success(src)
        # The counter reset, so the next failure is the first of a fresh window.
        assert webhooks.record_auth_failure(src) is False

    def test_sources_are_independent(self):
        for _ in range(webhooks._AUTH_FAIL_LIMIT):
            webhooks.record_auth_failure("10.0.0.6")
        assert webhooks.auth_throttle_blocked("10.0.0.6") is True
        assert webhooks.auth_throttle_blocked("10.0.0.7") is False

    def test_source_table_is_bounded(self):
        """A spoofed-source flood must not grow the table without limit."""
        for i in range(webhooks._AUTH_FAIL_MAX_SOURCES + 250):
            webhooks.record_auth_failure(f"10.1.{i // 256}.{i % 256}")
        assert len(webhooks._auth_failures) <= webhooks._AUTH_FAIL_MAX_SOURCES
