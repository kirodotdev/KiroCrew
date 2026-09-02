"""Test for the /api/health liveness endpoint."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import core as core_mod
from kiro_crew.dashboard.origin import mark_audit_claimed


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
    resp = await core_mod.api_health(_probe_req(headers={"X-Forwarded-For": "203.0.113.9"}))
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
    req.headers = {"Host": "127.0.0.1:5476" if host_allowed else "attacker.example"}
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
        "unready detail (startup/checks markers) must be withheld from " "disallowed-Host callers"
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
    resp = await core_mod.api_ready(_req_with_state(state, startup_complete=False))
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
        resp = await client.get("/api/sessions", headers={"Host": "attacker.example"})
        assert resp.status == 403
        assert "Host header not allowed" in await resp.text()


@pytest.mark.asyncio
async def test_allowed_host_non_probe_passes_host_barrier() -> None:
    """Coherence check: the barrier only rejects disallowed Hosts — an allowed Host
    reaches the handler (this app mounts no token auth; the real servers
    layer token_auth_middleware separately)."""
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(_host_barrier_app())) as client:
        resp = await client.get("/api/sessions", headers={"Host": "localhost:5476"})
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
        assert (
            "_make_host_validation_middleware(" in src
        ), f"{name} no longer uses the shared host-validation factory"
        assert "async def host_validation_middleware" not in src, (
            f"{name} re-introduced an inline host-validation middleware; "
            "keep the shared factory as the single exemption point"
        )


def test_both_servers_install_the_shared_deny_audit_boundary() -> None:
    """Wiring pin, mirroring the Host-barrier pin: BOTH entrypoints must install
    the deny-audit boundary from the shared factory.

    The boundary is what makes a pre-audit refusal audited by POSITION. Installed
    on one entrypoint only, the headless server would silently keep the old
    per-site guarantee while the dashboard had the structural one — the exact
    drift the shared factories exist to prevent."""
    import inspect

    from kiro_crew.dashboard import server as server_mod

    for func, name in (
        (server_mod.start_dashboard, "start_dashboard"),
        (server_mod.start_api_server, "start_api_server"),
    ):
        src = inspect.getsource(func)
        assert (
            "_make_deny_audit_middleware(" in src
        ), f"{name} no longer installs the shared deny-audit boundary"
        assert (
            "deny_audit_middleware," in src
        ), f"{name} builds the deny-audit boundary but never registers it"


def test_every_middleware_denial_is_audited_off_the_loop() -> None:
    """Wiring pin: every middleware that refuses BEFORE ``sel_audit_middleware``
    must route its audit through the shared ``_audit_denied`` helper.

    That helper owns two properties which are easy to omit at a new deny site
    and invisible when omitted: the write runs OFF the event loop (the first
    ``sel()`` of a process constructs the log — trust-dir creation, key
    validation, an ``icacls`` subprocess on Windows), and it is best-effort (an
    audit that raises must not turn the 403 into a 500).

    A bare raise with no audit at all is no longer a silent failure — the
    deny-audit boundary records it by position (see the chain tests below) — so
    these calls are what keeps each record's own reason detail, not what keeps
    the record. Both halves are still worth pinning: the boundary's generic
    record names the status and nothing about WHY.
    """
    import importlib
    import inspect

    from kiro_crew.dashboard import server as server_mod

    helper = inspect.getsource(server_mod._audit_denied)
    assert (
        "asyncio.to_thread" in helper
    ), "_audit_denied no longer offloads the SEL write off the event loop"
    assert "except Exception" in helper, "_audit_denied is no longer best-effort"

    # Both pre-audit barriers are built by a shared factory, so the deny arms live
    # there rather than in either entrypoint.
    for func, name in (
        (server_mod._make_host_validation_middleware, "host_validation"),
        (server_mod._make_csrf_middleware, "csrf"),
    ):
        src = inspect.getsource(func)
        assert (
            "_audit_denied(" in src
        ), f"{name} has a deny arm that no longer audits via _audit_denied"
        assert 'outcome="denied"' not in src, (
            f"{name} re-grew a hand-rolled denial audit; route it through "
            "_audit_denied so the off-loop and best-effort properties hold "
            "(sel_audit_middleware's ok/error request audit is unaffected)"
        )

    # The entrypoints themselves must not grow a private denial audit: a deny arm
    # added there would bypass both factories AND the helper's two properties.
    for func, name in (
        (server_mod.start_dashboard, "start_dashboard"),
        (server_mod.start_api_server, "start_api_server"),
    ):
        assert 'outcome="denied"' not in inspect.getsource(func), (
            f"{name} re-grew a hand-rolled denial audit; route it through "
            "_audit_denied so the off-loop and best-effort properties hold "
            "(sel_audit_middleware's ok/error request audit is unaffected)"
        )

    # The positional guarantee behind those enrichment calls: both audit
    # middlewares must CLAIM the requests they actually log, which is what keeps
    # the deny-audit boundary from doubling their own ``outcome="error"`` entry
    # for a refusal raised below them (see origin.AUDIT_CLAIMED_KEY). The claim
    # must stay INSIDE the logged branch: claiming unconditionally would promise
    # an audit for requests this middleware logs nowhere, and a cross-origin
    # WebSocket GET refused in its handler would be silently unaudited again.
    for func, name in (
        (server_mod.start_dashboard, "start_dashboard"),
        (server_mod.start_api_server, "start_api_server"),
    ):
        src = inspect.getsource(func)
        assert "mark_audit_claimed(request)" in src, (
            f"{name}'s sel_audit_middleware no longer claims the requests it "
            "logs; the deny-audit boundary would double-record refusals it owns"
        )
        claim_at = src.index("mark_audit_claimed(request)")
        guard_at = src.index('.startswith("/api/")')
        assert guard_at < claim_at, (
            f"{name}'s sel_audit_middleware claims before its method/path guard; "
            "an unlogged request would be claimed and its refusal lost"
        )

    # Every deny site that writes its OWN audit record must claim, or the
    # boundary adds a second, generic entry for the same refusal. These two do;
    # ws.py deliberately does not, because it audits nothing of its own.
    for module, path in (
        ("kiro_crew.dashboard.stt_stream", "stt_stream"),
        ("kiro_crew.dashboard.handlers.terminal", "handlers/terminal"),
    ):
        mod = importlib.import_module(module)
        src = inspect.getsource(mod)
        assert "mark_audit_claimed(request)" in src, (
            f"{path} logs its own WebSocket-origin denial but no longer claims "
            "the request; the deny-audit boundary will double-record it"
        )


# ── The deny-audit boundary, through a REAL middleware chain ─────────────────
# The pin above proves the three KNOWN deny sites still enrich their record. It
# cannot prove the property that matters for a FOURTH one: that a refusal raised
# before the audit middleware is recorded because of where the boundary sits,
# not because someone remembered the helper. These drive that through real HTTP.


class _SelSpy:
    """Collect ``log_api_access`` calls and the thread each ran on."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.threads: list[str] = []

    def log_api_access(self, **kwargs) -> None:
        import threading

        self.calls.append(kwargs)
        self.threads.append(threading.current_thread().name)

    def denials(self) -> list[dict]:
        return [c for c in self.calls if c.get("outcome") == "denied"]


