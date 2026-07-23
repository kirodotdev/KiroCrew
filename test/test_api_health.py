"""Test for the /api/health liveness endpoint."""

from __future__ import annotations

import asyncio
import json
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


def _req_with_state(state, *, startup_complete: bool = True) -> web.Request:
    if state is not None:
        state.ready = startup_complete
    req = MagicMock(spec=web.Request)
    req.app = {"state": state} if state is not None else {}
    return req


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
