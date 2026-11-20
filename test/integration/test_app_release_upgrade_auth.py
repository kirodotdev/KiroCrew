"""Third-party apps a 0.12 release installed still authenticate after the upgrade.

A 0.12 release kept each app's secret in the app's own folder
(``apps/<app>/.app_secret``). This version never reads that file. Each channel that
can hand an app a secret replaces it instead: the backend spawn, the load of an
app's in-process hooks, and the owner's registration of a self-managed app. A
migration that forgets one backend type leaves that type refused on every token
exchange, and nothing else notices. So the real gateway boots here on a home that
holds one sample app of each backend type the App Kit docs list, as the release
left it, and each app is checked end to end:

* in-process ``backend.hooks``: a hook route mints its token with
  ``ctx.app_token()`` and calls the gateway at ``ctx.gateway_origin()``;
* spawned ``backend.entryPoint``: the backend checks both proxy signatures and the
  origin proof with ``KIROCREW_PROXY_SECRET``, using the stdlib recipe the docs
  give, then exchanges that secret for a token and calls a granted route with it;
* self-managed: the app registers again, as it does on its next launch, and its
  backend checks both proxy signatures with the secret in the response, which is
  then exchanged for a token;
* an app's own agent ``mcpServers`` entry: no secret and no token reach it.

Every app is also refused the value left in its folder. The apps are then
updated, turned off and on, and the gateway restarted, and every check runs again.

The release's layout is written by hand, because this version writes a different
one. The files, fields and modes are the ones the release's own install, enable and
registration code writes for these four apps.

Owning doc: ``docs/app-kit/api-reference.md`` § Backend Environment Variables.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from aiohttp import web
from integration.conftest import GatewayBoot, IntegrationGateway

from kiro_crew import member_records
from kiro_crew.apps import manager
from kiro_crew.apps.backend_runtime.tracking import get_app_backend_port
from kiro_crew.apps.proxy_auth import (
    PROXY_NONCE_HEADER,
    PROXY_V2_HEADER,
    ProxyPrincipalReplayCache,
    raw_request_target,
    verify_proxy_request,
    verify_proxy_request_v2,
)
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.dashboard.token_auth import read_app_secret
from kiro_crew.testing.wait import async_wait_until

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(300),
    # Each boot spawns a backend on a port the gateway picks; two boots picking at
    # once in one worker pool can collide, so this module's boots run one at a time.
    pytest.mark.xdist_group(name="app_release_upgrade_auth"),
]

HOOKS = "sample-hooks"
SPAWNED = "sample-spawned"
SELF_MANAGED = "sample-self-managed"
MCP = "sample-mcp"
MCP_SERVER = "sample-mcp-notes"
GATEWAY_MANAGED = (HOOKS, SPAWNED, MCP)
ALL_APPS = (*GATEWAY_MANAGED, SELF_MANAGED)

#: The route every sample app is granted, and calls with its token.
API = "/api/sessions"

#: The folder secret the release left: 64 hex characters, like the ones it generated.
LEFT_IN_FOLDER = "0" * 63 + "1"

#: (status, ``code``) of a refused folder secret. The first refusal of a streak is a
#: 403; a repeat inside its window is counted and answered 429
#: (``kiro_crew.dashboard.app_token_denials``). Both say ``invalid secret``.
STALE_SECRET_REFUSALS = {(403, "invalid_secret"), (429, "auth_throttled")}

#: When the release installed the apps; any fixed time before this boot.
RELEASE_TIME = "2026-11-09T21:26:17Z"

#: The permission families the release's ``approved-grants.json`` holds, in its order.
RELEASE_GRANT_FAMILIES = (
    "api",
    "events",
    "linkPreviewHosts",
    "linkPreviewTextPatterns",
    "linkPreviewFullUrl",
    "messageActions",
    "linkOpenHosts",
    "linkOpenFullUrl",
    "composerActions",
    "selectionActions",
)

#: The hook routes of the in-process app. The probe answers what a hook sees.
_HOOKS_PY = """from urllib.parse import urlsplit

