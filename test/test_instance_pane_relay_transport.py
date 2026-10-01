"""Transport-level guards for the pane-relay proxy, exercised against REAL
loopback peers so the ``_GenerationFencedConnector`` (a real ``aiohttp``
``TCPConnector`` that a faked ``ClientSession`` would bypass) actually runs.

Three properties, all confirmed defects in the independent review:

* **The manager lock is never held across the peer's response-header wait.**
  ``aiohttp`` returns from ``session.request``/``ws_connect`` only after the
  response headers arrive; the old code held the single manager lock across that
  wait, so one slow peer serialized every other crew and ``disconnect`` for the
  whole idle budget. The fence now lives in ``TCPConnector.connect`` (the loopback
  dial + the generation re-check), released before the header wait — proven here
  by a peer that accepts the connection and then pauses before answering while an
  unrelated request AND a disconnect still run.
* **A forward replaced between credential selection and the dial is refused.**
  The generation is re-checked under the lock immediately before ``super().connect``,
  so a rebuild that reused the loopback port under the next generation raises
  before a byte reaches the replacement peer.
* **A WebSocket handshake never follows a peer redirect.** ``ws_connect`` has no
  ``allow_redirects`` switch and follows by default; the redirect-refusing trace
  stops the follow before a second request leaves the hub, so an isolated second
  listener receives nothing.

These use a custom tunnel factory whose tunnel reports a REAL server's port as
its ``local_port``, so ``_peer_target`` dials that server for real. Credential
exchange is stubbed (``_peer_headers_for``) so no real link exchange is needed.
"""

import asyncio

import pytest
from aiohttp import web

pytestmark = pytest.mark.asyncio


class _PortTunnel:
    """A fake tunnel that reports a fixed ``local_port`` (a real peer's port).

    Mirrors the shape ``SshTunnelManager.connect`` reads from a tunnel, but binds
    nothing itself — the real listener is started separately and its port handed
    in, so ``_peer_target`` resolves ``http://127.0.0.1:<real>/…`` and the proxy
    dials a live server through the real connector.
    """

    def __init__(self, iid, ssh_host, local_port, remote_port, **_kw):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        self.iid = iid
        self.ssh_host = ssh_host
        self.pid = None
        self.stopped = False
        self.transport = "ssh"
        self.ssm_target = ""
        self.aws_profile = ""
        self.aws_region = ""
        self._S = TunnelState
        self.status = TunnelStatus(instance_id=iid, local_port=local_port, remote_port=remote_port)

    async def start(self):
        self.status.state = self._S.CONNECTED
        return True

    async def stop(self):
        self.stopped = True
        self.status.state = self._S.STOPPED


def _factory_on_port(port):
    def _factory(iid, ssh_host, _lp, remote_port, **kw):
        # Ignore the manager's allocated port; report the real listener's port so
        # the proxy dials it.
        return _PortTunnel(iid, ssh_host, port, remote_port, **kw)

    return _factory


def _make_manager(tmp_path, port):
    from kiro_crew.instances.registry import InstancesRegistry
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

    reg = InstancesRegistry(path=tmp_path / "instances.json")

    async def ok_mint(host, **_kw):
        return "SECRET_TOK"

    mgr = SshTunnelManager(
        reg, base_port=53500, mint_token=ok_mint, tunnel_factory=_factory_on_port(port)
    )
    return reg, mgr


async def _serve(routes):
    """Start a loopback aiohttp app; return ``(runner, port)``. ``routes`` is a
    list of ``(method, path, handler)``."""
    app = web.Application()
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    return runner, port


async def _stub_headers(mgr):
    async def _no_headers(*_a, **_k):
        return {}

    mgr._peer_headers_for = _no_headers  # type: ignore[assignment]


