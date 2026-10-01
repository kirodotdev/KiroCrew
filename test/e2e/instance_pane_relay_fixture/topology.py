"""Real topology for the kc-46d84a incident E2E (same-origin Remote Crew pane relay).

Assembles the exact shape that failed in the incident, with ONLY the SSH/SSM
transport stood in (a real loopback peer gateway behind the manager seam):

  * PEER  — a real kirocrew gateway (``spawn_feature_gateway``) serving the real
            built SPA, real ``/api/status`` (advertising ``pane_relay_protocol``)
            and real ``/api/ws``. Its loopback port is NEVER handed to the
            browser.
  * SEAM  — :class:`_SeamPeer` implements the manager surface the real relay and
            the real owner issuer depend on (``PaneRelayPeer`` + ``connect`` +
            ``peer_capability``), forwarding to the peer over real HTTP/WebSocket
            with the peer's own port-scoped session cookie — exactly what
            ``SshTunnelManager`` does, minus the SSH child.
  * HUB   — an in-process aiohttp app over REAL TLS (one HTTPS origin). It mounts
            the REAL :class:`~kiro_crew.dashboard.instance_pane_relay.InstancePaneRelay`
            and the REAL owner issuer ``api_instances_open_pane``, serves the
            production API routes the Remote Crew list → auto-warm → open-pane
            flow needs (and a minimal body for everything else), and serves the
            REAL production-built parent SPA (``pane-host.vite.config.ts`` — a
            dedicated bundle that mounts the UNCHANGED ``InstancesViewport`` and
            relay authorities: connect, iframe construction with the ``window.name``
            envelope, endpoint parsing, channel + exact-``contentWindow``
            attribution, the ``RelayStorageBank``, and lease renewal). The parent
            is the real frontend code, not a hand-written mirror.

Nothing about the browser, the origin, the sandbox, the TLS/mixed-content layer,
the same-origin root-relative routing, or the parent's framing of the pane is
mocked — those are the layers the bug lived in. Only the SSH transport is a
real-loopback stand-in.

Heavy temp trees (the peer's KIROCREW_HOME, TLS certs) live under a caller-supplied
``artifacts`` directory, which the harness roots under the RUNNER's temporary root
(``RUNNER_TEMP``/``TMPDIR``, or ``KC46_ARTIFACT_ROOT`` when overridden) and removes
on exit — no author host path is assumed.
"""

from __future__ import annotations

import contextlib
import datetime
import http.cookiejar
import ipaddress
import json
import os
import ssl
import threading
import urllib.request
import warnings
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.web_exceptions import NotAppKeyWarning

FIXTURE_DIR = Path(__file__).resolve().parent
WORKTREE = FIXTURE_DIR.parents[2]  # test/e2e/instance_pane_relay_fixture -> worktree
WEBSITE = WORKTREE / "website"
INSTANCE_ID = "kc-46d84a"
_LOOPBACK = "127.0.0.1"

#: The relay lease TTL the hub issues, in seconds. Deliberately SHORT so the
#: production lease-renewal timer inside ``InstancesViewport`` fires DURING the
#: E2E — the spec asserts a short-TTL lease rotates the documentPath, channel and
#: iframe while HTTP/WS stay live. ``relayRenewDelayMs`` reissues at
#: ``ttl - min(120s, ttl/3)``, so 15s reissues ~10s in: past the fast initial
#: assertions, well before the 15s deadline, with the old lease still valid until
#: then so there is no gap. Overridable for a slower host.
_PANE_RELAY_TTL_SECONDS = int(os.environ.get("KC46_PANE_RELAY_TTL", "15"))


def _instance_view() -> dict[str, Any]:
    """One connected Remote Crew, the minimal ``InstanceView`` the switcher +
    viewport read. ``connected`` so ``InstancesViewport``'s own auto-warm raises
    the pane through the real connect path; ``ssh``/``was_connected`` so
    ``visibleInstanceTabs``/``hasDashboardPane`` admit it as a dashboard pane."""
    return {
        "id": INSTANCE_ID,
        "name": "kc-46d84a crew",
        "ssh_host": "peer.example",
        "remote_port": 7777,
        "local_port": 0,
        "ttl": "8h",
        "remote_bin": "",
        "connection_method": "ssh",
        "ssm_target": "",
        "aws_profile": "",
        "aws_region": "",
        "ssm_run_as": "",
        "was_connected": True,
        "status": {"instance_id": INSTANCE_ID, "state": "connected"},
    }


