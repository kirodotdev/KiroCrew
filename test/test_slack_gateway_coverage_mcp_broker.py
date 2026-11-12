"""Coverage for ``kiro_crew.slack.gateway``: the MCP-gateway control plane.

``_init_mcp_gateway``'s broker startup, its early returns and the rewriter-failure
fallback; the retry after a failed start; ``_stop_mcp_broker``, ``_apply_mcp_stub`` and
``_wire_mcp_gateway_dashboard``; and the broker's refresh, prefetch and
approval-persist arms. No real broker socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_coverage_helpers import _make_orchestrator, _mock_dashboard_state

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mcp_gateway.manager import GatewayManager, GatewaySpec
from kiro_crew.slack import gateway as gw
from kiro_crew.testing.wait import async_wait_until, await_until, finished

# ═════════════════════════════════════════════════════════════════════════
# MCP-gateway control plane
# ═════════════════════════════════════════════════════════════════════════


def _gateway_rewrite_inputs(tmp_path):
    def _inputs(_cfg, stubs):
        return {"socket_path": tmp_path / "gw.sock", "stub_servers": stubs}

    return _inputs


class TestInitMcpGateway:
    """Broker startup, its two early returns and the rewriter-failure fallback."""

    @pytest.mark.asyncio
    async def test_nothing_routed_returns_without_touching_platform_probe(self):
        """The shipped default: no stubbed server, so no broker and no probe."""
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = False
        orch._cfg.mcp_gateway.stub_servers = []
        with patch("kiro_crew.slack.gateway.is_gateway_supported") as probe:
            await orch._init_mcp_gateway()
        probe.assert_not_called()
        assert orch._mcp_gateway_manager is None

    @pytest.mark.asyncio
    async def test_a_routed_server_starts_the_broker_with_sharing_off(self):
        """Sharing off must not keep the broker down for a stubbed server.

        A stubbed server needs its stub, and the stub needs the broker's socket.
        Sharing decides how that server's backend is acquired, so gating the
        broker on it would make stub-only — the useful state for a stateful
        server — unreachable.
        """
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = False
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp"]
        with patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=False) as probe:
            await orch._init_mcp_gateway()
        # The routed server reached the platform probe; the unsupported answer
        # is what keeps the broker down here.
        probe.assert_called_once_with()
        assert orch._mcp_gateway_manager is None

    @pytest.mark.asyncio
    async def test_turning_sharing_off_restarts_rather_than_stops_when_routed(self):
        """The live-apply path must not strand MCP Apps.

        With sharing off and apps on, the broker must come back (a stop strands
        app render paths) and must RESTART: the rewriter reads the sharing flag at
        start, so only a restart re-emits stubs without ``--poolable``.

        The fixture names the SERVED set: a stub change waits for the next gateway
        start, so the configured list is not it.
        """
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = False
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp"]
        orch._mcp_gateway_manager = object()  # a broker is currently up
        orch._mcp_stub_servers_started = frozenset({"alpha-mcp"})  # serving that stub
        calls: list[str] = []

        async def _stop() -> None:
            calls.append("stop")
            orch._mcp_gateway_manager = None

        async def _init(stub_servers: frozenset[str] | None = None) -> None:
            calls.append("init")
            assert stub_servers == frozenset({"alpha-mcp"})

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=orch._cfg):
            with (
                patch.object(orch, "_stop_mcp_broker", _stop),
                patch.object(orch, "_init_mcp_gateway", _init),
            ):
                await orch._apply_mcp_gateway_enabled(False)

        assert calls == ["stop", "init"]

    @pytest.mark.asyncio
    async def test_turning_sharing_off_with_apps_off_leaves_the_broker_down(self):
        """The guard must not leak the other way: with neither switch on, the
        broker stays stopped rather than being restarted for nothing."""
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = False
        orch._cfg.mcp_gateway.stub_servers = []
        orch._mcp_gateway_manager = object()
        calls: list[str] = []

        async def _stop() -> None:
            calls.append("stop")
            orch._mcp_gateway_manager = None

        async def _init() -> None:
            calls.append("init")

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=orch._cfg):
            with (
                patch.object(orch, "_stop_mcp_broker", _stop),
                patch.object(orch, "_init_mcp_gateway", _init),
            ):
                await orch._apply_mcp_gateway_enabled(False)

        assert calls == ["stop"]

    @pytest.mark.asyncio
    async def test_unsupported_platform_returns_early(self):
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        with patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=False):
            with patch("kiro_crew.slack.gateway.rewrite_agents") as rewriter:
                await orch._init_mcp_gateway()
        rewriter.assert_not_called()
        assert orch._mcp_gateway_manager is None

    @pytest.mark.asyncio
    async def test_rewriter_failure_falls_back_to_per_session_mcp(self, tmp_path):
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        with (
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
            patch(
                "kiro_crew.slack.gateway.rewrite_kwargs",
                side_effect=_gateway_rewrite_inputs(tmp_path),
            ),
            patch("kiro_crew.slack.gateway.rewrite_agents", side_effect=RuntimeError("bad spec")),
            patch("kiro_crew.slack.gateway.GatewayManager") as mgr_cls,
        ):
            await orch._init_mcp_gateway()
        mgr_cls.assert_not_called()
        assert orch._mcp_gateway_manager is None

    @pytest.mark.asyncio
    async def test_successful_start_records_the_manager(self, tmp_path):
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        # A stubbed server is what asks for a broker at all; sharing only decides
        # how that server's backend is acquired.
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp"]
        manager = MagicMock()
        manager.start = AsyncMock(return_value=True)
        with (
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
            patch(
                "kiro_crew.slack.gateway.rewrite_kwargs",
                side_effect=_gateway_rewrite_inputs(tmp_path),
            ),
            patch(
                "kiro_crew.slack.gateway.rewrite_agents",
                return_value=(None, {"MC_MCP_TARGET_X": "1"}),
            ),
            patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
        ):
            await orch._init_mcp_gateway()
        assert orch._mcp_gateway_manager is manager

    @pytest.mark.asyncio
    async def test_the_started_set_is_recorded_and_the_override_wins(self, tmp_path):
        """Two properties of the real start, both load-bearing for the sharing path.

        An explicit ``stub_servers`` beats the configured list, so an unrelated
        restart does not apply a stub change meant for the next gateway start. The
        served set is remembered because the sharing path re-emits it; unrecorded,
        that path would find nothing to serve and stop the broker.
        """
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp", "beta-mcp"]  # pending
        manager = MagicMock()
        manager.start = AsyncMock(return_value=True)
        with (
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
            patch(
                "kiro_crew.slack.gateway.rewrite_kwargs",
                side_effect=_gateway_rewrite_inputs(tmp_path),
            ),
            patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})) as rewriter,
            patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
        ):
            await orch._init_mcp_gateway(stub_servers=frozenset({"alpha-mcp"}))

        assert rewriter.call_args.kwargs["stub_servers"] == frozenset({"alpha-mcp"}), (
            "the configured list was used, so an unrelated restart would apply a "
            "stub change reported as pending"
        )
        assert orch._mcp_stub_servers_started == frozenset({"alpha-mcp"})

    @pytest.mark.asyncio
    async def test_the_ready_log_counts_the_served_set_not_the_configured_one(
        self, tmp_path, caplog
    ):
        """This line is read during "why is my stub not live?".

        Config and the served set diverge exactly when a stub change is waiting
        for the next gateway start, so counting the configured list here would
        answer that question wrongly -- claiming two routed servers beside a
        broker serving one.
        """
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp", "beta-mcp"]  # beta pending
        manager = MagicMock()
        manager.start = AsyncMock(return_value=True)
        with (
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
            patch(
                "kiro_crew.slack.gateway.rewrite_kwargs",
                side_effect=_gateway_rewrite_inputs(tmp_path),
            ),
            patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})),
            patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
        ):
            with caplog.at_level(logging.INFO, logger="kiro_crew.slack.gateway"):
                await orch._init_mcp_gateway(stub_servers=frozenset({"alpha-mcp"}))

        ready = [r for r in caplog.records if "broker ready" in r.getMessage()]
        assert ready, "no broker-ready line was emitted"
        assert (
            "1 stubbed server(s)" in ready[0].getMessage()
        ), f"the ready line counted the configured set: {ready[0].getMessage()}"

    @pytest.mark.asyncio
    async def test_a_failed_start_still_records_the_set_so_a_retry_can_bring_it_up(self, tmp_path):
        """A start that fails leaves the broker down, and the set has to survive.

        Recording only on success would make a transient failure permanent: the
        next restart (the sharing toggle) would find nothing to serve. Whether a
        broker is up is ``_mcp_gateway_manager``'s answer, not the set's.
        """
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp"]
        manager = MagicMock()
        manager.start = AsyncMock(return_value=False)  # transient failure
        manager.shutdown = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
            patch(
                "kiro_crew.slack.gateway.rewrite_kwargs",
                side_effect=_gateway_rewrite_inputs(tmp_path),
            ),
            patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})),
            patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
        ):
            await orch._init_mcp_gateway()

        assert orch._mcp_gateway_manager is None, "a failed start must leave no manager"
        assert orch._mcp_stub_servers_started == frozenset({"alpha-mcp"}), (
            "the failed start dropped the set, so a later restart would skip the "
            "broker instead of retrying it"
        )
        await finished(orch._stop_mcp_broker(), "the broker stop ending the start retry")

    @pytest.mark.asyncio
    async def test_failed_start_leaves_no_manager(self, tmp_path):
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        manager = MagicMock()
        manager.start = AsyncMock(return_value=False)
        manager.shutdown = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
            patch(
                "kiro_crew.slack.gateway.rewrite_kwargs",
                side_effect=_gateway_rewrite_inputs(tmp_path),
            ),
            patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})),
            patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
        ):
            await orch._init_mcp_gateway()
        assert orch._mcp_gateway_manager is None
        await finished(orch._stop_mcp_broker(), "the broker stop ending the start retry")


class TestFailedMcpBrokerStartIsRetried:
    """A failed broker start is retried for the gateway's life, and stops with it.

    ``GatewayManager`` arms its watchdog only on a start that spawned or adopted a
    daemon, so nothing inside it ever calls ``start`` again after a start that
    returned False. Without a retry here every stubbed server stays on per-session
    exec until the gateway itself restarts.
    """

    @staticmethod
    def _orchestrator():
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp"]
        orch.sessions = None
        return orch

    @staticmethod
    def _broker_seams(stack: contextlib.ExitStack, tmp_path, manager_cls) -> MagicMock:
        """Patch the rewrite and the manager class; return the overlay-refresher setter.

        The setter is patched so a test never leaves its orchestrator installed in
        the process-wide refresher slot.
        """
        stack.enter_context(
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True)
        )
        stack.enter_context(
            patch(
                "kiro_crew.slack.gateway.rewrite_kwargs",
                side_effect=_gateway_rewrite_inputs(tmp_path),
            )
        )
        stack.enter_context(
            patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {}))
        )
        stack.enter_context(patch("kiro_crew.slack.gateway.GatewayManager", manager_cls))
        return stack.enter_context(
            patch("kiro_crew.mcp_gateway.overlay_refresh.set_overlay_refresher")
        )

    @pytest.mark.asyncio
    async def test_a_failed_start_is_retried_until_the_broker_comes_up(self, tmp_path, caplog):
        """Two failed starts, then one that comes up: the third publishes the broker
        exactly as a boot start does, on the SAME manager, so its lifetime
        stand-down budget and election gates carry across the attempts."""
        orch = self._orchestrator()
        ds = _mock_dashboard_state()
        ds._mcp_gateway_manager = None
        orch.dashboard_state = ds
        manager = MagicMock()
        manager.start = AsyncMock(side_effect=[False, False, True])
        manager.shutdown = AsyncMock()
        manager_cls = MagicMock(return_value=manager)
        with contextlib.ExitStack() as stack:
            set_refresher = self._broker_seams(stack, tmp_path, manager_cls)
            stack.enter_context(
                patch.object(orch, "_mcp_gateway_start_retry_delay", return_value=0.0)
            )
            stack.enter_context(caplog.at_level(logging.INFO, logger="kiro_crew.slack.gateway"))

            await orch._init_mcp_gateway()
            assert orch._mcp_gateway_manager is None, "a failed start published a broker"
            retry = orch._mcp_gateway_start_retry
            assert retry is not None, "a failed start scheduled no retry"
            await await_until(retry, what="the start retry bringing the broker up")

            assert orch._mcp_gateway_manager is manager
            assert orch._mcp_gateway_pending is None
            assert manager.start.await_count == 3
            manager_cls.assert_called_once()
            assert ds._mcp_gateway_manager is manager, (
                "the dashboard was wired at boot with no broker and never saw the "
                "one the retry brought up"
            )
            set_refresher.assert_called_once_with(orch._refresh_mcp_overlays)
            messages = [r.getMessage() for r in caplog.records]
            assert sum("retrying on the L4 schedule" in m for m in messages) == 1
            assert any("broker ready" in m for m in messages)

            await finished(orch._stop_mcp_broker(), "the broker stop")
        manager.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_cancels_a_pending_start_retry_and_shuts_its_manager_down(self, tmp_path):
        """A retry left running would start a daemon after the gateway stopped, and
        a daemon a cancelled attempt spawned is held by the pending manager."""
        orch = self._orchestrator()
        manager = MagicMock()
        manager.start = AsyncMock(return_value=False)
        manager.shutdown = AsyncMock()
        with contextlib.ExitStack() as stack:
            self._broker_seams(stack, tmp_path, MagicMock(return_value=manager))
            await orch._init_mcp_gateway()
            retry = getattr(orch, "_mcp_gateway_start_retry", None)
            await finished(orch._stop_mcp_broker(), "the broker stop")

        assert retry is None or retry.done(), "the stop left the start retry running"
        manager.shutdown.assert_awaited_once()
        assert manager.start.await_count == 1
        assert orch._mcp_gateway_manager is None
        assert getattr(orch, "_mcp_gateway_pending", None) is None
        assert getattr(orch, "_mcp_gateway_start_retry", None) is None

    @pytest.mark.asyncio
    async def test_stop_lets_an_in_flight_retry_start_finish_before_shutdown(self, tmp_path):
        """A start terminating its daemon has already cleared its process handle;
        cancelled there, it would skip the SIGKILL and reap and the shutdown after
        it would find nothing to stop. The fake shares one lock between start and
        shutdown, as ``GatewayManager``'s lifecycle lock does."""
        orch = self._orchestrator()
        events: list[str] = []
        lock = asyncio.Lock()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def _start() -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                return False
            async with lock:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    events.append("start cancelled")
                    raise
                events.append("start finished")
                return False

        async def _shutdown() -> None:
            async with lock:
                events.append("shutdown")

        manager = MagicMock()
        manager.start = _start
        manager.shutdown = _shutdown
        with contextlib.ExitStack() as stack:
            self._broker_seams(stack, tmp_path, MagicMock(return_value=manager))
            stack.enter_context(
                patch.object(orch, "_mcp_gateway_start_retry_delay", return_value=0.0)
            )
            await orch._init_mcp_gateway()
            retry = orch._mcp_gateway_start_retry
            await await_until(entered, what="the retry's second start taking the lock")
            stop = asyncio.ensure_future(orch._stop_mcp_broker())
            await finished(asyncio.wait([retry]), "the stop cancelling the retry")
            release.set()
            await finished(stop, "the broker stop")

        assert events == ["start finished", "shutdown"]

    @pytest.mark.asyncio
    async def test_sharing_toggle_stops_a_pending_retry_before_restarting(self, tmp_path):
        """The toggle builds a fresh manager; a retry still driving the old one would
        leave two managers contending for one socket."""
        orch = self._orchestrator()
        events: list[str] = []
        old = MagicMock()
        old.start = AsyncMock(return_value=False)
        old.shutdown = AsyncMock(side_effect=lambda: events.append("old shutdown"))
        new = MagicMock()
        new.start = AsyncMock(return_value=True)
        new.shutdown = AsyncMock()
        new.is_running = False
        built = iter([old, new])

        def _build(_spec):
            events.append("construct")
            return next(built)

        with contextlib.ExitStack() as stack:
            self._broker_seams(stack, tmp_path, MagicMock(side_effect=_build))
            stack.enter_context(
                patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=orch._cfg)
            )
            await orch._init_mcp_gateway()
            await finished(orch._apply_mcp_gateway_enabled(False), "the sharing toggle")

            assert events == ["construct", "old shutdown", "construct"]
            assert orch._mcp_gateway_manager is new
            await finished(orch._stop_mcp_broker(), "the broker stop")
        old.start.assert_awaited_once()

    @staticmethod
    def _owner_port_manager(tmp_path, monkeypatch, orch, retry_gate: asyncio.Event):
        """A real ``GatewayManager`` whose boot start fails and whose retry comes up.

        The election is scripted: the retry's start waits for *retry_gate*. The
        daemon's end of the control socket records each frame, with whether the
        broker was already published when it arrived, and acknowledges it.
        """
        manager = GatewayManager(GatewaySpec(socket_path=tmp_path / "gw.sock"))
        attempts = 0

        async def start_locked() -> bool:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return False
            await retry_gate.wait()
            return True

        received: list[tuple[dict, bool]] = []

        async def roundtrip(frame: dict) -> dict:
            received.append((frame, orch._mcp_gateway_manager is manager))
            return {"type": "owner-port-set", "port": frame["port"]}

        monkeypatch.setattr(manager, "_start_locked", start_locked)
        monkeypatch.setattr(manager, "_control_roundtrip", roundtrip)
        monkeypatch.setattr(manager, "shutdown", AsyncMock())
        return manager, received

    @pytest.mark.asyncio
    async def test_a_retry_after_the_wire_sends_the_port_the_wire_stored(
        self, tmp_path, monkeypatch
    ):
        """Boot: the start fails before the dashboard exists, so the wire finds the
        manager parked for the retry. It stores the port there, and the retry's own
        start sends it, before the broker is published."""
        orch = self._orchestrator()
        orch.dashboard_state = None
        wired = asyncio.Event()
        manager, received = self._owner_port_manager(tmp_path, monkeypatch, orch, wired)
        with contextlib.ExitStack() as stack:
            self._broker_seams(stack, tmp_path, MagicMock(return_value=manager))
            stack.enter_context(
                patch.object(orch, "_mcp_gateway_start_retry_delay", return_value=0.0)
            )
            await orch._init_mcp_gateway()
            assert orch._mcp_gateway_pending is manager
            ds = _mock_dashboard_state()
            ds._listener_bound_port = 47211
            orch.dashboard_state = ds
            orch._wire_mcp_gateway_dashboard()
            wired.set()
            await await_until(
                orch._mcp_gateway_start_retry, what="the retry bringing the broker up"
            )
            assert orch._mcp_gateway_manager is manager
            await finished(orch._stop_mcp_broker(), "the broker stop")
        assert received == [({"type": "owner-port", "port": 47211}, False)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bound_first", [False, True], ids=["before-the-dashboard", "between-bind-and-wire"]
    )
    async def test_a_retry_that_comes_up_before_the_wire_still_reaches_the_daemon(
        self, tmp_path, monkeypatch, bound_first
    ):
        """The other order. A retry that comes up before the dashboard exists
        leaves the announce to the wire; one that comes up after the bind, while
        the manager has no port, announces it itself."""
        orch = self._orchestrator()
        orch.dashboard_state = None
        retry_gate = asyncio.Event()
        manager, received = self._owner_port_manager(tmp_path, monkeypatch, orch, retry_gate)
        ds = _mock_dashboard_state()
        ds._listener_bound_port = 47211
        with contextlib.ExitStack() as stack:
            self._broker_seams(stack, tmp_path, MagicMock(return_value=manager))
            stack.enter_context(
                patch.object(orch, "_mcp_gateway_start_retry_delay", return_value=0.0)
            )
            await orch._init_mcp_gateway()
            if bound_first:
                orch.dashboard_state = ds
            retry_gate.set()
            await await_until(
                orch._mcp_gateway_start_retry, what="the retry bringing the broker up"
            )
            if not bound_first:
                assert received == [], "nothing to send before the bind"
                orch.dashboard_state = ds
                orch._wire_mcp_gateway_dashboard()
            await async_wait_until(lambda: received, describe=lambda: received)
            announces = [
                t for t in orch._background_tasks if t.get_name() == "mcp-gateway-owner-port"
            ]
            await finished(asyncio.gather(*announces), "the detached announce")
            await finished(orch._stop_mcp_broker(), "the broker stop")
        assert received == [({"type": "owner-port", "port": 47211}, True)]
        assert manager.owner_port == 47211


