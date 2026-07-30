"""``/api/hooks/agent`` must be reachable by external callers, token-gated only.

The endpoint used to sit in ``server._STRICT_INTERNAL_API_PATHS``, which made the
auth middleware refuse every non-loopback caller with 403 *before* the handler's
bearer check ran — the webhook token layer was unreachable, so the documented use
case (a CI runner calling back) could not work at all. It is now on
``token_auth._BYPASS_EXACT`` like ``/api/messaging/teams``: a self-authenticating
external webhook. These tests pin that contract in both directions, plus the
failed-auth throttle that exposure requires.
"""

from __future__ import annotations

import pytest

from kiro_crew import webhooks
from kiro_crew.dashboard import token_auth
from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

HOOK_PATH = "/api/hooks/agent"


class TestAuthSurface:
    def test_not_strict_internal(self):
        """A strict-internal entry denies non-loopback before the bearer check."""
        assert HOOK_PATH not in _STRICT_INTERNAL_API_PATHS

    def test_on_middleware_bypass(self):
        assert HOOK_PATH in token_auth._BYPASS_EXACT

    def test_teams_precedent_still_holds(self):
        """The bypass entry is justified by matching an existing self-auth webhook."""
        assert "/api/messaging/teams" in token_auth._BYPASS_EXACT

    def test_other_mcp_paths_stay_strict(self):
        """Widening one path must not widen the rest of the internal surface."""
        for path in ("/api/send-message", "/api/outbox/notify", "/api/spawn-approve"):
            assert path not in token_auth._BYPASS_EXACT

    def test_bypass_does_not_cover_the_dashboard_api(self):
        """The management endpoints stay dashboard-authed."""
        for path in ("/api/webhooks", "/api/webhooks/tokens", "/api/webhooks/test"):
            assert path not in token_auth._BYPASS_EXACT


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