async def _serve_instances_list(_req: web.Request) -> web.Response:
    """``GET /api/instances`` — the Remote Crew list, one connected crew."""
    return web.json_response(
        {
            "active": True,
            "instances": [_instance_view()],
            "warm_set_cap": 8,
            "sso": {"state": "ok", "seconds_remaining": None, "expires_at": None, "reason": ""},
        }
    )


async def _serve_instance_connect(request: web.Request) -> web.Response:
    """``POST /api/instances/{id}/connect`` — answers the connected-only probe
    auto-warm issues before it opens the pane. Connected, so the probe proceeds
    to the REAL ``openInstancePane`` issuer."""
    return web.json_response({"instance_id": request.match_info["id"], "state": "connected"})


async def _serve_api_stub(_req: web.Request) -> web.Response:
    """Minimal valid body for every other same-origin API the parent SPA polls
    at boot (config/theme/branding reconciliation). Enough to degrade gracefully;
    the incident flow needs only the three instance routes above + the relay."""
    return web.json_response({})


# The relay allow-list floor the real manager also enforces: a caller can never
# smuggle a credential or spoofed authority across the tunnel.
_FORWARD_DENY = frozenset({"cookie", "authorization", "host", "content-length"})


def _self_signed_cert(cert_dir: Path) -> tuple[Path, Path]:
    """Mint a self-signed cert for 127.0.0.1 (SAN IP). Browser uses ignoreHTTPSErrors."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert_dir.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _LOOPBACK)])
    # Timezone-aware UTC: ``datetime.utcnow()`` is deprecated on 3.12+, and
    # ``cryptography`` accepts aware datetimes (converting to UTC) on the
    # ``not_valid_*`` builders. This keeps the E2E cert mint warning-free without
    # touching any global warnings filter.
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(_LOOPBACK))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = cert_dir / "hub-cert.pem"
    key_path = cert_dir / "hub-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _mint_peer_cookie(port: int, token: str) -> tuple[str, str]:
    """GET /?token=<link> once and capture the peer's port-scoped session cookie.

    Exactly what a browser (or the real manager) does to turn the link token into
    a session; the manager then replays that cookie on every proxied request.
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"http://{_LOOPBACK}:{port}/?token={token}", timeout=30) as r:
        r.read()
    for c in jar:
        if c.name == f"mc_token_{port}":
            return c.name, c.value
    raise RuntimeError(f"peer did not mint mc_token_{port}; cookies={[c.name for c in jar]}")