import aiohttp
from aiohttp import web


class _Route:
    def __init__(self, method, path, handler):
        self.method, self.path, self.handler = method, path, handler


async def _probe(request, ctx):
    origin = ctx.gateway_origin()
    token = await ctx.app_token()
    cookie = f"mc_token_{urlsplit(origin).port}={token}"
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{origin}/api/sessions", headers={"Cookie": cookie}) as resp:
            api = resp.status
    return web.json_response({"origin": origin, "token": bool(token), "api": api})


def register_routes(ctx):
    return [_Route("GET", "/probe", _probe)]
"""

#: The spawned backend: the stdlib only, following the docs' recipe for a backend that
#: cannot import kiro_crew. ``/api/probe`` answers what it was handed and what it did.
_SERVER_PY = """import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

NAME = os.environ["KIROCREW_APP_NAME"]
SECRET = os.environ.get("KIROCREW_PROXY_SECRET", "")
ORIGIN = os.environ.get("KIROCREW_GATEWAY_ORIGIN", "")
PROOF = os.environ.get("KIROCREW_GATEWAY_ORIGIN_PROOF", "")


def _mac(message):
    return hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


def _signed(header, message_for):
    ts, _, sig = header.partition(":")
    if not (SECRET and ts.isdigit() and abs(time.time() - int(ts)) <= 60):
        return False
    return hmac.compare_digest(sig, _mac(message_for(ts)))