async def test_http_lock_not_held_across_response_header_wait(tmp_path):
    """A peer that pauses before its response headers must not block other crews.

    While instance ``a``'s request is parked in the header wait, an unrelated
    request on ``b`` AND a ``disconnect`` both complete — proving the manager lock
    was released after the dial, not held across the header wait. Under the old
    manager-lock-around-the-whole-request code the two ``wait_for`` calls would
    time out.
    """
    gate = asyncio.Event()
    hits: list[str] = []

    async def slow(request):
        hits.append("slow")
        await gate.wait()
        return web.Response(text="ok")

    async def fast(request):
        hits.append("fast")
        return web.Response(text="ok")

    runner, port = await _serve([("GET", "/slow", slow), ("GET", "/fast", fast)])
    reg, mgr = _make_manager(tmp_path, port)
    try:
        reg.add(name="A", ssh_host="a-alias", instance_id="a")
        reg.add(name="B", ssh_host="b-alias", instance_id="b")
        await mgr.connect("a")
        await mgr.connect("b")
        await _stub_headers(mgr)

        async def do_slow():
            async with mgr.proxy_request("a", "GET", "slow") as resp:
                return resp.status

        slow_task = asyncio.create_task(do_slow())
        # Wait until the slow request has dialed, sent, and entered the paused
        # handler — i.e. it is now awaiting response headers with the lock freed.
        for _ in range(500):
            if hits:
                break
            await asyncio.sleep(0.01)
        assert hits == ["slow"], "slow request never reached the paused peer"

        # The lock must be free: an unrelated request and a disconnect both run.
        async def do_fast():
            async with mgr.proxy_request("b", "GET", "fast") as resp:
                return resp.status

        assert await asyncio.wait_for(do_fast(), timeout=5) == 200
        assert await asyncio.wait_for(mgr.disconnect("b"), timeout=5) is True

        gate.set()
        assert await asyncio.wait_for(slow_task, timeout=5) == 200
    finally:
        gate.set()
        await mgr.shutdown()
        await runner.cleanup()


async def test_ws_lock_not_held_across_handshake_header_wait(tmp_path):
    """The WebSocket sibling: a peer that pauses before completing the upgrade
    must not hold the manager lock, so an unrelated HTTP request still runs."""
    gate = asyncio.Event()
    hits: list[str] = []

    async def slow_ws(request):
        hits.append("ws")
        await gate.wait()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close()
        return ws

    async def fast(request):
        return web.Response(text="ok")

    runner, port = await _serve([("GET", "/ws", slow_ws), ("GET", "/fast", fast)])
    reg, mgr = _make_manager(tmp_path, port)
    try:
        reg.add(name="A", ssh_host="a-alias", instance_id="a")
        reg.add(name="B", ssh_host="b-alias", instance_id="b")
        await mgr.connect("a")
        await mgr.connect("b")
        await _stub_headers(mgr)

        async def do_ws():
            async with mgr.proxy_websocket("a", "ws"):
                return "opened"

        ws_task = asyncio.create_task(do_ws())
        for _ in range(500):
            if hits:
                break
            await asyncio.sleep(0.01)
        assert hits == ["ws"], "WS handshake never reached the paused peer"

        async def do_fast():
            async with mgr.proxy_request("b", "GET", "fast") as resp:
                return resp.status

        assert await asyncio.wait_for(do_fast(), timeout=5) == 200

        gate.set()
        # The handshake then completes and the pane closes the socket.
        assert await asyncio.wait_for(ws_task, timeout=5) == "opened"
    finally:
        gate.set()
        await mgr.shutdown()
        await runner.cleanup()


async def test_ws_handshake_refuses_a_peer_redirect(tmp_path):
    """A peer answering the WS upgrade with a redirect must not make the hub
    contact a second authority. The isolated second listener records nothing."""
    from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

    second_hits: list[str] = []

    async def second(request):
        second_hits.append(request.path)
        return web.Response(text="should never be reached")

    runner2, port2 = await _serve([("GET", "/api/ws", second)])

    async def redirecting(request):
        # A 302 instead of a 101 upgrade, pointing OUTSIDE the selected forward.
        return web.Response(status=302, headers={"Location": f"http://127.0.0.1:{port2}/api/ws"})

    runner1, port1 = await _serve([("GET", "/api/ws", redirecting)])
    reg, mgr = _make_manager(tmp_path, port1)
    try:
        reg.add(name="A", ssh_host="a-alias", instance_id="a")
        await mgr.connect("a")
        await _stub_headers(mgr)

        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_websocket("a", "api/ws"):
                pass
        # Translated to an existing coded error, never a raw redirect follow.
        assert ei.value.code == "proxy_peer_unreachable"
        assert ei.value.http_status == 502
        # Give any (erroneously) issued follow a chance to land before asserting.
        await asyncio.sleep(0.05)
        assert second_hits == [], f"the handshake followed a redirect: {second_hits}"
    finally:
        await mgr.shutdown()
        await runner1.cleanup()
        await runner2.cleanup()


