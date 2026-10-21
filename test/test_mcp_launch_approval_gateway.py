"""The gateway start runs the approval pass whose results gatewayd enforces at spawn.

A gateway start runs the rewrite and the target filter on one worker thread off
the event loop, persists the pass's launch state only after readiness is
signalled, rewrites with the inputs the toggle resolves with, and serves a warm
boot over unchanged approvals from the rewrite cache. Seeding a stub name routes
it without approving its launch, re-approving a running server keeps its
declared env forwarded at gatewayd's next cold spawn, and gatewayd's resolvable
target stems agree with the store's target-stem codec. A suite-wide guard checks
that every test module naming gatewayd's spawn, target-env or declared-env seams
sets ``ENFORCE_LAUNCH_APPROVAL`` or is listed as exempt.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import threading
from pathlib import Path

import pytest
from mcp_launch_approval_helpers import (  # noqa: F401 -- the fixture is requested by name
    _APPROVED_CMD,
    _approval_module,
    _approvals_for,
    _declare,
    _next_boot_rewrite,
    _pool_key,
    _rewrite,
    _targets_running,
    _toggle_on,
    _write_agent,
    toggle_home_fixture,
)

from kiro_crew.mcp_gateway import rewriter

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True


_GATEWAY_LAUNCH_FIXTURE_EXEMPTIONS = frozenset(
    {
        "test_adaptive_controller.py",
        "test_mcp_gateway_cluster_fixes.py",
        "test_mcp_gateway_control_plane_repair.py",
        "test_mcp_gateway_shutdown_and_env.py",
        "test_mcp_gateway_spawn_gate.py",
        "test_mcp_gateway_stub_session_token.py",
        "test_mcp_gatewayd_coverage.py",
        "test_mcp_gatewayd_coverage_acquire_respawn.py",
        "test_runloop_integration.py",
    }
)


def test_gateway_spawn_and_env_tests_choose_launch_approval_fixture():
    def sensitive(name: str) -> bool:
        return "spawn" in name or name == "env_target_resolver" or name.startswith("_declared_env")

    offenders = []
    for path in Path(__file__).parent.glob("test_*.py"):
        text = path.read_text(encoding="utf-8")
        if "gatewayd" not in text or not any(
            token in text for token in ("spawn", "declared_env", "env_target_resolver")
        ):
            continue
        tree = ast.parse(text)
        module_aliases = set()
        imported_symbols = set()
        enforced = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.mcp_gateway":
                module_aliases.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "gatewayd"
                )
            elif (
                isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.mcp_gateway.gatewayd"
            ):
                imported_symbols.update(alias.name for alias in node.names if sensitive(alias.name))
            elif isinstance(node, ast.Assign):
                enforced = enforced or any(
                    isinstance(target, ast.Name)
                    and target.id == "ENFORCE_LAUNCH_APPROVAL"
                    and isinstance(node.value, ast.Constant)
                    and node.value.value is True
                    for target in node.targets
                )
        imported_symbols.update(
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in module_aliases
            and sensitive(node.attr)
        )
        if (
            imported_symbols
            and not enforced
            and path.name not in _GATEWAY_LAUNCH_FIXTURE_EXEMPTIONS
        ):
            offenders.append(path.name)
    assert offenders == []


def test_gatewayd_stems_share_the_approval_store_codec():
    mod = _approval_module()
    assert mod is not None
    codec = getattr(mod, "target_env_stem", None)
    assert codec is not None, "launch_approval must define the single target-stem codec"
    from kiro_crew.mcp_gateway import gatewayd

    env = {
        "KIROCREW_MCP_TARGET_SRV": "x",
        "MC_MCP_TARGET_OLD__abc": "x",
        "KIROCREW_MCP_TARGET_": "x",
        "OTHER": "x",
    }
    assert gatewayd.resolvable_target_stems(env) == ["OLD", "SRV"]
    assert [codec(k) for k in env] == ["SRV", "OLD", None, None]


def test_boot_persists_launch_state_only_after_process_readiness(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.slack import gateway as gw

    cfg = KiroCrewConfig()
    cfg.mcp_gateway.stub_servers = ["srv"]
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    order: list[str] = []

    async def _start() -> bool:
        order.append("bind")
        return True

    manager = MagicMock()
    manager.start = AsyncMock(side_effect=_start)

    def _save(_approvals) -> bool:
        order.append("persist")
        return True

    async def _run() -> None:
        await orch._init_mcp_gateway()
        assert order == ["bind"]
        persist = next(
            task
            for task in orch._background_tasks
            if task.get_name() == "mcp-launch-approval-persist"
        )
        orch._mcp_launch_approval_ready.set()
        # flake-ok: pre-existing unbounded await of the gateway's own persist task, released by the readiness event the test sets first, moved verbatim; tracked for a de-flake pass
        await persist

    with (
        patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
        patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})),
        patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
        patch("kiro_crew.slack.gateway.save_pass", side_effect=_save),
    ):
        asyncio.run(_run())

    assert order == ["bind", "persist"]


def test_gateway_rewrite_and_target_filter_share_a_maintenance_worker(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.slack import gateway as gw

    cfg = KiroCrewConfig()
    cfg.mcp_gateway.stub_servers = ["srv"]
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    manager = MagicMock()
    manager.start = AsyncMock(return_value=False)
    event_loop_thread = threading.get_ident()
    calls: list[tuple[str, int]] = []

    def _rewrite(**_kwargs):
        calls.append(("rewrite", threading.get_ident()))
        return None, {}

    def _filter(target_env, _approvals):
        calls.append(("filter", threading.get_ident()))
        return target_env, []

    with (
        patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
        patch("kiro_crew.slack.gateway.rewrite_agents", side_effect=_rewrite),
        patch("kiro_crew.slack.gateway.filter_target_env", side_effect=_filter),
        patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
    ):
        asyncio.run(orch._init_mcp_gateway())

    assert [name for name, _thread in calls] == ["rewrite", "filter"]
    assert calls[0][1] == calls[1][1]
    assert calls[0][1] != event_loop_thread


def test_the_toggle_resolves_with_the_inputs_the_gateway_start_rewrites_with(tmp_path):
    """One derivation of the rewrite inputs, or the click approves a different launch."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.slack import gateway as gw

    resolve = importlib.import_module("kiro_crew.mcp_gateway.launch_resolve")
    cfg = KiroCrewConfig()
    cfg.mcp_gateway.stub_servers = ["srv"]
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    manager = MagicMock()
    manager.start = AsyncMock(return_value=False)
    with (
        patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
        patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})) as rw,
        patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
    ):
        asyncio.run(orch._init_mcp_gateway())

    called = {k: v for k, v in rw.call_args.kwargs.items() if k != "approvals"}
    assert called == resolve.rewrite_kwargs(cfg, frozenset({"srv"}))