def _call(request):
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except ValueError:
        return 0, None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._reply({"ok": True})
        method, target = self.command, self.path
        body_hash = hashlib.sha256(b"").hexdigest()
        nonce = self.headers.get("X-KiroCrew-Proxy-Nonce", "")
        v1 = _signed(
            self.headers.get("X-KiroCrew-Proxy", ""),
            lambda ts: f"{ts}:{method}:{target}:{body_hash}",
        )
        v2 = _signed(
            self.headers.get("X-KiroCrew-Proxy-V2", ""),
            lambda ts: f"v2:{ts}:{nonce}:{method}:{target}:{body_hash}",
        )
        proof = bool(SECRET and ORIGIN) and hmac.compare_digest(PROOF, _mac(ORIGIN))
        exchange, grant = _call(
            urllib.request.Request(
                f"{ORIGIN}/api/apps/{NAME}/token",
                data=b"",
                method="POST",
                headers={"X-App-Secret": SECRET},
            )
        )
        token = (grant or {}).get("token", "")
        cookie = f"mc_token_{urlsplit(ORIGIN).port}={token}"
        api, _ = _call(urllib.request.Request(f"{ORIGIN}/api/sessions", headers={"Cookie": cookie}))
        self._reply(
            {
                "v1": v1,
                "v2": v2,
                "origin": ORIGIN,
                "origin_proof": proof,
                "exchange": exchange,
                "api": api,
            }
        )


ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
"""


@pytest.fixture(autouse=True)
def _outside_a_sandbox(_floor_monkeypatch):
    # The gateway runs unsandboxed; a test host may itself be a sandbox.
    _floor_monkeypatch.delenv(member_records._IN_SANDBOX_MARKER, raising=False)


class _SelfManagedServer:
    """The self-managed app's own backend, in this process, on a port the kernel picks.

    It holds the secret the app's last registration returned, and its probe answers
    whether both proxy signatures verify with it.
    """

    def __init__(self) -> None:
        self.secret = ""
        self.port = 0
        self._replays = ProxyPrincipalReplayCache()
        self._runner: web.AppRunner | None = None

    async def _probe(self, request: web.Request) -> web.Response:
        body = await request.read()
        target = raw_request_target(request)
        v1 = verify_proxy_request(
            request.headers.get("X-KiroCrew-Proxy", ""),
            method=request.method,
            target=target,
            body=body,
            secret=self.secret,
        )
        v2 = verify_proxy_request_v2(
            request.headers.get(PROXY_V2_HEADER, ""),
            request.headers.get(PROXY_NONCE_HEADER, ""),
            method=request.method,
            target=target,
            body=body,
            replay_cache=self._replays,
            secret=self.secret,
        )
        return web.json_response({"v1": v1, "v2": v2})

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/api/probe", self._probe)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", 0).start()
        self.port = int(self._runner.addresses[0][1])

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    def manifest(self, version: str) -> dict[str, Any]:
        return {
            "name": SELF_MANAGED,
            "version": version,
            "displayName": SELF_MANAGED,
            "mcpServers": {SELF_MANAGED: {"url": f"http://127.0.0.1:{self.port}/mcp"}},
            "permissions": {"api": [API]},
        }


def _manifest(name: str, version: str) -> dict[str, Any]:
    extras: dict[str, dict[str, Any]] = {
        HOOKS: {
            "backend": {"hooks": {"routes": "backend.routes:register_routes"}},
            "permissions": {"api": [API]},
        },
        SPAWNED: {
            "backend": {"entryPoint": "backend/server.py", "type": "python"},
            "permissions": {"api": [API]},
        },
        MCP: {"mcpServers": {MCP_SERVER: {"command": "python3", "args": ["server.py"]}}},
    }
    return {
        "name": name,
        "version": version,
        "displayName": name,
        "description": "A sample third-party app",
        "author": "tester",
        **extras[name],
    }


def _app_files(name: str) -> dict[str, str]:
    return {
        HOOKS: {"backend/__init__.py": "", "backend/routes.py": _HOOKS_PY},
        SPAWNED: {"backend/server.py": _SERVER_PY},
        MCP: {"server.py": "raise SystemExit(0)\n"},
    }[name]


def _write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def _write_json(path: Path, data: dict[str, Any], mode: int) -> None:
    _write(path, json.dumps(data, indent=2) + "\n", mode)


def _write_source(sources: Path, name: str, version: str) -> Path:
    """The folder the app was installed from, which an update reads again."""
    src = sources / name
    _write(src / "app.json", json.dumps(_manifest(name, version), indent=2), 0o644)
    for rel, text in _app_files(name).items():
        _write(src / rel, text, 0o644)
    return src


def _grants(api: list[str]) -> dict[str, list[str]]:
    return {family: (api if family == "api" else []) for family in RELEASE_GRANT_FAMILIES}


def _seed_release_app(home: Path, name: str, *, source: Path | None, manifest: dict) -> None:
    """One app's folder, and its owner-enable marker, as the release wrote them."""
    folder = home / "apps" / name
    folder.mkdir(parents=True)
    folder.chmod(0o755)
    record: dict[str, Any] = {
        "name": name,
        "version": manifest["version"],
        "displayName": manifest["displayName"],
        "enabled": True,
        "installedAt": RELEASE_TIME,
    }
    if source is not None:
        # Installed from a folder and enabled: the gateway owns its resources and
        # lifecycle, and the enable wrote the owner-enable marker.
        _write(folder / "app.json", json.dumps(manifest, indent=2), 0o644)
        for rel, text in _app_files(name).items():
            _write(folder / rel, text, 0o644)
        record.update(
            updatedAt=RELEASE_TIME,
            source=str(source),
            origin="local",
            resources="gateway",
            lifecycle="gateway",
        )
        grants = home / "host-exec-grants"
        grants.mkdir(mode=0o755, exist_ok=True)
        _write(grants / name, "", 0o644)
    else:
        # Registered by the app itself: the registration wrote no owner-enable marker.
        _write_json(folder / "app.json", manifest, 0o600)
        record.update(origin="external", resources="app", lifecycle="app")
    record.update(
        schemaVersion=2,
        dev=False,
        defaultOnBackfilled=False,
        sessionApprovalConsentPending=False,
        approvedGrantsStored=True,
    )
    _write_json(folder / "installed.json", record, 0o600)
    _write_json(
        folder / "approved-grants.json",
        _grants(manifest.get("permissions", {}).get("api", [])),
        0o600,
    )
    _write(folder / ".app_secret", LEFT_IN_FOLDER, 0o600)
    (folder / "data").mkdir(mode=0o700)


