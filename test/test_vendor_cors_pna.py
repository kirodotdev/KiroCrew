"""Tests for the ``/vendor/*`` CORS / Private-Network-Access contract.

Sandboxed widget/artifact iframes are null-origin (srcdoc/blob) documents —
NON-secure contexts — and on the default deployment the gateway is plain http
on loopback, a "more-private address space" under Chrome's Private Network
Access policy. Chrome therefore blocks the iframe's ``<script src>`` for the
vendored Tailwind runtime unless BOTH halves of the contract hold:

1. the ``/vendor/*`` subresource response carries
   ``Access-Control-Allow-Origin`` (the script tag loads with
   ``crossorigin="anonymous"``), and
2. the PNA preflight OPTIONS is answered 2xx with
   ``Access-Control-Allow-Private-Network: true``.

Without them the runtime never loads: Tailwind-classed widgets render
unstyled and the widget loading overlay sits on its 15s hang backstop as a
blank box (issue #6181). These tests pin both halves.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import token_auth
from kiro_crew.dashboard.server import (
    _apply_security_headers,
    _register_dist_static_routes,
)


def _make_app() -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(instances_manager=None)
    return app


def _make_dist(root: Path) -> Path:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "app.js").write_text("/* app */", encoding="utf-8")
    vendor = dist / "vendor"
    vendor.mkdir()
    (vendor / "tailwindcss-browser.js").write_text("/* runtime */", encoding="utf-8")
    return dist


class TestVendorCorsHeader:
    def test_vendor_response_gets_allow_origin(self) -> None:
        """A /vendor/ path gets Access-Control-Allow-Origin: * — the header a
        null-origin iframe's crossorigin="anonymous" script load requires."""
        resp = web.Response(text="ok")
        _apply_security_headers(resp, _make_app(), path="/vendor/tailwindcss-browser.js")
        assert resp.headers["Access-Control-Allow-Origin"] == "*"

    def test_non_vendor_paths_get_no_allow_origin(self) -> None:
        """CORS opening is scoped to /vendor/ only — the dashboard's own pages
        and APIs must NOT become cross-origin readable."""
        for path in ("/", "/api/health", "/assets/index-D9K94z8J.js", "/vendorx/a.js"):
            resp = web.Response(text="ok")
            _apply_security_headers(resp, _make_app(), path=path)
            assert "Access-Control-Allow-Origin" not in resp.headers, path


class TestVendorPreflight:
    """The OPTIONS route add_static cannot provide (it registers GET/HEAD only,
    so a preflight would 405 and Chrome fails the fetch closed).

    Each test opens an in-test ``TestClient`` with ``async with`` (matching
    ``test_api_kiro_hooks.py``) rather than an async-gen fixture: the CI-pinned
    ``pytest-asyncio==0.20.3`` is incompatible with the pinned ``pytest==8.4.1``
    for async fixtures (its wrapper reads the ``fixturedef.unittest`` attribute
    removed in pytest 8.1), so the whole suite avoids
    ``@pytest_asyncio.fixture`` by convention.
    """

    @pytest.fixture
    def app(self, tmp_path):
        # `crossorigin="anonymous"` on the widget script tag makes the CORS
        # header MANDATORY on the real wire response — a unit call on
        # _apply_security_headers alone would stay green if the middleware
        # stopped feeding it the request path. Mount a middleware using the
        # exact production call form (pinned against the source by
        # test_middleware_uses_the_request_path below) so the GET assertions
        # exercise the same path the browser sees.
        @web.middleware
        async def apply_headers(request: web.Request, handler):
            resp = await handler(request)
            _apply_security_headers(resp, request.app, request.path, request)
            return resp

        application = web.Application(middlewares=[apply_headers])
        application["state"] = SimpleNamespace(instances_manager=None)
        # _register_dist_static_routes resets token_auth's module-level
        # app-window registry as a side effect; restore it so this fixture
        # cannot leak state into unrelated tests in the same worker.
        prior_app_windows = token_auth._APP_WINDOW_EXCLUDED_PATHS
        _register_dist_static_routes(application, _make_dist(tmp_path))
        try:
            yield application
        finally:
            token_auth.register_app_window_paths(list(prior_app_windows))

    @pytest.mark.asyncio
    async def test_pna_preflight_grants_private_network(self, app) -> None:
        async with TestClient(TestServer(app)) as client:
            resp = await client.options(
                "/vendor/tailwindcss-browser.js",
                headers={"Access-Control-Request-Private-Network": "true"},
            )
            assert resp.status == 204
            assert resp.headers["Access-Control-Allow-Origin"] == "*"
            assert resp.headers["Access-Control-Allow-Private-Network"] == "true"
            assert "GET" in resp.headers["Access-Control-Allow-Methods"]

    @pytest.mark.asyncio
    async def test_plain_preflight_omits_pna_grant(self, app) -> None:
        """The PNA grant is echoed only when the request asks for it, per the
        spec's request/response pairing — a plain CORS preflight gets the CORS
        headers without the private-network grant."""
        async with TestClient(TestServer(app)) as client:
            resp = await client.options("/vendor/tailwindcss-browser.js")
            assert resp.status == 204
            assert resp.headers["Access-Control-Allow-Origin"] == "*"
            assert "Access-Control-Allow-Private-Network" not in resp.headers

    @pytest.mark.asyncio
    async def test_get_still_serves_the_file(self, app) -> None:
        """The OPTIONS route must not shadow the static GET route."""
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/vendor/tailwindcss-browser.js")
            assert resp.status == 200
            assert "runtime" in await resp.text()

    @pytest.mark.asyncio
    async def test_get_response_carries_allow_origin(self, app) -> None:
        """The real GET response — static route + header middleware — carries
        the CORS approval. This is the load-bearing half: the widget script
        tag's crossorigin="anonymous" makes the header a hard requirement
        (verified against real Chromium: without it the runtime load fails at
        the CORS layer and every Tailwind-classed widget renders unstyled)."""
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/vendor/tailwindcss-browser.js")
            assert resp.status == 200
            assert resp.headers["Access-Control-Allow-Origin"] == "*"

    @pytest.mark.asyncio
    async def test_get_outside_vendor_carries_no_allow_origin(self, app) -> None:
        """Same app, same middleware, non-vendor static path: no CORS header."""
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/assets/app.js")
            assert resp.status == 200
            assert "Access-Control-Allow-Origin" not in resp.headers


def test_middleware_uses_the_request_path() -> None:
    """Pin the production wiring the fixture middleware above replicates.

    The gateway's response-header middleware is a closure inside
    start_dashboard, so the integration fixture cannot import it; instead it
    replicates the call form. This guard fails if the production call drifts
    (e.g. to request.raw_path, or drops the path argument), which would
    silently decouple the fixture from what the browser actually receives.
    """
    from kiro_crew.dashboard import server as server_mod

    source = inspect.getsource(server_mod.start_dashboard)
    assert "_apply_security_headers(resp, request.app, request.path, request)" in source, (
        "start_dashboard's header middleware no longer calls "
        "_apply_security_headers(resp, request.app, request.path, request); "
        "update the fixture middleware in this file to match the new form."
    )
