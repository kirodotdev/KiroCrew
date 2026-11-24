"""Headless mode: keep registered services, remove the dashboard, bind loopback.

The page routes are real aiohttp routes behind the middleware, on a loopback server
with a port the kernel picks, so a refusal here is one a client would get.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import test_dashboard_server_startup_coverage as startup
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from aiohttp.web_urldispatcher import UrlMappingMatchInfo

from kiro_crew.dashboard.headless import (
    DASHBOARD_RESOURCES,
    api_only_middleware,
    bind_address,
    print_client_access,
)

PAGES = ["/", "/chat", "/manifest.json", "/settings/x", "/embed/sessions", "/favicon.ico"]
SERVICES = ["/api/health", "/v1/models", "/apps/x/ui/", "/future/service"]
LOCAL_AUTH = [
    ("GET", "/.well-known/oauth-authorization-server"),
    ("GET", "/oauth/authorize"),
    ("POST", "/oauth/token"),
    ("POST", "/oauth/revoke"),
]


@pytest_asyncio.fixture
async def gateway():
    served: list[str] = []

    async def _serve(request: web.Request) -> web.Response:
        served.append(request.path)
        return web.Response(text="served")

    app = web.Application(middlewares=[api_only_middleware])
    for path in ("/", "/favicon.ico", *SERVICES):
        app.router.add_get(path, _serve)
    app.router.add_get("/assets/{tail:.+}", _serve)
    app.router.add_get("/{name:manifest\\.json}", _serve)
    for method, path in LOCAL_AUTH:
        app.router.add_route(method, path, _serve)
    client = TestClient(TestServer(app, host="127.0.0.1", port=0))
    await client.start_server()
    client.served = served  # type: ignore[attr-defined]
    yield client
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PAGES)
async def test_a_headless_gateway_serves_no_dashboard_route(gateway, path) -> None:
    resp = await gateway.get(path)
    assert resp.status == 404
    assert (await resp.json())["code"] == "headless"
    assert gateway.served == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", SERVICES)
async def test_a_headless_gateway_still_serves_registered_services(gateway, path) -> None:
    resp = await gateway.get(path)
    assert (resp.status, await resp.text()) == (200, "served")


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), LOCAL_AUTH)
async def test_local_authorization_routes_reach_their_handlers(gateway, method, path) -> None:
    resp = await gateway.request(method, path)
    assert (resp.status, await resp.text()) == (200, "served")
    assert gateway.served == [path]


@pytest.mark.asyncio
async def test_a_path_that_only_looks_like_the_api_is_refused(gateway) -> None:
    for path in ("/api", "/v1", "/apix/health", "/v10/models"):
        assert (await gateway.get(path)).status == 404, path
    assert gateway.served == []


def test_headless_binds_loopback_whatever_an_ordinary_launch_would_bind() -> None:
    # ``0.0.0.0`` is what the official image's ``KIROCREW_BIND`` resolves to.
    assert bind_address(True, "0.0.0.0") == "127.0.0.1"
    assert bind_address(False, "0.0.0.0") == "0.0.0.0"


def _keep_shared_crew_globals(monkeypatch) -> None:
    """Put back the process-wide Shared Crew state a boot with the preview on writes."""
    from kiro_crew.dashboard import tailnet_direct_listener, tailnet_live_allowlist
    from kiro_crew.shared_crew import access, access_set, owner

    for module, names in (
        (access, ("_tailnet_answer", "_forwarded_attribution", "_forwarded_inputs")),
        (access, ("_identity_config_unreadable", "_direct_tailnet")),
        (access_set, ("_STATE", "_STATE_HOME", "_SEED_LIMIT")),
        (tailnet_live_allowlist, ("_SNAPSHOT",)),
        (owner, ("_tailnet_host_name", "_tailnet_display_name")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))
    # Containers are mutated in place, so each is swapped for a copy.
    for module, name in (
        (access_set, "_OWNER"),
        (access_set, "_REMOVED"),
        (tailnet_live_allowlist, "_GRANTED"),
        (tailnet_live_allowlist, "_REVOKED"),
        (tailnet_live_allowlist, "_LOGINS_BY_USER"),
        (tailnet_direct_listener, "_PENDING_INSTALL"),
    ):
        monkeypatch.setattr(module, name, type(getattr(module, name))(getattr(module, name)))


def _preview_with_spied_tailnet_listener(monkeypatch) -> dict:
    """Turn the Shared Crew preview on and record every direct tailnet listener call."""
    from kiro_crew.dashboard import tailnet_direct, tailnet_direct_listener
    from kiro_crew.shared_crew import preview

    _keep_shared_crew_globals(monkeypatch)
    monkeypatch.setattr(preview, "preview_enabled", lambda: True)
    # The boot's background warm resolves this machine's own names over DNS.
    monkeypatch.setattr(startup.srv, "warm_own_host_names", lambda: None)
    spies = {
        "prepare_at_start": MagicMock(return_value=tailnet_direct.GateRef()),
        "start_after_bind": AsyncMock(return_value=[]),
    }
    for name, spy in spies.items():
        monkeypatch.setattr(tailnet_direct_listener, name, spy)
    return spies


@pytest.mark.asyncio
async def test_a_headless_boot_listens_on_loopback_alone(tmp_path, monkeypatch) -> None:
    """The real boot, real sockets: the container override loses."""
    monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
    tailnet = _preview_with_spied_tailnet_listener(monkeypatch)
    boot = startup._dashboard(tmp_path, monkeypatch, real_listener=True, headless=True)
    async with boot as (runner, _state, _spies):
        hosts = {address[0] for address in runner.addresses}
        assert "127.0.0.1" in hosts
        assert hosts <= {"127.0.0.1", "::1"}, hosts
        tailnet["prepare_at_start"].assert_not_called()
        tailnet["start_after_bind"].assert_not_called()


@pytest.mark.asyncio
async def test_an_ordinary_boot_keeps_the_override_and_the_tailnet_listener(
    tmp_path, monkeypatch
) -> None:
    """The same boot without ``headless``, so the test above can fail."""
    monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
    tailnet = _preview_with_spied_tailnet_listener(monkeypatch)
    async with startup._dashboard(tmp_path, monkeypatch, local_only=False):
        assert startup.srv._reserve_dashboard_port.await_args.args[0] == "0.0.0.0"
        tailnet["prepare_at_start"].assert_called_once()
        tailnet["start_after_bind"].assert_awaited_once()


OWNER_LOGIN = "owner@example.com"
TAILNET_HOST = "crew.tail1.ts.net"
#: In the derived access set (the seed it was built from) and in ``allowed_logins``.
SEEDED = "alice@example.com"
#: Typed into ``allowed_logins`` after the set was seeded, so only the raw list has it.
ADDED_BY_HAND = "mallory@example.com"


def _shared_crew_tailnet(monkeypatch, tmp_path, *, preview: bool, tailscale: bool) -> dict:
    """``tailscale serve`` with Shared Crew, the daemon faked, and a set seeded with SEEDED.

    Returns what the boot recorded: every ``access.note_tailnet`` call, and the
    installed access set at the moment the dashboard socket starts accepting.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard import tailnet, tailnet_live_allowlist, tailnet_localapi
    from kiro_crew.shared_crew import access, access_set
    from kiro_crew.shared_crew import preview as preview_gate
    from kiro_crew.shared_crew import tailscale_invite_store as store

    _keep_shared_crew_globals(monkeypatch)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    tailnet_live_allowlist.reset_for_tests()
    access_set.reset_for_tests()
    monkeypatch.setattr(preview_gate, "preview_enabled", lambda: preview)
    monkeypatch.setattr(startup.srv, "warm_own_host_names", lambda: None)
    cfg = KiroCrewConfig()
    cfg.dashboard.tailscale.enabled = tailscale
    cfg.dashboard.tailscale.allowed_logins = [SEEDED, ADDED_BY_HAND]
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(
        tailnet, "resolve_tailnet_host", AsyncMock(return_value=TAILNET_HOST if tailscale else "")
    )
    status = {
        "BackendState": "Running",
        "Self": {"TailscaleIPs": ["100.64.0.1"], "UserID": 11, "DNSName": TAILNET_HOST + "."},
        "User": {"11": {"LoginName": OWNER_LOGIN}},
    }
    monkeypatch.setattr(
        tailnet_localapi,
        "status",
        lambda *_a, **_k: tailnet_localapi.Answer(status, tailnet_localapi.METHOD_LOCALAPI_UNIX),
    )
    # A set an earlier start seeded from SEEDED alone.
    store.ensure_root()
    (store.root() / access_set.ACCESS_FILE).write_text(
        '{"schema": 1, "seeded": true, "first_share_ack": null,'
        f' "seed": {{"logins": ["{SEEDED}"], "tags": []}}}}',
        encoding="utf-8",
    )
    seen: dict = {"note_tailnet": [], "at_bind": []}
    real_note = access.note_tailnet

    def _note(**kwargs) -> None:
        seen["note_tailnet"].append(kwargs)
        real_note(**kwargs)

    monkeypatch.setattr(access, "note_tailnet", _note)

    async def _start(_site) -> None:
        seen["at_bind"].append(tailnet_live_allowlist.snapshot())

    # The harness installs this double as ``web.SockSite``; its start() is the bind.
    monkeypatch.setattr(startup._FakeSockSite, "start", _start)
    return seen