def _seed_release_home(home: Path, sources: Path, server: _SelfManagedServer) -> None:
    """The four apps, and the owner's setting that admits third-party apps."""
    _write_json(home / "config.json", {"agent": {"apps_allow_third_party": True}}, 0o600)
    (home / "apps").mkdir(mode=0o755)
    for name in GATEWAY_MANAGED:
        source = _write_source(sources, name, "1.0.0")
        _seed_release_app(home, name, source=source, manifest=_manifest(name, "1.0.0"))
    _seed_release_app(home, SELF_MANAGED, source=None, manifest=server.manifest("1.0.0"))


@asynccontextmanager
async def _release_home(
    gateway_boot: GatewayBoot, home: Path, tmp_path: Path
) -> AsyncIterator[tuple[IntegrationGateway, _SelfManagedServer]]:
    """A boot on a home the release left, with the self-managed app's backend running."""
    server = _SelfManagedServer()
    await server.start()
    try:
        _seed_release_home(home, tmp_path / "release-sources", server)
        async with gateway_boot() as gw:
            yield gw, server
    finally:
        await server.stop()


def _app_cookie(gw: IntegrationGateway, token: str) -> dict[str, str]:
    return {"Cookie": f"mc_token_{gw.port}={token}"}


def _folder_secret(home: Path, name: str) -> Path:
    return home / "apps" / name / ".app_secret"


def _assert_secret_replaced(home: Path, name: str) -> None:
    assert not os.path.lexists(_folder_secret(home, name))
    assert read_app_secret(name) not in ("", LEFT_IN_FOLDER)


async def _exchange(gw: IntegrationGateway, name: str, secret: str) -> Any:
    return await gw.post(f"/api/apps/{name}/token", headers={"X-App-Secret": secret}, auth=False)


async def _check_hooks_app(gw: IntegrationGateway) -> None:
    probe = await gw.get_json(f"/api/apps/{HOOKS}/probe")
    assert probe == {"origin": gw.base_url, "token": True, "api": 200}
    _assert_secret_replaced(gw.home, HOOKS)


async def _check_spawned_app(gw: IntegrationGateway) -> None:
    # The proxy answers 502 until the backend's health check passes.
    await async_wait_until(
        lambda: get_app_backend_port(SPAWNED) is not None,
        off_loop=True,
        describe=lambda: f"{SPAWNED} backend port {get_app_backend_port(SPAWNED)!r}",
    )
    probe = await gw.get_json(f"/apps/{SPAWNED}/api/probe")
    assert probe == {
        "v1": True,
        "v2": True,
        "origin": gw.base_url,
        "origin_proof": True,
        "exchange": 200,
        "api": 200,
    }
    _assert_secret_replaced(gw.home, SPAWNED)


def _mcp_entry() -> dict[str, Any] | None:
    spec_path = kiro_agents_dir() / "kirocrew.json"
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = spec.get("mcpServers", {}).get(f"{MCP}:{MCP_SERVER}")
    return entry if isinstance(entry, dict) else None


async def _check_mcp_app(gw: IntegrationGateway) -> None:
    entry = await async_wait_until(
        _mcp_entry, off_loop=True, describe=lambda: f"{MCP}:{MCP_SERVER} in kirocrew.json"
    )
    # Today's documented behaviour: the server kiro-cli starts gets neither the app's
    # secret nor a token, so it cannot call the app's own routes.
    env = entry.get("env", {})
    assert not [key for key in env if "SECRET" in key.upper() or "TOKEN" in key.upper()], env
    text = json.dumps(entry)
    assert LEFT_IN_FOLDER not in text
    stored = read_app_secret(MCP)
    assert not stored or stored not in text


