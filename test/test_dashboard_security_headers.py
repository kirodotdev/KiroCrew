"""Tests for _apply_security_headers on dashboard responses.

Guards the Permissions-Policy header that unblocks
``navigator.clipboard.writeText`` on Chrome 143+ (crbug.com/414348233),
the Cache-Control triplet, and the CSP header (including the
instances-mode frame-src extension).
"""

from __future__ import annotations

from types import SimpleNamespace

from aiohttp import web

from kiro_crew.dashboard.server import _apply_security_headers


def _make_response() -> web.Response:
    return web.Response(text="ok")


def _make_app(with_instances: bool = False) -> web.Application:
    app = web.Application()
    instances_manager = object() if with_instances else None
    app["state"] = SimpleNamespace(instances_manager=instances_manager)
    return app


class TestApplySecurityHeaders:
    def test_permissions_policy_allows_clipboard_write(self) -> None:
        resp = _make_response()
        _apply_security_headers(resp, _make_app())
        # The specific value matters: Chrome 143+ requires the exact
        # allowlist form; a bare "clipboard-write" without the (self)
        # source expression does not grant permission.
        assert "clipboard-write=(self)" in resp.headers["Permissions-Policy"]
        assert "clipboard-read=(self)" in resp.headers["Permissions-Policy"]

    def test_cache_headers_prevent_stale_asset_caching(self) -> None:
        resp = _make_response()
        _apply_security_headers(resp, _make_app())
        assert "no-store" in resp.headers["Cache-Control"]
        assert resp.headers["Pragma"] == "no-cache"
        assert resp.headers["Expires"] == "0"

    def test_csp_default_no_instances(self) -> None:
        resp = _make_response()
        _apply_security_headers(resp, _make_app(with_instances=False))
        csp = resp.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "object-src 'none'" in csp
        # No loopback wildcards in frame-src when instances is disabled
        assert "http://127.0.0.1:*" not in csp
        assert "http://localhost:*" not in csp

    def test_csp_extends_frame_src_when_instances_enabled(self) -> None:
        resp = _make_response()
        _apply_security_headers(resp, _make_app(with_instances=True))
        csp = resp.headers["Content-Security-Policy"]
        # Loopback wildcards enable dynamically-connected tunnel port
        # iframes for the instances feature.
        assert "http://127.0.0.1:*" in csp
        assert "http://localhost:*" in csp
        assert "http://*.localhost:*" in csp

    def test_setdefault_semantics_do_not_override_handler_headers(self) -> None:
        resp = _make_response()
        # Simulate a handler that already set a header
        resp.headers["Cache-Control"] = "public, max-age=3600"
        _apply_security_headers(resp, _make_app())
        # Handler value preserved
        assert resp.headers["Cache-Control"] == "public, max-age=3600"
        # Other headers still applied
        assert "Permissions-Policy" in resp.headers

    def test_app_without_state_still_gets_headers(self) -> None:
        """Auth failure paths return responses on apps without state; the
        middleware must not raise on them."""
        resp = _make_response()
        app = web.Application()  # no state key
        _apply_security_headers(resp, app)
        assert "Permissions-Policy" in resp.headers
        assert "Content-Security-Policy" in resp.headers
