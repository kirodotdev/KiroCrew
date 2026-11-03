"""An app's agents get their MCP gateway overlay without a gateway restart.

The broker rewrites ``mcp-gateway/agents/*.json`` when it starts. An app
registered later writes its agent specs after that pass, so those agents had no
overlay until the next start. ``_register_app_off_loop`` now asks the running
broker to rebuild the overlays. The broker reuses the inputs it started with and
refuses when a setting fixed at start has moved, a routed target is new, or any
launch approval decision would change; it never writes launch approvals.
"""

from __future__ import annotations

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
_VAR = "KC_TEST_23761_TOKEN"
_DECLARED = {"TOK": "${" + _VAR + "}"}


def _seed_store(**servers: tuple[list[str], dict[str, str] | None, dict[str, str]]) -> None:
    """Approve one launch per server: ``name=(args, declared_env, expanded_env)``."""
    approvals = la.LaunchApprovals()
    for name, (args, declared, expanded) in servers.items():
        stem = la.target_stem(name)
        approvals.approved[stem] = {la.launch_fingerprint(_CMD, args, declared or {})}
        approvals.approved_pairs[stem] = {
            la.launch_pair(rewriter.hash_command(_CMD, args), la.env_fingerprint(expanded))
        }
        approvals.names[stem] = name
    la._write(la.approvals_path(), la._document(approvals, {}))


def _spec(agents: Path, stem: str, servers: dict[str, dict]) -> None:
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{stem}.json").write_text(json.dumps({"name": stem, "mcpServers": servers}))


def _srv(args: list[str] | None = None, env: dict[str, str] | None = None) -> dict:
    entry: dict = {"command": _CMD, "args": list(args or [])}
    if env is not None:
        entry["env"] = dict(env)
    return entry


@pytest.fixture
def home(tmp_path, monkeypatch):
    import kiro_crew.mcp_gateway.backend_tmp as backend_tmp

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    monkeypatch.setenv(_VAR, "old")
    monkeypatch.setattr(rewriter, "forward_declared_env_enabled", lambda: True)
    scratch_root = tmp_path / "probe"
    counter = iter(range(1000))

    def _scratch() -> Path:
        path = scratch_root / str(next(counter))
        path.mkdir(parents=True)
        return path

    monkeypatch.setattr(backend_tmp, "allocate_probe_tmp", _scratch)
    return SimpleNamespace(
        tmp=tmp_path,
        agents=tmp_path / "kiro" / "agents",
        overlay=tmp_path / "crew" / "mcp-gateway" / "agents",
        scratch=scratch_root,
    )


def _boot(home, stubs: frozenset[str]) -> GatewayOrchestrator:
    """Start a broker over the specs on disk, as ``_init_mcp_gateway`` does."""
    import kiro_crew.slack.gateway as gw

    inputs = {
        "source_dir": home.agents,
        "overlay_dir": home.overlay,
        "socket_path": home.tmp / "gw.sock",
        "work_dir": home.tmp / "work",
        "stub_servers": stubs,
    }
    approvals = la.load_approvals()
    _r, boot_env = rewriter.rewrite_agents(**inputs, approvals=approvals)
    boot_env, _dropped = gw.filter_target_env(boot_env, approvals)
    la.save_pass(approvals)  # boot persists its pass, refusals included
    assert boot_env, "the boot pass must route a server for this test to mean anything"
    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch._cfg = None  # the refresh must not read live config
    orch._mcp_gateway_manager = object()
    orch._mcp_stub_servers_started = stubs
    orch._mcp_target_env = dict(boot_env)
    orch._mcp_rewrite_inputs = dict(inputs)
    orch._mcp_rewrite_policy = overlay_refresh.rewrite_policy(home.overlay)
    orch._mcp_overlay_refresh_lock = None
    overlay_refresh.set_overlay_refresher(orch._refresh_mcp_overlays)
    return orch


async def _wrote_specs() -> bool:
    """A registration that wrote agent specs (the files the test already put down)."""
    return True


@pytest.fixture(autouse=True)
def _clear_refresher():
    yield
    overlay_refresh.set_overlay_refresher(None)


def _routed(home, stem: str, server: str = "srv") -> bool:
    entry = json.loads((home.overlay / f"{stem}.json").read_text())["mcpServers"][server]
    return STUB_MODULE in entry["args"]