def _boundary_app(*inner: object) -> web.Application:
    """The deny-audit boundary outermost, then whatever barrier is under test."""
    from kiro_crew.dashboard import server as server_mod

    app = web.Application(
        middlewares=[server_mod._make_deny_audit_middleware("dashboard_user"), *inner]
    )
    app["allowed_origins"] = {"http://localhost:5476"}

    async def ok(_req: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_get("/api/sessions", ok)
    app.router.add_post("/api/sessions", ok)
    return app


@pytest.mark.asyncio
async def test_a_forgetful_pre_audit_refusal_is_still_audited_by_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fourth deny site, written the way the pin cannot catch.

    This barrier raises a bare 403 and audits nothing — exactly the omission
    that used to leave a refusal in no log at all, because
    ``sel_audit_middleware`` is registered inner to it. The record must appear
    anyway, off the event loop, and the 403 must still reach the client.
    """
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard import server as server_mod

    spy = _SelSpy()
    monkeypatch.setattr(server_mod, "sel", lambda: spy)

    @web.middleware
    async def forgetful_barrier(request: web.Request, handler: object) -> web.StreamResponse:
        raise web.HTTPForbidden(text="nope")

    async with TestClient(TestServer(_boundary_app(forgetful_barrier))) as client:
        resp = await client.get("/api/sessions")
        assert resp.status == 403
        assert await resp.text() == "nope"

    denials = spy.denials()
    assert len(denials) == 1, f"the refusal was not audited: {spy.calls}"
    assert denials[0]["operation"] == "GET /api/sessions"
    assert denials[0]["resources"] == "/api/sessions"
    assert "403" in denials[0]["error"]
    # Off the loop, via the shared helper — not an inline sel() call on the
    # event loop, which the first sel() of a process would block on.
    assert spy.threads[0] != "MainThread"


@pytest.mark.asyncio
async def test_a_barrier_that_audits_itself_is_not_recorded_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real Host barrier keeps its own richer record and gains no second one.

    Enrichment and guarantee must not stack: the boundary is a backstop for
    unclaimed refusals, so a site that already called ``_audit_denied`` produces
    exactly one record — and it is the site's, naming the offending header.
    """
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard import server as server_mod

    spy = _SelSpy()
    monkeypatch.setattr(server_mod, "sel", lambda: spy)

    app = _boundary_app(server_mod._make_host_validation_middleware("dashboard_user"))
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/sessions", headers={"Host": "attacker.example"})
        assert resp.status == 403

    denials = spy.denials()
    assert len(denials) == 1, f"the refusal was recorded twice: {denials}"
    assert "host header not allowed" in denials[0]["error"]


@pytest.mark.asyncio
async def test_a_refusal_the_audit_middleware_logs_itself_is_not_doubled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 the audit middleware already recorded gains no second entry.

    On a request inside its method/path set, ``sel_audit_middleware`` logs a
    handler's refusal as ``outcome="error"`` and claims it, so the boundary
    stays out. Without the claim the same refusal would carry two records under
    two different outcomes.
    """
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard import server as server_mod

    spy = _SelSpy()
    monkeypatch.setattr(server_mod, "sel", lambda: spy)

    @web.middleware
    async def claims_like_the_audit_middleware(
        request: web.Request, handler: object
    ) -> web.StreamResponse:
        mark_audit_claimed(request)
        return await handler(request)  # type: ignore[operator]

    @web.middleware
    async def handler_refuses(request: web.Request, handler: object) -> web.StreamResponse:
        raise web.HTTPForbidden(text="handler said no")

    app = _boundary_app(claims_like_the_audit_middleware, handler_refuses)
    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/api/sessions")).status == 403

    assert spy.denials() == [], f"a claimed refusal was recorded twice: {spy.calls}"


@pytest.mark.asyncio
async def test_a_refusal_the_audit_middleware_does_not_log_is_recorded_here(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half the claim must NOT swallow.

    ``sel_audit_middleware`` logs only mutating ``/api/`` requests on the
    dashboard, so a cross-origin WebSocket upgrade (a GET) refused in its
    handler is logged nowhere by it. Claiming such a request would promise an
    audit nobody writes; leaving it unclaimed is what makes the boundary record
    it.
    """
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard import server as server_mod

    spy = _SelSpy()
    monkeypatch.setattr(server_mod, "sel", lambda: spy)

    @web.middleware
    async def audits_only_mutating_api(request: web.Request, handler: object) -> web.StreamResponse:
        if request.method in {"POST", "PUT", "DELETE", "PATCH"} and request.path.startswith(
            "/api/"
        ):
            mark_audit_claimed(request)
        return await handler(request)  # type: ignore[operator]

    @web.middleware
    async def ws_origin_refuses(request: web.Request, handler: object) -> web.StreamResponse:
        raise web.HTTPForbidden(text="WebSocket origin not allowed")

    app = _boundary_app(audits_only_mutating_api, ws_origin_refuses)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/sessions")).status == 403

    denials = spy.denials()
    assert len(denials) == 1, f"an unlogged GET refusal was lost: {spy.calls}"
    assert denials[0]["operation"] == "GET /api/sessions"


@pytest.mark.asyncio
async def test_the_record_names_the_authenticated_caller_not_the_static_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attribution: a refusal raised after auth is filed under who was refused.

    ``token_auth_middleware`` runs inner to this boundary and puts an identity on
    the request, so a WebSocket-origin 403 that reaches the boundary carries one.
    Recording the factory's static label would file an app's or a user's refusal
    under ``dashboard_user``. ``request["app"] == ""`` is positive proof of the
    dashboard user, so an empty app falls through to the user, not to the label.
    """
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard import server as server_mod

    def _authed(identity: dict) -> object:
        @web.middleware
        async def sets_identity(request: web.Request, handler: object) -> web.StreamResponse:
            for k, v in identity.items():
                request[k] = v
            return await handler(request)  # type: ignore[operator]

        return sets_identity

    @web.middleware
    async def ws_origin_refuses(request: web.Request, handler: object) -> web.StreamResponse:
        raise web.HTTPForbidden(text="WebSocket origin not allowed")

    for identity, expected in (
        ({"app": "code-review-sage", "user": "u-1"}, "code-review-sage"),
        ({"app": "", "user": "alice"}, "alice"),
        ({}, "dashboard_user"),
    ):
        spy = _SelSpy()
        monkeypatch.setattr(server_mod, "sel", lambda s=spy: s)
        app = _boundary_app(_authed(identity), ws_origin_refuses)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/sessions")).status == 403
        denials = spy.denials()
        assert len(denials) == 1, f"{identity!r}: {spy.calls}"
        assert denials[0]["caller"] == expected, f"{identity!r} recorded as {denials[0]['caller']}"


def test_every_self_auditing_raised_refusal_claims_the_request() -> None:
    """Exhaustive: enumerate every raised 401/403 and check its claim state.

    The boundary records an unclaimed raised refusal, so a deny site that writes
    its own record must claim or be double-logged. Rather than trusting a list,
    walk the tree: each ``raise web.HTTPForbidden``/``HTTPUnauthorized`` either
    audits itself and claims, or audits nothing and is covered by position.
    """
    import re
    from pathlib import Path

    import kiro_crew

    root = Path(kiro_crew.__file__).parent
    raise_re = re.compile(r"raise web\.HTTP(?:Forbidden|Unauthorized)\(")
    found = 0
    for path in sorted(root.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not raise_re.search(line):
                continue
            found += 1
            # The deny site's own arm: the 12 lines above the raise.
            arm = "\n".join(lines[max(0, i - 12) : i])
            audits = "log_api_access(" in arm or "_emit_guard_audit(" in arm
            claims = "mark_audit_claimed(" in arm
            if audits and not claims:
                raise AssertionError(
                    f"{path.relative_to(root)}:{i + 1} writes its own denial audit but does "
                    "not claim the request; the deny-audit boundary will double-record it. "
                    "Call origin.mark_audit_claimed(request) before the raise."
                )
    assert found >= 5, f"the raise-site walk found only {found} sites; the regex has drifted"


@pytest.mark.asyncio
async def test_the_boundary_ignores_outcomes_that_are_not_refusals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a permission decision is a denial.

    A 302 from host canonicalization and a 404 from routing are raised as
    exceptions by aiohttp too; auditing those as denials would bury the real
    refusals in noise.
    """
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard import server as server_mod

    spy = _SelSpy()
    monkeypatch.setattr(server_mod, "sel", lambda: spy)

    @web.middleware
    async def redirects(request: web.Request, handler: object) -> web.StreamResponse:
        if request.path == "/api/redirect":
            raise web.HTTPFound(location="/api/sessions")
        return await handler(request)  # type: ignore[operator]

    async with TestClient(TestServer(_boundary_app(redirects))) as client:
        assert (await client.get("/api/redirect", allow_redirects=False)).status == 302
        assert (await client.get("/api/does-not-exist")).status == 404
        assert (await client.get("/api/sessions")).status == 200

    assert spy.denials() == [], f"a non-refusal was audited as a denial: {spy.calls}"


@pytest.mark.asyncio
async def test_an_audit_failure_never_turns_the_refusal_into_a_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort, end to end: losing the record must not lose the denial.

    A trust root too short to sign the chain makes ``sel()`` construction raise.
    The client must still get the 403.
    """
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard import server as server_mod

    def _explode():
        raise RuntimeError("trust root unusable")

    monkeypatch.setattr(server_mod, "sel", _explode)

    @web.middleware
    async def forgetful_barrier(request: web.Request, handler: object) -> web.StreamResponse:
        raise web.HTTPForbidden(text="nope")

    async with TestClient(TestServer(_boundary_app(forgetful_barrier))) as client:
        resp = await client.get("/api/sessions")
        assert resp.status == 403
        assert await resp.text() == "nope"


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
        assert "warm_up()" in inspect.getsource(
            func
        ), f"{name} no longer warms the Kiro readiness probe at startup"


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
