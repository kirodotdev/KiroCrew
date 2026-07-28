"""Test for the /api/health liveness endpoint."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import core as core_mod


def _probe_req(remote: str = "127.0.0.1", headers=None) -> web.Request:
    req = MagicMock(spec=web.Request)
    req.remote = remote
    req.headers = headers or {}
    return req


@pytest.mark.asyncio
async def test_health_returns_ok_with_identity() -> None:
    """The payload carries identity fields (app, version) for the desktop
    shell's cross-app instance guard: nightly and production apps share
    ~/.kirocrew and the gateway port, so the shell must be able to tell
    WHICH KiroCrew-family gateway owns the port."""
    from kiro_crew import __version__

    resp = await core_mod.api_health(_probe_req())
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert body["app"] == "kirocrew"
    assert body["version"] == __version__


@pytest.mark.asyncio
async def test_remote_health_omits_build_identity() -> None:
    """Anonymous non-loopback probes expose only the liveness bit."""
    resp = await core_mod.api_health(_probe_req("203.0.113.9"))
    assert json.loads(resp.body) == {"ok": True}


@pytest.mark.asyncio
async def test_rebound_loopback_health_omits_build_identity() -> None:
    """DNS-rebinding hardening for the probe Host-check exemption.

    The probe paths bypass host_validation_middleware (orchestrators address
    pods by IP), so a rebound loopback request with a forged Host CAN reach
    this handler. The identity fields must then be withheld: check_host
    inside _liveness_payload is the second gate.
    """
    req = _probe_req(headers={"Host": "attacker.example"})
    req.app = {"allowed_origins": {"http://localhost:5476"}}
    resp = await core_mod.api_health(req)
    assert json.loads(resp.body) == {"ok": True}


@pytest.mark.asyncio
async def test_direct_local_health_with_served_host_keeps_identity() -> None:
    """The desktop cross-app guard path (loopback + real served Host) still
    receives identity after the check_host gate was added."""
    from kiro_crew import __version__

    req = _probe_req(headers={"Host": "127.0.0.1:5476"})
    req.app = {"allowed_origins": {"http://localhost:5476"}}
    resp = await core_mod.api_health(req)
    body = json.loads(resp.body)
    assert body["app"] == "kirocrew"
    assert body["version"] == __version__


@pytest.mark.asyncio
async def test_forwarded_loopback_health_omits_build_identity() -> None:
    """A reverse-proxied remote request is not treated as desktop-local."""
    resp = await core_mod.api_health(
        _probe_req(headers={"X-Forwarded-For": "203.0.113.9"})
    )
    assert json.loads(resp.body) == {"ok": True}


@pytest.mark.asyncio
async def test_live_alias_returns_ok() -> None:
    """/api/live is a liveness alias mirroring /api/health identity fields."""
    from kiro_crew import __version__

    resp = await core_mod.api_live(_probe_req())
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert body["app"] == "kirocrew"
    assert body["version"] == __version__


def _req_with_state(
    state, *, startup_complete: bool = True, host_allowed: bool = True
) -> web.Request:
    """Fake readiness request. ``host_allowed`` models the check_host gate:
    True = operator/orchestrator addressing an allowed host (full detail),
    False = disallowed Host reaching the probe exemption (generic body).
    Host/allowlist pairing mirrors the /api/health identity-gate tests."""
    if state is not None:
        state.ready = startup_complete
    req = MagicMock(spec=web.Request)
    req.headers = {
        "Host": "127.0.0.1:5476" if host_allowed else "attacker.example"
    }
    app = {"state": state} if state is not None else {}
    app["allowed_origins"] = {"http://localhost:5476"}
    req.app = app
    return req


@pytest.mark.asyncio
async def test_ready_disallowed_host_gets_only_the_ready_bit() -> None:
    """DNS-rebinding hardening for the probe Host-check exemption, readiness
    edition: /api/ready bypasses host_validation_middleware, so a
    disallowed-Host request CAN reach the handler. It must learn ONLY the
    ready boolean — the exact bit the status code already carries — never
    the startup/shutdown/subsystem markers."""
    state = MagicMock()
    state.sessions = MagicMock()
    resp = await core_mod.api_ready(_req_with_state(state, host_allowed=False))
    assert resp.status == 200
    assert json.loads(resp.body) == {"ready": True}

    resp = await core_mod.api_ready(
        _req_with_state(state, startup_complete=False, host_allowed=False)
    )
    assert resp.status == 503
    assert json.loads(resp.body) == {"ready": False}, (
        "unready detail (startup/checks markers) must be withheld from "
        "disallowed-Host callers"
    )


@pytest.mark.asyncio
async def test_ready_returns_200_after_startup_complete() -> None:
    """Readiness is 200 only after the final boot boundary is published."""
    state = MagicMock()
    state.sessions = MagicMock()
    resp = await core_mod.api_ready(_req_with_state(state))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ready"] is True
    assert body["startup_complete"] is True
    assert body["checks"] == {"state": True, "sessions": True}


@pytest.mark.asyncio
async def test_ready_returns_503_after_bind_until_startup_complete() -> None:
    """A bound server stays unready while post-bind startup work is running."""
    state = MagicMock()
    state.sessions = MagicMock()
    resp = await core_mod.api_ready(
        _req_with_state(state, startup_complete=False)
    )
    assert resp.status == 503
    body = json.loads(resp.body)
    assert body["ready"] is False
    assert body["startup_complete"] is False
    # State wiring alone must not make readiness vacuously true.
    assert body["checks"] == {"state": True, "sessions": True}


@pytest.mark.asyncio
async def test_ready_returns_503_before_state_wired() -> None:
    """Before startup wiring completes, readiness is 503 so orchestrators wait."""
    resp = await core_mod.api_ready(_req_with_state(None))
    assert resp.status == 503
    body = json.loads(resp.body)
    assert body["ready"] is False
    assert body["checks"]["state"] is False


@pytest.mark.asyncio
async def test_ready_returns_503_when_sessions_missing() -> None:
    """State present but SessionManager not yet attached => not ready."""
    state = MagicMock()
    state.sessions = None
    resp = await core_mod.api_ready(_req_with_state(state))
    assert resp.status == 503
    body = json.loads(resp.body)
    assert body["ready"] is False
    assert body["checks"] == {"state": True, "sessions": False}


def test_probes_are_auth_bypassed() -> None:
    """Probe endpoints must be reachable without a token (rec #6)."""
    import kiro_crew.dashboard.token_auth as ta

    for path in ("/api/health", "/api/live", "/api/ready"):
        assert path in ta._BYPASS_EXACT


@pytest.mark.asyncio
async def test_public_probe_contract_frozen_minimal_anonymous_surface_and_statuses() -> None:
    """Frozen public contract: auth, minimal payloads, and lifecycle statuses.

    External orchestrators may depend on anonymous access, exact liveness
    payloads, and the readiness status plus ``ready`` boolean. Readiness
    diagnostics are intentionally not frozen so internal checks can evolve.
    """
    import kiro_crew.dashboard.token_auth as ta

    paths = ("/api/health", "/api/live", "/api/ready")
    assert all(path in ta._BYPASS_EXACT for path in paths)

    remote = _probe_req("203.0.113.9")
    for handler in (core_mod.api_health, core_mod.api_live):
        response = await handler(remote)
        assert response.status == 200
        assert json.loads(response.body) == {"ok": True}

    state = MagicMock()
    state.sessions = MagicMock()
    serving = _req_with_state(state)
    serving.remote = "203.0.113.9"
    serving.headers = {}
    response = await core_mod.api_ready(serving)
    assert response.status == 200
    assert json.loads(response.body)["ready"] is True

    starting = _req_with_state(state, startup_complete=False)
    starting.remote = "203.0.113.9"
    starting.headers = {}
    response = await core_mod.api_ready(starting)
    assert response.status == 503
    assert json.loads(response.body)["ready"] is False

    shutdown = asyncio.Event()
    shutdown.set()
    state.ready = True
    with patch("kiro_crew.shutdown_event", shutdown):
        response = await core_mod.api_ready(serving)
        assert response.status == 503
        assert json.loads(response.body)["ready"] is False
        response = await core_mod.api_live(remote)
        assert response.status == 200
        assert json.loads(response.body) == {"ok": True}


# ── Graceful-shutdown lifecycle (rec #6) ─────────────────────────────────────
# readiness must reflect the ACTUAL lifecycle state, not just "subsystems wired".
# The process-wide shutdown_event is the single trigger for graceful stop
# (SIGTERM/SIGINT handler AND POST /api/shutdown both set it). api_ready does a
# function-local `from kiro_crew import shutdown_event`, so patching the source
# attribute swaps the event the handler observes.


@pytest.mark.asyncio
async def test_ready_returns_503_during_shutdown() -> None:
    """During graceful shutdown, readiness flips to 503 EVEN THOUGH every
    subsystem is still wired — so a load balancer drains traffic before the
    socket closes. The 503 is purely lifecycle-driven: the subsystem checks
    still report healthy."""
    ev = asyncio.Event()
    ev.set()  # a stop has been requested
    state = MagicMock()
    state.sessions = MagicMock()
    with patch("kiro_crew.shutdown_event", ev):
        resp = await core_mod.api_ready(_req_with_state(state))
    assert resp.status == 503
    body = json.loads(resp.body)
    assert body["ready"] is False
    assert body["shutting_down"] is True
    # Subsystems remain wired — readiness dropped only because we are draining.
    assert body["checks"] == {"state": True, "sessions": True}


@pytest.mark.asyncio
async def test_ready_omits_shutdown_marker_while_serving() -> None:
    """When not shutting down, the payload carries no shutdown marker and the
    probe reports ready."""
    ev = asyncio.Event()  # never set → not shutting down
    state = MagicMock()
    state.sessions = MagicMock()
    with patch("kiro_crew.shutdown_event", ev):
        resp = await core_mod.api_ready(_req_with_state(state))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ready"] is True
    assert "shutting_down" not in body


@pytest.mark.asyncio
async def test_ready_shutdown_precedes_subsystem_state() -> None:
    """Shutdown takes precedence: a draining instance is never advertised as
    ready, even if it somehow still looks not-fully-wired. This proves the gate
    ordering — shutdown short-circuits the readiness decision."""
    ev = asyncio.Event()
    ev.set()
    # State missing AND shutting down: still 503, and the shutdown marker is set.
    with patch("kiro_crew.shutdown_event", ev):
        resp = await core_mod.api_ready(_req_with_state(None))
    assert resp.status == 503
    body = json.loads(resp.body)
    assert body["ready"] is False
    assert body["shutting_down"] is True
    assert body["checks"]["state"] is False


@pytest.mark.asyncio
async def test_live_stays_200_during_shutdown() -> None:
    """Liveness is distinct from readiness: the process is still alive during
    graceful shutdown, so /api/live stays 200 while /api/ready goes 503. This
    keeps a liveness-based supervisor from killing the process mid-drain."""
    ev = asyncio.Event()
    ev.set()
    with patch("kiro_crew.shutdown_event", ev):
        resp = await core_mod.api_live(_probe_req())
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ok"] is True


@pytest.mark.asyncio
async def test_ready_recovers_when_shutdown_flag_cleared() -> None:
    """Readiness is driven live by the event: clearing it (fully wired, not
    draining) returns to 200 with no shutdown marker. Guards against a sticky
    'once-503-always-503' regression."""
    ev = asyncio.Event()
    state = MagicMock()
    state.sessions = MagicMock()
    with patch("kiro_crew.shutdown_event", ev):
        ev.set()
        draining = await core_mod.api_ready(_req_with_state(state))
        assert draining.status == 503

        ev.clear()
        serving = await core_mod.api_ready(_req_with_state(state))
    assert serving.status == 200
    body = json.loads(serving.body)
    assert body["ready"] is True
    assert "shutting_down" not in body


# ── Probe Host-exemption through the REAL middleware chain ───────────────────
# The tests above call handlers directly, which cannot detect a revert of the
# middleware exemption itself (a reverted middleware 403s the probe BEFORE the
# handler runs). These tests mount the SHARED factory the servers install
# (server._make_host_validation_middleware — single source of truth for the
# barrier and its PROBE_PATHS carve-out) into a real aiohttp app and drive
# real HTTP requests with a DISALLOWED Host header across the wire.


def _host_barrier_app() -> web.Application:
    from kiro_crew.dashboard import server as server_mod

    app = web.Application(
        middlewares=[server_mod._make_host_validation_middleware("dashboard_user")]
    )
    app["allowed_origins"] = {"http://localhost:5476"}
    app.router.add_get("/api/health", core_mod.api_health)
    app.router.add_get("/api/live", core_mod.api_live)
    app.router.add_get("/api/ready", core_mod.api_ready)

    async def protected(_req: web.Request) -> web.Response:
        return web.json_response({"secret": True})

    app.router.add_get("/api/sessions", protected)
    return app


@pytest.mark.asyncio
async def test_disallowed_host_probes_pass_through_middleware_chain() -> None:
    """An orchestrator probe with a Host outside the allowlist (pod IP,
    container IP, LB VIP — never in the allowlist by construction) must
    reach the probe handlers THROUGH the middleware. Reverting the
    PROBE_PATHS exemption in the shared factory fails this test with a 403.
    """
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(_host_barrier_app())) as client:
        for path in ("/api/health", "/api/live"):
            resp = await client.get(path, headers={"Host": "10.42.7.13:5476"})
            assert resp.status == 200, f"{path} must be probe-reachable"
            # And the identity gate holds on this path: forged/unknown Host ⇒
            # liveness bit only, no build fingerprint.
            assert await resp.json() == {"ok": True}
        # /api/ready is exempt too: it must pass the barrier (its 503-until-
        # ready status is orthogonal to the Host exemption under test).
        resp = await client.get("/api/ready", headers={"Host": "10.42.7.13:5476"})
        assert resp.status in (200, 503)


