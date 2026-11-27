"""One per-project server's Share switch changes only that server, with no broker restart.

``mcp_broker._apply_mcp_dont_share`` rewrites the overlays through the probe an
app install runs, with the name moved in the rewrite's ``dont_share`` set and the
served stub set. The daemon, its backends and every attached stub are left alone:
the broker is never stopped or started, no chat is reset, and another server's
overlay entry is byte-identical before and after.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.mcp_gateway import launch_approval as la
from kiro_crew.mcp_gateway import overlay_refresh, rewriter
from kiro_crew.slack.gateway import GatewayOrchestrator

_CMD = sys.executable


@pytest.fixture
def home(tmp_path, monkeypatch):
    import kiro_crew.mcp_gateway.backend_tmp as backend_tmp

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    scratch_root = tmp_path / "probe"
    counter = iter(range(1000))

    def _scratch() -> Path:
        path = scratch_root / str(next(counter))
        path.mkdir(parents=True)
        return path

    monkeypatch.setattr(backend_tmp, "allocate_probe_tmp", _scratch)
    agents = tmp_path / "kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "host.json").write_text(
        json.dumps(
            {
                "name": "host",
                "tools": ["*"],
                "mcpServers": {
                    "srv": {"command": _CMD, "args": []},
                    "other": {"command": _CMD, "args": ["-V"]},
                },
            }
        )
    )
    return SimpleNamespace(
        tmp=tmp_path, agents=agents, overlay=tmp_path / "crew" / "mcp-gateway" / "agents"
    )


class _Sessions:
    def __init__(self) -> None:
        self.refreshed = 0

    async def refresh_defaults(self) -> None:
        self.refreshed += 1


def _boot(home, monkeypatch, dont_share: frozenset[str] = frozenset()) -> GatewayOrchestrator:
    """A broker started over the spec, per-project routing on, as the gateway starts one."""
    import kiro_crew.slack.gateway as gw
    from kiro_crew.config import loader

    inputs = {
        "source_dir": home.agents,
        "overlay_dir": home.overlay,
        "socket_path": home.tmp / "gw.sock",
        "work_dir": home.tmp / "work",
        "stub_servers": frozenset({"srv", "other"}) - dont_share,
        "pooling_enabled": True,
        "project_backends": True,
        "dont_share": dont_share,
    }
    approvals = la.load_approvals()
    _r, boot_env = rewriter.rewrite_agents(**inputs, approvals=approvals)
    boot_env, _dropped = gw.filter_target_env(boot_env, approvals)
    la.save_pass(approvals)
    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch._mcp_gateway_manager = object()
    orch._mcp_stub_servers_started = inputs["stub_servers"]
    orch._mcp_target_env = dict(boot_env)
    orch._mcp_rewrite_inputs = dict(inputs)
    orch._mcp_rewrite_policy = overlay_refresh.rewrite_policy(home.overlay)
    orch._mcp_overlay_refresh_lock = None
    orch.sessions = _Sessions()
    orch.dashboard_state = None

    async def _never(*_a, **_k):
        raise AssertionError("the broker must not restart")

    orch._stop_mcp_broker = _never
    orch._init_mcp_gateway = _never
    config = home.tmp / "crew" / "config.json"
    config.write_text(
        json.dumps(
            {
                "mcp_gateway": {
                    "stub_servers": ["other", "srv"],
                    "stub_overrides": {name: False for name in dont_share},
                }
            }
        )
    )
    assert frozenset(loader.KiroCrewConfig.load().mcp_gateway.stub_servers) == (
        inputs["stub_servers"]
    )
    return orch


def _entry(home, server: str) -> dict:
    return json.loads((home.overlay / "host.json").read_text())["mcpServers"][server]


def _routed(home, server: str) -> bool:
    return STUB_MODULE in _entry(home, server)["args"]


def _project_scoped(home, server: str) -> bool:
    flags = next(a for a in _entry(home, server)["args"] if a.startswith("--stub-flags-b64="))
    return "--project-scoped" in json.loads(base64.b64decode(flags.split("=", 1)[1]))


@pytest.mark.asyncio
async def test_turning_dont_share_on_changes_only_that_server(home, monkeypatch) -> None:
    orch = _boot(home, monkeypatch)
    manager = orch._mcp_gateway_manager
    assert _project_scoped(home, "srv") and _project_scoped(home, "other")
    other_before = _entry(home, "other")

    out = await orch._apply_mcp_dont_share("srv", True)

    assert out == {"applied": True, "restart_required": False, "sessions_reset": 0}
    assert not _routed(home, "srv")
    assert _entry(home, "other") == other_before
    # The same broker serves on; only new chats see the change.
    assert orch._mcp_gateway_manager is manager
    assert orch._mcp_rewrite_inputs["dont_share"] == frozenset({"srv"})
    assert orch.sessions.refreshed == 1


@pytest.mark.asyncio
async def test_turning_it_off_again_reroutes_with_the_target_the_daemon_holds(
    home, monkeypatch
) -> None:
    orch = _boot(home, monkeypatch)
    srv_before, other_before = _entry(home, "srv"), _entry(home, "other")
    await orch._apply_mcp_dont_share("srv", True)

    out = await orch._apply_mcp_dont_share("srv", False)

    assert out["applied"] is True and out["restart_required"] is False
    assert _entry(home, "srv") == srv_before
    assert _entry(home, "other") == other_before


@pytest.mark.asyncio
async def test_sharing_a_server_the_daemon_never_held_waits_for_the_next_start(
    home, monkeypatch
) -> None:
    orch = _boot(home, monkeypatch, dont_share=frozenset({"srv"}))
    before = (home.overlay / "host.json").read_bytes()

    out = await orch._apply_mcp_dont_share("srv", False)

    assert out == {"applied": False, "restart_required": True, "sessions_reset": 0}
    assert (home.overlay / "host.json").read_bytes() == before
    assert orch._mcp_rewrite_inputs["dont_share"] == frozenset({"srv"})
    assert orch.sessions.refreshed == 0