def _admits(login: str) -> bool:
    """What the ``tailscale serve`` path answers for a forwarded *login*."""
    from kiro_crew.dashboard import tailnet

    return tailnet.login_allowed(login, (SEEDED, ADDED_BY_HAND))


@pytest.mark.asyncio
@pytest.mark.parametrize("headless", [True, False])
async def test_every_boot_installs_the_shared_crew_access_set(
    tmp_path, monkeypatch, headless
) -> None:
    """Headless or not: placeholder before the bind, the derived set after it."""
    from kiro_crew.dashboard import tailnet_live_allowlist
    from kiro_crew.shared_crew import access

    seen = _shared_crew_tailnet(monkeypatch, tmp_path, preview=True, tailscale=True)
    async with startup._dashboard(tmp_path, monkeypatch, headless=headless) as (runner, _s, _p):
        # The primary socket and the other loopback family, both before the set is read.
        assert seen["at_bind"], "the boot started no listener"
        for at_bind in seen["at_bind"]:
            assert at_bind is not None and at_bind.unreadable, "no placeholder at the bind"
        installed = tailnet_live_allowlist.snapshot()
        assert installed is not None and not installed.unreadable
        assert installed.owner_login == OWNER_LOGIN
        [note] = seen["note_tailnet"]
        assert (note["enabled"], note["host"], note["loopback_only"]) == (True, TAILNET_HOST, True)
        assert _admits(SEEDED) and _admits(OWNER_LOGIN)
        assert not _admits(ADDED_BY_HAND)
        # No direct tailnet site either way: the flag is off by default, and a
        # headless boot never resolves one (the real-socket test above).
        assert not access.tailnet_direct().active


