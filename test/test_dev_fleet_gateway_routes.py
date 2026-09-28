"""Dev Fleet's in-gateway cutover routes and pointer-state broker.

Why the cutover lives in the gateway process, in one paragraph: ``live_target.json``
decides which checkout the gateway ``execve``s into next, so it is bind-masked from
every sandboxed process. Dev Fleet's backend is one, and its ``npm ci`` / build
children share its namespace (a nested sandbox is denied by design) — a carve-out for
the backend was a carve-out for any worktree's lifecycle script. So the pointer stays
masked, the cutover moved into the gateway behind the dashboard OWNER's own request,
and the backend reads pointer state through ``GET /api/apps/dev-fleet/live-target``
with its App Kit token, which is allowed to read and refused every write.

These tests pin the boundary: who may write, who may read, that the backend never
serves the pointer routes any more, and that a broker outage is a loud refusal on the
destructive paths rather than a silent "nothing is live".
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import sys
import time
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins import BUILTIN_NAMES
from kiro_crew.apps.builtins.dev_fleet import gateway_routes, live, pointer_broker, server

pytestmark = pytest.mark.asyncio


class TestRegistration:
    def test_dev_fleet_is_a_builtin_with_in_gateway_routes(self) -> None:
        """The ``BUILTIN_NAMES`` loop is what mounts ``register_routes`` at startup; the
        package hook composes the agent pod routes with these gateway routes, so what is
        pinned is that a call through the PACKAGE mounts every gateway route."""
        assert "dev_fleet" in BUILTIN_NAMES
        mod = importlib.import_module("kiro_crew.apps.builtins.dev_fleet")
        via_package = web.Application()
        mod.register_routes(via_package)
        direct = web.Application()
        gateway_routes.register_routes(direct)
        mounted = {(r.method, r.resource.canonical) for r in via_package.router.routes()}
        expected = {(r.method, r.resource.canonical) for r in direct.router.routes()}
        assert expected <= mounted, sorted(expected - mounted)

    def test_register_routes_mounts_exactly_the_six(self) -> None:
        app = web.Application()
        gateway_routes.register_routes(app)
        mounted = sorted(
            (r.method, r.resource.canonical) for r in app.router.routes() if r.method != "HEAD"
        )
        assert mounted == [
            ("DELETE", "/api/apps/dev-fleet/live-target/removal-lease"),
            ("GET", "/api/apps/dev-fleet/live-target"),
            ("POST", "/api/apps/dev-fleet/live-target/removal-lease"),
            ("POST", "/api/apps/dev-fleet/make-live"),
            ("POST", "/api/apps/dev-fleet/restart-gateway"),
            ("PUT", "/api/apps/dev-fleet/live-target/removal-lease"),
        ]

    def test_the_backend_no_longer_serves_the_pointer_routes(self) -> None:
        """The sandboxed backend must not carry a route that touches the pointer."""
        paths = {r.resource.canonical for r in server.create_app().router.routes()}
        assert "/api/make-live" not in paths
        assert "/api/restart-gateway" not in paths
        assert not hasattr(server.http_api, "api_dev_fleet_make_live")
        assert not hasattr(server.http_api, "api_dev_fleet_restart_gateway")


# --- a gateway stand-in: the token middleware's contract, not its implementation ---


def _make_app(*, principal_app: str | None, user: str = "owner", owner_id: str = "owner"):
    """An aiohttp app with the routes mounted and ``request['app']`` / ``request['user']``
    pre-stamped the way ``token_auth_middleware`` does: ``""`` for a dashboard user,
    the app name for an app token, ABSENT when unauthenticated."""

    @web.middleware
    async def _stamp(request: web.Request, handler):
        if principal_app is not None:
            request["app"] = principal_app
            request["user"] = user
        return await handler(request)

    app = web.Application(middlewares=[_stamp])

    class _State:
        pass

    state = _State()
    state.owner_id = owner_id  # type: ignore[attr-defined]
    app["state"] = state
    gateway_routes.register_routes(app)
    return app


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(gateway_routes, "is_app_enabled", lambda name: name == "dev-fleet")


@pytest.fixture
def recorded(monkeypatch):
    """Replace the cutover/restart/state functions with recorders."""
    calls: dict[str, Any] = {}

    async def _make_live(path, dry_run=False, expected_staged=None, *, undo=False):
        calls["make_live"] = (path, dry_run, expected_staged)
        calls["make_live_undo"] = undo
        return {"ok": True, "cutover": True, "target": path}

    async def _restart():
        calls["restart"] = True
        return {"ok": True, "start_id": "1"}

    async def _state(*, fresh=False):
        calls["state_fresh"] = fresh
        return live.PointerState(
            live="/wt/live",
            staged="/wt/staged",
            staged_cancel_available=True,
            previous="/wt/previous",
        )

    async def _ensure():
        calls["discovered"] = True

    monkeypatch.setattr(live, "_make_live", _make_live)
    monkeypatch.setattr(live, "_restart_gateway", _restart)
    monkeypatch.setattr(live, "pointer_state", _state)
    monkeypatch.setattr(gateway_routes.repository, "ensure_main_repo_discovered", _ensure)
    monkeypatch.setattr(gateway_routes, "sel", lambda: _NullSel())
    return calls


class _NullSel:
    def log_api_access(self, **kwargs):
        pass


async def _client(app) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


class TestTheWriteRoutesAreOwnerOnly:
    async def test_owner_human_can_make_live(self, enabled, recorded) -> None:
        client = await _client(_make_app(principal_app=""))
        try:
            resp = await client.post(
                "/api/apps/dev-fleet/make-live",
                json={"path": "/wt/next", "dry_run": False, "expected_staged": None},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert recorded["make_live"] == ("/wt/next", False, None)
            assert recorded["discovered"] is True, "the gateway must discover the repo first"
        finally:
            await client.close()

    async def test_an_unconfigured_checkout_is_a_409_not_a_500(
        self, enabled, recorded, monkeypatch
    ) -> None:
        """Discovery can finish with NO checkout resolved (a packaged install with Dev
        Fleet enabled and nothing configured); the cutover's own worktree lookup then
        raises ``RepoUnavailable``. That is the documented setup state, answered with
        the same 409 a failed discovery gets — never an unhandled 500."""

        async def _raise(path, dry_run, *, expected_staged=None, undo=False):
            raise gateway_routes.repository.RepoNotConfigured("no checkout configured")

        monkeypatch.setattr(live, "_make_live", _raise)
        client = await _client(_make_app(principal_app=""))
        try:
            resp = await client.post(
                "/api/apps/dev-fleet/make-live",
                json={"path": "/wt/next", "dry_run": False, "expected_staged": None},
            )
            assert resp.status == 409
            body = await resp.json()
            assert body["ok"] is False and body["code"] == "repo_not_configured"
        finally:
            await client.close()

    async def test_dev_fleets_own_app_token_is_refused_for_the_write(
        self, enabled, recorded
    ) -> None:
        """The credential the backend holds is readable by its build children, so it
        buys no write — not even for the app that owns the route."""
        client = await _client(_make_app(principal_app="dev-fleet"))
        try:
            resp = await client.post("/api/apps/dev-fleet/make-live", json={"path": "/wt/next"})
            assert resp.status == 403
            assert "make_live" not in recorded
            resp = await client.post("/api/apps/dev-fleet/restart-gateway", json={})
            assert resp.status == 403
            assert "restart" not in recorded
        finally:
            await client.close()

    async def test_another_apps_token_is_refused_for_the_write(self, enabled, recorded) -> None:
        client = await _client(_make_app(principal_app="md-notebook"))
        try:
            resp = await client.post("/api/apps/dev-fleet/make-live", json={"path": "/wt/next"})
            assert resp.status == 403
            assert "make_live" not in recorded
        finally:
            await client.close()

    async def test_a_non_owner_human_is_refused(self, enabled, recorded) -> None:
        client = await _client(_make_app(principal_app="", user="guest", owner_id="owner"))
        try:
            resp = await client.post("/api/apps/dev-fleet/make-live", json={"path": "/wt/next"})
            assert resp.status in (401, 403)
            assert "make_live" not in recorded
        finally:
            await client.close()

    async def test_body_contract_is_unchanged_from_the_backend_route(
        self, enabled, recorded
    ) -> None:
        client = await _client(_make_app(principal_app=""))
        try:
            r = await client.post("/api/apps/dev-fleet/make-live", json={"path": ""})
            assert r.status == 400 and "path" in (await r.json())["error"]
            r = await client.post(
                "/api/apps/dev-fleet/make-live", json={"path": "/x", "dry_run": "yes"}
            )
            assert r.status == 400 and "dry_run" in (await r.json())["error"]
            r = await client.post(
                "/api/apps/dev-fleet/make-live", json={"path": "/x", "expected_staged": "a\x00b"}
            )
            assert r.status == 400 and (await r.json())["code"] == "invalid_expected_staged"
            assert "make_live" not in recorded
        finally:
            await client.close()

    async def test_undo_is_validated_and_passed_through(self, enabled, recorded) -> None:
        """The one-level undo rides the same owner-only route: a non-bool
        is refused, undo and expected_staged are mutually exclusive, and a valid undo
        reaches the cutover as its keyword."""
        client = await _client(_make_app(principal_app=""))
        try:
            r = await client.post(
                "/api/apps/dev-fleet/make-live", json={"path": "/wt/main", "undo": "yes"}
            )
            assert r.status == 400 and (await r.json())["code"] == "invalid_undo"
            r = await client.post(
                "/api/apps/dev-fleet/make-live",
                json={"path": "/wt/main", "undo": True, "expected_staged": "/wt/feature"},
            )
            assert r.status == 400
            assert (await r.json())["code"] == "undo_conflicts_with_expected_staged"
            assert "make_live" not in recorded
            r = await client.post(
                "/api/apps/dev-fleet/make-live", json={"path": "/wt/main", "undo": True}
            )
            assert r.status == 200
            assert recorded["make_live"] == ("/wt/main", False, None)
            assert recorded["make_live_undo"] is True
        finally:
            await client.close()

    async def test_disabled_app_has_no_routes(self, recorded, monkeypatch) -> None:
        """A disabled app answers 404 on every gateway route — and each such answer is a
        refused access, so it leaves the same SEL record every other denial here does
        (an authenticated caller probing a disabled app is exactly what an audit reads)."""
        monkeypatch.setattr(gateway_routes, "is_app_enabled", lambda name: False)
        rows: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                rows.append(kw)

        monkeypatch.setattr(gateway_routes, "sel", lambda: _Sel())
        client = await _client(_make_app(principal_app=""))
        try:
            r = await client.post("/api/apps/dev-fleet/make-live", json={"path": "/x"})
            assert r.status == 404 and (await r.json())["code"] == "app_not_enabled"
            r = await client.get("/api/apps/dev-fleet/live-target")
            assert r.status == 404
        finally:
            await client.close()
        assert [(r["operation"], r["outcome"], r["resources"]) for r in rows] == [
            ("dev_fleet_gateway_route_disabled", "denied", "/api/apps/dev-fleet/make-live"),
            ("dev_fleet_gateway_route_disabled", "denied", "/api/apps/dev-fleet/live-target"),
        ]
        assert "make_live" not in recorded

    @pytest.mark.parametrize("bad", ["\x00", "a\x00b", "", 7])
    async def test_malformed_expected_staged_is_refused_before_make_live(
        self, enabled, recorded, bad
    ) -> None:
        """A NUL byte in expected_staged would reach Path.resolve() and raise into a
        500; the route refuses it (and the other malformed shapes) with a 400 before
        _make_live ever runs — the same gate the backend route carried."""
        client = await _client(_make_app(principal_app=""))
        try:
            r = await client.post(
                "/api/apps/dev-fleet/make-live", json={"path": "/w/x", "expected_staged": bad}
            )
            assert r.status == 400
            assert "expected_staged" in (await r.json())["error"]
            assert "make_live" not in recorded
        finally:
            await client.close()

    async def test_dry_run_and_expected_staged_are_forwarded(self, enabled, recorded) -> None:
        client = await _client(_make_app(principal_app=""))
        try:
            r = await client.post(
                "/api/apps/dev-fleet/make-live",
                json={"path": "/w", "dry_run": True, "expected_staged": "/w/staged"},
            )
            assert r.status == 200
            assert recorded["make_live"] == ("/w", True, "/w/staged")
        finally:
            await client.close()

    async def test_unparseable_body_is_a_400(self, enabled, recorded) -> None:
        client = await _client(_make_app(principal_app=""))
        try:
            r = await client.post(
                "/api/apps/dev-fleet/make-live",
                data=b"{not json",
                headers={"Content-Type": "application/json"},
            )
            assert r.status == 400
            assert "make_live" not in recorded
        finally:
            await client.close()


class TestTheReadBroker:
    async def test_dev_fleets_token_may_read(self, enabled, recorded) -> None:
        client = await _client(_make_app(principal_app="dev-fleet"))
        try:
            r = await client.get("/api/apps/dev-fleet/live-target?fresh=1")
            assert r.status == 200
            assert await r.json() == {
                "ok": True,
                "live": "/wt/live",
                "staged": "/wt/staged",
                "staged_cancel_available": True,
                "previous": "/wt/previous",
            }
            assert recorded["state_fresh"] is True
        finally:
            await client.close()

    async def test_a_dashboard_user_may_not_read(self, enabled, recorded) -> None:
        """The read answers with unredacted checkout paths and has exactly one
        consumer — the backend's broker. The dashboard reads the redacted fleet
        payload instead, so a plain dashboard principal (owner or guest) is refused."""
        for user in ("owner", "guest"):
            client = await _client(_make_app(principal_app="", user=user))
            try:
                r = await client.get("/api/apps/dev-fleet/live-target")
                assert r.status == 403, (user, r.status)
                assert "state_fresh" not in recorded
            finally:
                await client.close()

    async def test_another_apps_token_may_not_read(self, enabled, recorded) -> None:
        client = await _client(_make_app(principal_app="md-notebook"))
        try:
            r = await client.get("/api/apps/dev-fleet/live-target")
            assert r.status == 403
            assert "state_fresh" not in recorded
        finally:
            await client.close()


class TestTheBackendClient:
    """``GatewayPointerBroker`` against a fake gateway: token exchange, caching,
    re-exchange on a refused token, and loud failure."""

    @staticmethod
    def _fake_gateway(*, tokens: list[str], refuse_first_get: bool = False):
        seen: dict[str, Any] = {"exchanges": 0, "gets": []}

        async def token(request: web.Request):
            seen["exchanges"] += 1
            if request.headers.get("X-App-Secret") != "s3cret":
                return web.json_response({"error": "invalid secret"}, status=403)
            return web.json_response({"token": tokens[min(seen["exchanges"] - 1, len(tokens) - 1)]})

        async def state(request: web.Request):
            seen["gets"].append(dict(request.query))
            if refuse_first_get and len(seen["gets"]) == 1:
                return web.json_response({"error": "expired"}, status=401)
            if request.query.get("token") not in tokens:
                return web.json_response({"error": "bad"}, status=403)
            return web.json_response(
                {"ok": True, "live": "/wt/live", "staged": None, "staged_cancel_available": False}
            )

        app = web.Application()
        app.router.add_post("/api/apps/dev-fleet/token", token)
        app.router.add_get("/api/apps/dev-fleet/live-target", state)
        return app, seen

    async def test_exchanges_once_and_caches(self) -> None:
        app, seen = self._fake_gateway(tokens=["t1"])
        client = await _client(app)
        try:
            broker = pointer_broker.GatewayPointerBroker(
                port=client.server.port, app_secret="s3cret"
            )
            first = await broker(False)
            second = await broker(False)
            assert first == second == live.PointerState("/wt/live", None, False)
            assert seen["exchanges"] == 1
            assert len(seen["gets"]) == 1, "the second read must come from the cache"
            fresh = await broker(True)
            assert fresh.live == "/wt/live"
            assert seen["gets"][-1].get("fresh") == "1"
            await broker.aclose()
        finally:
            await client.close()

    async def test_reexchanges_once_when_the_token_is_refused(self) -> None:
        app, seen = self._fake_gateway(tokens=["t1", "t2"], refuse_first_get=True)
        client = await _client(app)
        try:
            broker = pointer_broker.GatewayPointerBroker(
                port=client.server.port, app_secret="s3cret"
            )
            state = await broker(False)
            assert state.live == "/wt/live"
            assert seen["exchanges"] == 2
            await broker.aclose()
        finally:
            await client.close()

    async def test_wrong_secret_is_an_outage_not_none(self) -> None:
        app, _seen = self._fake_gateway(tokens=["t1"])
        client = await _client(app)
        try:
            broker = pointer_broker.GatewayPointerBroker(
                port=client.server.port, app_secret="wrong"
            )
            with pytest.raises(live.PointerUnavailable):
                await broker(False)
            await broker.aclose()
        finally:
            await client.close()

    async def test_unreachable_gateway_is_an_outage(self) -> None:
        broker = pointer_broker.GatewayPointerBroker(port=1, app_secret="s3cret")
        with pytest.raises(live.PointerUnavailable):
            await broker(True)
        await broker.aclose()

    def test_no_port_gives_an_unconfigured_provider(self) -> None:
        with pytest.raises(ValueError):
            pointer_broker.GatewayPointerBroker(port=0, app_secret="s")

    async def test_unconfigured_provider_reports_the_reason(self) -> None:
        provider = pointer_broker.unconfigured_provider("no port")
        with pytest.raises(live.PointerUnavailable, match="no port"):
            await provider(False)


class TestLiveModuleSeam:
    """``live`` answers locally in the gateway and through the provider in the backend."""

    async def test_reads_route_through_the_installed_provider(self, monkeypatch) -> None:
        calls: list[bool] = []

        async def provider(fresh: bool) -> live.PointerState:
            calls.append(fresh)
            return live.PointerState(live="/wt/a", staged="/wt/b", staged_cancel_available=True)

        live.install_pointer_provider(provider)
        try:
            assert await live._live_worktree_path(fresh=True) == "/wt/a"
            assert await live._staged_target_resolved() == "/wt/b"
            assert await live._staged_cancel_available() is True
            assert live._POINTER_PROVIDER is not None
        finally:
            live.install_pointer_provider(None)
        assert calls == [True, False, False]

    async def test_make_live_refuses_to_run_in_the_backend(self, monkeypatch) -> None:
        async def provider(fresh: bool) -> live.PointerState:
            raise AssertionError("must not be consulted")

        live.install_pointer_provider(provider)
        try:
            result = await live._make_live("/wt/x")
            assert result == {
                "ok": False,
                "code": "wrong_process",
                "error": result["error"],
            }
            assert "gateway process" in result["error"]
        finally:
            live.install_pointer_provider(None)

    async def test_local_reads_when_no_provider(self, monkeypatch, tmp_path) -> None:
        assert live._POINTER_PROVIDER is None
        monkeypatch.setattr(live.live_target, "read_target", lambda: None)
        assert live._staged_target() is None
        assert await live._staged_target_resolved() is None


def test_pointer_state_payload_is_json_shaped() -> None:
    state = live.PointerState(live=None, staged=None, staged_cancel_available=False)
    assert json.loads(json.dumps(state.__dict__)) == {
        "live": None,
        "staged": None,
        "staged_cancel_available": False,
        "previous": None,
    }


@pytest.fixture
def clean_leases(monkeypatch):
    live._REMOVAL_LEASES.clear()
    live._LEASE_LOSS_EVENTS.clear()
    monkeypatch.setattr(live, "_MAKE_LIVE_COMMITTED", False)
    yield
    live._REMOVAL_LEASES.clear()
    live._LEASE_LOSS_EVENTS.clear()


class TestRemovalLeasesInTheGateway:
    """Gateway-held CAPABILITIES replace a shared lock file: nothing a sandboxed child
    can unlink, recreate, or name by path decides whether a cutover and a removal
    exclude each other."""

    def test_a_lease_is_a_token_and_is_released_by_it(self, clean_leases) -> None:
        token = live.acquire_removal_lease("/wt/a")
        assert isinstance(token, str) and len(token) >= 24
        assert live.removal_in_progress() is True
        assert live.removal_in_progress() is True
        live.release_removal_lease(token)
        assert live.removal_in_progress() is False
        live.release_removal_lease(token)  # idempotent

    def test_a_forged_release_is_a_no_op(self, clean_leases) -> None:
        """Knowing the PATH (or holding the shared app credential) is not enough: only
        the capability returned at acquisition releases the lease."""
        token = live.acquire_removal_lease("/wt/a")
        live.release_removal_lease("/wt/a")
        live.release_removal_lease("not-the-token")
        assert live.removal_in_progress() is True
        live.release_removal_lease(token)
        assert live.removal_in_progress() is False

    def test_renewal_extends_and_a_lapsed_lease_refuses_renewal(
        self, clean_leases, monkeypatch
    ) -> None:
        token = live.acquire_removal_lease("/wt/a")
        assert token is not None
        live._REMOVAL_LEASES[token].expires_at = time.monotonic() + 0.5
        assert live.renew_removal_lease(token) is True
        assert live._REMOVAL_LEASES[token].expires_at > time.monotonic() + 1
        live._REMOVAL_LEASES[token].expires_at = time.monotonic() - 1  # TTL passed
        assert live.renew_removal_lease(token) is False, (
            "a holder that fell behind must learn the lease is lost, even while the "
            "grace barrier still blocks cutovers"
        )

    def test_a_lapsed_lease_keeps_blocking_for_the_grace_barrier(
        self, clean_leases, monkeypatch
    ) -> None:
        """A lease that expired WITHOUT release means its holder may be inside the
        uninterruptible git mutation with no way to be told; cutovers and restarts
        stay excluded for the mutation timeout plus margin, then the barrier lifts."""
        token = live.acquire_removal_lease("/wt/a")
        assert token is not None
        live._REMOVAL_LEASES[token].expires_at = time.monotonic() - 1
        assert live.removal_in_progress() is True
        assert live.removal_in_progress() is True
        live._REMOVAL_LEASES[token].expires_at = (
            time.monotonic() - live._REMOVAL_LEASE_GRACE_SECS - 1
        )
        assert live.removal_in_progress() is False
        assert token not in live._REMOVAL_LEASES

    def test_release_ends_the_barrier_early(self, clean_leases) -> None:
        token = live.acquire_removal_lease("/wt/a")
        assert token is not None
        live._REMOVAL_LEASES[token].expires_at = time.monotonic() - 1
        assert live.removal_in_progress()
        live.release_removal_lease(token)
        assert not live.removal_in_progress()

    def test_grace_covers_the_git_mutation_timeout(self) -> None:
        """The barrier shields a mutation only while the mutation cannot outlive it:
        pinned against the REAL `git worktree remove` timeout, so a future bump there
        cannot silently reopen the overlap window."""
        from kiro_crew.apps.builtins.dev_fleet import worktree_ops

        assert (
            live._REMOVAL_LEASE_GRACE_SECS
            >= worktree_ops._GIT_WORKTREE_REMOVE_TIMEOUT_SECS + live._REMOVAL_LEASE_RENEW_SECS
        )

    async def test_no_lease_while_a_cutover_holds_the_lock(self, clean_leases) -> None:
        async with live._MAKE_LIVE_LOCK:
            assert live.acquire_removal_lease("/wt/a") is None
        assert live.acquire_removal_lease("/wt/a") is not None

    def test_outstanding_leases_are_capped_and_the_cap_is_a_refusal(
        self, clean_leases, monkeypatch
    ) -> None:
        """The app token that leases is readable by every build child in the backend's
        namespace, so a child looping on acquire must not grow the gateway's lease table
        without bound (each entry outlives its TTL by the grace barrier). At the cap the
        acquisition is refused — the ordinary ``busy`` answer — and recovers once a lease
        is released or its barrier passes."""
        cap = live._REMOVAL_LEASE_MAX_OUTSTANDING
        tokens = [live.acquire_removal_lease(f"/wt/{i}") for i in range(cap)]
        assert all(tokens)
        assert live.acquire_removal_lease("/wt/overflow") is None
        assert len(live._REMOVAL_LEASES) == cap, "the refusal must not mint a token"
        live.release_removal_lease(tokens[0])
        assert live.acquire_removal_lease("/wt/again") is not None
        assert live.acquire_removal_lease("/wt/overflow") is None
        # Expiry past the grace barrier frees the slots too.
        far = time.monotonic() + live._REMOVAL_LEASE_TTL_SECS + live._REMOVAL_LEASE_GRACE_SECS + 1
        monkeypatch.setattr(live.time, "monotonic", lambda: far)
        assert live.acquire_removal_lease("/wt/late") is not None

    def test_the_cap_exceeds_the_parallel_prune_width(self) -> None:
        from kiro_crew.apps.builtins.dev_fleet import worktree_ops

        assert live._REMOVAL_LEASE_MAX_OUTSTANDING >= 4 * worktree_ops._PRUNE_CONCURRENCY

    def test_no_lease_once_a_cutover_has_committed(self, clean_leases, monkeypatch) -> None:
        monkeypatch.setattr(live, "_MAKE_LIVE_COMMITTED", True)
        assert live.acquire_removal_lease("/wt/a") is None

    async def test_make_live_refuses_busy_while_a_removal_is_leased(
        self, clean_leases, monkeypatch
    ) -> None:
        async def _inner(*a, **k):
            raise AssertionError("must not run while a removal is leased")

        monkeypatch.setattr(live, "_make_live_inner", _inner)
        live.acquire_removal_lease("/wt/victim")
        result = await live._make_live("/wt/other")
        assert result["ok"] is False and result["code"] == "busy"

    async def test_dry_run_ignores_leases(self, clean_leases, monkeypatch) -> None:
        async def _inner(path, dry_run=False, expected_staged=None, *, undo=False):
            return {"ok": True, "dry_run": dry_run}

        monkeypatch.setattr(live, "_make_live_inner", _inner)
        live.acquire_removal_lease("/wt/victim")
        assert await live._make_live("/wt/other", dry_run=True) == {"ok": True, "dry_run": True}

    async def test_restart_gateway_refuses_while_a_removal_is_leased(
        self, clean_leases, monkeypatch
    ) -> None:
        def _boom():
            raise AssertionError("the service backend must not be consulted")

        monkeypatch.setattr(live, "_gateway_backend", _boom)
        live.acquire_removal_lease("/wt/victim")
        result = await live._restart_gateway()
        assert result["ok"] is False and "removal is in progress" in result["error"]

    async def test_gateway_local_removal_lease_context(self, clean_leases) -> None:
        async with live.removal_lease("/wt/a") as granted:
            assert granted.granted is True and granted.refusal is None
            assert live.removal_in_progress()
            assert live.removal_lease_lost("/wt/a") is False
        assert not live.removal_in_progress()
        assert "/wt/a" not in live._LEASE_LOSS_EVENTS


class TestRemovalLeasesFromTheBackend:
    @staticmethod
    def _install(acquire, renew, release):
        live.install_removal_lease_client((acquire, renew, release))

    async def test_lease_goes_through_the_installed_client(self, clean_leases) -> None:
        calls: list[tuple[str, str]] = []

        async def acquire(path: str) -> str | None:
            calls.append(("acquire", path))
            return "cap-1"

        async def renew(token: str) -> bool:
            calls.append(("renew", token))
            return True

        async def release(token: str) -> None:
            calls.append(("release", token))

        self._install(acquire, renew, release)
        try:
            async with live.removal_lease("/wt/a") as granted:
                assert granted.granted is True and granted.refusal is None
                assert not live._REMOVAL_LEASES, "the backend must not touch gateway state"
        finally:
            live.install_removal_lease_client(None)
        assert calls == [("acquire", "/wt/a"), ("release", "cap-1")]

    async def test_the_heartbeat_renews_with_the_capability(
        self, clean_leases, monkeypatch
    ) -> None:
        monkeypatch.setattr(live, "_REMOVAL_LEASE_RENEW_SECS", 0.02)
        renewals: list[str] = []

        async def acquire(path: str) -> str | None:
            return "cap-hb"

        async def renew(token: str) -> bool:
            renewals.append(token)
            return True

        async def release(token: str) -> None:
            pass

        self._install(acquire, renew, release)
        try:
            async with live.removal_lease("/wt/a") as granted:
                assert granted
                await asyncio.sleep(0.15)
                assert live.removal_lease_lost("/wt/a") is False
        finally:
            live.install_removal_lease_client(None)
        assert len(renewals) >= 3 and set(renewals) == {"cap-hb"}

    async def test_a_refused_renewal_marks_the_lease_lost(self, clean_leases, monkeypatch) -> None:
        """The removal checks this at its mutation boundary and refuses to start."""
        monkeypatch.setattr(live, "_REMOVAL_LEASE_RENEW_SECS", 0.02)

        async def acquire(path: str) -> str | None:
            return "cap-lost"

        async def renew(token: str) -> bool:
            return False

        async def release(token: str) -> None:
            pass

        self._install(acquire, renew, release)
        try:
            async with live.removal_lease("/wt/a") as granted:
                assert granted
                for _ in range(50):
                    if live.removal_lease_lost("/wt/a"):
                        break
                    await asyncio.sleep(0.01)
                assert live.removal_lease_lost("/wt/a") is True
        finally:
            live.install_removal_lease_client(None)

    async def test_a_transient_renewal_outage_is_not_a_loss(
        self, clean_leases, monkeypatch
    ) -> None:
        monkeypatch.setattr(live, "_REMOVAL_LEASE_RENEW_SECS", 0.02)

        async def acquire(path: str) -> str | None:
            return "cap-flaky"

        async def renew(token: str) -> bool:
            raise live.PointerUnavailable("blip")

        async def release(token: str) -> None:
            pass

        self._install(acquire, renew, release)
        try:
            async with live.removal_lease("/wt/a") as granted:
                assert granted
                await asyncio.sleep(0.1)
                assert live.removal_lease_lost("/wt/a") is False
        finally:
            live.install_removal_lease_client(None)

    async def test_a_refused_lease_is_not_released(self, clean_leases) -> None:
        calls: list[str] = []

        async def acquire(path: str) -> str | None:
            return None

        async def renew(token: str) -> bool:
            return True

        async def release(token: str) -> None:
            calls.append(token)

        self._install(acquire, renew, release)
        try:
            async with live.removal_lease("/wt/a") as granted:
                assert granted.granted is False and granted.refusal == "busy"
        finally:
            live.install_removal_lease_client(None)
        assert calls == []

    async def test_a_broker_outage_is_not_granted(self, clean_leases) -> None:
        async def acquire(path: str) -> str | None:
            raise live.PointerUnavailable("down")

        async def renew(token: str) -> bool:
            raise AssertionError("never renewed what was never granted")

        async def release(token: str) -> None:
            raise AssertionError("never released what was never granted")

        self._install(acquire, renew, release)
        try:
            async with live.removal_lease("/wt/a") as granted:
                assert granted.granted is False and granted.refusal == "unavailable"
        finally:
            live.install_removal_lease_client(None)

    async def test_unconfigured_lease_client_refuses(self) -> None:
        acquire, renew, release = pointer_broker.unconfigured_lease_client("no port")
        for call in (acquire("/wt/a"), renew("t"), release("t")):
            with pytest.raises(live.PointerUnavailable, match="no port"):
                await call

    async def test_broker_client_round_trips_the_lease_routes(self, clean_leases) -> None:
        seen: list[tuple[str, dict]] = []

        async def token(request: web.Request):
            return web.json_response({"token": "t1"})

        async def lease(request: web.Request):
            body = await request.json()
            seen.append((request.method, body))
            if request.method == "POST":
                busy = body["path"] == "/wt/busy"
                return web.json_response(
                    {"ok": True, "granted": not busy, "token": None if busy else "cap-9"}
                )
            if request.method == "PUT":
                return web.json_response({"ok": True, "renewed": body["token"] == "cap-9"})
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/api/apps/dev-fleet/token", token)
        for method in ("POST", "PUT", "DELETE"):
            app.router.add_route(method, "/api/apps/dev-fleet/live-target/removal-lease", lease)
        client = await _client(app)
        try:
            broker = pointer_broker.GatewayPointerBroker(
                port=client.server.port, app_secret="s3cret"
            )
            assert await broker.acquire_removal_lease("/wt/a") == "cap-9"
            assert await broker.acquire_removal_lease("/wt/busy") is None
            assert await broker.renew_removal_lease("cap-9") is True
            assert await broker.renew_removal_lease("stale") is False
            await broker.release_removal_lease("cap-9")
            await broker.aclose()
        finally:
            await client.close()
        assert seen == [
            ("POST", {"path": "/wt/a"}),
            ("POST", {"path": "/wt/busy"}),
            ("PUT", {"token": "cap-9"}),
            ("PUT", {"token": "stale"}),
            ("DELETE", {"token": "cap-9"}),
        ]


class TestTheLeaseRoutes:
    async def test_dev_fleet_token_may_lease_renew_and_release(self, enabled, clean_leases) -> None:
        client = await _client(_make_app(principal_app="dev-fleet"))
        try:
            r = await client.post(
                "/api/apps/dev-fleet/live-target/removal-lease", json={"path": "/wt/a"}
            )
            body = await r.json()
            assert r.status == 200 and body["ok"] and body["granted"]
            cap = body["token"]
            assert isinstance(cap, str) and cap
            assert live.removal_in_progress()
            r = await client.put(
                "/api/apps/dev-fleet/live-target/removal-lease", json={"token": cap}
            )
            assert (await r.json()) == {"ok": True, "renewed": True}
            # A path-only or wrong-token release changes nothing.
            r = await client.delete(
                "/api/apps/dev-fleet/live-target/removal-lease", json={"token": "/wt/a"}
            )
            assert r.status == 200 and live.removal_in_progress()
            r = await client.delete(
                "/api/apps/dev-fleet/live-target/removal-lease", json={"token": cap}
            )
            assert r.status == 200
            assert not live.removal_in_progress()
        finally:
            await client.close()

    async def test_accepted_reads_and_lease_calls_leave_a_sel_record(
        self, enabled, recorded, clean_leases, monkeypatch
    ) -> None:
        """`_deny` records the refusals; the ACCEPTED side of the same authorization
        decision must be recorded too — one row per pointer read and per lease
        acquire / renew / release, naming the fact, with the capability token never
        written to the log."""
        rows: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                rows.append(kw)

        monkeypatch.setattr(gateway_routes, "sel", lambda: _Sel())
        client = await _client(_make_app(principal_app="dev-fleet"))
        try:
            await client.get("/api/apps/dev-fleet/live-target")
            r = await client.post(
                "/api/apps/dev-fleet/live-target/removal-lease", json={"path": "/wt/a"}
            )
            cap = (await r.json())["token"]
            await client.put("/api/apps/dev-fleet/live-target/removal-lease", json={"token": cap})
            await client.put(
                "/api/apps/dev-fleet/live-target/removal-lease", json={"token": "stale"}
            )
            await client.delete(
                "/api/apps/dev-fleet/live-target/removal-lease", json={"token": cap}
            )
        finally:
            await client.close()
        facts = [(r["operation"], r["outcome"]) for r in rows]
        assert facts == [
            ("dev_fleet_live_target_read", "read"),
            ("dev_fleet_removal_lease", "granted"),
            ("dev_fleet_removal_lease", "renewed"),
            ("dev_fleet_removal_lease", "lapsed"),
            ("dev_fleet_removal_lease", "released"),
        ]
        assert all(r["source"] == "dev_fleet_gateway" for r in rows)
        assert rows[1]["resources"] == "/wt/a"
        assert not any(cap in str(r) for r in rows), "the capability must never reach the log"

    async def test_another_apps_token_may_not_lease(self, enabled, clean_leases) -> None:
        client = await _client(_make_app(principal_app="md-notebook"))
        try:
            for method, body in (
                ("POST", {"path": "/wt/a"}),
                ("PUT", {"token": "x"}),
                ("DELETE", {"token": "x"}),
            ):
                r = await client.request(
                    method, "/api/apps/dev-fleet/live-target/removal-lease", json=body
                )
                assert r.status == 403
            assert not live.removal_in_progress()
        finally:
            await client.close()

    @pytest.mark.parametrize("user", ["owner", "slack-guest"])
    async def test_a_dashboard_human_may_not_lease(self, enabled, clean_leases, user) -> None:
        """A lease latches ``busy`` on the owner-only cutover and restart, so the read
        broker's principal set (any dashboard user) is too wide here: a Slack-allowlisted
        non-owner holding a plain dashboard token must not be able to deny the owner's
        Make Live indefinitely. Only the backend's own app token may lease."""
        client = await _client(_make_app(principal_app="", user=user))
        try:
            for method, body in (
                ("POST", {"path": "/wt/a"}),
                ("PUT", {"token": "x"}),
                ("DELETE", {"token": "x"}),
            ):
                r = await client.request(
                    method, "/api/apps/dev-fleet/live-target/removal-lease", json=body
                )
                assert r.status == 403, (method, r.status)
                assert (await r.json())["code"] == "forbidden"
            assert not live.removal_in_progress()
        finally:
            await client.close()

    async def test_lease_is_refused_not_errored_during_a_cutover(
        self, enabled, clean_leases
    ) -> None:
        client = await _client(_make_app(principal_app="dev-fleet"))
        try:
            async with live._MAKE_LIVE_LOCK:
                r = await client.post(
                    "/api/apps/dev-fleet/live-target/removal-lease", json={"path": "/wt/a"}
                )
                assert r.status == 200
                assert (await r.json()) == {"ok": True, "granted": False, "token": None}
        finally:
            await client.close()

    async def test_lease_bodies_are_validated(self, enabled, clean_leases) -> None:
        client = await _client(_make_app(principal_app="dev-fleet"))
        try:
            for bad in ({"path": ""}, {"path": "a\x00b"}, {"path": 3}, {}):
                r = await client.post("/api/apps/dev-fleet/live-target/removal-lease", json=bad)
                assert r.status == 400
            for bad in ({"token": ""}, {"token": 3}, {}):
                r = await client.put("/api/apps/dev-fleet/live-target/removal-lease", json=bad)
                assert r.status == 400
        finally:
            await client.close()


