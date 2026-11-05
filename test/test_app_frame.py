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

from kiro_crew.apps import app_frame as lp
from kiro_crew.dashboard.token_auth_paths import (
    _BYPASS_PREFIXES,
    SPA_FALLBACK_EXCLUDED_PREFIXES,
    is_csrf_exempt,
)

APP = "cards-app"
PID = "cards"
MINT = f"/api/apps/{APP}/frames/linkPreviews/{PID}"


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
    monkeypatch.setattr(
        lp,
        "_provider_entry",
        lambda name, pid=None: entry if name == APP and pid in (None, PID) else None,
    )
    monkeypatch.setattr("kiro_crew.apps.hooks_integration.get_route_registry", lambda: registry)
    # The mint request's validated embed-parent claim; a test sets it on the caller.
    monkeypatch.setattr(
        "kiro_crew.dashboard.server._embed_parent_port",
        lambda request, app: request.get("embed_parent_port_for_test"),
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
    lp.register_app_frame_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, registry
    finally:
        await client.close()


async def _mint(client: TestClient) -> dict[str, Any]:
    resp = await client.post(MINT)
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
        # Everything not named is refused, and each kind the card may need is named.
        for d in (
            "default-src 'none'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' https: data: blob:",
            "font-src 'self' data:",
            "media-src 'none'",
            "worker-src 'none'",
            "frame-src 'none'",
            "child-src 'none'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'none'",
        ):
            assert d in directives, d
        assert any(d.startswith("frame-ancestors ") for d in directives)
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        html = await resp.text()
        assert '<meta name="referrer" content="no-referrer">' in html
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
    assert "location.pathname.indexOf('/app-frame/')" in boot
    assert "CFG.entry = MOUNT + CFG.entry;" in boot
    assert "CFG.fetchBase = MOUNT + CFG.fetchBase;" in boot
    # Both uses come after the prefix is applied.
    assert boot.index("CFG.fetchBase = MOUNT") < boot.index("new URL(CFG.fetchBase")
    assert boot.index("CFG.entry = MOUNT") < boot.index("import(CFG.entry)")


#: Runs the real bootstrap under node with a stub window and a provider that
#: answers every show late. Prints what the frame posted to the dashboard.
_BOOTSTRAP_HARNESS = r"""
const posted = [];
const listeners = {};
const parent = { postMessage: (m) => posted.push(m) };
globalThis.window = { parent, addEventListener: (t, f) => { (listeners[t] ||= []).push(f); } };
globalThis.location = { pathname: '/app-frame/a/linkPreviews/c/f/t', href: 'http://gw/app-frame/a/linkPreviews/c/f/t' };
const lateProvider = 'export function activate(ctx) { return { show(link) { globalThis.shows.push(link); } }; }';
globalThis.shows = [];
globalThis.document = {
  activeElement: null,
  body: {},
  getElementById: () => ({ textContent: JSON.stringify({
    app: 'a', entry: 'data:text/javascript,' + encodeURIComponent(lateProvider),
    fetchBase: '/app-frame-fetch/a/', theme: 'light',
  }) }),
};
"""
_BOOTSTRAP_DRIVER = r"""
const deliver = (data) => (listeners.message || []).forEach((f) => f({ source: parent, data: { 'kirocrew-app-frame': 1, ...data } }));
const done = (async () => {
  deliver({ type: 'init', capability: 'c' });
  for (let i = 0; i < 50 && !posted.some((m) => m.type === 'ready'); i++) await new Promise((r) => setTimeout(r, 10));
  const rect = { left: 0, top: 0, width: 1, height: 1 };
  deliver({ type: 'show', invocation: 1, url: 'https://a.example.com/1', rect, theme: 'light' });
  deliver({ type: 'show', invocation: 2, url: 'https://a.example.com/2', rect, theme: 'light' });
  deliver({ type: 'show', url: 'https://a.example.com/3', rect, theme: 'light' });
  // The first hover's fetch finishes after the second hover began.
  shows[0].card.open();
  shows[0].card.resize({ width: 10, height: 10 });
  shows[1].card.open();
  console.log(JSON.stringify({
    shows: shows.length,
    frozen: Object.isFrozen(shows[0].card),
    card: posted.filter((m) => ['open', 'resize'].includes(m.type)).map((m) => [m.type, m.invocation]),
  }));
})();
await done;
"""


def test_card_controls_keep_the_invocation_of_their_own_show() -> None:
    """A slow completion for an earlier hover names that hover, not the newest one.

    The dashboard drops a card message whose invocation is not the frame's latest
    (``linkPreviewFrames.ts``). That only works if the frame stamps each control with
    the show it was handed to, so the controls are per show, not on ``ctx``.
    """
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            _BOOTSTRAP_HARNESS + lp._BOOTSTRAP + _BOOTSTRAP_DRIVER,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    # A show without an invocation is not delivered: it could not be answered.
    assert result["shows"] == 2
    assert result["frozen"] is True
    assert result["card"] == [["open", 1], ["resize", 1], ["open", 2]]
    # The controls exist only per show.
    for name in ("open", "close", "resize", "pin", "dragBy", "dock"):
        assert f"\n  {name}: " not in lp._BOOTSTRAP.split("const ctx = {", 1)[1].split("};", 1)[0]


def test_the_frame_embeds_the_installed_name_not_the_request_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider whose manifest names another app is not served."""
    from types import SimpleNamespace

    manifest = SimpleNamespace(
        name="other-app",
        contributes=SimpleNamespace(linkPreviews=[SimpleNamespace(entry="dist/lp.mjs")]),
        backend=SimpleNamespace(entryPoint=""),
        mcpServers={},
    )
    monkeypatch.setattr(lp, "is_app_enabled", lambda name: True)
    monkeypatch.setattr(lp, "get_app_manifest", lambda name: manifest)
    monkeypatch.setattr(
        lp,
        "link_preview_grant_row",
        lambda name, m: {"linkPreviewGrant": {"hosts": ["a.example.com"]}},
    )
    assert lp._provider_entry(APP) is None
    manifest.name = APP
    assert lp._provider_entry(APP) == ("dist/lp.mjs", False, APP)


def test_a_self_managed_http_backend_goes_through_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An app whose backend is only an ``mcpServers`` loopback URL is proxied too.

    The proxy resolves that backend (``resolve_mcp_backend_url``) and the app secret
    is issued for it, so ``ctx.fetch`` must take the proxy, not the route registry.
    """
    from types import SimpleNamespace

    manifest = SimpleNamespace(
        name=APP,
        contributes=SimpleNamespace(linkPreviews=[SimpleNamespace(entry="dist/lp.mjs")]),
        backend=SimpleNamespace(entryPoint=""),
        mcpServers={"srv": {"url": "http://127.0.0.1:7778/mcp"}},
    )
    monkeypatch.setattr(lp, "is_app_enabled", lambda name: True)
    monkeypatch.setattr(lp, "get_app_manifest", lambda name: manifest)
    monkeypatch.setattr(
        lp,
        "link_preview_grant_row",
        lambda name, m: {"linkPreviewGrant": {"hosts": ["a.example.com"]}},
    )
    assert lp._provider_entry(APP) == ("dist/lp.mjs", True, APP)
    manifest.mcpServers = {}
    assert lp._provider_entry(APP) == ("dist/lp.mjs", False, APP)


@pytest.mark.asyncio
async def test_an_embedded_dashboard_frame_admits_its_parent_from_the_signed_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In an Instances embed the frame's grandparent is another loopback dashboard.

    The frame request is credentialless and on the auth bypass, so it carries no
    cookie a parent port could be read from. The mint, on the person's session, reads
    the validated claim and binds the port into the frame token; the frame trusts
    only that signed value, and a port edited into the URL voids the token.
    """
    session = {"is_dashboard_user": True, "embed_parent_port_for_test": 7001}
    async with _client(monkeypatch, session=session) as (client, _):
        minted = await _mint(client)
        frame_id, token = minted["frame"].rsplit("/", 2)[1:]
        assert token.startswith("7001-")
        resp = await client.get(minted["frame"])  # no cookie, no dashboard token
        assert resp.status == 200
        ancestors = next(
            d
            for d in resp.headers["Content-Security-Policy"].split("; ")
            if d.startswith("frame-ancestors ")
        )
        assert "'self'" in ancestors
        assert "http://127.0.0.1:7001" in ancestors
        forged = (
            f"{lp.FRAME_PREFIX}{APP}/linkPreviews/{PID}/{frame_id}/7002-{token.split('-', 1)[1]}"
        )
        assert (await client.get(forged)).status == 404
    async with _client(monkeypatch) as (client, _):
        minted = await _mint(client)
        assert minted["frame"].rsplit("/", 1)[1].startswith("0-")
        resp = await client.get(minted["frame"])
        ancestors = next(
            d
            for d in resp.headers["Content-Security-Policy"].split("; ")
            if d.startswith("frame-ancestors ")
        )
        assert ancestors == "frame-ancestors 'self'"


@pytest.mark.asyncio
async def test_the_frame_needs_its_own_token(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(monkeypatch) as (client, _):
        minted = await _mint(client)
        frame_id, token = minted["frame"].rsplit("/", 2)[1:]
        assert frame_id == minted["frameId"]
        assert (
            await client.get(f"{lp.FRAME_PREFIX}{APP}/linkPreviews/{PID}/{frame_id}/nonsense")
        ).status == 404
        # A token minted for one app does not open another app's frame.
        assert (
            await client.get(f"{lp.FRAME_PREFIX}other-app/linkPreviews/{PID}/{frame_id}/{token}")
        ).status == 404
        # Nor another frame's.
        other_id = (await _mint(client))["frameId"]
        assert (
            await client.get(f"{lp.FRAME_PREFIX}{APP}/linkPreviews/{PID}/{other_id}/{token}")
        ).status == 404
        # The fetch capability is not a frame token (separate signers).
        cap_token = minted["capability"].split("~")[3]
        assert (
            await client.get(f"{lp.FRAME_PREFIX}{APP}/linkPreviews/{PID}/{frame_id}/{cap_token}")
        ).status == 404


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
        resp = await client.post(MINT)
        assert resp.status == 403


@pytest.mark.asyncio
async def test_an_app_without_an_enabled_provider_gets_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _client(monkeypatch, entry=None) as (client, _):
        assert (await client.post(MINT)).status == 404


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
    cap = f"{fid}~linkPreviews~{PID}~{lp._signer('fetch').mint(APP, '10.0.0.1', fid, 'linkPreviews', PID)}"
    assert lp._capability_ok(cap, APP, "10.0.0.1")
    assert not lp._capability_ok(cap, APP, "10.0.0.2")
    assert not lp._capability_ok(cap, "other-app", "10.0.0.1")
    assert not lp._capability_ok(cap.replace(fid, "g" * 16, 1), APP, "10.0.0.1")
    assert not lp._signer("frame").verify(
        cap.split("~")[3], APP, "10.0.0.1", fid, "0", "linkPreviews", PID
    )
    assert lp._CAPABILITY_TTL_SECS <= 900, "the capability is meant to be short-lived"


@pytest.mark.asyncio
async def test_a_renewal_keeps_the_frame_id(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(monkeypatch) as (client, _):
        first = await _mint(client)
        resp = await client.post(f"{MINT}?frame={first['frameId']}")
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
        cap = f"{fid}~linkPreviews~{PID}~{lp._signer('fetch').mint(APP, '127.0.0.1', fid, 'linkPreviews', PID)}"
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
    assert not is_csrf_exempt(f"{lp.FRAME_PREFIX}{APP}/linkPreviews/{PID}/f/t", "POST")
    assert not is_csrf_exempt(MINT, "POST")
    assert not is_csrf_exempt(f"/apps/{APP}/api/kv", "POST")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["TRACE", "PROPFIND"])
async def test_fetch_refuses_other_methods(monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    async with _client(monkeypatch) as (client, registry):
        cap = (await _mint(client))["capability"]
        resp = await client.request(
            method, f"{lp.FETCH_PREFIX}{APP}/status", headers={lp.CAPABILITY_HEADER: cap}
        )
        assert resp.status == 405
        assert registry.calls == []


@pytest.mark.asyncio
async def test_fetch_reaches_only_a_registered_method_and_path() -> None:
    """The real route table: an app route answers its own method and path, nothing else."""
    from kiro_crew.apps.route_registry import RouteRegistry, _RegisteredRoute

    async def status(request: web.Request, ctx: Any) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application()
    registry = RouteRegistry(app)
    registry._routes[APP] = [
        _RegisteredRoute(method="GET", path="/status", handler=status, has_params=False)
    ]
    registry._contexts[APP] = object()  # type: ignore[assignment]
    lp.register_app_frame_routes(app)
    fid = "f" * 16
    import kiro_crew.apps.hooks_integration as hooks

    old = hooks.get_route_registry
    old_entry = lp._provider_entry
    hooks.get_route_registry = lambda: registry  # type: ignore[assignment]
    lp._provider_entry = lambda name, pid=None: ("dist/lp.mjs", False, APP)  # type: ignore[assignment]
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        cap = f"{fid}~linkPreviews~{PID}~{lp._signer('fetch').mint(APP, '127.0.0.1', fid, 'linkPreviews', PID)}"
        h = {lp.CAPABILITY_HEADER: cap}
        assert (await client.get(f"{lp.FETCH_PREFIX}{APP}/status", headers=h)).status == 200
        assert (await client.post(f"{lp.FETCH_PREFIX}{APP}/status", headers=h)).status == 404
        assert (await client.get(f"{lp.FETCH_PREFIX}{APP}/other", headers=h)).status == 404
    finally:
        await client.close()
        hooks.get_route_registry = old  # type: ignore[assignment]
        lp._provider_entry = old_entry  # type: ignore[assignment]