@pytest.mark.asyncio
async def test_fresh_install_builds_overlays_for_every_app_agent(home) -> None:
    _seed_store(srv=([], None, {}))
    _spec(home.agents, "host", {"srv": _srv()})
    _boot(home, frozenset({"srv"}))
    _spec(home.agents, "myapp--one", {"srv": _srv()})
    _spec(home.agents, "myapp--two", {"srv": _srv()})
    assert not (home.overlay / "myapp--one.json").exists()

    out = await overlay_refresh.refresh_after_agent_change(_wrote_specs)

    assert out == {"applied": True, "restart_required": False}
    assert _routed(home, "myapp--one") and _routed(home, "myapp--two")
    assert list(home.scratch.iterdir()) == [], "the probe pass's scratch is removed"


@pytest.mark.asyncio
async def test_reinstall_restores_an_overlay_the_uninstall_removed(home) -> None:
    _seed_store(srv=([], None, {}))
    _spec(home.agents, "host", {"srv": _srv()})
    _spec(home.agents, "myapp--one", {"srv": _srv()})
    _spec(home.agents, "myapp--two", {"srv": _srv()})
    _boot(home, frozenset({"srv"}))
    # Uninstall purges the overlays; the reinstall writes the specs back.
    (home.overlay / "myapp--one.json").unlink()
    (home.overlay / "myapp--two.json").unlink()
    _spec(home.agents, "myapp--one", {"srv": _srv()})
    _spec(home.agents, "myapp--two", {"srv": _srv()})

    out = await overlay_refresh.refresh_after_agent_change(_wrote_specs)

    assert out["applied"] is True
    assert _routed(home, "myapp--one") and _routed(home, "myapp--two")


@pytest.mark.asyncio
async def test_a_launch_the_daemon_does_not_serve_leaves_the_overlays_alone(home) -> None:
    """A stub whose target the running daemon lacks is a terminal rejection, so
    the overlays wait for the restart rather than routing to it."""
    _seed_store(srv=([], None, {}))
    _spec(home.agents, "host", {"srv": _srv()})
    _boot(home, frozenset({"srv"}))
    store = la.load_approvals()
    store.approved[la.target_stem("srv")].add(la.launch_fingerprint(_CMD, ["-V"], {}))
    store.approved_pairs[la.target_stem("srv")].add(
        la.launch_pair(rewriter.hash_command(_CMD, ["-V"]), la.env_fingerprint({}))
    )
    la._write(la.approvals_path(), la._document(store, {}))
    _spec(home.agents, "myapp--one", {"srv": _srv(["-V"])})
    before = sorted(p.name for p in home.overlay.glob("*.json"))

    out = await overlay_refresh.refresh_after_agent_change(_wrote_specs)

    assert out == {"applied": False, "restart_required": True}
    assert sorted(p.name for p in home.overlay.glob("*.json")) == before


@pytest.mark.asyncio
async def test_a_restart_only_setting_changed_since_start_is_not_applied(home, monkeypatch) -> None:
    """``forward_declared_env`` is read live by the rewriter; a refresh that saw
    a different value would apply that restart-only edit early."""
    _seed_store(srv=([], None, {}))
    _spec(home.agents, "host", {"srv": _srv()})
    _boot(home, frozenset({"srv"}))
    monkeypatch.setattr(rewriter, "forward_declared_env_enabled", lambda: False)
    _spec(home.agents, "myapp--one", {"srv": _srv()})

    out = await overlay_refresh.refresh_after_agent_change(_wrote_specs)

    assert out == {"applied": False, "restart_required": True}
    assert not (home.overlay / "myapp--one.json").exists()


def _sidecars(home) -> dict[str, bytes]:
    root = home.overlay.parent / "stubs"
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*.json")}


@pytest.mark.asyncio
async def test_a_server_still_pending_approval_does_not_block_the_refresh(home) -> None:
    """Boot refused and recorded ``pending``'s unapproved launch. The probe
    refuses it again in the same way; that is no new decision, so an app that
    only uses an approved server still gets its overlay."""
    _seed_store(srv=([], None, {}))
    _spec(home.agents, "host", {"srv": _srv(), "pending": _srv(["-V"])})
    _boot(home, frozenset({"srv", "pending"}))
    store_before = la.approvals_path().read_bytes()
    assert la.target_stem("pending") in la.load_approvals().stored_refused
    _spec(home.agents, "myapp--one", {"srv": _srv()})

    out = await overlay_refresh.refresh_after_agent_change(_wrote_specs)

    assert out == {"applied": True, "restart_required": False}
    assert _routed(home, "myapp--one")
    assert la.approvals_path().read_bytes() == store_before