async def _check_self_managed_app(
    gw: IntegrationGateway, server: _SelfManagedServer, *, version: str
) -> None:
    """The app's next launch: it registers, and uses the secret the response carries."""
    registered = await gw.post_json(
        "/api/apps/register",
        {
            "name": SELF_MANAGED,
            "version": version,
            "displayName": SELF_MANAGED,
            "manifest": server.manifest(version),
        },
        expect=201,
    )
    server.secret = registered.get("secret", "")
    assert server.secret not in ("", LEFT_IN_FOLDER)
    exchange = await _exchange(gw, SELF_MANAGED, server.secret)
    body = await exchange.text()
    assert exchange.status == 200, body
    api = await gw.get(API, auth=False, headers=_app_cookie(gw, json.loads(body)["token"]))
    assert api.status == 200, await api.text()
    assert await gw.get_json(f"/apps/{SELF_MANAGED}/api/probe") == {"v1": True, "v2": True}
    assert not os.path.lexists(_folder_secret(gw.home, SELF_MANAGED))


async def _check_gateway_managed_apps(gw: IntegrationGateway) -> None:
    await _check_hooks_app(gw)
    await _check_spawned_app(gw)
    await _check_mcp_app(gw)
    for name in ALL_APPS:
        refused = await _exchange(gw, name, LEFT_IN_FOLDER)
        body = await refused.json()
        assert body.get("error") == "invalid secret", (name, refused.status, body)
        assert (refused.status, body.get("code")) in STALE_SECRET_REFUSALS, (name, body)


async def _toggle(gw: IntegrationGateway, name: str) -> None:
    await gw.post_json(f"/api/apps/{name}/disable")
    await gw.post_json(f"/api/apps/{name}/enable")


@pytest.mark.asyncio
async def test_apps_a_release_installed_authenticate_on_the_first_start(
    gateway_boot, integration_home, tmp_path
):
    async with _release_home(gateway_boot, integration_home, tmp_path) as (gw, server):
        await _check_gateway_managed_apps(gw)
        # The owner turns the self-managed app off and on once, as the refusal's
        # hint asks; the next test covers its launch without that step.
        await _toggle(gw, SELF_MANAGED)
        await _check_self_managed_app(gw, server, version="1.0.0")


@pytest.mark.xfail(strict=True, reason="GH #39593")
@pytest.mark.asyncio
async def test_a_self_managed_app_the_release_registered_authenticates_on_its_next_launch(
    gateway_boot, integration_home, tmp_path
):
    async with _release_home(gateway_boot, integration_home, tmp_path) as (gw, server):
        await _check_self_managed_app(gw, server, version="1.0.0")


@pytest.mark.asyncio
async def test_apps_a_release_installed_authenticate_after_an_update_a_toggle_and_a_restart(
    gateway_boot, integration_home, tmp_path
):
    sources = tmp_path / "release-sources"
    async with _release_home(gateway_boot, integration_home, tmp_path) as (gw, server):
        await _toggle(gw, SELF_MANAGED)

        for name in GATEWAY_MANAGED:
            _write_source(sources, name, "1.1.0")
            await gw.post_json(f"/api/apps/{name}/update", {})
            record = manager._read_installed(name)
            assert record is not None and (record.version, record.enabled) == ("1.1.0", True)
        await _check_gateway_managed_apps(gw)
        await _check_self_managed_app(gw, server, version="1.1.0")

        for name in ALL_APPS:
            await _toggle(gw, name)
        await _check_gateway_managed_apps(gw)
        await _check_self_managed_app(gw, server, version="1.1.0")

        await gw.restart()
        await _check_gateway_managed_apps(gw)
        await _check_self_managed_app(gw, server, version="1.1.0")