@pytest.mark.asyncio
@pytest.mark.parametrize("headless", [True, False])
@pytest.mark.parametrize(("preview", "tailscale"), [(False, True), (True, False)])
async def test_without_a_tailnet_path_every_boot_keeps_the_raw_list(
    tmp_path, monkeypatch, headless, preview, tailscale
) -> None:
    """Preview off or Tailscale off: no set is installed and ``allowed_logins`` decides."""
    from kiro_crew.dashboard import tailnet_live_allowlist

    seen = _shared_crew_tailnet(monkeypatch, tmp_path, preview=preview, tailscale=tailscale)
    async with startup._dashboard(tmp_path, monkeypatch, headless=headless):
        assert seen["at_bind"] and set(seen["at_bind"]) == {None}
        assert tailnet_live_allowlist.snapshot() is None
        assert [n["enabled"] for n in seen["note_tailnet"]] == ([False] if preview else [])
        assert _admits(SEEDED) and _admits(ADDED_BY_HAND)


@pytest_asyncio.fixture
async def frontend_dist(tmp_path, monkeypatch):
    """Build files only, independent of the checkout's frontend installation."""
    from kiro_crew.dashboard import dist_file_response, token_auth

    dist = tmp_path / "dist"
    files = {
        "src/apps/demo/panel.html": b'<script type="module" src="/assets/panel.js"></script>',
        "assets/panel.js": b"export const panel = true;",
        "fonts/demo.woff2": b"fixture font",
        "vendor/demo.mjs": b"export const vendor = true;",
    }
    for name, content in files.items():
        path = dist / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    monkeypatch.setattr(startup.srv, "_DIST_DIR", dist)
    monkeypatch.setattr(startup.srv, "warm_own_host_names", lambda: None)
    monkeypatch.setattr(token_auth, "_APP_WINDOW_EXCLUDED_PATHS", frozenset())
    earlier = set(dist_file_response._PENDING_CLOSES)
    yield files
    # File responses close on the static pool after sending their last byte. Wait only
    # for this test's: a close left by an earlier test whose loop has ended never finishes.
    ours = dist_file_response._PENDING_CLOSES - earlier
    await asyncio.wait_for(asyncio.gather(*ours), timeout=30)