@pytest.mark.asyncio
async def test_a_refresh_that_would_change_a_launch_decision_writes_nothing(
    home, monkeypatch
) -> None:
    """The app's own server's ``${VAR}`` expansion moved, so the pass would
    rebind its launch. That decision belongs to the next start: the refresh
    reports a restart and leaves overlays, sidecars and approvals untouched."""
    _seed_store(srv=([], _DECLARED, {"TOK": "old"}))
    _spec(home.agents, "host", {"srv": _srv(env=_DECLARED)})
    _boot(home, frozenset({"srv"}))
    overlays_before = {p.name: p.read_bytes() for p in home.overlay.glob("*.json")}
    sidecars_before = _sidecars(home)
    store_before = la.approvals_path().read_bytes()
    monkeypatch.setenv(_VAR, "new")
    _spec(home.agents, "myapp--one", {"srv": _srv(env=_DECLARED)})

    out = await overlay_refresh.refresh_after_agent_change(_wrote_specs)

    assert out == {"applied": False, "restart_required": True}
    assert {p.name: p.read_bytes() for p in home.overlay.glob("*.json")} == overlays_before
    assert _sidecars(home) == sidecars_before
    assert la.approvals_path().read_bytes() == store_before


@pytest.mark.asyncio
async def test_a_changed_launch_of_another_agents_server_writes_nothing(home, monkeypatch) -> None:
    """Another agent's server's ``${VAR}`` expansion moved. Publishing its new
    sidecar without saving its approval would break that server, so the
    refresh leaves every overlay, sidecar and approval as it was."""
    _seed_store(srv=([], None, {}), other=([], _DECLARED, {"TOK": "old"}))
    _spec(home.agents, "host", {"srv": _srv(), "other": _srv(env=_DECLARED)})
    _boot(home, frozenset({"srv", "other"}))
    host_before = (home.overlay / "host.json").read_bytes()
    sidecars_before = _sidecars(home)
    store_before = la.approvals_path().read_bytes()
    monkeypatch.setenv(_VAR, "new")
    _spec(home.agents, "myapp--one", {"srv": _srv()})

    out = await overlay_refresh.refresh_after_agent_change(_wrote_specs)

    assert out == {"applied": False, "restart_required": True}
    assert (home.overlay / "host.json").read_bytes() == host_before
    assert _sidecars(home) == sidecars_before
    assert la.approvals_path().read_bytes() == store_before
    assert not (home.overlay / "myapp--one.json").exists()


def test_reading_the_start_policy_costs_no_second_spec_scan(home, monkeypatch) -> None:
    calls: list[int] = []
    real = rewriter._rewrite_inputs_fingerprint

    def _spy(**kwargs):
        calls.append(1)
        return real(**kwargs)

    monkeypatch.setattr(rewriter, "_rewrite_inputs_fingerprint", _spy)
    _seed_store(srv=([], None, {}))
    _spec(home.agents, "host", {"srv": _srv()})

    orch = _boot(home, frozenset({"srv"}))

    assert calls == [1], "boot plus the policy capture must fingerprint the specs once"
    assert orch._mcp_rewrite_policy is not None


@pytest.mark.asyncio
async def test_no_broker_means_no_rewrite(home) -> None:
    _seed_store(srv=([], None, {}))
    _spec(home.agents, "host", {"srv": _srv()})
    orch = _boot(home, frozenset({"srv"}))
    orch._mcp_gateway_manager = None
    _spec(home.agents, "myapp--one", {"srv": _srv()})

    out = await overlay_refresh.refresh_after_agent_change(_wrote_specs)

    assert out == {"applied": False, "restart_required": False}
    assert not (home.overlay / "myapp--one.json").exists()


@pytest.mark.asyncio
async def test_registering_an_app_with_agents_asks_for_the_refresh(monkeypatch) -> None:
    from kiro_crew.apps import routes as routes_facade
    from kiro_crew.apps import routes_lifecycle
    from kiro_crew.apps.bridges import RegistrationResult

    calls: list[bool] = []

    async def _refresh(register) -> None:
        calls.append(await register())

    def _register(name: str) -> RegistrationResult:
        result = RegistrationResult()
        result.agents = ["myapp/one"] if name == "with-agents" else []
        return result

    monkeypatch.setattr(routes_facade, "register_app", _register)
    overlay_refresh.set_overlay_refresher(_refresh)
    assert (await routes_lifecycle._register_app_off_loop("no-agents")).agents == []
    assert (await routes_lifecycle._register_app_off_loop("with-agents")).agents == ["myapp/one"]
    assert calls == [False, True]
    overlay_refresh.set_overlay_refresher(None)
    assert (await routes_lifecycle._register_app_off_loop("with-agents")).agents == ["myapp/one"]