class _SeamPeer:
    """The manager seam: forwards to the real peer gateway over the loopback.

    Implements the ``PaneRelayPeer`` protocol the relay depends on PLUS the slice
    of the manager the owner issuer calls (``connect``, ``peer_capability``).
    Faithful to ``SshTunnelManager`` where it matters: the target is resolved from
    the instance id alone (never the request), the upstream headers are built from
    scratch with the peer's own port-scoped cookie added, and no upstream redirect
    is ever followed.
    """

    def __init__(self, peer_port: int, cookie_name: str, cookie_value: str) -> None:
        self._port = peer_port
        self._cookie = f"{cookie_name}={cookie_value}"
        self._epoch = 1
        self._connected = True

    # ── generation binding (PaneRelayPeer) ──
    def peer_forward_snapshot(self, instance_id: str) -> tuple[int, int]:
        return (self._port if self._connected else 0, self._epoch)

    def peer_forward_current(self, instance_id: str, snapshot: tuple[int, int]) -> bool:
        return self._connected and self._port > 0 and (self._port, self._epoch) == snapshot

    def _require_forward(self, expected_forward) -> None:
        """Mirror the real manager: a request pinned to the generation its lease
        was issued against is refused if the live forward has moved past it."""
        if expected_forward is not None and self.peer_forward_snapshot("_") != expected_forward:
            from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

            raise ProxyRequestError(
                "proxy_peer_not_connected",
                "forward generation moved since the lease was issued",
                http_status=503,
            )

    # ── HTTP forward (PaneRelayPeer) ──
    @contextlib.asynccontextmanager
    async def proxy_request(
        self,
        instance_id,
        method,
        path,
        *,
        data=None,
        content_type="",
        extra_headers=None,
        expected_forward=None,
    ):
        self._require_forward(expected_forward)
        headers: dict[str, str] = {}
        if extra_headers:
            for key, value in extra_headers.items():
                if key.lower() in _FORWARD_DENY:
                    continue
                headers[key] = value
        if content_type:
            headers["Content-Type"] = content_type
        headers["Cookie"] = self._cookie
        url = f"http://{_LOOPBACK}:{self._port}/{path.lstrip('/')}"
        session = aiohttp.ClientSession()
        try:
            resp = await session.request(
                method, url, data=data, headers=headers, allow_redirects=False
            )
            try:
                yield resp
            finally:
                resp.release()
        finally:
            await session.close()

    # ── WebSocket forward (PaneRelayPeer) ──
    @contextlib.asynccontextmanager
    async def proxy_websocket(
        self, instance_id, path, *, subprotocols=(), max_msg_size=0, expected_forward=None
    ):
        self._require_forward(expected_forward)
        url = f"http://{_LOOPBACK}:{self._port}/{path.lstrip('/')}"
        session = aiohttp.ClientSession()
        try:
            ws = await session.ws_connect(
                url,
                protocols=subprotocols,
                max_msg_size=max_msg_size or 4 * 1024 * 1024,
                headers={"Cookie": self._cookie, "Origin": f"http://{_LOOPBACK}:{self._port}"},
            )
            try:
                yield ws
            finally:
                with contextlib.suppress(Exception):
                    await ws.close()
        finally:
            await session.close()

    # ── owner-issuer manager surface ──
    async def connect(self, instance_id, *, rebuild=False, only_if_connected=False):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        state = TunnelState.CONNECTED if self._connected else TunnelState.DISCONNECTED
        return TunnelStatus(instance_id, state, local_port=self._port, remote_port=7777)

    async def peer_capability(self, instance_id, path):
        """Read the REAL peer's advertised capability (its /api/status)."""
        async with self.proxy_request(instance_id, "GET", path) as resp:
            if resp.status != 200:
                return False, {"code": "capability_http_error", "status": resp.status}
            return True, await resp.json()

    # direct-mode helpers (unused by the relay path; present for surface parity)
    def get_token(self, instance_id):
        return ""

    async def token_validates(self, local_port, token):
        return True

    async def refresh_token(self, instance_id):
        return ""


class _HubState:
    """Minimal DashboardState stand-in the real relay + issuer read from."""

    owner_id = "owner"

    def __init__(self, manager: _SeamPeer, relay: Any) -> None:
        self.instances_manager = manager
        self.instance_pane_relay = relay
        self.instances_registry = None


@web.middleware
async def _owner_identity_middleware(request: web.Request, handler):
    """Publish the owner identity the way ``token_auth`` does for the dashboard's
    own owner, so the REAL owner gate in ``api_instances_open_pane`` runs against a
    real owner subject. The relay route is self-authenticating (capability in the
    path) and is left ungated, exactly as production keeps ``/instance-pane`` on
    token_auth's bypass list."""
    if request.path.startswith("/api/instances/"):
        # Production's owner guard reads these with STRING keys
        # (``request.get('user')``, ``'app' in request``, ``request['app']``), so
        # the harness must set them the same way; aiohttp warns only on the SET.
        # Scope the ignore to exactly NotAppKeyWarning here — no global filter, so
        # real production deprecations still surface in the E2E run.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NotAppKeyWarning)
            request["user"] = "owner"
            request["app"] = ""
    return await handler(request)


