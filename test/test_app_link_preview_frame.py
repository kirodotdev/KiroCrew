"""The link-preview frame: an app's hover card runs in an opaque-origin sandbox.

What keeps the dashboard safe from an app's card is asserted here, not assumed:

* the frame document is ``sandbox allow-scripts`` with no ``allow-same-origin``,
  on the response itself, so it is an opaque origin wherever it is opened;
* the mint answers a person's dashboard session only;
* the fetch route reads the capability header and nothing else, is bound to the
  app and the client, and reaches only the app's own routes, never a core route;
* CORS answers the opaque ``null`` origin only and never allows credentials.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import link_preview_frame as lp
from kiro_crew.dashboard.token_auth_paths import (
    _BYPASS_PREFIXES,
    SPA_FALLBACK_EXCLUDED_PREFIXES,
    is_csrf_exempt,
)

APP = "cards-app"


class _Registry:
    """Stands in for the hooks route registry: records what it dispatched."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def dispatch(self, request: web.Request) -> web.Response:
        self.calls.append(
            (request.match_info["app_name"], request.method, request.match_info["path"])
        )
        return web.json_response({"ok": True, "app": request.get("app")})


@asynccontextmanager
async def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    entry: tuple[str, bool, str] | None = ("dist/lp.mjs", False, APP),
    session: dict[str, Any] | None = None,
) -> AsyncIterator[tuple[TestClient, _Registry]]:
    registry = _Registry()
    monkeypatch.setattr(lp, "_provider_entry", lambda name: entry if name == APP else None)
    monkeypatch.setattr("kiro_crew.apps.hooks_integration.get_route_registry", lambda: registry)
    monkeypatch.setattr(
        "kiro_crew.dashboard.server._extra_frame_ancestors", lambda request, app: ()
    )
    caller = {"is_dashboard_user": True} if session is None else session

    @web.middleware
    async def auth(request: web.Request, handler: Any) -> web.StreamResponse:
        # The real middleware bypasses the frame and fetch prefixes; the mint is
        # under /api/ and sees whatever caller the test names.
        if request.path.startswith("/api/"):
            for k, v in caller.items():
                request[k] = v
        return await handler(request)

    app = web.Application(middlewares=[auth])
    lp.register_link_preview_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, registry
    finally:
        await client.close()


async def _mint(client: TestClient) -> dict[str, Any]:
    resp = await client.post(f"/api/apps/{APP}/link-preview")
    assert resp.status == 200, await resp.text()
    data: dict[str, Any] = await resp.json()
    return data


@pytest.mark.asyncio
async def test_the_frame_is_an_opaque_origin_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(monkeypatch) as (client, _):
        minted = await _mint(client)
        resp = await client.get(minted["frame"])
        assert resp.status == 200
        csp = resp.headers["Content-Security-Policy"]
        directives = [d.strip() for d in csp.split(";")]
        assert "sandbox allow-scripts" in directives, csp
        assert "allow-same-origin" not in csp, (
            "the frame document grants allow-same-origin: the app's card would then "
            "run on the dashboard's origin, with its DOM, cookies and token in reach"
        )
        for flag in ("allow-top-navigation", "allow-popups", "allow-forms"):
            assert flag not in csp
        assert "connect-src 'self'" in directives
        html = await resp.text()
        # The module comes from the app's own UI directory, named by the manifest.
        assert f'"entry": "/apps/{APP}/ui/dist/lp.mjs"' in html
        nonce = (
            next(d for d in directives if d.startswith("script-src"))
            .split("'nonce-")[1]
            .rstrip("'")
        )
        assert f'nonce="{nonce}"' in html
        assert resp.headers["Cache-Control"] == "no-store"


def test_the_bootstrap_keeps_a_sub_path_mount() -> None:
    """Behind a proxy at a sub-path the module and the fetch go under the mount.

    The gateway never sees the mount (the proxy strips it), so the frame derives it
    from its own URL and puts it in front of both paths, and the containment check
    compares against the prefixed fetch base.
    """
    boot = lp._BOOTSTRAP
    assert "location.pathname.indexOf('/link-preview-frame/')" in boot
    assert "CFG.entry = MOUNT + CFG.entry;" in boot
    assert "CFG.fetchBase = MOUNT + CFG.fetchBase;" in boot
    # Both uses come after the prefix is applied.
    assert boot.index("CFG.fetchBase = MOUNT") < boot.index("new URL(CFG.fetchBase")
    assert boot.index("CFG.entry = MOUNT") < boot.index("import(CFG.entry)")


