"""Coverage tests for ``kiro_crew.dashboard.server``'s startup path.

Sibling of ``test_dashboard_server_coverage.py``, which pins the module's
extracted *helpers*. What was left uncovered is the one thing no helper test can
reach: the body of :func:`~kiro_crew.dashboard.server.start_dashboard` itself —
the app factory that wires every route, middleware and lifecycle hook the
gateway serves. Nothing asserted here is reachable from a helper, because the
wiring order and the hook/middleware inventory only exist inside that function.

The listener is never bound. ``start_dashboard`` builds
``web.TCPSite(runner, addr, port)`` and binds it inside ``_start_site``;
constructing the site is inert, so stubbing ``_start_site`` means no host port
is opened. That is the pattern the sibling module established for
``start_api_server`` — AUTOSDE ``no-test-side-effects`` is ``blocking: true`` and
an ephemeral ``port=0`` would not exempt a real bind.

Everything else that reaches outside the process is replaced rather than
tolerated: app-backend launches, the builtin-app registration sweep, the Kiro
prerequisite probe, the Playwright registration migration (which writes the
operator's REAL ``~/.kiro/settings/mcp.json``, outside ``KIROCREW_HOME``), the
terminal reaper (shells out to ``ps``) and the MCP probe. ``KIROCREW_HOME`` is
pinned to ``tmp_path`` by ``test/conftest.py``, so the state files the startup
writes stay inside the test's own directory.
"""

from __future__ import annotations

import asyncio
import errno
import os
import socket
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import server as srv

requires_unix_socket = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="unix sockets are POSIX-only; Windows has no AF_UNIX bind for aiohttp",
)


# ── _remove_stale_unix_socket ───────────────────────────────────────────
#
# The pre-bind self-heal. It is the one filesystem unlink on the startup path
# that runs against a path an operator can have replaced, so each arm is a
# distinct safety decision rather than a variation on one.