def test_seeding_routes_the_name_without_approving_its_launch(tmp_path, toggle_home):
    from kiro_crew.mcp_gateway import verdict_cache as vc
    from kiro_crew.mcp_gateway.seed import SeedPlan, apply_seed

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert apply_seed(SeedPlan(add_stub=("srv",)), {}, vc.VerdictCache(vc.cache_path(tmp_path)))

    target_env, approvals = _next_boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(target_env, _APPROVED_CMD) == []
    assert approvals.refused == {"SRV": _approval_module().REFUSED_UNAPPROVED}
    assert _approval_module().load_approvals().approved == {}


def test_a_warm_boot_with_approvals_is_served_from_the_rewrite_cache(tmp_path):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    approvals = _approvals_for(mod, "srv", _APPROVED_CMD, [])
    mod._write(store, mod._document(approvals, {}))
    _write_agent(tmp_path / "agents", {"srv": {"command": _APPROVED_CMD, "args": []}})

    first = mod.load_approvals(store)
    _rewrite(tmp_path, frozenset({"srv"}), first)
    assert first.full_pass
    mod.save_pass(first, path=store)

    again = mod.load_approvals(store)
    fingerprint = tmp_path / "home" / "mcp-gateway" / "agents" / rewriter._FINGERPRINT_NAME
    stored = json.loads(fingerprint.read_text())
    assert stored["inputs"]["launch_approvals"] == again.digest()
    target_env = _rewrite(tmp_path, frozenset({"srv"}), again)

    assert not again.full_pass, "a boot over unchanged inputs and approvals rewrote everything"
    assert _targets_running(target_env, _APPROVED_CMD)


@pytest.mark.asyncio
async def test_re_approving_a_running_server_keeps_its_declared_env_forwarded(
    tmp_path, toggle_home
):
    from kiro_crew.mcp_gateway import gatewayd
    from kiro_crew.mcp_gateway.hashing import hash_effective_env

    declared = {"FOO": "bar"}
    (toggle_home / "a.json").write_text(
        json.dumps(
            {
                "name": "a",
                "mcpServers": {"srv": {"command": _APPROVED_CMD, "args": [], "env": declared}},
            }
        )
    )
    assert (await _toggle_on())[0] == 200
    # The gateway start the running daemon came from: it writes the env sidecar
    # gatewayd reads at cold spawn and derives the env it may forward.
    mod = _approval_module()
    approvals = mod.load_approvals()
    rewriter.rewrite_agents(
        source_dir=toggle_home,
        overlay_dir=rewriter.resolve_overlay_dir(),
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "work",
        stub_servers=frozenset({"srv"}),
        approvals=approvals,
    )
    mod.save_pass(approvals)

    # The operator turns the stub on again; the broker is not restarted.
    assert (await _toggle_on())[0] == 200

    identity_keys = rewriter.pool_identity_env_keys()
    key = _pool_key("srv")
    key = type(key)(
        **{
            **{f: getattr(key, f) for f in inspect.signature(type(key)).parameters},
            "effective_env_hash": hash_effective_env(declared, identity_keys=identity_keys),
        }
    )
    assert (
        gatewayd._declared_env_pairs(key, identity_keys, declaring_agent="a") == declared
    ), "the next cold spawn after the toggle would start the backend without its declared env"