class PaneRelayTopology:
    """Context manager yielding a live hub URL for the incident E2E.

    Args:
        artifacts: a directory (rooted under the runner's temporary root by the
            harness — see ``test_instance_pane_relay_e2e._artifact_root``) for the
            peer's KIROCREW_HOME, the TLS cert, and the hub's config home. The
            caller removes it on exit unless evidence retention is requested.
    """

    def __init__(self, artifacts: Path) -> None:
        self.artifacts = Path(artifacts)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        # Every heavy harness temp tree (the peer gateway HOME) lands here.
        os.environ["TMPDIR"] = str(self.artifacts)
        os.environ.setdefault("KIROCREW_SKIP_MODEL_DOWNLOAD", "1")
        self._stack = contextlib.ExitStack()
        self.hub_url = ""
        self.peer_port = 0
        self.instance_id = INSTANCE_ID
        self._loop_thread: threading.Thread | None = None

    def __enter__(self) -> "PaneRelayTopology":
        from kiro_crew.testing.harness import spawn_feature_gateway

        # 1) Real peer gateway (the remote crew's dashboard).
        gw = self._stack.enter_context(spawn_feature_gateway(fixture="minimal", approval="reads"))
        self.peer_port = gw.port
        cookie_name, cookie_value = _mint_peer_cookie(gw.port, gw.token)

        # 2) Manager seam + the REAL relay bound to it.
        from kiro_crew.dashboard.instance_pane_relay import (
            InstancePaneRelay,
            api_instance_pane_relay,
        )

        manager = _SeamPeer(gw.port, cookie_name, cookie_value)
        # SHORT lease TTL: the frontend renewal timer must fire during the E2E so
        # the spec can assert a lease rotation (documentPath + channel + iframe)
        # with HTTP/WS staying live. See _PANE_RELAY_TTL_SECONDS.
        relay = InstancePaneRelay(manager, ttl_seconds=_PANE_RELAY_TTL_SECONDS)
        state = _HubState(manager, relay)

        # 3) Enable the instances feature for the REAL owner issuer's config gate.
        hub_home = self.artifacts / "hub-home"
        hub_home.mkdir(parents=True, exist_ok=True)
        (hub_home / "config.json").write_text(json.dumps({"instances": {"enabled": True}}))
        os.environ["KIROCREW_HOME"] = str(hub_home)
        from kiro_crew.config import loader

        loader._invalidate_config_cache()

        # 4) Hub app over real TLS: real relay + real issuer + the parent page.
        from kiro_crew.dashboard.handlers_instances import api_instances_open_pane

        app = web.Application(middlewares=[_owner_identity_middleware])
        # Mirror production's string-keyed app state (dashboard/server.py stores
        # ``app["state"]`` and every handler reads ``request.app["state"]``).
        # aiohttp warns only on the SET, not on those request-time reads, so scope
        # the ignore to exactly this line and exactly NotAppKeyWarning — no global
        # warnings filter, so real production deprecations still surface in the
        # E2E run.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NotAppKeyWarning)
            app["state"] = state

        # Production API surface for the Remote Crew list → auto-warm → open-pane
        # flow. Specific routes FIRST so the catch-all never shadows them (aiohttp
        # resolves in registration order): the REAL owner issuer serves the pane,
        # the list + connect routes drive the viewport's own connect path, and
        # every other same-origin API the parent SPA polls at boot gets a minimal
        # valid body.
        app.router.add_get("/api/instances", _serve_instances_list)
        app.router.add_post("/api/instances/{id}/connect", _serve_instance_connect)
        app.router.add_post("/api/instances/{id}/pane", api_instances_open_pane)
        app.router.add_route("*", "/instance-pane", api_instance_pane_relay)
        app.router.add_route("*", "/instance-pane/{tail:.*}", api_instance_pane_relay)
        app.router.add_route("*", "/api/{tail:.*}", _serve_api_stub)

        # The REAL built parent SPA: a production-source bundle that mounts the
        # UNCHANGED InstancesViewport + relay authorities (connect, iframe
        # construction, endpoint parsing, channel attribution, storage bank,
        # renewal). Built by the harness (pane-host.vite.config.ts) into
        # KC46_PANE_HOST_DIST; served here at the hub root over HTTPS. It resolves
        # `direct` runtime for its OWN gateway (the hub root) and same-origin-relay
        # for the panes it frames — the exact production split.
        host_dist = Path(os.environ.get("KC46_PANE_HOST_DIST") or (WEBSITE / "pane-host-dist"))
        index_html = (host_dist / "index.html").read_text().replace("__INSTANCE_ID__", INSTANCE_ID)
        # Production-faithful parent CSP: the relay pane is framed same-origin
        # (frame-src 'self'); the parent script is a same-origin module
        # (script-src 'self', NO 'unsafe-inline' — the dedicated host build carries
        # no inline importmap); connect-src 'self' both proves same-origin API and
        # is what blocks the raw-loopback-port mixed-content probe. Any pane
        # subresource that escaped to the hub root would surface as a CSP/404.
        parent_csp = (
            "default-src 'self'; script-src 'self'; connect-src 'self'; "
            "img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; frame-src 'self'; worker-src 'self' blob:; "
            "object-src 'none'; base-uri 'self'"
        )

        async def _serve_parent(_req: web.Request) -> web.Response:
            return web.Response(
                text=index_html,
                content_type="text/html",
                headers={"Content-Security-Policy": parent_csp},
            )

        app.router.add_get("/", _serve_parent)
        # The host bundle's own hashed assets, same-origin under /assets/.
        app.router.add_static("/assets/", str(host_dist / "assets"))

        # A neutral, OFF-CAPABILITY document on the SAME hub origin, outside
        # /instance-pane/. The negative browser regression navigates the opaque
        # pane iframe here — its WindowProxy survives the navigation and still
        # matches the parent's frame map — and asserts the parent delivers it
        # NEITHER the pane channel NOR any host model. With the document-bound port
        # fix the parent only ever posts downward over the authenticated port the
        # REPLACED document transferred (now neutered), never a wildcard
        # `frame.postMessage(msg, '*')` this successor could receive. It is served
        # by the hub, NOT the relay, so it carries no bootstrap script, makes no
        # handshake, and is bound no port. It only records what its parent sends.
        #
        # The recorder is an EXTERNAL same-origin script, not inline: the document
        # is served under the parent's production-like CSP (`script-src 'self'`,
        # no 'unsafe-inline'), which Chromium enforces by refusing an inline
        # listener outright. An inline recorder would never install, and the spec
        # would then be inspecting nothing. `window.__recorderInstalled` is the
        # positive proof the spec requires before it accepts any negative result.
        recorder_html = (
            "<!doctype html><html><head><meta charset=utf-8>"
            '<script src="/neutral-recorder.js"></script></head>'
            "<body>off-capability-recorder</body></html>"
        )
        recorder_js = (
            "window.__received=[];window.__recorderInstalled=true;"
            "window.addEventListener('message',function(e){try{"
            "window.__received.push({data:e.data,origin:e.origin});}catch(_){}});"
        )

        async def _serve_recorder(_req: web.Request) -> web.Response:
            return web.Response(
                text=recorder_html,
                content_type="text/html",
                headers={"Content-Security-Policy": parent_csp},
            )

        async def _serve_recorder_js(_req: web.Request) -> web.Response:
            return web.Response(text=recorder_js, content_type="application/javascript")

        app.router.add_get("/neutral-recorder", _serve_recorder)
        app.router.add_get("/neutral-recorder.js", _serve_recorder_js)

        cert_path, key_path = _self_signed_cert(self.artifacts / "tls")
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(str(cert_path), str(key_path))

        # Run the hub on its own event loop in a background thread so the sync
        # harness (and the shelled Playwright run) proceed while it serves.
        import asyncio

        started = threading.Event()
        self._hub_loop: asyncio.AbstractEventLoop | None = None
        self._runner: web.AppRunner | None = None
        self._start_error: BaseException | None = None

        def _run_hub() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._hub_loop = loop

            async def _start() -> None:
                runner = web.AppRunner(app)
                await runner.setup()
                self._runner = runner
                site = web.TCPSite(runner, _LOOPBACK, 0, ssl_context=ssl_ctx)
                await site.start()
                sock = list(runner.sites)[0]._server.sockets[0]
                self._hub_port = sock.getsockname()[1]
                started.set()

            try:
                loop.run_until_complete(_start())
            except BaseException as exc:  # surface a bind/TLS failure to __enter__
                self._start_error = exc
                started.set()
                return
            loop.run_forever()

        self._loop_thread = threading.Thread(target=_run_hub, daemon=True)
        self._loop_thread.start()
        if not started.wait(timeout=30):
            raise RuntimeError("hub did not start within 30s")
        if self._start_error is not None:
            raise self._start_error
        self.hub_url = f"https://{_LOOPBACK}:{self._hub_port}"
        return self

    def __exit__(self, *exc) -> None:
        import asyncio

        loop = getattr(self, "_hub_loop", None)
        runner = getattr(self, "_runner", None)
        if loop is not None and runner is not None:
            with contextlib.suppress(Exception):
                fut = asyncio.run_coroutine_threadsafe(runner.cleanup(), loop)
                fut.result(timeout=10)
            loop.call_soon_threadsafe(loop.stop)
        self._stack.close()