@pytest.mark.asyncio
async def test_registration_waits_for_an_in_flight_refresh(home, monkeypatch) -> None:
    """App registration writes its specs under the refresh lock, so it cannot
    land them between another refresh's check and its publish."""
    import asyncio

    from kiro_crew.apps import routes as routes_facade
    from kiro_crew.apps import routes_lifecycle
    from kiro_crew.apps.bridges import RegistrationResult
    from kiro_crew.testing.wait import until_parked

    _seed_store(srv=([], None, {}))
    _spec(home.agents, "host", {"srv": _srv()})
    orch = _boot(home, frozenset({"srv"}))
    registered: list[str] = []

    def _register(name: str) -> RegistrationResult:
        registered.append(name)
        _spec(home.agents, "myapp--one", {"srv": _srv()})
        result = RegistrationResult()
        result.agents = ["myapp/one"]
        return result

    monkeypatch.setattr(routes_facade, "register_app", _register)
    orch._mcp_overlay_refresh_lock = asyncio.Lock()
    await orch._mcp_overlay_refresh_lock.acquire()
    try:
        task = asyncio.ensure_future(routes_lifecycle._register_app_off_loop("myapp"))
        await until_parked(orch._mcp_overlay_refresh_lock, timeout=10)
        assert registered == [], "registration ran while a refresh held the lock"
    finally:
        orch._mcp_overlay_refresh_lock.release()

    await asyncio.wait_for(task, timeout=30)

    assert registered == ["myapp"]
    assert _routed(home, "myapp--one")


@pytest.mark.asyncio
async def test_a_failed_refresh_does_not_fail_the_app_action() -> None:
    async def _boom(_register) -> None:
        raise RuntimeError("disk full")

    overlay_refresh.set_overlay_refresher(_boom)
    assert await overlay_refresh.refresh_after_agent_change(_wrote_specs) is None


@pytest.mark.asyncio
async def test_the_refresh_hook_lives_exactly_as_long_as_the_broker() -> None:
    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch._mcp_resolve_prefetch = None
    orch._mcp_gateway_manager = None
    orch.dashboard_state = SimpleNamespace()
    orch._wire_mcp_gateway_dashboard()
    assert overlay_refresh._refresher is None, "wiring alone must not register the hook"
    overlay_refresh.set_overlay_refresher(orch._refresh_mcp_overlays)

    await orch._stop_mcp_broker()

    assert overlay_refresh._refresher is None


@pytest.mark.asyncio
async def test_a_refresh_waiting_on_a_broker_restart_reads_the_restarted_state(home) -> None:
    """A refresh that queued behind a broker restart (the sharing toggle) must
    read the broker's state after that restart, not before it, so it cannot
    publish overlays built from the inputs the restart replaced."""
    import asyncio

    from kiro_crew.testing.wait import until_parked

    _seed_store(srv=([], None, {}))
    _spec(home.agents, "host", {"srv": _srv()})
    orch = _boot(home, frozenset({"srv"}))
    _spec(home.agents, "myapp--one", {"srv": _srv()})
    orch._mcp_overlay_refresh_lock = asyncio.Lock()
    await orch._mcp_overlay_refresh_lock.acquire()
    try:
        task = asyncio.ensure_future(overlay_refresh.refresh_after_agent_change(_wrote_specs))
        await until_parked(orch._mcp_overlay_refresh_lock, timeout=10)
        # The restart under the lock stops the broker it read from.
        orch._mcp_gateway_manager = None
    finally:
        orch._mcp_overlay_refresh_lock.release()

    out = await asyncio.wait_for(task, timeout=30)

    assert out == {"applied": False, "restart_required": False}
    assert not (home.overlay / "myapp--one.json").exists()


@pytest.mark.asyncio
async def test_the_sharing_toggle_restarts_the_broker_under_the_refresh_lock(monkeypatch) -> None:
    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch._cfg = SimpleNamespace(mcp_gateway=SimpleNamespace(stub_servers=["srv"]))
    orch._mcp_stub_servers_started = frozenset({"srv"})
    orch._mcp_gateway_manager = object()
    orch._mcp_overlay_refresh_lock = None
    orch.dashboard_state = None
    orch.sessions = None
    held: list[bool] = []

    async def _stop() -> None:
        held.append(orch._mcp_overlay_refresh_lock.locked())
        orch._mcp_gateway_manager = None

    async def _init(stub_servers=None) -> None:
        held.append(orch._mcp_overlay_refresh_lock.locked())

    monkeypatch.setattr(orch, "_stop_mcp_broker", _stop)
    monkeypatch.setattr(orch, "_init_mcp_gateway", _init)
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: orch._cfg)
    )

    await orch._apply_mcp_gateway_enabled(False)

    assert held == [True, True]
