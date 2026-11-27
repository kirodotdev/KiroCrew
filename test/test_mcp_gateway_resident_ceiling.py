"""The pool's resident cap never sits below the number of stubbed servers.

On a real host 71 stubbed servers met the default ``max_backends`` of 64, and the
servers past it were refused for capacity in every chat.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_subagent_memory_bounds_concurrency import host_gb  # noqa: F401 -- fixture by import

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mcp_gateway.admission import derive_resident_ceiling

pytestmark = pytest.mark.xdist_group("mcp_gateway")


def test_the_resident_ceiling_is_the_larger_of_the_cap_and_the_stub_count() -> None:
    assert derive_resident_ceiling(64, 71) == 71
    assert derive_resident_ceiling(64, 3) == 64
    assert derive_resident_ceiling(0, 0) == 1


@pytest.mark.asyncio
async def test_the_daemon_gets_a_slot_for_every_stubbed_server(
    tmp_path, host_gb  # noqa: F811 -- the fixture imported above
) -> None:
    from kiro_crew.slack import gateway as gw

    host_gb(40.0)
    cfg = KiroCrewConfig()
    cfg.mcp_gateway.stub_servers = [f"s{i:02d}" for i in range(71)]
    assert cfg.mcp_gateway.max_backends < 71
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    manager = MagicMock()
    manager.start = AsyncMock(return_value=False)
    with (
        patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
        patch(
            "kiro_crew.slack.gateway.rewrite_kwargs",
            side_effect=lambda _cfg, stubs: {
                "socket_path": tmp_path / "gw.sock",
                "stub_servers": stubs,
            },
        ),
        patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})),
        patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager) as mgr_cls,
    ):
        await orch._init_mcp_gateway()
    # The False start leaves a start retry and the background tasks behind.
    leftover = [orch._mcp_resolve_prefetch, orch._mcp_gateway_start_retry]
    leftover.extend(orch._background_tasks)
    for task in leftover:
        task.cancel()
    await asyncio.gather(*leftover, return_exceptions=True)
    assert mgr_cls.call_args.args[0].max_backends == 71
