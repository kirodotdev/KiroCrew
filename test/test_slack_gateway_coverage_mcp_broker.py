"""Coverage for ``kiro_crew.slack.gateway``: the MCP-gateway control plane.

``_init_mcp_gateway``'s broker startup, its early returns and the rewriter-failure
fallback; ``_stop_mcp_broker``, ``_apply_mcp_stub`` and
``_wire_mcp_gateway_dashboard``; and the broker's refresh, prefetch and
approval-persist arms. No real broker socket.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_coverage_helpers import _make_orchestrator, _mock_dashboard_state

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.slack import gateway as gw
from kiro_crew.testing.wait import await_until, finished

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

        Two things have to happen when sharing goes off while apps stays on, and
        only asserting both distinguishes the fix from either failure mode:

        * the broker must come back — a plain stop would take away the render
          and callback paths of servers the operator never unstubbed;
        * it must be a RESTART, not a no-op — the rewriter reads the sharing flag
          when the broker starts, so re-running it is what re-emits every stub
          without ``--poolable`` and actually stops the sharing just turned off.

        The set it re-emits is the one the broker is SERVING, so the fixture has
        to say what that is; the configured list alone is not it, because a stub
        change is recorded for the next gateway start rather than applied.
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
    @pytest.mark.parametrize("enabled", [False, True])
    async def test_resource_subscription_restore_runs_only_after_readiness_when_enabled(
        self, tmp_path, enabled
    ):
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp"]
        orch._cfg.mcp_gateway.resource_subscriptions.enabled = enabled
        orch.dashboard_state = MagicMock()
        orch.dashboard_state.open_slots_restored = False
        orch.dashboard_state.slot_exists.return_value = False
        orch.dashboard_state._slots = {}
        orch.sessions = MagicMock()
        orch.sessions.has_session.return_value = False
        manager = MagicMock()
        manager.start = AsyncMock(return_value=True)
        manager.shutdown = AsyncMock()
        start_service = AsyncMock()
        stop_service = AsyncMock()
        run_dispatcher = AsyncMock()
        resource_imports: list[str] = []
        real_import = builtins.__import__
        feature_modules = (
            "kiro_crew.mcp_gateway.resource_subscriptions",
            "kiro_crew.dashboard.resource_wakes",
        )

        def track_import(name, globals_=None, locals_=None, fromlist=(), level=0):
            # ``from pkg import mod`` arrives as name="pkg", fromlist=("mod",).
            for candidate in (name, *(f"{name}.{item}" for item in fromlist or ())):
                if candidate in feature_modules:
                    resource_imports.append(candidate)
            return real_import(name, globals_, locals_, fromlist, level)

        with (
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
            patch(
                "kiro_crew.slack.gateway.rewrite_kwargs",
                side_effect=_gateway_rewrite_inputs(tmp_path),
            ),
            patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})),
            patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
            patch(
                "kiro_crew.mcp_gateway.resource_subscriptions.start_active_service",
                start_service,
            ),
            patch(
                "kiro_crew.mcp_gateway.resource_subscriptions.stop_active_service",
                stop_service,
            ),
            patch("kiro_crew.dashboard.resource_wakes.run_dispatcher", run_dispatcher),
            patch("builtins.__import__", side_effect=track_import),
        ):
            await orch._init_mcp_gateway()
            start_service.assert_not_awaited()
            task = getattr(orch, "_mcp_resource_subscription_task", None)
            if enabled:
                assert task is not None
                assert resource_imports == []
                orch.dashboard_state.open_slots_restored = True
                orch._mcp_launch_approval_ready.set()
                await finished(task, "resource subscription restoration")
                assert resource_imports == list(feature_modules)
                start_service.assert_awaited_once()
                session_is_closed = start_service.await_args.kwargs["session_is_closed"]
                # Channel sessions get a runtime lazily, so at boot neither an open
                # tab nor a runtime is evidence about them: they are never closed here.
                assert await session_is_closed("slack:C1:1700000000.000100") is False
                assert await session_is_closed("cron:nightly-digest") is False
                metadata = orch.dashboard_state.conversation_log.get_metadata_status
                metadata.return_value = ({"closed": True}, True)
                workflow_slot = MagicMock(
                    key="workflow-run1", linked_session_key="dashboard:origin"
                )
                orch.dashboard_state._slots = {"workflow-run1": workflow_slot}
                assert await session_is_closed("dashboard:origin") is False
                orch.dashboard_state._slots = {}
                assert await session_is_closed("dashboard:closed") is True
                orch.dashboard_state.slot_exists.assert_called_with("closed")
                orch.dashboard_state.slot_exists.return_value = True
                assert await session_is_closed("dashboard:open") is False
                orch.dashboard_state.slot_exists.return_value = False
                orch.sessions.has_session.return_value = True
                assert await session_is_closed("dashboard:runtime") is False
                orch.sessions.has_session.return_value = False
                orch.dashboard_state.open_slots_restored = False
                assert await session_is_closed("dashboard:unknown") is False
                orch.dashboard_state.open_slots_restored = True
                orch.dashboard_state.slot_exists.side_effect = RuntimeError("state unavailable")
                assert await session_is_closed("dashboard:unverifiable") is False
                orch.dashboard_state.slot_exists.side_effect = None
                orch.sessions.has_session.side_effect = RuntimeError("sessions unavailable")
                assert await session_is_closed("dashboard:unverifiable") is False
                orch.sessions.has_session.side_effect = None
                # Absent with no closure record is missing evidence, never closure.
                metadata.return_value = ({}, True)
                assert await session_is_closed("dashboard:absent") is False

                def reopened_during_read(_history_key: str) -> tuple[dict, bool]:
                    orch.dashboard_state.slot_exists.return_value = True
                    return {"closed": True}, True

                metadata.side_effect = reopened_during_read
                assert await session_is_closed("dashboard:reopened") is False
                metadata.side_effect = None
                orch.dashboard_state.slot_exists.return_value = False
                # The started service's events go to the wake dispatcher.
                run_dispatcher.assert_awaited_once()
                assert run_dispatcher.await_args.args[0] is start_service.return_value
                assert (
                    start_service.await_args.kwargs["backend_spawn_wait_secs"]
                    == orch._cfg.mcp_gateway.spawn_queue_wait_secs
                )
            else:
                assert task is None
                assert resource_imports == []
                run_dispatcher.assert_not_awaited()
            await orch._stop_mcp_broker()

        if enabled:
            stop_service.assert_awaited_once_with()
        else:
            stop_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_retires_a_dashboard_session_only_on_recorded_closure(self, tmp_path):
        """A tab past the restore budget or with an uncommitted close is not closed.

        Only a readable persisted ``closed`` flag retires its subscriptions at
        start.
        """
        from kiro_crew.config.integration_sections import McpResourceSubscriptionsConfig
        from kiro_crew.dashboard.channel_slots import note_slot_closed
        from kiro_crew.mcp_gateway.resource_subscription_store import (
            HolderRecord,
            ResourceSubscriptionStore,
        )
        from kiro_crew.mcp_gateway.resource_subscriptions import (
            RecordingSink,
            ResourceSubscriptionService,
        )

        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp"]
        orch._cfg.mcp_gateway.resource_subscriptions.enabled = True
        orch.dashboard_state = MagicMock()
        orch.dashboard_state.open_slots_restored = True
        orch.dashboard_state.slot_exists.return_value = False
        orch.dashboard_state._slots = {}
        orch.sessions = MagicMock()
        orch.sessions.has_session.return_value = False

        def metadata_status(history_key: str) -> tuple[dict, bool]:
            if "persisted" in history_key:
                return {"closed": True}, True
            if "unreadable" in history_key:
                return {"closed": True}, False
            if "raising" in history_key:
                raise OSError("metadata unavailable")
            return {}, True

        orch.dashboard_state.conversation_log.get_metadata_status.side_effect = metadata_status
        note_slot_closed(orch.dashboard_state, "tombstoned")
        manager = MagicMock()
        manager.start = AsyncMock(return_value=True)
        manager.shutdown = AsyncMock()
        start_service = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
            patch(
                "kiro_crew.slack.gateway.rewrite_kwargs",
                side_effect=_gateway_rewrite_inputs(tmp_path),
            ),
            patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})),
            patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
            patch(
                "kiro_crew.mcp_gateway.resource_subscriptions.start_active_service",
                start_service,
            ),
            patch("kiro_crew.mcp_gateway.resource_subscriptions.stop_active_service", AsyncMock()),
            patch("kiro_crew.dashboard.resource_wakes.run_dispatcher", AsyncMock()),
        ):
            await orch._init_mcp_gateway()
            orch._mcp_launch_approval_ready.set()
            await finished(orch._mcp_resource_subscription_task, "resource subscription start")
            session_is_closed = start_service.await_args.kwargs["session_is_closed"]

            store = ResourceSubscriptionStore(tmp_path / "subscriptions.json")
            names = ("budget-skipped", "persisted", "tombstoned", "unreadable", "raising")
            for name in names:
                store.put(
                    HolderRecord(
                        session_key=f"dashboard:{name}",
                        channel_id="",
                        server="fake",
                        register={"server_name": "fake"},
                        initialize={"method": "initialize", "params": {}},
                        uris=["crux://reviews/CR-1"],
                    )
                )
            service = ResourceSubscriptionService(
                store=store,
                socket_path=lambda: None,
                sink=RecordingSink(),
                settings=lambda: McpResourceSubscriptionsConfig(enabled=True),
                session_is_closed=session_is_closed,
            )
            started: list[str] = []
            service._start_holder = lambda record, **_kwargs: started.append(  # type: ignore[method-assign]
                record.session_key
            )
            try:
                await service.start()
            finally:
                await service.close()
            await orch._stop_mcp_broker()

        kept = [
            "dashboard:budget-skipped",
            "dashboard:raising",
            "dashboard:tombstoned",
            "dashboard:unreadable",
        ]
        assert sorted(r.session_key for r in ResourceSubscriptionStore(store.path).load()) == kept
        assert sorted(started) == kept

    @pytest.mark.asyncio
    async def test_a_failed_close_during_start_keeps_the_sessions_subscriptions(
        self, tmp_path, monkeypatch
    ):
        """A close tombstone cannot retire records before its history save commits."""
        from chat_test_helpers import _make_state
        from test_mcp_resource_subscription_cleanup import _Req

        from kiro_crew.config.integration_sections import McpResourceSubscriptionsConfig
        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.mcp_gateway.resource_subscription_store import (
            HolderRecord,
            ResourceSubscriptionStore,
        )
        from kiro_crew.mcp_gateway.resource_subscriptions import (
            RecordingSink,
            ResourceSubscriptionService,
        )

        name = "close-refused"
        session_key = f"dashboard:{name}"
        state = _make_state(tmp_path / "state")
        state.open_slots_restored = True
        state.sessions.has_session.return_value = False
        slot = state.get_or_create_slot(name)
        slot.append("user", "watch the review")
        slot.drain()

        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp"]
        orch._cfg.mcp_gateway.resource_subscriptions.enabled = True
        orch.dashboard_state = state
        orch.sessions = state.sessions
        manager = MagicMock()
        manager.start = AsyncMock(return_value=True)
        manager.shutdown = AsyncMock()
        start_service = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
            patch(
                "kiro_crew.slack.gateway.rewrite_kwargs",
                side_effect=_gateway_rewrite_inputs(tmp_path),
            ),
            patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})),
            patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
            patch(
                "kiro_crew.mcp_gateway.resource_subscriptions.start_active_service",
                start_service,
            ),
            patch("kiro_crew.mcp_gateway.resource_subscriptions.stop_active_service", AsyncMock()),
            patch("kiro_crew.dashboard.resource_wakes.run_dispatcher", AsyncMock()),
        ):
            await orch._init_mcp_gateway()
            orch._mcp_launch_approval_ready.set()
            await finished(orch._mcp_resource_subscription_task, "resource subscription start")
            session_is_closed = start_service.await_args.kwargs["session_is_closed"]

            store = ResourceSubscriptionStore(tmp_path / "subscriptions.json")
            store.put(
                HolderRecord(
                    session_key=session_key,
                    channel_id="",
                    server="fake",
                    register={"server_name": "fake"},
                    initialize={"method": "initialize", "params": {}},
                    uris=["crux://reviews/CR-1"],
                )
            )
            service = ResourceSubscriptionService(
                store=store,
                socket_path=lambda: None,
                sink=RecordingSink(),
                settings=lambda: McpResourceSubscriptionsConfig(enabled=True),
                session_is_closed=session_is_closed,
            )
            service._start_holder = lambda _record, **_kwargs: None  # type: ignore[method-assign]

            async def fail_after_start(*_args, **_kwargs):
                await service.start()
                raise RuntimeError("disk wedged")

            monkeypatch.setattr(chat_handlers, "save_slot_off_loop", fail_after_start)
            try:
                response = await chat_handlers.api_chat_slot_delete(_Req(state, name))
            finally:
                await service.close()
                await orch._stop_mcp_broker()

        assert response.status == 500
        assert b'"code": "history_save_failed"' in response.body
        assert state._slots[name] is slot
        assert [r.session_key for r in ResourceSubscriptionStore(store.path).load()] == [
            session_key
        ]

    @pytest.mark.asyncio
    async def test_the_started_set_is_recorded_and_the_override_wins(self, tmp_path):
        """Two properties of the real start, both load-bearing for the sharing path.

        The set handed to the rewriter is what the broker ends up serving, and an
        explicit ``stub_servers`` must beat the configured list -- that argument
        is how an unrelated restart avoids applying a stub change recorded for the
        next gateway start. And the served set has to be REMEMBERED, because the
        sharing path re-emits it rather than re-reading config; if it were not
        recorded, that path would find nothing to serve and silently stop the
        broker it was supposed to restart.
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

        Recording only on success would make a transient start failure permanent:
        the broker is absent, and the next restart for an unrelated reason -- the
        sharing toggle -- would find nothing to serve and skip the start instead
        of retrying it. The set says what the attempt was made with, not that the
        attempt worked; ``_mcp_gateway_manager`` is what says a broker is up.
        """
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        orch._cfg.mcp_gateway.stub_servers = ["alpha-mcp"]
        manager = MagicMock()
        manager.start = AsyncMock(return_value=False)  # transient failure
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

    @pytest.mark.asyncio
    async def test_failed_start_leaves_no_manager(self, tmp_path):
        orch = _make_orchestrator()
        orch._cfg.mcp_gateway.enabled = True
        manager = MagicMock()
        manager.start = AsyncMock(return_value=False)
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