@pytest.mark.asyncio
@pytest.mark.parametrize("headless", [False, True])
async def test_app_window_and_shared_build_files_are_served(
    tmp_path, monkeypatch, frontend_dist, headless
) -> None:
    from kiro_crew.dashboard.token_auth import generate_token

    async with startup._dashboard(tmp_path, monkeypatch, headless=headless) as (runner, _, _):
        server = startup._RunningAppServer(runner, host="127.0.0.1", port=0)
        async with TestClient(server) as client:
            # Shared build files load without a credential, even in opaque-origin frames.
            for name in ("assets/panel.js", "fonts/demo.woff2", "vendor/demo.mjs"):
                response = await client.get(f"/{name}", allow_redirects=False)
                assert (response.status, await response.read()) == (200, frontend_dist[name])
            # The window itself keeps its ordinary session authentication.
            token = generate_token("window-test")
            response = await client.get(
                "/app-windows/demo/panel.html", params={"token": token}, allow_redirects=False
            )
            assert (response.status, await response.read()) == (
                200,
                frontend_dist["src/apps/demo/panel.html"],
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1", "not-an-app.localhost"])
async def test_missing_shared_build_files_keep_the_ordinary_response(
    tmp_path, monkeypatch, frontend_dist, host
) -> None:
    replies = []
    for headless in (False, True):
        with monkeypatch.context() as patches:
            async with startup._dashboard(tmp_path, patches, headless=headless) as (runner, _, _):
                server = startup._RunningAppServer(runner, host="127.0.0.1", port=0)
                async with TestClient(server) as client:
                    responses = []
                    for prefix in ("assets", "fonts", "vendor"):
                        response = await client.get(
                            f"/{prefix}/missing.js", headers={"Host": host}, allow_redirects=False
                        )
                        body = await response.text()
                        assert "headless" not in body
                        responses.append((response.status, body))
                    replies.append(responses)
    assert replies[1] == replies[0]
    assert {status for status, _ in replies[0]} == {404 if host == "127.0.0.1" else 403}


@pytest.mark.asyncio
async def test_headless_boot_refuses_dashboard_entries(
    tmp_path, monkeypatch, frontend_dist
) -> None:
    async with startup._dashboard(tmp_path, monkeypatch, headless=True) as (runner, _, _):
        server = startup._RunningAppServer(runner, host="127.0.0.1", port=0)
        async with TestClient(server) as client:
            for path in ("/", "/chat", "/manifest.json", "/settings/x"):
                response = await client.get(path, allow_redirects=False)
                assert response.status == 404, path
                assert (await response.json())["code"] == "headless", path
                assert "Location" not in response.headers


@pytest.mark.asyncio
async def test_headless_keeps_every_other_live_resource(
    tmp_path, monkeypatch, frontend_dist
) -> None:
    """The boot's router, not a second inventory, supplies every route to check."""
    from kiro_crew.apps import app_web_origin, backend, manager

    manifest = SimpleNamespace(backend=SimpleNamespace(webOrigin=True))
    monkeypatch.setattr(
        manager, "get_app_manifest", lambda name: manifest if name == "demo" else None
    )
    monkeypatch.setattr(manager, "is_app_enabled", lambda name: name == "demo")
    monkeypatch.setattr(backend, "get_app_backend_port", lambda name: 1 if name == "demo" else None)

    async def app_dispatch(request, target, headers, ancestors):
        assert target.host == "127.0.0.1"
        assert target.port == 1  # The relay is stubbed; no backend socket is opened.
        return web.json_response({"app_path": request.path})

    monkeypatch.setattr(app_web_origin, "_relay_http", app_dispatch)
    monkeypatch.setattr(startup.srv, "warm_own_host_names", lambda: None)
    boot = startup._dashboard(tmp_path, monkeypatch, real_listener=True, headless=True)
    async with boot as (runner, _state, _spies):
        resources = list(runner.app.router.resources())
        assert DASHBOARD_RESOURCES <= {resource.canonical for resource in resources}
        port = next(address[1] for address in runner.addresses if address[0] == "127.0.0.1")
        async with ClientSession() as client:
            for resource in resources:
                canonical = resource.canonical
                params = {name: "sample" for name in re.findall(r"\{(\w+)\}", canonical)}
                if canonical == "/{name}":
                    params["name"] = "manifest.json"
                if isinstance(resource, web.StaticResource):
                    params["filename"] = "sample.css"
                path = str(resource.url_for(**params))
                route = next(route for route in resource if route.method != "HEAD")
                method = "GET" if route.method == "*" else route.method
                async with client.request(
                    method,
                    f"http://127.0.0.1:{port}{path}",
                    headers={"Host": f"demo.apps.localhost:{port}"},
                    allow_redirects=False,
                ) as response:
                    assert response.status == 200, (method, path)
                    assert await response.json() == {"app_path": path}
                request = make_mocked_request(method, path, app=runner.app)
                if canonical.startswith(("/api/", "/v1/")):
                    # Exercise the actual middleware without running mutating API handlers.
                    request._match_info = UrlMappingMatchInfo(params, route)
                    handler = AsyncMock(return_value=web.Response(status=204))
                    assert await api_only_middleware(request, handler) is handler.return_value
                    handler.assert_awaited_once_with(request)
                    continue
                request._match_info = await runner.app.router.resolve(request)
                assert request.match_info.route.resource is not None, (method, path)
                assert request.match_info.route.resource.canonical == canonical, (method, path)
                async with client.request(
                    method, f"http://127.0.0.1:{port}{path}", allow_redirects=False
                ) as response:
                    body = (
                        await response.json()
                        if response.content_type == "application/json"
                        else await response.text()
                    )
                    refused = (
                        response.status == 404
                        and isinstance(body, dict)
                        and body.get("code") == "headless"
                    )
                    assert refused == (canonical in DASHBOARD_RESOURCES), (
                        method,
                        path,
                        response.status,
                        body,
                    )

            for path in ("/chat", "/settings/x"):
                async with client.get(
                    f"http://127.0.0.1:{port}{path}",
                    headers={"Host": f"demo.apps.localhost:{port}"},
                ) as response:
                    assert response.status == 200
                    assert await response.json() == {"app_path": path}
                async with client.get(
                    f"http://127.0.0.1:{port}{path}",
                    headers={"Accept": "text/html"},
                    allow_redirects=False,
                ) as response:
                    assert response.status == 404, path
                    assert (await response.json())["code"] == "headless", path
                    assert "Location" not in response.headers

            for path in ("/", "/assets/index.js", "/settings/x"):
                async with client.get(
                    f"http://127.0.0.1:{port}{path}",
                    headers={"Host": f"not-an-app.localhost:{port}"},
                ) as response:
                    if path == "/assets/index.js":
                        assert response.status == 403  # The ordinary Host barrier refuses it.
                        assert "headless" not in await response.text()
                    else:
                        assert response.status == 404
                        assert (await response.json())["code"] == "headless"
                async with client.get(
                    f"http://127.0.0.1:{port}{path}",
                    headers={"Host": f"disabled.apps.localhost:{port}"},
                ) as response:
                    assert (response.status, await response.text()) == (404, "not found")
                async with client.get(
                    f"http://127.0.0.1:{port}{path}",
                    headers={
                        "Host": f"demo.apps.localhost:{port}",
                        "X-Forwarded-For": "127.0.0.2",
                    },
                ) as response:
                    assert response.status == 421


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), [("GET", "/oauth/token"), ("DELETE", "/v1/models")])
async def test_headless_preserves_method_not_allowed(gateway, method, path) -> None:
    response = await gateway.request(method, path)
    assert response.status == 405
    assert "Allow" in response.headers
    assert gateway.served == []


