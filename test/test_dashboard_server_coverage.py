"""Coverage tests for the residual branches of ``kiro_crew.dashboard.server``.

The module's route/middleware wiring is already pinned elsewhere
(``test_api_server.py``, ``test_api_health.py``, ``test_dashboard_static_routes.py``,
``test_dashboard_security_headers.py``, ``test_write_secret_file.py``,
``test_dashboard_yolo_startup.py``). What was left untested were the *failure*
and *edge* paths of the extracted helpers — the ones that exist precisely so a
config hiccup, a hostile symlink, an unreachable SSH host or a raising OS
inhibitor can never wedge or crash gateway startup. Those are exercised here
against real behaviour (real ``aiohttp`` apps, real ``on_cleanup`` dispatch,
real ``asyncio`` tasks) rather than by asserting internals.

Everything stays inside ``tmp_path``: no network, no subprocess, no fixed port
(``port=0`` only), and ``SleepInhibitor`` is always replaced so no
``caffeinate`` / ``systemd-inhibit`` process is ever started.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from conftest import requires_symlinks
from kiro_crew.dashboard import server as srv

# ── helpers ─────────────────────────────────────────────────────────────


class _FakeInhibitor:
    """Stand-in for ``power.SleepInhibitor``.

    The real one shells out to ``caffeinate`` / ``systemd-inhibit``; this
    records the requested states instead so the poll's behaviour is observable
    without a subprocess. ``raise_on`` models an OS refusal.
    """

    def __init__(self, raise_on: bool | None = None) -> None:
        self.states: list[bool] = []
        self._raise_on = raise_on

    def set_active(self, active: bool) -> None:
        self.states.append(active)
        if self._raise_on is not None and active is self._raise_on:
            raise RuntimeError("inhibitor refused")


def _state(**kwargs: Any) -> Any:
    """A duck-typed DashboardState stand-in.

    Every helper under test reaches its collaborators through ``getattr`` with a
    default, so a namespace is a faithful stand-in and keeps the test away from
    ``DashboardState``'s real (config-dir touching) constructor.
    """
    kwargs.setdefault("_background_tasks", set())
    return SimpleNamespace(**kwargs)


def _cfg(*, prevent_sleep: bool = False) -> Any:
    return SimpleNamespace(dashboard=SimpleNamespace(prevent_sleep=prevent_sleep))


# ── _should_prevent_sleep ───────────────────────────────────────────────


class TestShouldPreventSleep:
    """Fail-closed contract: anything that goes wrong resolves to "allow sleep",
    because the failure mode of the opposite default is a laptop that never
    sleeps again."""

    @pytest.mark.asyncio
    async def test_config_read_failure_allows_sleep(self, monkeypatch) -> None:
        loader = MagicMock()
        loader.load.side_effect = RuntimeError("corrupt config")
        monkeypatch.setattr(srv, "KiroCrewConfig", loader)

        assert await srv._should_prevent_sleep(_state(sessions=MagicMock())) is False

    @pytest.mark.asyncio
    async def test_opt_out_allows_sleep_without_consulting_sessions(
        self, monkeypatch
    ) -> None:
        loader = MagicMock()
        loader.load.return_value = _cfg(prevent_sleep=False)
        monkeypatch.setattr(srv, "KiroCrewConfig", loader)
        sessions = MagicMock()

        assert await srv._should_prevent_sleep(_state(sessions=sessions)) is False
        assert not sessions.any_active_turn.called

    @pytest.mark.asyncio
    async def test_missing_session_manager_allows_sleep(self, monkeypatch) -> None:
        loader = MagicMock()
        loader.load.return_value = _cfg(prevent_sleep=True)
        monkeypatch.setattr(srv, "KiroCrewConfig", loader)

        assert await srv._should_prevent_sleep(_state(sessions=None)) is False

    @pytest.mark.asyncio
    async def test_active_turn_blocks_sleep(self, monkeypatch) -> None:
        loader = MagicMock()
        loader.load.return_value = _cfg(prevent_sleep=True)
        monkeypatch.setattr(srv, "KiroCrewConfig", loader)
        sessions = MagicMock(any_active_turn=MagicMock(return_value=True))

        assert await srv._should_prevent_sleep(_state(sessions=sessions)) is True

    @pytest.mark.asyncio
    async def test_active_turn_probe_failure_allows_sleep(self, monkeypatch) -> None:
        loader = MagicMock()
        loader.load.return_value = _cfg(prevent_sleep=True)
        monkeypatch.setattr(srv, "KiroCrewConfig", loader)
        sessions = MagicMock(
            any_active_turn=MagicMock(side_effect=RuntimeError("map busy"))
        )

        assert await srv._should_prevent_sleep(_state(sessions=sessions)) is False


# ── _extra_frame_ancestors ──────────────────────────────────────────────


class TestExtraFrameAncestors:
    def test_out_of_range_stashed_port_is_rejected_not_trusted(
        self, monkeypatch
    ) -> None:
        """A digits-only but out-of-range stashed claim must not become an origin.

        ``0`` and ``70000`` pass ``.isdigit()``, so only the range check stops
        them from being formatted straight into ``frame-ancestors``. The reader
        then falls through to the token path, which here yields nothing.
        """
        monkeypatch.setattr(srv, "token_embed_parent_port", lambda _token: None)
        for bogus in ("0", "70000"):
            request = make_mocked_request("GET", "/")
            request["embed_parent_port"] = bogus
            assert srv._extra_frame_ancestors(request, None) == []

    def test_no_request_yields_no_extra_ancestors(self) -> None:
        assert srv._extra_frame_ancestors(None) == []


# ── discover_app_window_entries / _window_entry_handler ─────────────────


def _windows_root(tmp_path: Path) -> Path:
    root = tmp_path / "dist" / "src" / "apps"
    root.mkdir(parents=True)
    return root


class TestDiscoverAppWindowEntries:
    def test_absent_root_yields_nothing(self, tmp_path) -> None:
        assert srv.discover_app_window_entries(tmp_path / "nope") == []

    def test_entries_are_routed_by_app_and_window_name(self, tmp_path) -> None:
        root = _windows_root(tmp_path)
        (root / "meetings").mkdir()
        (root / "meetings" / "board.html").write_text("<html></html>", encoding="utf-8")

        entries = srv.discover_app_window_entries(root)

        assert [route for route, _ in entries] == [
            f"/{srv.APP_WINDOW_URL_PREFIX}/meetings/board.html"
        ]
        assert entries[0][1] == (root / "meetings" / "board.html").resolve()

    @requires_symlinks
    def test_symlink_escaping_the_build_tree_is_refused(self, tmp_path, caplog) -> None:
        """A symlink planted inside dist/ must not be served.

        Every discovered path is handed to ``web.FileResponse`` — an
        unconditional read of whatever it points at — so the containment check
        is the only thing standing between a planted link and arbitrary file
        disclosure.
        """
        outside = tmp_path / "outside" / "secret.html"
        outside.parent.mkdir()
        outside.write_text("secret", encoding="utf-8")
        root = _windows_root(tmp_path)
        (root / "evil").mkdir()
        os.symlink(outside, root / "evil" / "leak.html")

        with caplog.at_level(logging.ERROR, logger=srv.logger.name):
            assert srv.discover_app_window_entries(root) == []
        assert "resolves outside the build tree" in caplog.text


class TestWindowEntryHandler:
    @pytest.mark.asyncio
    async def test_handler_serves_the_file_bound_at_registration(
        self, tmp_path
    ) -> None:
        entry = tmp_path / "board.html"
        entry.write_text("<h1>board</h1>", encoding="utf-8")
        app = web.Application()
        app.router.add_get("/w.html", srv._window_entry_handler(entry))

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/w.html")
            assert resp.status == 200
            assert await resp.text() == "<h1>board</h1>"

    @pytest.mark.asyncio
    async def test_app_window_entries_are_registered_as_get_routes(
        self, tmp_path
    ) -> None:
        """``_register_dist_static_routes`` must mount every discovered window.

        A missing mount is not a small failure: the request falls through to the
        SPA shell, so the window opens showing a whole dashboard instead of its
        own UI.
        """
        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "app-assets").mkdir()
        root = dist / "src" / "apps"
        (root / "papyrus").mkdir(parents=True)
        (root / "papyrus" / "editor.html").write_text("<html></html>", encoding="utf-8")
        app = web.Application()

        srv._register_dist_static_routes(app, dist)

        canonical = {r.resource.canonical for r in app.router.routes() if r.resource}
        assert f"/{srv.APP_WINDOW_URL_PREFIX}/papyrus/editor.html" in canonical
        # App Store brand assets: without this mount the builtin app icons and
        # hero images fall through to the SPA shell and render as placeholders.
        prefixes = {
            r.get_info().get("prefix")
            for r in app.router.resources()
            if r.get_info().get("prefix")
        }
        assert "/app-assets" in prefixes


# ── _migrate_playwright_to_proxy / _precompute_telemetry ────────────────


def test_migrate_playwright_delegates_to_the_owned_registration_sweep(
    monkeypatch,
) -> None:
    called = MagicMock()
    monkeypatch.setattr(srv, "migrate_owned_playwright_registration", called)

    srv._migrate_playwright_to_proxy()

    called.assert_called_once_with()


class TestPrecomputeTelemetry:
    """Telemetry is best-effort: no individual failure may abort startup."""

    def test_every_stage_failure_is_swallowed(self, monkeypatch) -> None:
        import kiro_crew.dashboard.handlers_system as hs

        monkeypatch.setattr(
            hs, "_get_owner_hash", MagicMock(side_effect=RuntimeError("no owner"))
        )
        monkeypatch.setattr(
            hs, "_get_static_system_info", MagicMock(side_effect=RuntimeError("no os"))
        )
        monkeypatch.setattr(
            srv, "current_context", MagicMock(side_effect=RuntimeError("no context"))
        )

        srv._precompute_telemetry(_state())  # must not raise

    def test_gateway_start_event_carries_the_precomputed_fields(
        self, monkeypatch
    ) -> None:
        import kiro_crew.dashboard.handlers_system as hs

        monkeypatch.setattr(hs, "_get_owner_hash", MagicMock(return_value="hash-1"))
        monkeypatch.setattr(
            hs,
            "_get_static_system_info",
            MagicMock(return_value={"os": "linux", "arch": "x86_64"}),
        )
        telemetry = MagicMock()
        monkeypatch.setattr(
            srv, "current_context", MagicMock(return_value=SimpleNamespace(telemetry=telemetry))
        )

        srv._precompute_telemetry(_state())

        telemetry.record_event.assert_called_once_with(
            "gateway_start",
            {"owner_id_hash": "hash-1", "os_type": "linux", "arch": "x86_64"},
        )


# ── _write_secret_file ──────────────────────────────────────────────────


class TestWriteSecretFileDescriptorCleanup:
    """The ``finally`` arm closes the fd when the write never took ownership.

    ``test_write_secret_file.py`` covers the ``os.open`` failure (no fd yet);
    these cover a failure AFTER the fd exists, which is the only path that can
    leak a descriptor per failed gateway start.
    """

    def test_permission_hardening_failure_closes_fd_and_removes_file(
        self, tmp_path, monkeypatch
    ) -> None:
        secret_path = tmp_path / ".local_secret"
        monkeypatch.setattr(
            srv.platform_compat,
            "restrict_to_owner",
            MagicMock(side_effect=OSError("chmod denied")),
        )
        closed: list[int] = []
        real_close = os.close

        def _spy(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        with patch("kiro_crew.dashboard.server.os.close", _spy):
            with pytest.raises(OSError, match="chmod denied"):
                srv._write_secret_file(secret_path, "s")

        assert closed, "the open descriptor must be closed on the failure path"
        assert not secret_path.exists()

    def test_close_failure_does_not_mask_the_original_error(
        self, tmp_path, monkeypatch
    ) -> None:
        secret_path = tmp_path / ".local_secret"
        monkeypatch.setattr(
            srv.platform_compat,
            "restrict_to_owner",
            MagicMock(side_effect=OSError("chmod denied")),
        )
        real_close = os.close

        def _close_then_fail(fd: int) -> None:
            real_close(fd)  # keep the descriptor from leaking in-process
            raise OSError("close failed")

        with patch("kiro_crew.dashboard.server.os.close", _close_then_fail):
            with pytest.raises(OSError, match="chmod denied"):
                srv._write_secret_file(secret_path, "s")

        assert not secret_path.exists()


# ── _claimed_dashboard_slots ────────────────────────────────────────────


class TestClaimedDashboardSlots:
    def test_only_dashboard_prefixed_keys_are_claimed(self) -> None:
        sessions = SimpleNamespace(
            _session_map=SimpleNamespace(
                _data={
                    "dashboard:slot-a": {},
                    "dashboard:slot-b": {},
                    "channel:C123": {},
                    "slack:U1": {},
                }
            )
        )

        assert srv._claimed_dashboard_slots(_state(sessions=sessions)) == frozenset(
            {"slot-a", "slot-b"}
        )

    def test_unreadable_map_yields_no_claims(self) -> None:
        sessions = SimpleNamespace(_session_map=SimpleNamespace(_data=None))
        assert srv._claimed_dashboard_slots(_state(sessions=sessions)) == frozenset()

    def test_raising_session_map_yields_no_claims(self) -> None:
        class _Boom:
            @property
            def _session_map(self) -> Any:
                raise RuntimeError("map unavailable")

        assert srv._claimed_dashboard_slots(_state(sessions=_Boom())) == frozenset()


# ── _apply_startup_yolo ─────────────────────────────────────────────────


def test_startup_yolo_survives_a_failing_duration_seed(monkeypatch) -> None:
    """A broken ``yolo_duration`` seed must not abort the rest of startup."""
    monkeypatch.setattr(
        srv, "apply_config_duration", MagicMock(side_effect=RuntimeError("bad policy"))
    )
    granted = MagicMock()
    monkeypatch.setattr(srv, "grant_declared_yolo", granted)
    cfg = SimpleNamespace(agent=SimpleNamespace(dangerously_skip_permissions=False))

    srv._apply_startup_yolo(_state(), cfg)

    # Opt-out config: the seed failure is logged, and no grant is attempted.
    assert not granted.called


# ── _revive_intended_instances ──────────────────────────────────────────


class TestReviveIntendedInstances:
    @pytest.mark.asyncio
    async def test_no_intent_means_no_connect_attempt(self) -> None:
        registry = MagicMock(list=MagicMock(return_value=[SimpleNamespace(id="a", was_connected=False)]))
        manager = MagicMock(connect=AsyncMock())

        await srv._revive_intended_instances(registry, manager)

        assert not manager.connect.called

    @pytest.mark.asyncio
    async def test_one_failing_host_does_not_abort_the_rest(self) -> None:
        """Per-instance isolation is the whole point: a down host must leave its
        own tab in an error state without stopping the other revivals."""
        registry = MagicMock(
            list=MagicMock(
                return_value=[
                    SimpleNamespace(id="boom", was_connected=True),
                    SimpleNamespace(id="degraded", was_connected=True),
                    SimpleNamespace(id="ok", was_connected=True),
                ]
            )
        )
        results = {
            "boom": RuntimeError("ssh refused"),
            "degraded": SimpleNamespace(
                state=srv.TunnelState.ERROR, error="auth required"
            ),
            "ok": SimpleNamespace(state=srv.TunnelState.CONNECTED, error=""),
        }

        async def _connect(inst_id: str) -> Any:
            outcome = results[inst_id]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        manager = MagicMock(connect=AsyncMock(side_effect=_connect))

        await srv._revive_intended_instances(registry, manager)

        assert [c.args[0] for c in manager.connect.await_args_list] == [
            "boom",
            "degraded",
            "ok",
        ]


# ── _register_prevent_sleep_shutdown ────────────────────────────────────


async def _run_cleanup(app: web.Application) -> None:
    """Dispatch the hooks the helpers appended, in registration order."""
    for hook in list(app.on_cleanup):
        await hook(app)


class TestPreventSleepShutdown:
    @pytest.mark.asyncio
    async def test_nothing_armed_is_a_clean_no_op(self) -> None:
        app = web.Application()
        srv._register_prevent_sleep_shutdown(app, _state())

        await _run_cleanup(app)  # neither task nor inhibitor exists yet

    @pytest.mark.asyncio
    async def test_cancels_the_poll_and_releases_the_os_block(self) -> None:
        app = web.Application()
        inhibitor = _FakeInhibitor()

        async def _forever() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(_forever())
        srv._register_prevent_sleep_shutdown(
            app, _state(_prevent_sleep_task=task, _sleep_inhibitor=inhibitor)
        )

        await _run_cleanup(app)
        await asyncio.gather(task, return_exceptions=True)

        assert task.cancelled()
        assert inhibitor.states == [False]

    @pytest.mark.asyncio
    async def test_a_refusing_inhibitor_does_not_break_shutdown(self) -> None:
        """``on_cleanup`` hooks run in sequence and a raise aborts the rest, so a
        stubborn OS inhibitor must never propagate."""
        app = web.Application()
        srv._register_prevent_sleep_shutdown(
            app, _state(_sleep_inhibitor=_FakeInhibitor(raise_on=False))
        )

        await _run_cleanup(app)


# ── _arm_prevent_sleep_poll ─────────────────────────────────────────────


class TestArmPreventSleepPoll:
    """The poll is driven by an ``asyncio.Event`` the fake predicate sets, so
    every test finishes on a real signal rather than a timeout — no wall clock,
    no sleeps.
    """

    @staticmethod
    def _arm(monkeypatch, inhibitor: _FakeInhibitor, interval: Any = 0) -> Any:
        monkeypatch.setattr(srv, "SleepInhibitor", lambda: inhibitor)
        monkeypatch.setattr(srv, "_PREVENT_SLEEP_POLL_INTERVAL_SECS", interval)
        state = _state()
        srv._arm_prevent_sleep_poll(state)
        return state

    @pytest.mark.asyncio
    async def test_poll_applies_the_predicate_then_releases_on_cancel(
        self, monkeypatch
    ) -> None:
        polled = asyncio.Event()

        async def _should(_state_arg: Any) -> bool:
            polled.set()
            return True

        monkeypatch.setattr(srv, "_should_prevent_sleep", _should)
        inhibitor = _FakeInhibitor()
        state = self._arm(monkeypatch, inhibitor)

        await polled.wait()
        assert state._sleep_inhibitor is inhibitor
        state._prevent_sleep_task.cancel()
        await asyncio.gather(state._prevent_sleep_task, return_exceptions=True)

        assert state._prevent_sleep_task.cancelled()
        assert True in inhibitor.states, "an active turn must request the OS block"
        assert inhibitor.states[-1] is False, "cancel must release the OS block"

    @pytest.mark.asyncio
    async def test_a_refusing_inhibitor_keeps_the_poll_alive(self, monkeypatch) -> None:
        polled = asyncio.Event()

        async def _should(_state_arg: Any) -> bool:
            polled.set()
            return True

        monkeypatch.setattr(srv, "_should_prevent_sleep", _should)
        # Refuses only the acquire, so the cancel-path release still succeeds.
        inhibitor = _FakeInhibitor(raise_on=True)
        state = self._arm(monkeypatch, inhibitor)

        await polled.wait()
        task = state._prevent_sleep_task
        assert not task.done(), "a refused acquire must not kill the poll"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_an_unexpected_poll_crash_is_reported_not_silent(
        self, monkeypatch, caplog
    ) -> None:
        """A poll that dies outside the guarded toggle must surface in the log.

        A non-numeric interval makes ``asyncio.sleep`` raise, which is the one
        way out of the loop that neither the inner guard nor the
        ``CancelledError`` arm handles — the case the done-callback exists for.
        """
        inhibitor = _FakeInhibitor()
        with caplog.at_level(logging.ERROR, logger=srv.logger.name):
            state = self._arm(monkeypatch, inhibitor, interval="not-a-number")
            result = await asyncio.gather(
                state._prevent_sleep_task, return_exceptions=True
            )
            await asyncio.sleep(0)  # let the done-callback run

        assert isinstance(result[0], TypeError)
        assert "prevent-sleep poll task exited unexpectedly" in caplog.text


# ── start_api_server residual paths ─────────────────────────────────────


async def _start_api(tmp_path: Path, monkeypatch, **kwargs: Any) -> Any:
    """Start the headless API server WITHOUT binding a real listener.

    ``data_home`` (the server's own module-scope binding, used for
    ``.local_secret``), ``state.config_dir`` and ``loader.config_dir`` (resolved
    lazily by ``token_auth``) are all redirected into ``tmp_path``.

    The listener is neutralised too. ``start_api_server`` constructs
    ``web.TCPSite(runner, bind_addr, port)`` and then binds it inside
    ``_start_site``; constructing the site is inert, so stubbing ``_start_site``
    alone means no host port is ever bound. AUTOSDE `no-test-side-effects` is
    `blocking: true` and forbids a test starting a service -- and an ephemeral
    ``port=0`` does not exempt it, because the rule is about the side effect, not
    the port number. (``test/test_api_server.py`` on main does bind for real;
    that is a pre-existing violation, not a licence for a new one.)

    Stubbing only the bind keeps every residual path this module exists to
    cover -- notably the secret-write failure arm, which still reaches
    ``runner.cleanup()``.
    """
    import kiro_crew.config.loader as _loader
    import kiro_crew.dashboard.state as _st

    monkeypatch.setattr(srv, "data_home", lambda: tmp_path)
    monkeypatch.setattr(_st, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(_loader, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(srv, "_start_site", AsyncMock())

    return await srv.start_api_server(
        sessions=MagicMock(count=0),
        crons=MagicMock(
            list_jobs=MagicMock(return_value=[]),
            list_jobs_async=AsyncMock(return_value=[]),
            status=MagicMock(return_value={}),
        ),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        port=0,
        **kwargs,
    )


class TestStartApiServerResidualPaths:
    @pytest.mark.asyncio
    async def test_secret_persistence_failure_tears_the_listener_down(
        self, tmp_path, monkeypatch
    ) -> None:
        """A failed ``.local_secret`` write must not leave the port bound.

        The write is deliberately deferred until after the bind, so this arm is
        the only thing that stops a half-started gateway from holding the port
        that the next start would then have to reclaim.
        """
        runners: list[Any] = []
        real_builder = srv.build_hardened_runner

        def _spy(app: web.Application, **kw: Any) -> Any:
            runner = real_builder(app, **kw)
            runners.append(runner)
            return runner

        monkeypatch.setattr(srv, "build_hardened_runner", _spy)
        monkeypatch.setattr(
            srv,
            "_write_secret_file",
            MagicMock(side_effect=OSError("read-only filesystem")),
        )

        with pytest.raises(OSError, match="read-only filesystem"):
            await _start_api(tmp_path, monkeypatch)

        assert runners, "the runner must have been built before the write"
        assert not runners[0].addresses, "cleanup must have removed the bound site"

    @pytest.mark.asyncio
    async def test_a_raising_handler_is_audited_as_an_error_and_re_raised(
        self, tmp_path, monkeypatch
    ) -> None:
        """The SEL audit middleware must record a crashed MCP call, not swallow it.

        An unhandled handler exception is exactly the event an audit log needs;
        the middleware logs ``outcome=error`` and re-raises so aiohttp still
        answers 500.
        """
        audit = MagicMock()
        monkeypatch.setattr(srv, "sel", MagicMock(return_value=audit))
        subagents = MagicMock()
        subagents.spawn.side_effect = RuntimeError("spawn exploded")

        runner, _state_obj = await _start_api(
            tmp_path, monkeypatch, subagents=subagents
        )
        try:
            secret = (tmp_path / ".local_secret").read_text(encoding="utf-8").strip()
            # Drive the app through the in-process harness rather than the
            # production listener: the audit middleware is installed on the app,
            # so the same stack runs, and no host port is bound (AUTOSDE
            # no-test-side-effects is blocking and forbids starting a service).
            async with TestClient(TestServer(runner.app)) as client:
                resp = await client.post(
                    "/api/spawn",
                    json={"task": "noop"},
                    headers={"X-Internal-Secret": secret},
                )
                assert resp.status == 500
        finally:
            await runner.cleanup()

        errors = [
            call.kwargs
            for call in audit.log_api_access.call_args_list
            if call.kwargs.get("outcome") == "error"
            and call.kwargs.get("resources") == "/api/spawn"
        ]
        assert errors, "the crashed MCP call must be audited"
        assert "spawn exploded" in errors[-1]["error"]

    @pytest.mark.asyncio
    async def test_a_successful_api_call_is_audited_as_ok(
        self, tmp_path, monkeypatch
    ) -> None:
        """The success arm of the audit must record the call, not only failures.

        (The middleware's non-``/api/`` pass-through is unreachable on this
        server: every route it mounts is under ``/api/``, and anything else is
        denied by token auth before the audit runs.)
        """
        audit = MagicMock()
        monkeypatch.setattr(srv, "sel", MagicMock(return_value=audit))

        runner, _state_obj = await _start_api(tmp_path, monkeypatch)
        try:
            secret = (tmp_path / ".local_secret").read_text(encoding="utf-8").strip()
            # In-process harness, not the production listener -- see the sibling
            # test above for why.
            async with TestClient(TestServer(runner.app)) as client:
                resp = await client.get(
                    "/api/crons", headers={"X-Internal-Secret": secret}
                )
                assert resp.status == 200
        finally:
            await runner.cleanup()

        audited = {
            call.kwargs.get("resources"): call.kwargs.get("outcome")
            for call in audit.log_api_access.call_args_list
        }
        assert audited.get("/api/crons") == "ok"