@pytest.mark.asyncio
async def test_disallowed_host_non_probe_still_403s_in_middleware_chain() -> None:
    """The exemption is EXACTLY the three probe paths: any other route with a
    disallowed Host keeps the DNS-rebinding 403. Guards against the carve-out
    silently widening."""
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(_host_barrier_app())) as client:
        resp = await client.get(
            "/api/sessions", headers={"Host": "attacker.example"}
        )
        assert resp.status == 403
        assert "Host header not allowed" in await resp.text()


@pytest.mark.asyncio
async def test_allowed_host_non_probe_passes_host_barrier() -> None:
    """Coherence check: the barrier only rejects disallowed Hosts — an allowed Host
    reaches the handler (this app mounts no token auth; the real servers
    layer token_auth_middleware separately)."""
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(_host_barrier_app())) as client:
        resp = await client.get(
            "/api/sessions", headers={"Host": "localhost:5476"}
        )
        assert resp.status == 200


def test_both_servers_install_the_shared_host_barrier() -> None:
    """Wiring pin: BOTH entrypoints must build their Host barrier from the
    shared factory (the single exemption point the chain tests above cover),
    and neither may re-grow a private inline copy that could drop or widen
    the exemption independently."""
    import inspect

    from kiro_crew.dashboard import server as server_mod

    dashboard_src = inspect.getsource(server_mod.start_dashboard)
    api_src = inspect.getsource(server_mod.start_api_server)
    for src, name in ((dashboard_src, "start_dashboard"), (api_src, "start_api_server")):
        assert "_make_host_validation_middleware(" in src, (
            f"{name} no longer uses the shared host-validation factory"
        )
        assert "async def host_validation_middleware" not in src, (
            f"{name} re-introduced an inline host-validation middleware; "
            "keep the shared factory as the single exemption point"
        )