class TestStopAndApplyMcpBroker:
    """``_stop_mcp_broker`` / ``_apply_mcp_stub`` / ``_wire_mcp_gateway_dashboard``."""

    @pytest.mark.asyncio
    async def test_stop_is_a_noop_without_a_broker(self):
        orch = _make_orchestrator()
        orch._mcp_gateway_manager = None
        await orch._stop_mcp_broker()
        assert orch._mcp_gateway_manager is None

    @pytest.mark.asyncio
    async def test_stop_shuts_down_and_clears_the_handle(self):
        orch = _make_orchestrator()
        mgr = MagicMock()
        mgr.shutdown = AsyncMock()
        orch._mcp_gateway_manager = mgr
        await orch._stop_mcp_broker()
        mgr.shutdown.assert_awaited_once()
        assert orch._mcp_gateway_manager is None

    @pytest.mark.asyncio
    async def test_stop_swallows_a_shutdown_failure_but_still_clears(self):
        orch = _make_orchestrator()
        mgr = MagicMock()
        mgr.shutdown = AsyncMock(side_effect=RuntimeError("socket stuck"))
        orch._mcp_gateway_manager = mgr
        await orch._stop_mcp_broker()
        assert orch._mcp_gateway_manager is None

    @pytest.mark.asyncio
    async def test_apply_stub_reports_not_applied_and_asks_for_a_restart(self):
        """``applied: False`` is the designed outcome, not a failure.

        Nothing is applied in place any more, so there is no "reached state" to
        compare against the wanted one. The pair the dashboard needs is
        ``applied: False`` plus ``restart_required: True``: the first stops the
        switch being drawn as live, the second stops that being read as an error.
        """
        orch = _make_orchestrator()
        orch._mcp_gateway_manager = None

        async def _init_that_must_not_run() -> None:  # pragma: no cover
            raise AssertionError("apply must not start a broker")

        orch._init_mcp_gateway = _init_that_must_not_run

        cfg = KiroCrewConfig()
        cfg.mcp_gateway.stub_servers = ["beta", "alpha"]
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            out = await orch._apply_mcp_stub()
        assert out == {
            "applied": False,
            "restart_required": True,
            "stub_servers": ["alpha", "beta"],
        }

    @pytest.mark.asyncio
    async def test_apply_stub_leaves_a_live_broker_alone(self):
        """The drain is the destructive part: sessions attached to this manager
        lose their in-flight tool calls to it and never re-handshake."""
        orch = _make_orchestrator()
        old = MagicMock()
        old.shutdown = AsyncMock()
        orch._mcp_gateway_manager = old
        ds = _mock_dashboard_state()
        ds._mcp_gateway_manager = old
        orch.dashboard_state = ds

        async def _init_that_must_not_run() -> None:  # pragma: no cover
            raise AssertionError("apply must not respawn the broker")

        orch._init_mcp_gateway = _init_that_must_not_run

        cfg = KiroCrewConfig()
        cfg.mcp_gateway.stub_servers = ["alpha"]
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            out = await orch._apply_mcp_stub()

        old.shutdown.assert_not_awaited()
        assert orch._mcp_gateway_manager is old
        assert ds._mcp_gateway_manager is old
        assert out == {
            "applied": False,
            "restart_required": True,
            "stub_servers": ["alpha"],
        }

    def test_wire_dashboard_is_a_noop_without_dashboard_state(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch._wire_mcp_gateway_dashboard()  # must not raise

    def test_wire_dashboard_publishes_broker_and_callbacks(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        mgr = MagicMock()
        orch._mcp_gateway_manager = mgr

        orch._wire_mcp_gateway_dashboard()

        assert ds._mcp_gateway_manager is mgr
        assert ds._mcp_gateway_apply == orch._apply_mcp_gateway_enabled
        assert ds._mcp_gateway_apply_stub == orch._apply_mcp_stub

    @pytest.mark.asyncio
    async def test_wire_dashboard_announces_the_port_the_listener_bound(self, monkeypatch):
        """The broker started before the bind, so this is where its daemon learns
        the port: from the listener's record, never the inherited environment."""
        monkeypatch.setenv("KIROCREW_PORT", "47299")
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "47299")
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        ds._listener_bound_port = 47211
        orch.dashboard_state = ds
        mgr = MagicMock()
        mgr.announce_owner_port = AsyncMock(return_value=True)
        orch._mcp_gateway_manager = mgr

        orch._wire_mcp_gateway_dashboard()
        await finished(asyncio.gather(*orch._background_tasks), "the announce task")

        mgr.announce_owner_port.assert_awaited_once_with(47211)

    @pytest.mark.parametrize("bound", [0, None, "47211"])
    def test_wire_dashboard_announces_nothing_without_a_bound_port(self, bound):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        ds._listener_bound_port = bound
        orch.dashboard_state = ds
        mgr = MagicMock()
        orch._mcp_gateway_manager = mgr

        orch._wire_mcp_gateway_dashboard()

        mgr.announce_owner_port.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bound", [47211, None], ids=["after-the-bind", "at-boot"])
    async def test_a_broker_started_after_the_bind_takes_the_listeners_port(
        self, tmp_path, monkeypatch, bound
    ):
        """A broker restart (the sharing toggle) builds a new manager; it learns the
        port from the same record, before ``start()`` spawns the daemon."""
        monkeypatch.setenv("KIROCREW_PORT", "47299")
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp"]
        if bound is None:
            orch.dashboard_state = None
        else:
            orch.dashboard_state = _mock_dashboard_state()
            orch.dashboard_state._listener_bound_port = bound
        manager = MagicMock()
        manager.start = AsyncMock(return_value=True)
        with (
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
            patch(
                "kiro_crew.slack.gateway.rewrite_kwargs",
                side_effect=_gateway_rewrite_inputs(tmp_path),
            ),
            patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})),
            patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
        ):
            await orch._init_mcp_gateway()

        calls = [name for name, _args, _kw in manager.method_calls]
        if bound is None:
            manager.set_owner_port.assert_not_called()
        else:
            manager.set_owner_port.assert_called_once_with(47211)
            assert calls.index("set_owner_port") < calls.index("start")