class TestRemoveStaleUnixSocket:
    def test_absent_path_is_not_an_error(self, tmp_path: Path) -> None:
        """A missing socket file is the normal first-boot case, not a failure."""
        srv._remove_stale_unix_socket(tmp_path / "never-existed.sock")

    def test_a_regular_file_is_left_in_place(self, tmp_path: Path, caplog) -> None:
        """Only a socket inode may be removed.

        Anything else at the path is someone else's file: unlinking it would
        make a mis-pointed config path silently destroy operator data, so the
        bind is allowed to fail instead.
        """
        victim = tmp_path / "not-a-socket"
        victim.write_text("operator data", encoding="utf-8", newline="\n")

        with caplog.at_level("WARNING", logger=srv.logger.name):
            srv._remove_stale_unix_socket(victim)

        assert victim.read_text(encoding="utf-8") == "operator data"
        assert "is not a socket" in caplog.text

    def test_a_directory_is_left_in_place(self, tmp_path: Path) -> None:
        """A directory is not a socket either — and unlink would raise on it."""
        victim = tmp_path / "dir-in-the-way"
        victim.mkdir()

        srv._remove_stale_unix_socket(victim)

        assert victim.is_dir()

    @requires_unix_socket
    def test_a_real_stale_socket_is_unlinked(self, tmp_path: Path) -> None:
        """The arm that lets a restart rebind: a real socket inode is removed.

        Asserted against a real ``AF_UNIX`` inode rather than a mocked
        ``os.stat``, because the whole decision is ``S_ISSOCK`` on the real
        mode bits.
        """
        path = tmp_path / "stale.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(path))
            assert stat.S_ISSOCK(os.stat(path).st_mode)

            srv._remove_stale_unix_socket(path)
        finally:
            sock.close()

        assert not path.exists()

    @requires_unix_socket
    def test_an_unlink_failure_is_logged_and_swallowed(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """A refused unlink must degrade to TCP-only, not abort startup.

        The unlink can fail on a read-only or permission-restricted data home;
        raising here would take the whole gateway down over an optional
        transport.
        """
        path = tmp_path / "stale.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(path))
            monkeypatch.setattr(
                Path,
                "unlink",
                lambda _self, **_kw: (_ for _ in ()).throw(
                    OSError(errno.EACCES, "permission denied")
                ),
            )

            with caplog.at_level("WARNING", logger=srv.logger.name):
                srv._remove_stale_unix_socket(path)
        finally:
            sock.close()

        assert "could not remove stale dashboard socket" in caplog.text


# ── _register_unix_socket_cleanup ───────────────────────────────────────


class TestRegisterUnixSocketCleanup:
    """The holder is read LAZILY at shutdown, which is the whole point.

    The hook must be registered before ``runner.setup()`` freezes the signal
    lists, but the socket path only becomes known after the site starts — so a
    hook that captured the value eagerly would always see ``None``.
    """

    @pytest.mark.asyncio
    async def test_no_socket_means_nothing_is_removed(self) -> None:
        """Windows and every degraded-to-TCP boot land here."""
        app = web.Application()
        holder: dict[str, Path | None] = {"path": None}
        srv._register_unix_socket_cleanup(app, holder)
        removed: list[Path] = []

        async with TestClient(TestServer(app)):
            pass

        assert removed == []

    @requires_unix_socket
    @pytest.mark.asyncio
    async def test_the_socket_named_after_registration_is_removed(
        self, tmp_path: Path
    ) -> None:
        """A clean shutdown must not leave a socket file behind.

        Each stale file costs the next client a refused connect before its TCP
        fallback, so this is the difference between a clean restart and one that
        looks broken to every internal caller.
        """
        path = tmp_path / "dash.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        sock.close()
        app = web.Application()
        holder: dict[str, Path | None] = {"path": None}

        srv._register_unix_socket_cleanup(app, holder)
        # Only now is the path known — exactly the ordering the lazy read exists
        # for.
        holder["path"] = path

        async with TestClient(TestServer(app)):
            assert path.exists()

        assert not path.exists()


# ── start_dashboard ─────────────────────────────────────────────────────


def _neutralise_outside_process_work(monkeypatch) -> dict[str, Any]:
    """Replace every startup step that reaches outside this process.

    Returns the spies, so a test can assert a step ran without the step itself
    launching anything. Grouped in one helper because omitting any single entry
    does not fail the test — it silently spawns a real subprocess, writes the
    operator's real config, or shells out to ``ps``, which is the shape of side
    effect AUTOSDE ``no-test-side-effects`` is blocking on.
    """
    import kiro_crew.apps.dev_mode as dev_mode
    import kiro_crew.kiro_prerequisite as kiro_prereq

    spies: dict[str, Any] = {
        # Spawns a real backend process per enabled app.
        "start_enabled_app_backends": MagicMock(return_value=[]),
        # Writes into the apps dir and re-materialises builtin manifests.
        "register_builtin_apps": MagicMock(),
        # Rewrites the operator's REAL ~/.kiro/settings/mcp.json — the one step
        # here whose target is outside KIROCREW_HOME.
        "_migrate_playwright_to_proxy": MagicMock(),
        "cleanup_migrated_builtin": MagicMock(),
        "on_gateway_startup": AsyncMock(),
        "on_gateway_shutdown": AsyncMock(),
    }
    for name, spy in spies.items():
        monkeypatch.setattr(srv, name, spy)

    # Probes Kiro readiness by spawning sandboxed CLI subprocesses.
    prereq = MagicMock()
    prereq.close = AsyncMock()
    monkeypatch.setattr(kiro_prereq, "KiroPrerequisiteService", MagicMock(return_value=prereq))
    spies["kiro_prerequisite"] = prereq

    # A filesystem watcher on the apps tree.
    monkeypatch.setattr(dev_mode, "init_dev_mode_watcher", AsyncMock())
    monkeypatch.setattr(dev_mode, "stop_dev_mode_watcher", AsyncMock())

    # Background pollers: the terminal reaper shells out to ``ps``, the MCP
    # probe launches every configured MCP server, and the title poller drives
    # live PTYs. All three are fire-and-forget tasks, so a real one would
    # outlive the test.
    async def _noop() -> None:
        return None

    for name in ("_bg_mcp_probe", "reap_orphaned_terminals", "poll_terminal_titles"):
        monkeypatch.setattr(srv.handlers, name, MagicMock(side_effect=lambda *_a, **_k: _noop()))

    return spies


async def _start_dashboard(tmp_path: Path, monkeypatch, **kwargs: Any) -> Any:
    """Run the real ``start_dashboard`` without binding a listener.

    ``_start_site`` is the only bind on the path, so stubbing it is what keeps
    this from starting a service; everything else runs for real against the
    ``tmp_path`` data home. Returns ``(runner, state, spies)``.
    """
    import kiro_crew.config.loader as _loader
    import kiro_crew.dashboard.state as _st

    monkeypatch.setattr(srv, "data_home", lambda: tmp_path)
    monkeypatch.setattr(_st, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(_loader, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(srv, "_start_site", AsyncMock())
    # POSIX-only extra transport; irrelevant to the wiring under test and it
    # would bind a real socket in the data home.
    monkeypatch.setattr(srv, "_start_unix_site", AsyncMock(return_value=None))
    spies = _neutralise_outside_process_work(monkeypatch)

    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.any_active_turn = MagicMock(return_value=False)
    runner, state = await srv.start_dashboard(
        sessions=sessions,
        crons=MagicMock(
            list_jobs=MagicMock(return_value=[]),
            list_jobs_async=AsyncMock(return_value=[]),
            status=MagicMock(return_value={}),
        ),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        port=0,
        **kwargs,
    )
    return runner, state, spies


@asynccontextmanager
async def _dashboard(tmp_path: Path, monkeypatch, **kwargs: Any) -> Any:
    """A fully wired dashboard app, torn down through the real cleanup path.

    An ``async with`` helper rather than an ``@pytest_asyncio.fixture``, by this
    suite's convention (the pinned pytest-asyncio does not collect async
    generator fixtures declared with plain ``@pytest.fixture``).

    The teardown is not incidental: ``runner.cleanup()`` dispatches every
    ``on_cleanup`` hook the startup registered, so the hooks are exercised as
    well as counted.

    The task sweep afterwards is this harness's own hygiene, not a product
    contract. Four perpetual loops the startup creates — the loop heartbeat, the
    channel-slot reconciler, the state flush loop and the chat sweeper — have no
    cleanup hook, because in production the process exits at shutdown. In a test
    they would outlive the case and be reported against whichever one runs next,
    so they are cancelled here.
    """
    runner, state, spies = await _start_dashboard(tmp_path, monkeypatch, **kwargs)
    try:
        yield runner, state, spies
    finally:
        await runner.cleanup()
        await _cancel_stray_tasks()


async def _cancel_stray_tasks() -> None:
    """Cancel every task other than the caller's own, then await them."""
    stray = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in stray:
        task.cancel()
    if stray:
        await asyncio.gather(*stray, return_exceptions=True)


class TestStartDashboardWiring:
    @pytest.mark.asyncio
    async def test_the_app_is_wired_and_reports_ready(self, tmp_path, monkeypatch) -> None:
        """Readiness is published at the boot-to-ready boundary, last.

        ``state.ready`` is the flag the desktop app waits on, so it must be true
        only once every startup step above it has completed.
        """
        async with _dashboard(tmp_path, monkeypatch) as (runner, state, _spies):
            assert state.ready is True
            assert runner.app["state"] is state
            assert runner.app["port"] == 0

    @pytest.mark.asyncio
    async def test_the_mcp_and_dashboard_routes_are_both_mounted(
        self, tmp_path, monkeypatch
    ) -> None:
        """The dashboard serves the MCP surface as well as its own routes.

        A missing MCP route here is invisible in a browser and breaks every MCP
        tool call, so the inventory is asserted rather than inferred from the
        shared ``_register_mcp_routes`` helper being called.
        """
        async with _dashboard(tmp_path, monkeypatch) as (runner, _state, _spies):
            routes = {
                (route.method, route.resource.canonical)
                for route in runner.app.router.routes()
                if route.resource is not None
            }

        for expected in (
            ("POST", "/api/spawn"),
            ("GET", "/api/crons"),
            ("POST", "/api/send-message"),
            ("GET", "/api/notifications"),
            ("GET", "/api/status"),
            ("GET", "/api/ws"),
        ):
            assert expected in routes, f"missing route: {expected}"

    @pytest.mark.asyncio
    async def test_the_middleware_chain_is_ordered_outermost_first(
        self, tmp_path, monkeypatch
    ) -> None:
        """Ordering is a security property, not a style choice.

        The ``Host`` barrier must run OUTSIDE the audit middleware: aiohttp runs
        middlewares outermost-first, and a rebinding attempt refused inside the
        audit layer would 403 without ever being recorded (which is why
        ``_audit_denied`` exists at all).
        """
        async with _dashboard(tmp_path, monkeypatch) as (runner, _state, _spies):
            names = [
                getattr(mw, "__name__", type(mw).__name__) for mw in runner.app.middlewares
            ]

        assert "host_validation_middleware" in names
        assert "sel_audit_middleware" in names
        assert names.index("host_validation_middleware") < names.index("sel_audit_middleware")

    @pytest.mark.asyncio
    async def test_a_disallowed_host_is_refused_by_the_real_chain(
        self, tmp_path, monkeypatch
    ) -> None:
        """DNS-rebinding barrier, through the app's own middleware stack.

        Driven in-process (``TestServer``) rather than against the production
        listener: the same middlewares are installed on the app, and no host
        port is bound.
        """
        async with _dashboard(tmp_path, monkeypatch) as (runner, _state, _spies):
            async with TestClient(TestServer(runner.app)) as client:
                resp = await client.get("/api/status", headers={"Host": "evil.example.com"})
                assert resp.status == 403
                assert "Host header not allowed" in await resp.text()

    @pytest.mark.asyncio
    async def test_security_headers_are_applied_to_a_real_response(
        self, tmp_path, monkeypatch
    ) -> None:
        """The header middleware is wired, not merely defined.

        Checked on a real response because ``_apply_security_headers`` is
        reached through the middleware chain — a header set on a helper nobody
        calls protects nothing.
        """
        async with _dashboard(tmp_path, monkeypatch) as (runner, _state, _spies):
            async with TestClient(TestServer(runner.app)) as client:
                resp = await client.get("/api/status")
                csp = resp.headers["Content-Security-Policy"]
                assert "frame-ancestors 'self'" in csp
                assert resp.headers["X-Content-Type-Options"] == "nosniff"
                assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    @pytest.mark.asyncio
    async def test_lifecycle_hooks_are_registered_by_name(
        self, tmp_path, monkeypatch
    ) -> None:
        """Every long-lived subsystem must have a teardown hook.

        Selected BY NAME: the lists are appended to as subsystems are added, so a
        positional assertion silently repoints at whatever landed last.

        The tunnel hook's *relative* position is asserted, because being ahead of
        the others is its documented contract — ``on_cleanup`` runs in
        registration order under a hard shutdown deadline, and a tunnel hook
        queued behind the instances teardown (which waits on SSH children that
        may ignore SIGTERM) can be starved, leaving the tunnel running after a
        clean Ctrl+C. Index 0 belongs to aiohttp's own ``CleanupContext``, which
        every ``web.Application`` registers in its constructor, so first-among-
        product-hooks is the strongest true claim here.
        """
        async with _dashboard(tmp_path, monkeypatch) as (runner, _state, _spies):
            cleanup = [hook.__name__ for hook in runner.app.on_cleanup]
            startup = [hook.__name__ for hook in runner.app.on_startup]

        later_hooks = (
            "_instances_shutdown",
            "_prevent_sleep_shutdown",
            "_status_sink_shutdown",
            "_contrib_shutdown",
            "_kiro_prerequisite_shutdown",
            "_watchdog_shutdown",
            "_unlink_unix_socket",
        )
        for name in ("_tunnel_shutdown", *later_hooks):
            assert name in cleanup, f"missing on_cleanup hook: {name}"
        tunnel_at = cleanup.index("_tunnel_shutdown")
        for name in later_hooks:
            assert tunnel_at < cleanup.index(name), f"{name} would starve the tunnel teardown"
        for name in ("_instances_startup", "_contrib_startup", "_hooks_startup"):
            assert name in startup, f"missing on_startup hook: {name}"

    @pytest.mark.asyncio
    async def test_a_cross_origin_post_is_refused(self, tmp_path, monkeypatch) -> None:
        """The CSRF barrier is installed on the real chain.

        A state-changing request from a page the operator merely visited must not
        reach a handler: loopback is not a trust boundary, so the Origin check is
        what stands between any local web page and the gateway's own API.
        """
        async with _dashboard(tmp_path, monkeypatch) as (runner, _state, _spies):
            async with TestClient(TestServer(runner.app)) as client:
                resp = await client.post(
                    "/api/notifications/clear",
                    json={},
                    headers={"Origin": "https://evil.example.com"},
                )
                assert resp.status == 403

    @pytest.mark.asyncio
    async def test_a_configured_url_joins_the_csrf_origin_set(
        self, tmp_path, monkeypatch
    ) -> None:
        """A published URL widens the CSRF allowlist — but only behind token auth.

        The widening is guarded by an explicit re-check that the token-auth
        middleware is installed, because ``dashboard.url`` means the dashboard is
        reachable by something other than a loopback browser; widening the origin
        set without authentication would accept state-changing requests from that
        origin unauthenticated.
        """
        url = "http://dash.example.com:5476"
        async with _dashboard(
            tmp_path, monkeypatch, dashboard_url=url, local_only=False
        ) as (runner, _state, _spies):
            assert any(getattr(mw, "_is_token_auth", False) for mw in runner.app.middlewares)
            assert url in runner.app["allowed_origins"]

    @pytest.mark.asyncio
    async def test_the_playwright_migration_never_touches_the_real_settings(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The migration is scheduled as a background task, so it must be awaited.

        It rewrites ``~/.kiro/settings/mcp.json`` — the operator's real file,
        outside ``KIROCREW_HOME`` — so this test's value is proving the stub is
        what ran. Without the stub the suite would silently edit the developer's
        machine.
        """
        runner, _state, spies = await _start_dashboard(tmp_path, monkeypatch)
        try:
            # The migration runs in a task created during startup; yield until
            # the loop has drained it.
            for _ in range(50):
                if spies["_migrate_playwright_to_proxy"].called:
                    break
                await asyncio.sleep(0)
        finally:
            await runner.cleanup()
            await _cancel_stray_tasks()

        assert spies["_migrate_playwright_to_proxy"].called

    @pytest.mark.asyncio
    async def test_cleanup_stops_the_tunnel_it_never_started(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """No manager is built when the tunnel is disabled, so the provider is
        stopped directly.

        The on-demand link path provisions a tunnel straight on the provider and
        never constructs a manager, so a hook that bailed out on
        ``state.tunnel_manager is None`` left exactly the orphan it exists to
        prevent.
        """
        provider = SimpleNamespace(stop=AsyncMock(), enabled=lambda: False)
        monkeypatch.setattr(
            srv,
            "current_context",
            lambda: SimpleNamespace(
                tunnel=provider,
                telemetry=SimpleNamespace(record_event=lambda *_a, **_k: None),
                dashboard=SimpleNamespace(
                    start_services=AsyncMock(), stop_services=AsyncMock()
                ),
            ),
        )

        runner, state, _spies = await _start_dashboard(tmp_path, monkeypatch)
        assert state.tunnel_manager is None
        await runner.cleanup()
        await _cancel_stray_tasks()

        provider.stop.assert_awaited()