def test_both_servers_warm_the_kiro_readiness_probe() -> None:
    """Wiring pin: BOTH entrypoints must warm the Kiro readiness probe at boot.

    Without the warm-up the cold probe (two sandboxed kiro-cli subprocesses) runs
    on the dashboard's FIRST status request instead, which is what left returning
    users staring at pre-resolution setup chrome. Nothing else fails if these
    calls are dropped in an upstream sync, so pin them.
    """
    import inspect

    from kiro_crew.dashboard import server as server_mod

    for func, name in (
        (server_mod.start_dashboard, "start_dashboard"),
        (server_mod.start_api_server, "start_api_server"),
    ):
        assert "warm_up()" in inspect.getsource(func), (
            f"{name} no longer warms the Kiro readiness probe at startup"
        )


@pytest.mark.asyncio
async def test_ready_never_waits_on_the_kiro_cli_check() -> None:
    """Readiness must not depend on Kiro CLI state.

    Kiro readiness gates starting a TURN, not serving the dashboard — a
    signed-out user is meant to get in and see the reauthentication banner — so
    an unresolved or failing Kiro probe must never hold up readiness (or, by
    extension, first paint).
    """
    for service in (
        None,
        SimpleNamespace(),
        SimpleNamespace(cached_ready=False),
        SimpleNamespace(warm_up_settled=False),
    ):
        state = MagicMock()
        state.sessions = MagicMock()
        state.kiro_prerequisite_service = service

        resp = await core_mod.api_ready(_req_with_state(state))
        assert resp.status == 200, f"{service!r} must not withhold readiness"
        body = json.loads(resp.body)
        assert body["ready"] is True
        assert "kiro_probe" not in body["checks"]


def test_api_server_resolves_bind_address_via_shared_helper() -> None:
    """Wiring pin for the container bind override: start_api_server must
    resolve its TCP bind through bind_address_for (KIROCREW_BIND-aware,
    itself covered in test_dashboard_origin.py) rather than a hardcoded
    loopback literal — otherwise `gateway --slack-only` in the official
    image binds loopback and is unreachable through a published port."""
    import inspect

    from kiro_crew.dashboard import server as server_mod

    src = inspect.getsource(server_mod.start_api_server)
    assert "bind_address_for(local_only)" in src
    assert 'TCPSite(runner, "127.0.0.1"' not in src
