"""Tests for the supervised-sidecar gating decisions on GatewayOrchestrator.

Supervised mode (contract C1) makes the gateway a loopback-only child of a host
process: it must report an approval surface (the host renders prompts) and must
NOT run the mcp-gateway broker (whose daemon orphans out of the process group).
These are the two branch decisions the orchestrator owns; the READY-budget and
child-reaping obligations are exercised by the subprocess smoke test.

Instances are built via ``__new__`` (bypassing ``__init__``) the same way
``test_heartbeat_prompt_deliver.py`` does — ``_supervised`` is a class-level
default, so a partially built orchestrator reads it without ``__init__`` running.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew.slack.gateway import GatewayOrchestrator


def _orch(*, supervised: bool) -> GatewayOrchestrator:
    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch._supervised = supervised
    return orch


class TestDashboardClientAttached:
    def test_supervised_reports_attached_without_any_ws_client(self):
        # The whole point: a headless supervised sidecar has zero
        # dashboard-user websockets, but the host promised to render approvals,
        # so a spawn must not be refused as unreachable.
        orch = _orch(supervised=True)
        orch.dashboard_state = None  # not even a dashboard object
        assert orch._dashboard_client_attached() is True

    def test_supervised_short_circuits_before_reading_dashboard_state(self):
        # A dashboard_state whose ws-count would raise must never be consulted
        # in supervised mode — the supervised branch returns first.
        orch = _orch(supervised=True)
        boom = MagicMock()
        boom.dashboard_user_ws_count.side_effect = AssertionError(
            "must not be called in supervised mode"
        )
        orch.dashboard_state = boom
        assert orch._dashboard_client_attached() is True
        boom.dashboard_user_ws_count.assert_not_called()

    def test_unsupervised_no_dashboard_is_detached(self):
        orch = _orch(supervised=False)
        orch.dashboard_state = None
        assert orch._dashboard_client_attached() is False

    def test_unsupervised_counts_ws_clients(self):
        orch = _orch(supervised=False)
        state = MagicMock()
        state.dashboard_user_ws_count.return_value = 2
        orch.dashboard_state = state
        assert orch._dashboard_client_attached() is True
        state.dashboard_user_ws_count.return_value = 0
        assert orch._dashboard_client_attached() is False


class TestMcpGatewaySkipped:
    @pytest.mark.asyncio()
    async def test_supervised_skips_broker_without_touching_config(self):
        # The supervised early-return must fire BEFORE reading self._cfg; if it
        # did not, this call would AttributeError on the unset _cfg. Returning
        # cleanly here is the proof the broker (and its orphan-prone daemon) is
        # never constructed.
        orch = _orch(supervised=True)
        # _cfg deliberately NOT set — reaching it would raise.
        assert await orch._init_mcp_gateway() is None

    @pytest.mark.asyncio()
    async def test_unsupervised_reaches_config_and_returns_on_empty_stubs(self):
        # Not supervised: it proceeds past the supervised gate, reads
        # mcp_gateway.stub_servers, and returns early when nothing is stubbed
        # (the default). This proves the supervised branch is what skipped the
        # config read above, not an unconditional early return.
        orch = _orch(supervised=False)
        cfg = MagicMock()
        cfg.mcp_gateway.stub_servers = []
        orch._cfg = cfg
        assert await orch._init_mcp_gateway() is None


class TestHeartbeatSkipped:
    @pytest.mark.asyncio()
    async def test_supervised_skips_heartbeat_before_memory_check(self):
        # Supervised returns before reading _memory_startup; an unset attribute
        # here would raise if the supervised gate were not first.
        orch = _orch(supervised=True)
        assert await orch._init_heartbeat() is None
