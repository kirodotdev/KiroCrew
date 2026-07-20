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

    def test_hashed_assets_are_immutable_cached(self) -> None:
        """Vite content-hashed bundles under /assets/ must be cacheable:
        the URL is the version, so no-store would force a full multi-MB
        re-download on every page load (and make post-restart reloads bet
        on a 6MB transfer during gateway cold-start)."""
        resp = _make_response()
        _apply_security_headers(
            resp, _make_app(), path="/assets/index-D9K94z8J.js"
        )
        cc = resp.headers["Cache-Control"]
        assert "immutable" in cc
        assert "max-age=31536000" in cc
        assert "no-store" not in cc
        # The no-cache companion headers must not undermine the cache
        assert "Pragma" not in resp.headers
        assert "Expires" not in resp.headers
        # Security headers still applied on the immutable path
        assert "Content-Security-Policy" in resp.headers
        assert "Permissions-Policy" in resp.headers

    def test_non_200_under_assets_stays_no_store(self) -> None:
        """During cold-start, /assets/* may return 404 or 503. Caching that
        with immutable would be a permanent black screen. Only success
        statuses get the immutable treatment."""
        for status in (404, 503):
            resp = web.Response(text="error", status=status)
            _apply_security_headers(
                resp, _make_app(), path="/assets/index-D9K94z8J.js"
            )
            assert "no-store" in resp.headers["Cache-Control"], f"status={status}"
            assert "immutable" not in resp.headers["Cache-Control"], f"status={status}"

    def test_conditional_and_range_under_assets_stay_immutable(self) -> None:
        """aiohttp's static handler answers 304 (conditional) and 206 (range)
        for hashed assets. A 304's headers merge into the browser's stored
        cache entry — answering it with no-store would degrade the cached
        immutable bundle back to uncacheable."""
        for status in (206, 304):
            resp = web.Response(status=status)
            _apply_security_headers(
                resp, _make_app(), path="/assets/index-D9K94z8J.js"
            )
            assert "immutable" in resp.headers["Cache-Control"], f"status={status}"
            assert "no-store" not in resp.headers["Cache-Control"], f"status={status}"

    def test_shell_and_api_paths_stay_no_store(self) -> None:
        for path in ("/", "/index.html", "/api/health", "/apps/dev-fleet"):
            resp = _make_response()
            _apply_security_headers(resp, _make_app(), path=path)
            assert "no-store" in resp.headers["Cache-Control"], path

    def test_unhashed_static_prefixes_stay_no_store(self) -> None:
        """/vendor, /fonts and /sprites use stable filenames — immutable
        caching would pin stale content across upgrades."""
        for path in (
            "/vendor/react.js",
            "/fonts/diatype.woff2",
            "/sprites/icons.svg",
        ):
            resp = _make_response()
            _apply_security_headers(resp, _make_app(), path=path)
            assert "no-store" in resp.headers["Cache-Control"], path

    def test_csp_default_no_instances(self) -> None:
        resp = _make_response()
        _apply_security_headers(resp, _make_app(with_instances=False))
        csp = resp.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "object-src 'none'" in csp
        # No loopback wildcards in frame-src when instances is disabled
        assert "http://127.0.0.1:*" not in csp
        assert "http://localhost:*" not in csp

    def test_csp_frame_src_allows_cloudfront_previews(self) -> None:
        """Webapp artifact live previews iframe the deployed CloudFront site
        (WebAppArtifactCard / WebAppThumb): https-only wildcard, present in
        BOTH modes, and never a bare scheme wildcard."""
        for with_instances in (False, True):
            resp = _make_response()
            _apply_security_headers(resp, _make_app(with_instances=with_instances))
            csp = resp.headers["Content-Security-Policy"]
            frame_src = next(d for d in csp.split(";") if d.strip().startswith("frame-src"))
            assert "https://*.cloudfront.net" in frame_src
            assert "http://*.cloudfront.net" not in frame_src
            assert "https://*" + " " not in frame_src  # no bare https wildcard

    def test_defense_in_depth_headers_present(self) -> None:
        """P475357944 (CWE-1021/693/200/319): the global pipeline sets
        clickjacking / MIME-sniffing / referrer / HSTS headers + CSP
        frame-ancestors."""
        resp = _make_response()
        _apply_security_headers(resp, _make_app(with_instances=False))
        assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "max-age=31536000" in resp.headers["Strict-Transport-Security"]
        assert "frame-ancestors 'self'" in resp.headers["Content-Security-Policy"]

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