class TestThePreSpawnGate:
    """The point of no return: ``_run_cmd`` evaluates ``pre_spawn`` AFTER sandbox
    preparation and immediately before the child is spawned, and a refusal never
    spawns (and cleans up the prepared launcher)."""

    async def test_run_cmd_refuses_without_spawning_and_unlinks_the_launcher(
        self, monkeypatch, tmp_path
    ) -> None:
        from kiro_crew.apps.builtins.dev_fleet import runtime

        launcher = tmp_path / "launcher.sh"
        launcher.write_text("#!/bin/sh\n")
        order: list[str] = []

        async def _prep(prepare, *, executor=None):
            order.append("prepare")
            return (["/bin/true"], {}, str(launcher))

        async def _spawn(*a, **k):
            order.append("spawn")
            raise AssertionError("must not spawn after a refused gate")

        async def _gate() -> str | None:
            order.append("gate")
            return "refusing: the gateway could not confirm that no cutover overlaps this removal"

        monkeypatch.setattr(runtime, "shielded_prepare_off_loop", _prep)
        monkeypatch.setattr(runtime, "create_subprocess_limited", _spawn)
        monkeypatch.setattr(runtime, "_trusted_bin", lambda name: "/bin/true")
        rc, out, err = await runtime._run_cmd(["true"], pre_spawn=_gate)
        assert (rc, out) == (-1, "")
        assert err.startswith("refusing: the gateway could not confirm")
        assert order == ["prepare", "gate"], "the gate must run after preparation, never before"
        assert not launcher.exists(), "a refused gate must not leak the prepared launcher"

    async def test_run_cmd_proceeds_when_the_gate_allows(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.apps.builtins.dev_fleet import runtime

        order: list[str] = []
        # sys.executable exists on every CI platform; /bin/true does not (macOS
        # has /usr/bin/true, Windows has neither).
        argv = [sys.executable, "-c", "pass"]

        async def _prep(prepare, *, executor=None):
            order.append("prepare")
            return (list(argv), {}, None)

        async def _gate() -> str | None:
            order.append("gate")
            return None

        monkeypatch.setattr(runtime, "shielded_prepare_off_loop", _prep)
        monkeypatch.setattr(runtime, "_trusted_bin", lambda name: sys.executable)
        rc, _out, _err = await runtime._run_cmd(list(argv), pre_spawn=_gate)
        assert rc == 0
        assert order == ["prepare", "gate"]

    async def test_confirm_is_an_explicit_renewal(self, clean_leases) -> None:
        renewals: list[str] = []

        async def acquire(path: str) -> str | None:
            return "cap-c"

        async def renew(token: str) -> bool:
            renewals.append(token)
            return True

        async def release(token: str) -> None:
            pass

        live.install_removal_lease_client((acquire, renew, release))
        try:
            assert await live.confirm_removal_lease("/wt/a") is False, "no lease held yet"
            async with live.removal_lease("/wt/a"):
                assert await live.confirm_removal_lease("/wt/a") is True
                assert renewals == ["cap-c"]
            assert await live.confirm_removal_lease("/wt/a") is False, "released"
        finally:
            live.install_removal_lease_client(None)

    async def test_confirm_fails_closed_on_refusal_or_outage_and_marks_loss(
        self, clean_leases
    ) -> None:
        answers: list = [False]

        async def acquire(path: str) -> str | None:
            return "cap-d"

        async def renew(token: str) -> bool:
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a

        async def release(token: str) -> None:
            pass

        live.install_removal_lease_client((acquire, renew, release))
        try:
            async with live.removal_lease("/wt/a"):
                assert await live.confirm_removal_lease("/wt/a") is False
                assert live.removal_lease_lost("/wt/a") is True
            answers[:] = [live.PointerUnavailable("down")]
            async with live.removal_lease("/wt/b"):
                assert await live.confirm_removal_lease("/wt/b") is False
                assert live.removal_lease_lost("/wt/b") is True
        finally:
            live.install_removal_lease_client(None)

    async def test_a_full_ttl_of_unreachable_renewals_is_a_loss(
        self, clean_leases, monkeypatch
    ) -> None:
        """An unreachable gateway has let the lease lapse after one TTL; the holder
        must not keep believing in it just because the failures were 'transient'."""
        monkeypatch.setattr(live, "_REMOVAL_LEASE_RENEW_SECS", 0.02)
        monkeypatch.setattr(live, "_REMOVAL_LEASE_TTL_SECS", 0.08)

        async def acquire(path: str) -> str | None:
            return "cap-e"

        async def renew(token: str) -> bool:
            raise live.PointerUnavailable("down for good")

        async def release(token: str) -> None:
            pass

        live.install_removal_lease_client((acquire, renew, release))
        try:
            async with live.removal_lease("/wt/a"):
                for _ in range(60):
                    if live.removal_lease_lost("/wt/a"):
                        break
                    await asyncio.sleep(0.01)
                assert live.removal_lease_lost("/wt/a") is True
        finally:
            live.install_removal_lease_client(None)


class TestFleetViewDegradesWithRowsDuringAnOutage:
    async def test_gateway_service_reason_treats_an_unavailable_pointer_as_unknown(
        self, monkeypatch
    ) -> None:
        """The advisory hint must not raise out of a payload field: an outage would
        otherwise collapse `/api/fleet` to an error instead of rows without badges."""

        async def _inactive():
            return False

        async def _status():
            return "no_user_unit"

        async def provider(fresh: bool) -> live.PointerState:
            raise live.PointerUnavailable("down")

        monkeypatch.setattr(live, "_gateway_service_active", _inactive)
        monkeypatch.setattr(live, "_live_user_unit_status", _status)
        live.install_pointer_provider(provider)
        try:
            reason = await live._gateway_service_reason()
        finally:
            live.install_pointer_provider(None)
        assert isinstance(reason, str) and reason
        assert "does not belong to any known worktree" not in reason

    async def test_fleet_payload_takes_one_pointer_snapshot(self, monkeypatch) -> None:
        """All three pointer-derived fields come from ONE broker read, so a gateway that
        goes away mid-build cannot fail a later field (the cancel-availability probe
        would be a second, unguarded read)."""
        from kiro_crew.apps.builtins.dev_fleet import fleet_state

        calls: list[bool] = []

        async def provider(fresh: bool) -> live.PointerState:
            calls.append(fresh)
            if len(calls) > 1:
                raise live.PointerUnavailable("gone after the first read")
            return live.PointerState(
                live="/wt/live", staged="/wt/staged", staged_cancel_available=True
            )

        seen: dict = {}

        async def _cancel_probe():
            seen["probed"] = True
            raise AssertionError("cancel availability must come from the snapshot")

        monkeypatch.setattr(live, "_staged_cancel_available", _cancel_probe)
        live.install_pointer_provider(provider)
        try:
            src = inspect.getsource(fleet_state._build_fleet)
        finally:
            live.install_pointer_provider(None)
        # Structural pin (the full payload needs a git repo): exactly one pointer read,
        # and the cancel flag is taken from that snapshot rather than probed again.
        assert src.count("live.pointer_state()") == 1
        assert "pointer.staged_cancel_available" in src
        assert "await live._staged_cancel_available()" not in src
        assert "await live._live_worktree_path()" not in src
        assert "await live._staged_target_resolved()" not in src