def test_the_frame_embeds_the_installed_name_not_the_request_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider whose manifest names another app is not served."""
    from types import SimpleNamespace

    manifest = SimpleNamespace(
        name="other-app",
        contributes=SimpleNamespace(linkPreviews=[SimpleNamespace(entry="dist/lp.mjs")]),
        backend=SimpleNamespace(entryPoint=""),
    )
    monkeypatch.setattr(lp, "is_app_enabled", lambda name: True)
    monkeypatch.setattr(lp, "get_app_manifest", lambda name: manifest)
    assert lp._provider_entry(APP) is None
    manifest.name = APP
    assert lp._provider_entry(APP) == ("dist/lp.mjs", False, APP)


@pytest.mark.asyncio
async def test_the_frame_needs_its_own_token(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(monkeypatch) as (client, _):
        minted = await _mint(client)
        frame_id, token = minted["frame"].rsplit("/", 2)[1:]
        assert frame_id == minted["frameId"]
        assert (await client.get(f"{lp.FRAME_PREFIX}{APP}/{frame_id}/nonsense")).status == 404
        # A token minted for one app does not open another app's frame.
        assert (await client.get(f"{lp.FRAME_PREFIX}other-app/{frame_id}/{token}")).status == 404
        # Nor another frame's.
        other_id = (await _mint(client))["frameId"]
        assert (await client.get(f"{lp.FRAME_PREFIX}{APP}/{other_id}/{token}")).status == 404
        # The fetch capability is not a frame token (separate signers).
        cap_token = minted["capability"].split("~", 1)[1]
        assert (await client.get(f"{lp.FRAME_PREFIX}{APP}/{frame_id}/{cap_token}")).status == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session",
    [
        {"is_dashboard_user": True, "app": APP},
        {"is_dashboard_user": True, "internal_auth": True},
        {"is_dashboard_user": False},
        {},
    ],
)
async def test_only_a_dashboard_session_can_mint(
    monkeypatch: pytest.MonkeyPatch, session: dict[str, Any]
) -> None:
    async with _client(monkeypatch, session=session) as (client, _):
        resp = await client.post(f"/api/apps/{APP}/link-preview")
        assert resp.status == 403


@pytest.mark.asyncio
async def test_an_app_without_an_enabled_provider_gets_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _client(monkeypatch, entry=None) as (client, _):
        assert (await client.post(f"/api/apps/{APP}/link-preview")).status == 404


@pytest.mark.asyncio
async def test_fetch_reads_the_capability_and_reaches_only_the_app_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _client(monkeypatch) as (client, registry):
        cap = (await _mint(client))["capability"]
        url = f"{lp.FETCH_PREFIX}{APP}/status?x=1"
        # No capability: refused, and the opaque origin may not even read the refusal.
        resp = await client.get(url, headers={"Origin": "null"})
        assert resp.status == 401
        assert "Access-Control-Allow-Origin" not in resp.headers
        assert registry.calls == []
        # A capability for another frame id, or a forged one: refused the same way.
        frame_id, _, token = cap.partition("~")
        for bad in (f"{'x' * 16}~{token}", token, f"{frame_id}~{token}x", "~"):
            resp = await client.get(url, headers={lp.CAPABILITY_HEADER: bad, "Origin": "null"})
            assert resp.status == 401, bad
            assert "Access-Control-Allow-Origin" not in resp.headers
        # A session cookie is not a credential here.
        resp = await client.get(url, headers={"Cookie": "mc_token_1=abc"})
        assert resp.status == 401
        # With it: the app's own registered route, as the app.
        resp = await client.get(url, headers={lp.CAPABILITY_HEADER: cap, "Origin": "null"})
        assert resp.status == 200
        assert (await resp.json()) == {"ok": True, "app": APP}
        assert registry.calls == [(APP, "GET", "status")]
        assert resp.headers["Access-Control-Allow-Origin"] == "null"
        assert "Access-Control-Allow-Credentials" not in resp.headers
        # Another app's routes are out of reach with this app's capability.
        resp = await client.get(
            f"{lp.FETCH_PREFIX}other-app/status", headers={lp.CAPABILITY_HEADER: cap}
        )
        assert resp.status == 401
        assert len(registry.calls) == 1


@pytest.mark.asyncio
async def test_fetch_never_reaches_a_core_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """``config`` is a core route under /api/apps/<name>/: the registry, not the router, answers."""
    async with _client(monkeypatch) as (client, registry):
        cap = (await _mint(client))["capability"]
        resp = await client.post(
            f"{lp.FETCH_PREFIX}{APP}/config", headers={lp.CAPABILITY_HEADER: cap}
        )
        assert resp.status == 200
        assert registry.calls == [
            (APP, "POST", "config")
        ], "the fetch reached something other than the app's own route table"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["..%2Fx", "a/..%2F..%2Fb", "%5Cx"])
async def test_fetch_refuses_paths_that_leave_the_app(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    async with _client(monkeypatch) as (client, registry):
        cap = (await _mint(client))["capability"]
        resp = await client.get(
            f"{lp.FETCH_PREFIX}{APP}/{path}", headers={lp.CAPABILITY_HEADER: cap}
        )
        assert resp.status in (400, 404)
        assert registry.calls == []


@pytest.mark.asyncio
async def test_a_preflight_grants_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(monkeypatch) as (client, registry):
        resp = await client.options(
            f"{lp.FETCH_PREFIX}{APP}/status",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type, x-kirocrew-capability",
            },
        )
        assert resp.status == 204
        assert resp.headers["Access-Control-Allow-Origin"] == "null"
        assert "x-kirocrew-capability" in resp.headers["Access-Control-Allow-Headers"]
        assert registry.calls == []


@pytest.mark.asyncio
async def test_a_process_backend_goes_through_the_signed_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, str]] = []

    async def proxy(request: web.Request) -> web.Response:
        seen.append((request.match_info["name"], request.match_info["path"], request.get("app")))
        return web.json_response({"proxied": True})

    monkeypatch.setattr("kiro_crew.apps.routes.handle_app_api_proxy", proxy)
    async with _client(monkeypatch, entry=("dist/lp.mjs", True, APP)) as (client, registry):
        cap = (await _mint(client))["capability"]
        resp = await client.get(
            f"{lp.FETCH_PREFIX}{APP}/items", headers={lp.CAPABILITY_HEADER: cap}
        )
        assert resp.status == 200
        assert seen == [(APP, "items", APP)]
        assert registry.calls == []


def test_the_capability_is_bound_to_the_app_the_client_and_the_frame() -> None:
    fid = "f" * 16
    cap = f"{fid}~{lp._signer('fetch').mint(APP, '10.0.0.1', fid)}"
    assert lp._capability_ok(cap, APP, "10.0.0.1")
    assert not lp._capability_ok(cap, APP, "10.0.0.2")
    assert not lp._capability_ok(cap, "other-app", "10.0.0.1")
    assert not lp._capability_ok(cap.replace(fid, "g" * 16, 1), APP, "10.0.0.1")
    assert not lp._signer("frame").verify(cap.split("~", 1)[1], APP, "10.0.0.1", fid)
    assert lp._CAPABILITY_TTL_SECS <= 900, "the capability is meant to be short-lived"


@pytest.mark.asyncio
async def test_a_renewal_keeps_the_frame_id(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(monkeypatch) as (client, _):
        first = await _mint(client)
        resp = await client.post(f"/api/apps/{APP}/link-preview?frame={first['frameId']}")
        renewed = await resp.json()
        assert renewed["frameId"] == first["frameId"]
        assert renewed["capability"].split("~", 1)[0] == first["frameId"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/apps", "/api/apps/cards-app/config", "/api/status", "/"])
async def test_the_capability_is_no_credential_anywhere_else(path: str) -> None:
    """The real auth middleware: a frame request to any other route is refused.

    The frame sends ``Origin: null`` and no cookie (an opaque origin has none to
    send); with the capability header, without it, or with a session-looking cookie
    the middleware still finds no dashboard credential.
    """
    from kiro_crew.dashboard import token_auth as auth

    async def ok(_request: web.Request) -> web.Response:
        return web.json_response({"reached": True})

    app = web.Application(middlewares=[auth.token_auth_middleware(port=7999)])
    app.router.add_route("*", "/{tail:.*}", ok)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        fid = "f" * 16
        cap = f"{fid}~{lp._signer('fetch').mint(APP, '127.0.0.1', fid)}"
        for headers in (
            {"Origin": "null", lp.CAPABILITY_HEADER: cap},
            {"Origin": "null"},
            {"Origin": "null", lp.CAPABILITY_HEADER: cap, "Cookie": "mc_token_7999=forged"},
        ):
            resp = await client.get(path, headers=headers)
            assert resp.status in (401, 403), (path, headers, resp.status)
            body = await resp.text()
            assert "reached" not in body
            assert "Access-Control-Allow-Origin" not in resp.headers
    finally:
        await client.close()


def test_the_routes_are_bypassed_excluded_from_the_shell_and_csrf_exempt() -> None:
    for prefix in (lp.FRAME_PREFIX, lp.FETCH_PREFIX):
        assert prefix in _BYPASS_PREFIXES
        assert prefix in SPA_FALLBACK_EXCLUDED_PREFIXES
    assert is_csrf_exempt(f"{lp.FETCH_PREFIX}{APP}/kv", "POST")
    # The frame document and every other route keep the Origin check.
    assert not is_csrf_exempt(f"{lp.FRAME_PREFIX}{APP}/t", "POST")
    assert not is_csrf_exempt(f"/api/apps/{APP}/link-preview", "POST")
    assert not is_csrf_exempt(f"/apps/{APP}/api/kv", "POST")