async def test_http_replacement_before_dial_is_rejected(tmp_path):
    """A forward replaced (same port, next generation) after credential selection
    but before the dial is refused under the lock; the real peer sees no request."""
    from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

    hits: list[str] = []

    async def any_path(request):
        hits.append(request.path)
        return web.Response(text="ok")

    runner, port = await _serve([("GET", "/status", any_path)])
    reg, mgr = _make_manager(tmp_path, port)
    try:
        reg.add(name="A", ssh_host="a-alias", instance_id="a")
        await mgr.connect("a")
        live = mgr.peer_forward_snapshot("a")
        landed = {"done": False}

        async def headers_then_rebuild(_inst, _url, _cookie, _stamp):
            # Fire exactly once, in the window between credential selection and the
            # connector's under-lock re-check: bump the generation while the port
            # stays fixed, modelling a same-port rebuild the lease never named.
            if not landed["done"]:
                landed["done"] = True
                mgr._tunnel_epoch["a"] = mgr._tunnel_epoch.get("a", 0) + 1
            return {}

        mgr._peer_headers_for = headers_then_rebuild  # type: ignore[assignment]

        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_request("a", "GET", "status", expected_forward=live):
                pass
        assert ei.value.code == "proxy_peer_not_connected"
        assert landed["done"] is True  # the rebuild really fired inside the window
        assert hits == []  # yet no request dialed the replacement peer
    finally:
        await mgr.shutdown()
        await runner.cleanup()


async def test_ws_replacement_before_dial_is_rejected(tmp_path):
    """The WebSocket sibling of the rebuild-before-dial refusal."""
    from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

    hits: list[str] = []

    async def any_ws(request):
        hits.append(request.path)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close()
        return ws

    runner, port = await _serve([("GET", "/ws", any_ws)])
    reg, mgr = _make_manager(tmp_path, port)
    try:
        reg.add(name="A", ssh_host="a-alias", instance_id="a")
        await mgr.connect("a")
        live = mgr.peer_forward_snapshot("a")
        landed = {"done": False}

        async def headers_then_rebuild(_inst, _url, _cookie, _stamp):
            if not landed["done"]:
                landed["done"] = True
                mgr._tunnel_epoch["a"] = mgr._tunnel_epoch.get("a", 0) + 1
            return {}

        mgr._peer_headers_for = headers_then_rebuild  # type: ignore[assignment]

        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_websocket("a", "ws", expected_forward=live):
                pass
        assert ei.value.code == "proxy_peer_not_connected"
        assert landed["done"] is True
        assert hits == []  # no handshake dialed the replacement peer
    finally:
        await mgr.shutdown()
        await runner.cleanup()


async def test_generation_fenced_connector_refuses_stale_generation_and_releases_lock(tmp_path):
    """A generation mismatch at connect time raises ``_PeerUnavailable`` and leaves
    the manager lock FREE — the lock-release contract, exercised against the
    installed aiohttp's connector seam.

    This is a behavioural test of the override, not an aiohttp version pin: it
    drives ``connect(req, traces, timeout)`` on the INSTALLED aiohttp (``connect``
    is aiohttp-internal, not public API) and asserts the fence refuses a
    superseded generation while releasing the lock, so a rebuild can proceed. The
    full end-to-end transport behaviour against the real manager is covered by the
    actual-manager tests above; this one isolates the fence + release path.
    """
    import aiohttp

    from kiro_crew.instances.ssh_tunnel_manager import (
        _GenerationFencedConnector,
        _PeerUnavailable,
    )

    lock = asyncio.Lock()
    conn = _GenerationFencedConnector(
        stamp_of=lambda _iid: (7, 3),  # live generation
        instance_id="x",
        stamp=(7, 2),  # the lease's (superseded) generation
        lock=lock,
    )
    try:
        with pytest.raises(_PeerUnavailable):
            await conn.connect(object(), traces=[], timeout=aiohttp.ClientTimeout())
        assert not lock.locked()  # released even though the dial was refused
    finally:
        await conn.close()