class TestMcpBrokerRefreshPrefetchAndPersistArms:
    """The broker's refresh, prefetch and approval-persist arms nothing else reaches.

    Each seam is patched on the gateway module, which is also what proves the moved
    broker code still reads those names from the facade's globals.
    """

    @pytest.mark.asyncio
    async def test_a_refresh_before_any_broker_start_reports_no_targets(self):
        orch = _make_orchestrator()
        orch._mcp_target_env = {}
        orch._prefetch_mcp_resolutions = AsyncMock()
        fresh = KiroCrewConfig()
        with patch.object(gw.KiroCrewConfig, "load", return_value=fresh):
            result = await orch._refresh_mcp_resolutions()
        assert result == {"ok": False, "reason": "no_targets", "resolved": {}}
        assert orch._cfg is fresh
        orch._prefetch_mcp_resolutions.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_refresh_forces_a_pass_and_names_the_ready_packages(self):
        orch = _make_orchestrator()
        orch._mcp_target_env = {"KIROCREW_MCP_TARGET_A": "npx a"}
        outcomes = {"b": "failed", "a": "ready"}
        orch._prefetch_mcp_resolutions = AsyncMock(return_value=outcomes)
        with patch.object(gw.KiroCrewConfig, "load", return_value=KiroCrewConfig()):
            result = await orch._refresh_mcp_resolutions()
        assert result == {"ok": True, "resolved": outcomes, "ready": ["a"]}
        orch._prefetch_mcp_resolutions.assert_awaited_once_with(
            {"KIROCREW_MCP_TARGET_A": "npx a"}, force=True
        )

    @pytest.mark.asyncio
    async def test_a_failed_prefetch_pass_is_logged_and_reports_nothing(self, caplog):
        orch = _make_orchestrator()
        failing = AsyncMock(side_effect=RuntimeError("registry unreachable"))
        with patch.object(gw, "resolve_prefetch", failing):
            with caplog.at_level(logging.ERROR, logger="kiro_crew.slack.gateway"):
                assert await orch._prefetch_mcp_resolutions({"K": "v"}) == {}
        failing.assert_awaited_once()
        assert "pre-resolve pass failed" in caplog.text

    @pytest.mark.asyncio
    async def test_a_cancelled_prefetch_pass_is_not_swallowed(self):
        orch = _make_orchestrator()
        with patch.object(gw, "resolve_prefetch", AsyncMock(side_effect=asyncio.CancelledError())):
            with pytest.raises(asyncio.CancelledError):
                await orch._prefetch_mcp_resolutions({"K": "v"})

    @pytest.mark.asyncio
    async def test_a_failed_approval_persist_is_logged_and_its_task_released(self, caplog):
        orch = _make_orchestrator()
        orch._background_tasks = set()
        with patch.object(gw, "save_pass", side_effect=OSError("disk full")):
            with caplog.at_level(logging.WARNING, logger="kiro_crew.slack.gateway"):
                orch._schedule_mcp_launch_approval_persist(MagicMock())
                (task,) = orch._background_tasks
                orch._mcp_launch_approval_ready.set()
                await await_until(task, what="the approval persist task finishing")
                # flake-ok: pre-existing zero yield that lets the finished persist task's done-callback leave the background set; moved verbatim, tracked for a de-flake pass
                await asyncio.sleep(0)
        assert "could not persist the approval store" in caplog.text
        assert orch._background_tasks == set()

    @pytest.mark.asyncio
    async def test_stopping_the_broker_cancels_an_in_flight_prefetch(self):
        orch = _make_orchestrator()
        orch._mcp_gateway_manager = None
        started = asyncio.Event()

        async def _pending_install() -> None:
            started.set()
            # flake-ok: pre-existing unbounded wait in the stubbed install; stopping the broker cancels it; moved verbatim, tracked for a de-flake pass
            await asyncio.Event().wait()

        task = asyncio.create_task(_pending_install())
        # flake-ok: pre-existing unbounded wait for the stubbed install to start; the task sets started on its first step; moved verbatim, tracked for a de-flake pass
        await started.wait()
        orch._mcp_resolve_prefetch = task
        await finished(orch._stop_mcp_broker(), "the broker stop cancelling the prefetch")
        assert task.cancelled()
        assert orch._mcp_resolve_prefetch is None