@pytest.mark.asyncio
async def test_unmatched_api_paths_keep_the_normal_chain(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(startup.srv, "warm_own_host_names", lambda: None)
    replies = []
    for headless in (False, True):
        with monkeypatch.context() as patches:
            async with startup._dashboard(tmp_path, patches, headless=headless) as (runner, _, _):
                server = startup._RunningAppServer(runner, host="127.0.0.1", port=0)
                async with TestClient(server) as client:
                    responses = []
                    for path in ("/api/unregistered", "/v1/unregistered"):
                        response = await client.get(
                            path,
                            headers={"Accept": "text/html", "X-Request-Id": "req_headless_test"},
                            allow_redirects=False,
                        )
                        responses.append((response.status, await response.text()))
                    replies.append(responses)
    assert replies[1] == replies[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("headless", [False, True])
async def test_kept_route_not_found_never_becomes_a_headless_shell(
    tmp_path, monkeypatch, headless
) -> None:
    from kiro_crew.dashboard.headless import HEADLESS_KEY

    monkeypatch.setattr(startup.srv, "warm_own_host_names", lambda: None)
    shell = AsyncMock(return_value=web.Response(text="dashboard shell", content_type="text/html"))
    monkeypatch.setattr(startup.srv.handlers, "index", shell)
    async with startup._dashboard(tmp_path, monkeypatch, headless=headless) as (runner, _, _):
        # Keep the real fallback and the boot's mode marker, without the auth shell bypass.
        fallback = next(mw for mw in runner.app.middlewares if mw.__name__ == "spa_fallback")
        app = web.Application(middlewares=[api_only_middleware, fallback])
        if HEADLESS_KEY in runner.app:
            app[HEADLESS_KEY] = runner.app[HEADLESS_KEY]

        async def missing(request):
            raise web.HTTPNotFound(text="authorization not found")

        app.router.add_get("/oauth/authorize", missing)
        async with TestClient(TestServer(app, host="127.0.0.1", port=0)) as client:
            response = await client.get("/oauth/authorize", allow_redirects=False)
            if headless:
                assert (response.status, await response.text()) == (404, "authorization not found")
                assert response.content_type != "text/html"
                assert "Location" not in response.headers
                shell.assert_not_awaited()
            else:
                assert (response.status, await response.text()) == (200, "dashboard shell")
                shell.assert_awaited_once()


def test_orchestrator_refuses_headless_without_full_dashboard_before_start(monkeypatch) -> None:
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.slack.gateway import GatewayOrchestrator

    cfg = KiroCrewConfig()
    monkeypatch.setattr(cfg, "load_credentials", MagicMock(return_value={}))
    starts = [MagicMock(), MagicMock(), MagicMock()]
    for name, spy in zip(("run", "_init_dashboard", "_init_api_server"), starts):
        monkeypatch.setattr(GatewayOrchestrator, name, spy)
    with pytest.raises(ValueError, match="headless=True requires no_dashboard=False"):
        GatewayOrchestrator(cfg, headless=True, no_dashboard=True)
    cfg.load_credentials.assert_not_called()
    for spy in starts:
        spy.assert_not_called()


@pytest.mark.parametrize(("headless", "no_dashboard"), [(True, False), (False, True)])
def test_orchestrator_accepts_each_mode_alone(monkeypatch, headless, no_dashboard) -> None:
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.slack.gateway import GatewayOrchestrator

    cfg = KiroCrewConfig()
    monkeypatch.setattr(cfg, "load_credentials", lambda: {})
    gateway = GatewayOrchestrator(cfg, headless=headless, no_dashboard=no_dashboard)
    assert gateway._headless is headless
    assert gateway._no_dashboard is no_dashboard


@pytest.mark.asyncio
@pytest.mark.parametrize("headless", [True, False])
async def test_init_dashboard_hands_the_mode_to_start_dashboard(monkeypatch, headless) -> None:
    from test_slack_gateway_more_coverage import _mock_dashboard_state, _prepared_orchestrator

    from kiro_crew.slack import gateway as gw

    orch = _prepared_orchestrator()
    orch._no_dashboard = False
    orch._headless = headless
    # As a managed-proxy host would answer, so only headless makes the bind loopback.
    monkeypatch.setattr(gw, "is_local_only", lambda host, slack_connected: False)
    start = AsyncMock(return_value=(MagicMock(), _mock_dashboard_state()))
    monkeypatch.setattr(gw, "LessonStore", MagicMock())
    monkeypatch.setattr(gw, "start_dashboard", start)
    await orch._init_dashboard()
    start.assert_awaited_once()
    assert start.await_args.kwargs["headless"] is headless
    assert start.await_args.kwargs["local_only"] is headless


@pytest.mark.asyncio
@pytest.mark.parametrize("headless", [False, True])
@pytest.mark.parametrize("credential", ["absent", "invalid", "valid"])
@pytest.mark.parametrize("closed", [False, True])
async def test_local_authorization_never_uses_headless_signin_fallbacks(
    tmp_path, monkeypatch, headless, credential, closed
) -> None:
    from kiro_crew.dashboard import lts_gate_middleware as lts
    from kiro_crew.dashboard.token_auth import generate_token
    from kiro_crew.lts_gate import LoginWatch, LtsGate

    monkeypatch.setattr(startup.srv, "warm_own_host_names", lambda: None)
    shell = AsyncMock(
        side_effect=lambda request: web.Response(text="dashboard shell", content_type="text/html")
    )
    monkeypatch.setattr(startup.srv.handlers, "index", shell)
    if closed:
        gate = LtsGate()
        watch = LoginWatch(gate, SimpleNamespace(authenticated=False))
        monkeypatch.setattr(
            lts,
            "lts_gate_middleware_for",
            lambda app, *, spa_shell_handler: lts.make_lts_gate_middleware(
                gate, watch, admitted=lts._AdmittedRequests(), spa_shell_handler=spa_shell_handler
            ),
        )
    async with startup._dashboard(tmp_path, monkeypatch, headless=headless) as (runner, _, _):
        async with TestClient(
            startup._RunningAppServer(runner, host="127.0.0.1", port=0)
        ) as client:
            if credential != "absent":
                token = (
                    generate_token("signin-test", session=True)
                    if credential == "valid"
                    else "invalid"
                )
                client.session.cookie_jar.update_cookies({f"mc_token_{client.port}": token})
            for path in (
                "/oauth/token",
                "/oauth/revoke",
                "/oauth/authorize",
                "/.well-known/oauth-authorization-server",
            ):
                for method in ("GET", "HEAD"):
                    bypass = method == "GET" and path not in ("/oauth/token", "/oauth/revoke")
                    refused = not bypass and credential != "valid"
                    expected_shell = not headless and (refused or closed)
                    if expected_shell:
                        status, content_type = 200, "text/html"
                    elif refused or closed:
                        status, content_type = 403, "application/json"
                    elif path in ("/oauth/authorize", "/.well-known/oauth-authorization-server"):
                        status, content_type = (
                            400 if path == "/oauth/authorize" else 200
                        ), "application/json"
                    else:
                        status, content_type = 405, "text/plain"
                    response = await client.request(
                        method, path, headers={"Accept": "text/html"}, allow_redirects=False
                    )
                    assert (response.status, response.content_type) == (status, content_type), (
                        method,
                        path,
                        credential,
                        closed,
                    )
                    assert "Location" not in response.headers
                    body = await response.text()
                    if method == "HEAD":
                        assert body == ""
                    elif expected_shell:
                        assert body == "dashboard shell"
                    elif refused:
                        data = await response.json()
                        assert data["code"] == "forbidden"
                        assert data["error"]
                        assert response.headers["X-Auth-Required"] == "true"
                    elif closed:
                        assert (await response.json())["code"] == "lts_gate_closed"
                    elif status == 400:
                        assert (await response.json())["error"] == "invalid_request"
                    elif status == 405:
                        assert "Allow" in response.headers
                    else:
                        assert "authorization_endpoint" in await response.json()
            if headless:
                shell.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("headless", [False, True])
async def test_signin_refusal_body_changes_only_for_headless(
    tmp_path, monkeypatch, headless
) -> None:
    from kiro_crew.dashboard import ssm_gate
    from kiro_crew.dashboard.token_auth_paths import _403_HTML, _deny

    monkeypatch.setattr(startup.srv, "warm_own_host_names", lambda: None)
    async with startup._dashboard(tmp_path, monkeypatch, headless=headless) as (runner, _, _):
        for method in ("GET", "HEAD"):
            request = make_mocked_request(
                method, "/oauth/token", app=runner.app, headers={"Accept": "text/html"}
            )
            for code in ("", "owner_only"):
                response = _deny(request, "Token required", code)
                assert response.status == 403
                assert response.headers["X-Auth-Required"] == "true"
                if headless:
                    import json

                    assert response.content_type == "application/json"
                    assert json.loads(response.text) == {
                        "error": "Token required",
                        "code": code or "forbidden",
                    }
                else:
                    assert response.content_type == "text/html"
                    assert response.text == _403_HTML.format(reason="Token required")
            # The SSM site also runs on a headless gateway. The direct tailnet
            # listener does not, so its refusal page never meets a headless request.
            response = ssm_gate.refusal_response(request, ssm_gate.REFUSE_NOT_INVITED)
            assert response.status == 403
            assert response.content_type == ("application/json" if headless else "text/html")
            assert "Location" not in response.headers
            # The reduced server has no headless marker and retains its exact sign-in page.
            reduced = make_mocked_request(method, "/oauth/token", app=web.Application())
            assert _deny(reduced, "Token required").text == _403_HTML.format(
                reason="Token required"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("headless", [False, True])
async def test_live_get_routes_never_return_a_headless_dashboard(
    tmp_path, monkeypatch, frontend_dist, headless
) -> None:
    shell = AsyncMock(
        side_effect=lambda request: web.Response(text="dashboard shell", content_type="text/html")
    )
    monkeypatch.setattr(startup.srv.handlers, "index", shell)
    async with startup._dashboard(tmp_path, monkeypatch, headless=headless) as (runner, _, _):
        async with TestClient(
            startup._RunningAppServer(runner, host="127.0.0.1", port=0)
        ) as client:
            html_resources = set()
            for resource in runner.app.router.resources():
                if not any(route.method in ("GET", "*") for route in resource):
                    continue
                canonical = resource.canonical
                params = {name: "sample" for name in re.findall(r"\{(\w+)\}", canonical)}
                if canonical == "/{name}":
                    params["name"] = "manifest.json"
                if isinstance(resource, web.StaticResource):
                    params["filename"] = "sample.css"
                path = str(resource.url_for(**params))
                response = await client.get(
                    path, headers={"Accept": "text/html"}, allow_redirects=False
                )
                # Refusals cannot bootstrap the dashboard or send the browser there.
                if response.content_type == "text/html":
                    html_resources.add(canonical)
                if headless:
                    if canonical == "/api/connections/oauth/callback":
                        # Vendor OAuth completion page, not dashboard sign-in or its shell.
                        assert response.status == 400
                        assert "This sign-in link is not the current one." in await response.text()
                    else:
                        assert response.content_type != "text/html", (
                            canonical,
                            response.status,
                            await response.text(),
                        )
                    assert response.headers.get("Location") not in ("/", "/chat")
                await response.read()
            if headless:
                shell.assert_not_awaited()
            else:
                assert "/" in html_resources
                assert shell.await_count > 0


def test_the_printed_credential_command_names_the_bound_port(capsys) -> None:
    """A gateway started with ``--port`` prints a command that reaches that port."""
    print_client_access(7001)
    assert capsys.readouterr().out == (
        "👻 Headless: API on http://127.0.0.1:7001/v1\n"
        "   Credential for a Crew API client: kirocrew token --bearer --port 7001\n"
    )
