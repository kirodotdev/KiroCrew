"""The usage routes start a credit refresh only on the interval or on demand.

``GET /api/sessions/usage`` refreshes only once the cached reading is older than
the refresh interval, and no auth-store trigger exists beside that timer.
``POST /api/sessions/usage/refresh`` runs the same API-first, scrape-second refresh
for any authenticated caller, with no cool-down, refuses a click while another
refresh is in flight, and reports a parked scrape with its retry delay. The
refresh logic itself is pinned in test_session_usage.py and its topic siblings.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from session_usage_helpers import (
    _IDENTITY_A,
    SAMPLE_USAGE,
    _api_result,
    _mock_proc,
    _reset_usage_globals,
)

import kiro_crew.dashboard.handlers.sessions as sessions_mod
from kiro_crew.testing.wait import async_wait_until


class TestPollNeverRefreshesInsideTheInterval:
    """The 30s dashboard poll must not be able to trigger a refresh inside the
    interval.

    An earlier revision refreshed whenever the kiro-cli auth store changed on
    disk, to pick a profile switch up in seconds. But that store is SHARED --
    `data.sqlite3` holds `conversations`, `history` and `state` alongside
    `auth_kv` -- so ordinary chat traffic rewrites it roughly every 30 seconds
    (observed: the SQLite header change counter incrementing on that cadence with
    sessions active). The trigger therefore fired on almost every poll, and a fire
    can reach the `/usage` text scrape, a minute-scale kiro-cli subprocess.
    Refreshing the credit readout must never spawn that faster than the interval.
    """

    @pytest.fixture(autouse=True)
    def _reset(self):
        _reset_usage_globals()
        yield
        _reset_usage_globals()

    def _request(self):
        request = MagicMock()
        request.app = {"state": SimpleNamespace(_background_tasks=set())}
        return request

    @pytest.mark.asyncio
    async def test_fresh_cache_never_refreshes(self):
        sessions_mod._usage_cache = {"credits_plan": 10.0}
        # flake-ok: pre-existing real-clock read stamping the usage cache fresh against the product's own clock, moved verbatim; tracked for a de-flake pass
        sessions_mod._usage_cache_ts = time.time()
        with (
            patch.object(sessions_mod, "reject_if_kiro_unverified", AsyncMock(return_value=None)),
            patch.object(sessions_mod, "_fetch_usage_bg", AsyncMock()) as fetch,
        ):
            resp = await sessions_mod.api_sessions_usage(self._request())
        fetch.assert_not_called()
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_elapsed_interval_does_refresh(self):
        # The timer is still the trigger -- this is not "never refresh".
        sessions_mod._usage_cache = {"credits_plan": 10.0}
        # flake-ok: pre-existing real-clock read stamping the usage cache one refresh interval past against the product's own clock, moved verbatim; tracked for a de-flake pass
        sessions_mod._usage_cache_ts = time.time() - (sessions_mod._USAGE_REFRESH_SECS + 1)
        with (
            patch.object(sessions_mod, "reject_if_kiro_unverified", AsyncMock(return_value=None)),
            patch.object(sessions_mod, "_fetch_usage_bg", AsyncMock()) as fetch,
        ):
            await sessions_mod.api_sessions_usage(self._request())
        fetch.assert_called_once()

    def test_no_auth_store_trigger_is_reintroduced(self):
        # Guard the reason, not just the behaviour: a filesystem-watching trigger
        # on this handler cannot distinguish a credential write from a chat write,
        # so reintroducing one re-creates the credit-spend loop.
        assert not hasattr(sessions_mod, "_auth_store_changed")
        assert not hasattr(sessions_mod, "_usage_auth_fingerprint")
        assert not hasattr(sessions_mod.kiro_usage_api, "auth_store_fingerprint")


def _refresh_app():
    """A bare app carrying the usage GET and the refresh POST, as the router wires them."""
    from aiohttp import web

    app = web.Application()
    app["state"] = SimpleNamespace(_background_tasks=set())
    app.router.add_get("/api/sessions/usage", sessions_mod.api_sessions_usage)
    app.router.add_post("/api/sessions/usage/refresh", sessions_mod.api_sessions_usage_refresh)
    return app


def _owner(verdict: bool):
    """Pin the shared owner predicate, to prove the route does not consult it."""
    return patch(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        return_value=verdict,
    )


class TestUsageRefreshRoute:
    """``POST /api/sessions/usage/refresh`` refreshes the credit reading on demand.

    It runs the same API-first, scrape-second refresh the timer runs. What is
    pinned here: any authenticated caller may use it; two refreshes never run at
    once; and a parked scrape is REPORTED after the API attempt, never used as an
    excuse to skip the API.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture, moved verbatim; no test of the class calls monkeypatch.undo()
    def _reset(self, monkeypatch):
        _reset_usage_globals()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.sessions.wrap_argv",
            lambda argv, **k: (list(argv), None),
        )
        # The API yields no plan -- the case where only the scrape can produce a
        # reading.
        monkeypatch.setattr(
            sessions_mod.kiro_usage_api, "fetch_usage_limits", lambda **k: _api_result(None)
        )
        monkeypatch.setattr(
            sessions_mod, "_resolve_kiro_bin_for_spawn", AsyncMock(return_value="/bin/kiro")
        )
        monkeypatch.setattr(
            sessions_mod, "_fetch_whoami", AsyncMock(return_value=dict(_IDENTITY_A))
        )
        monkeypatch.setattr(sessions_mod, "reject_if_kiro_unverified", AsyncMock(return_value=None))
        yield
        _reset_usage_globals()

    def _spawn_mock(self, stdout: bytes = SAMPLE_USAGE.encode()):
        return AsyncMock(return_value=_mock_proc(stdout))

    @pytest.mark.asyncio
    async def test_the_click_runs_the_scrape_and_replaces_the_cache(self):
        from aiohttp.test_utils import TestClient, TestServer

        sessions_mod._usage_cache = {"available": False}
        # flake-ok: pre-existing real-clock read stamping the usage cache fresh against the product's own clock, moved verbatim; tracked for a de-flake pass
        sessions_mod._usage_cache_ts = time.time()
        spawn = self._spawn_mock()
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            # Same envelope as the GET, so the frontend reuses one parser.
            assert body["usage"]["credits_plan"] == 10000.0
            assert "skipped" not in body
            # The cache the pill polls now serves the refreshed reading.
            got = await (await c.get("/api/sessions/usage")).json()
        assert got["usage"]["credits_plan"] == 10000.0
        assert spawn.await_count == 1
        assert "/usage" in list(spawn.await_args.args)

    @pytest.mark.asyncio
    async def test_any_authenticated_caller_may_refresh(self):
        """No owner gate: the read costs nothing, so a dashboard token is enough."""
        from aiohttp.test_utils import TestClient, TestServer

        spawn = self._spawn_mock()
        async with TestClient(TestServer(_refresh_app())) as c:
            with _owner(False), patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 200, await resp.text()
        assert spawn.await_count == 1

    @pytest.mark.asyncio
    async def test_the_unverified_kiro_guard_still_applies(self, monkeypatch):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        monkeypatch.setattr(
            sessions_mod,
            "reject_if_kiro_unverified",
            AsyncMock(return_value=web.json_response({"error": "kiro"}, status=503)),
        )
        spawn = self._spawn_mock()
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 503
        assert spawn.await_count == 0

    @pytest.mark.asyncio
    async def test_back_to_back_clicks_both_run(self):
        """No cool-down: the second click is a second refresh, not a refusal."""
        from aiohttp.test_utils import TestClient, TestServer

        assert not hasattr(sessions_mod, "_USAGE_REFRESH_COOLDOWN_SECS")
        spawn = self._spawn_mock()
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                first = await c.post("/api/sessions/usage/refresh")
                second = await c.post("/api/sessions/usage/refresh")
            assert first.status == 200
            assert second.status == 200
        assert spawn.await_count == 2

    @pytest.mark.asyncio
    async def test_a_click_during_an_in_flight_refresh_is_refused_with_409(self):
        """Two concurrent clicks: ONE subprocess, the loser told a refresh is running."""
        from aiohttp.test_utils import TestClient, TestServer

        release = asyncio.Event()
        proc = MagicMock()

        async def _communicate():
            # flake-ok: pre-existing unbounded Event wait inside the stub scrape, released by the test after the second click, moved verbatim; tracked for a de-flake pass
            await release.wait()
            return SAMPLE_USAGE.encode(), b""

        proc.communicate = _communicate
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        spawn = AsyncMock(return_value=proc)
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                first = asyncio.ensure_future(c.post("/api/sessions/usage/refresh"))
                # Let the first click reach the scrape and block inside it.
                await async_wait_until(lambda: spawn.await_count)
                assert spawn.await_count == 1
                second = await c.post("/api/sessions/usage/refresh")
                assert second.status == 409, await second.text()
                assert (await second.json())["code"] == "refresh_in_flight"
                release.set()
                # flake-ok: pre-existing unbounded await of the first click's request, moved verbatim; tracked for a de-flake pass
                resp = await first
                assert resp.status == 200, await resp.text()
                assert (await resp.json())["usage"]["credits_plan"] == 10000.0
        assert spawn.await_count == 1

    @pytest.mark.asyncio
    async def test_a_timer_refresh_in_flight_also_refuses_the_click(self):
        from aiohttp.test_utils import TestClient, TestServer

        sessions_mod._usage_fetching = True
        spawn = self._spawn_mock()
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 409
        assert spawn.await_count == 0

    @pytest.mark.asyncio
    async def test_a_parked_scrape_is_reported_after_the_api_returns_no_plan(self):
        """Parked and no plan from the API: the cache comes back unchanged, marked."""
        from aiohttp.test_utils import TestClient, TestServer

        # flake-ok: pre-existing real-clock read parking the scrape an hour ahead of the product's own monotonic clock, moved verbatim; tracked for a de-flake pass
        sessions_mod._usage_scrape_backoff_until = time.monotonic() + 3600
        sessions_mod._usage_cache = {"available": False}
        # flake-ok: pre-existing real-clock read stamping the usage cache fresh against the product's own clock, moved verbatim; tracked for a de-flake pass
        sessions_mod._usage_cache_ts = time.time()
        spawn = self._spawn_mock()
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["skipped"] == "scrape_parked"
        assert body["usage"] == {"available": False}
        assert 1 <= body["retry_after"] <= 3600
        assert spawn.await_count == 0

    @pytest.mark.asyncio
    async def test_a_scrape_that_parks_during_this_refresh_is_reported_too(self):
        """The third miss lands INSIDE the click: the answer still says parked.

        Two misses are on the counter; this refresh's scrape prints no table
        and parks the scrape. The user pressed Refresh and got no reading, and
        must learn that pressing again is pointless until the park lifts --
        the same ``skipped`` + ``retry_after`` a pre-parked refresh reports.
        """
        from aiohttp.test_utils import TestClient, TestServer

        sessions_mod._usage_scrape_failures = sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD - 1
        spawn = self._spawn_mock(b"not a usage block")
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert spawn.await_count == 1  # the scrape DID run: this is not a pre-check
        assert sessions_mod._scrape_in_backoff()
        assert body["skipped"] == "scrape_parked"
        assert 1 <= body["retry_after"] <= sessions_mod._USAGE_SCRAPE_BACKOFF_SECS
        assert body["usage"] == {"available": False}

    @pytest.mark.asyncio
    async def test_a_scrape_that_times_out_into_the_park_is_reported_too(self):
        """Same third miss, but the scrape hangs: the timeout path reports the park."""
        from aiohttp.test_utils import TestClient, TestServer

        sessions_mod._usage_scrape_failures = sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD - 1
        proc = _mock_proc(b"")
        proc.returncode = None
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert sessions_mod._scrape_in_backoff()
        assert body["skipped"] == "scrape_parked"
        assert 1 <= body["retry_after"] <= sessions_mod._USAGE_SCRAPE_BACKOFF_SECS
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_scrape_that_errors_into_the_park_is_reported_too(self):
        """Same third miss, the scrape spawn raises: the exception path reports the park."""
        from aiohttp.test_utils import TestClient, TestServer

        sessions_mod._usage_scrape_failures = sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD - 1
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=OSError("spawn"))):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert sessions_mod._scrape_in_backoff()
        assert body["skipped"] == "scrape_parked"

    @pytest.mark.asyncio
    async def test_a_miss_short_of_the_park_reports_no_skip(self):
        """Control: a failed scrape that does NOT park is a plain failed refresh."""
        from aiohttp.test_utils import TestClient, TestServer

        sessions_mod._usage_scrape_failures = sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD - 2
        spawn = self._spawn_mock(b"not a usage block")
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert not sessions_mod._scrape_in_backoff()
        assert "skipped" not in body
        assert body["usage"] == {"available": False}

    @pytest.mark.asyncio
    async def test_a_parked_scrape_does_not_block_a_reading_the_api_returns(self, monkeypatch):
        """The park is about the scrape only: the API attempt still runs first."""
        from aiohttp.test_utils import TestClient, TestServer

        # flake-ok: pre-existing real-clock read parking the scrape an hour ahead of the product's own monotonic clock, moved verbatim; tracked for a de-flake pass
        sessions_mod._usage_scrape_backoff_until = time.monotonic() + 3600
        # A proven account: the API reading publishes only when whoami names one.
        monkeypatch.setattr(
            sessions_mod, "_fetch_whoami", AsyncMock(return_value=dict(_IDENTITY_A))
        )
        monkeypatch.setattr(
            sessions_mod.kiro_usage_api,
            "fetch_usage_limits",
            lambda **k: _api_result({"credits_plan": 500.0, "credits_used": 12.0}),
        )
        spawn = self._spawn_mock()
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["usage"]["credits_plan"] == 500.0
        assert "skipped" not in body
        assert spawn.await_count == 0

    @pytest.mark.asyncio
    async def test_the_api_is_still_preferred_over_the_scrape(self, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer

        monkeypatch.setattr(
            sessions_mod, "_fetch_whoami", AsyncMock(return_value=dict(_IDENTITY_A))
        )
        monkeypatch.setattr(
            sessions_mod.kiro_usage_api,
            "fetch_usage_limits",
            lambda **k: _api_result({"credits_plan": 500.0, "credits_used": 12.0}),
        )
        spawn = self._spawn_mock()
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert (await resp.json())["usage"]["credits_plan"] == 500.0
        assert spawn.await_count == 0

    def test_the_route_is_registered_next_to_the_get(self):
        from aiohttp import web

        from kiro_crew.dashboard.routes.system import register as register_system_routes

        app = web.Application()
        register_system_routes(app)
        routes = {(r.method, r.resource.canonical) for r in app.router.routes()}
        assert ("POST", "/api/sessions/usage/refresh") in routes
        assert ("GET", "/api/sessions/usage") in routes
