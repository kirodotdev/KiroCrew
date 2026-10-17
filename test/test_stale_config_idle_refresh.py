"""Stale-config detection for idle chats.

The write-triggered refresh, the stat-guarded sweep, the ConfigWatch
subscription and the pin that every config-writing route triggers a refresh.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from stale_config_helpers import _scan_on_every_os_fixture  # noqa: F401
from stale_config_helpers import kiro_home_fixture  # noqa: F401
from stale_config_helpers import _SERVER, _idle_chats, _status_slot, _write_spec

from kiro_crew.agent import DerivedSpecStale
from kiro_crew.dashboard import config_staleness as cs
from kiro_crew.dashboard import stale_config
from kiro_crew.dashboard.stale_config import SpawnInputs, is_stale

# ── Idle chats: write-triggered refresh and the stat-guarded sweep ───────────


def _settle(state):
    async def _drain():
        cs.schedule_refresh_all(state)
        while state._background_tasks:
            await asyncio.gather(*list(state._background_tasks))

    asyncio.run(_drain())


def test_an_idle_chat_is_badged_right_after_a_gateway_mcp_add(kiro_home, tmp_path, monkeypatch):
    """No turn: the write itself refreshes every live chat."""
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    state = _idle_chats(tmp_path, monkeypatch, kiro_home, keys=("s1", "s2"))
    _write_spec(kiro_home, {"mcpServers": {**_SERVER, "github": {"command": "gh"}}})

    _settle(state)

    assert all(s.config_stale for s in state._slots.values())


def test_the_gateway_config_writes_are_the_ones_that_refresh():
    assert cs.is_config_write("POST", "/api/mcp/toggle")
    assert cs.is_config_write("PUT", "/api/agents/detail/kirocrew")
    assert cs.is_config_write("PUT", "/api/agent/config")
    # The capability manager rewrites the spec's ``mcpServers`` and ``tools``.
    assert cs.is_config_write("POST", "/api/capability/mcp/install")
    assert cs.is_config_write("POST", "/api/capability/mcp/uninstall")
    assert cs.is_config_write("POST", "/api/capability/agents/install")
    assert not cs.is_config_write("GET", "/api/capability/mcp")
    assert not cs.is_config_write("POST", "/api/mcp-gateway/servers/stub")  # not fingerprinted
    assert not cs.is_config_write("POST", "/api/mcp-gateway/enable")
    assert not cs.is_config_write("PUT", "/api/config/kirocrew")  # ConfigWatch hears it
    assert not cs.is_config_write("GET", "/api/mcp/servers")
    assert not cs.is_config_write("POST", "/api/mcp-apps/call")
    assert not cs.is_config_write("POST", "/api/chat/slots/s1/reload")


def test_the_middleware_refreshes_after_a_successful_write_only(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    scheduled = MagicMock()
    monkeypatch.setattr(cs, "schedule_refresh_all", scheduled)
    middleware = cs.config_write_refresh_middleware(state)

    def _request(method, path):
        request = MagicMock()
        request.method, request.path = method, path
        return request

    async def _ok(_request):
        return MagicMock(status=200)

    async def _refused(_request):
        return MagicMock(status=409)

    asyncio.run(middleware(_request("POST", "/api/mcp/toggle"), _ok))
    asyncio.run(middleware(_request("POST", "/api/mcp/toggle"), _refused))
    asyncio.run(middleware(_request("GET", "/api/mcp/servers"), _ok))

    scheduled.assert_called_once_with(state)


def test_the_gateway_wires_the_write_refresh_and_the_sweep():
    """In the ordered list each chain assigns, so the assignment cannot drop it."""
    import inspect
    import re

    from kiro_crew.dashboard import server

    for entry in (server.start_dashboard, server.start_api_server):
        src = inspect.getsource(entry)
        assert "config_write_refresh=_config_write_refresh(state)" in src, entry.__name__
        assert "_arm_config_stale_sweep(app, state)" in src
    for install in (server._install_dashboard_middlewares, server._install_api_middlewares):
        src = inspect.getsource(install)
        chain = re.search(r"app\.middlewares\[:\] = \[(.*?)\n\s*\]\n", src, re.S)
        assert chain is not None, install.__name__
        assert "\n        config_write_refresh," in chain.group(1), install.__name__
    body = inspect.getsource(server._arm_config_stale_sweep)
    assert "start_config_stale_sweep(state)" in body


def _live_watch_subs():
    from kiro_crew.config import live

    return list(live.watch().subscriptions())


@pytest.mark.asyncio
async def test_stop_config_stale_detection_cancels_both(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    monkeypatch.setattr(cs, "CONFIG_STALE_SWEEP_SECS", 3600.0)
    state._config_stale_sweep = cs.start_config_stale_sweep(state)
    sub = MagicMock()
    state._config_stale_backend_sub = sub

    await cs.stop_config_stale_detection(state)

    sub.cancel.assert_called_once_with()
    assert state._config_stale_sweep is None
    assert not [t for t in asyncio.all_tasks() if "config_stale_sweep_loop" in repr(t)]


@pytest.mark.asyncio
async def test_the_built_api_server_keeps_the_write_refresh_and_it_fires(tmp_path, monkeypatch):
    """Built the way production builds it: the middleware survives, and a write refreshes."""
    import kiro_crew.config.loader as _loader
    import kiro_crew.dashboard.server as _srv
    import kiro_crew.dashboard.state as _st
    import kiro_crew.kiro_prerequisite as _prerequisite

    monkeypatch.setattr(_st, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(_srv, "data_home", lambda: tmp_path)
    monkeypatch.setattr(_loader, "config_dir", lambda: tmp_path)
    service = MagicMock()
    service.close = AsyncMock()
    service.seed_sessions_baseline = AsyncMock(return_value=True)
    monkeypatch.setattr(_prerequisite, "KiroPrerequisiteService", MagicMock(return_value=service))
    scheduled = MagicMock()
    monkeypatch.setattr(cs, "schedule_refresh_all", scheduled)

    runner, state = await _srv.start_api_server(
        sessions=MagicMock(count=0),
        crons=MagicMock(
            list_jobs=MagicMock(return_value=[]),
            list_jobs_async=AsyncMock(return_value=[]),
            status=MagicMock(return_value={}),
        ),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        port=0,
        assume_kiro_ready=True,
    )
    try:
        installed = [
            mw for mw in runner.app.middlewares if getattr(mw, "_is_config_write_refresh", False)
        ]
        assert len(installed) == 1
        request = MagicMock()
        request.method, request.path = "POST", "/api/mcp/toggle"

        async def _ok(_request):
            return MagicMock(status=200)

        await installed[0](request, _ok)
        scheduled.assert_called_once_with(state)
        assert state._config_stale_sweep is not None
        sweep = state._config_stale_sweep
        sub = state._config_stale_backend_sub
        assert not sweep.done() and sub is not None
    finally:
        await runner.cleanup()
    # Production cleanup ends both: no sweep task and no subscription outlive the app.
    assert sweep.done() and sweep.cancelled()
    assert state._config_stale_sweep is None and state._config_stale_backend_sub is None
    assert sub not in _live_watch_subs()


def test_an_external_edit_badges_an_idle_chat_on_one_sweep_tick(kiro_home, tmp_path, monkeypatch):
    _write_spec(kiro_home, {"prompt": "a"})
    state = _idle_chats(tmp_path, monkeypatch, kiro_home)
    slot = state._slots["s1"]
    assert asyncio.run(cs.refresh_all_config_stale(state, guarded=True)) == 1
    assert slot.config_stale is False

    _write_spec(kiro_home, {"prompt": "edited by hand"})
    asyncio.run(cs.refresh_all_config_stale(state, guarded=True))

    assert slot.config_stale is True

    # Reverting the edit clears it on the next tick.
    _write_spec(kiro_home, {"prompt": "a"})
    asyncio.run(cs.refresh_all_config_stale(state, guarded=True))
    assert slot.config_stale is False


def test_a_new_spec_file_appearing_counts_as_a_change(kiro_home, tmp_path):
    _write_spec(kiro_home, {"prompt": "a"})
    agents = kiro_home / "agents"
    st = agents.stat()
    before = stale_config.input_signature(SpawnInputs("kirocrew", ""))
    (agents / "other.json").write_text("{}")
    # The listing itself carries it, whatever the directory's own stat does.
    os.utime(agents, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = stale_config.input_signature(SpawnInputs("kirocrew", ""))
    assert after != before
    assert "other.json" in repr(after) and "other.json" not in repr(before)


def test_an_unchanged_stat_skips_the_fingerprint(kiro_home, tmp_path, monkeypatch):
    """The cost guard: an idle sweep over unchanged files fingerprints nothing."""
    _write_spec(kiro_home, {"prompt": "a"})
    state = _idle_chats(tmp_path, monkeypatch, kiro_home)
    asyncio.run(cs.refresh_all_config_stale(state, guarded=True))
    spy = MagicMock(side_effect=cs.current_config_fingerprint)
    monkeypatch.setattr(cs, "current_config_fingerprint", spy)

    assert asyncio.run(cs.refresh_all_config_stale(state, guarded=True)) == 0
    spy.assert_not_called()


def test_an_unchanged_stat_skips_a_chat_whose_reading_is_unknown(kiro_home, tmp_path, monkeypatch):
    """An input that cannot be read is no reason to re-fingerprint on every tick."""
    _write_spec(kiro_home, {"prompt": "a"})
    state = _idle_chats(tmp_path, monkeypatch, kiro_home)
    (kiro_home / "settings" / "mcp.json").mkdir()  # exists, cannot be read
    assert asyncio.run(cs.refresh_all_config_stale(state, guarded=True)) == 1
    spy = MagicMock(side_effect=cs.current_config_fingerprint)
    monkeypatch.setattr(cs, "current_config_fingerprint", spy)

    assert asyncio.run(cs.refresh_all_config_stale(state, guarded=True)) == 0
    spy.assert_not_called()

    # A stat change re-checks it, as for a known reading.
    (kiro_home / "settings" / "mcp.json").rmdir()
    assert asyncio.run(cs.refresh_all_config_stale(state, guarded=True)) == 1
    spy.assert_called_once()


def test_a_failed_recompute_is_retried_on_the_next_tick(kiro_home, tmp_path, monkeypatch):
    """An unknown that names no unreadable input records no guard."""
    _write_spec(kiro_home, {"prompt": "a"})
    state = _idle_chats(tmp_path, monkeypatch, kiro_home)
    monkeypatch.setattr(cs, "current_config_fingerprint", MagicMock(side_effect=OSError("x")))
    asyncio.run(cs.refresh_all_config_stale(state, guarded=True))

    assert next(iter(state._slots.values()))._config_stat_sig is None


class _CtimeShifted:
    """An ``lstat`` result with ``st_ctime_ns`` replaced, every other field as read."""

    def __init__(self, st: os.stat_result, ctime_ns: int) -> None:
        self._st = st
        self.st_ctime_ns = ctime_ns

    def __getattr__(self, name: str) -> object:
        return getattr(self._st, name)


def test_a_permission_change_alone_changes_the_signature(kiro_home, monkeypatch):
    """A ctime move with mtime and size unchanged (a chmod or chown) is a change.

    The ctime values are injected through ``PinnedDirectory.lstat`` rather than
    waiting on the filesystem's coarse ctime clock to tick.
    """
    from kiro_crew import platform_compat

    _write_spec(kiro_home, {"prompt": "a"})
    real_lstat = platform_compat.PinnedDirectory.lstat
    ctime = {"ns": 1_000}

    def _lstat(self, name):
        st = real_lstat(self, name)
        return _CtimeShifted(st, ctime["ns"]) if name == "kirocrew.json" else st

    monkeypatch.setattr(platform_compat.PinnedDirectory, "lstat", _lstat)
    before = stale_config.input_signature(SpawnInputs("kirocrew", ""))
    assert stale_config.input_signature(SpawnInputs("kirocrew", "")) == before
    ctime["ns"] = 2_000

    assert stale_config.input_signature(SpawnInputs("kirocrew", "")) != before


def test_the_spawn_fingerprint_is_taken_after_the_spawn_heals_the_managed_spec(
    kiro_home, tmp_path, monkeypatch
):
    """A deleted managed spec the spawn re-materializes is recorded as present."""
    healed: list[str | None] = []

    def _heal(agent):
        healed.append(agent)
        _write_spec(kiro_home, {"prompt": "regenerated"})
        return True

    monkeypatch.setattr(cs, "ensure_agent_materialized", _heal)
    cfg = MagicMock()
    cfg.agent.member_acp_backend = ""
    cfg.agent.acp_backend = ""
    monkeypatch.setattr(cs.KiroCrewConfig, "load", MagicMock(return_value=cfg))
    inputs = SpawnInputs("kirocrew", "")

    recorded = cs.spawn_config_fingerprint(cfg, "s1", inputs)

    assert healed == ["kirocrew"]
    now = cs.current_config_fingerprint(cfg, "s1", inputs)
    assert not is_stale(recorded, now, hot_reloads=False)
    # The warm pool takes its fingerprint the same way.
    (kiro_home / "agents" / "kirocrew.json").unlink()
    _inputs, pooled = cs.pool_spawn_config("kirocrew", "")
    assert not is_stale(pooled, cs.current_config_fingerprint(cfg, "", inputs), hot_reloads=False)


def test_the_spawn_fingerprint_passes_the_spawn_freshness_gate_after_the_heal(
    kiro_home, tmp_path, monkeypatch
):
    """The spawn's two pre-start steps run here in the spawn's order, with the project.

    A worker mirror the spawn's freshness gate re-derives is hashed as re-derived, and
    a gate refusal -- which refuses the spawn too -- yields no fingerprint at all."""
    calls: list[tuple] = []
    monkeypatch.setattr(cs, "ensure_agent_materialized", lambda a: calls.append(("heal", a)))

    def _gate(agent, work_dir):
        calls.append(("gate", agent, work_dir))
        _write_spec(kiro_home, {"prompt": "re-derived"})

    monkeypatch.setattr(cs, "require_fresh_derived_spec", _gate)
    cfg = MagicMock()
    cfg.agent.member_acp_backend = ""
    cfg.agent.acp_backend = ""
    inputs = SpawnInputs("kirocrew", str(tmp_path))

    recorded = cs.spawn_config_fingerprint(cfg, "s1", inputs)

    assert calls == [("heal", "kirocrew"), ("gate", "kirocrew", str(tmp_path))]
    now = cs.current_config_fingerprint(cfg, "s1", inputs)
    assert not is_stale(recorded, now, hot_reloads=False)

    def _refuse(agent, work_dir):
        raise DerivedSpecStale("refused")

    monkeypatch.setattr(cs, "require_fresh_derived_spec", _refuse)
    with pytest.raises(DerivedSpecStale):
        cs.spawn_config_fingerprint(cfg, "s1", inputs)


def _write_global_mcp(kiro_home: Path, servers: dict) -> None:
    (kiro_home / "settings" / "mcp.json").write_text(json.dumps({"mcpServers": servers}))


def test_a_kirocrew_server_declaration_edit_is_stale_on_a_hot_reloading_provider(
    kiro_home, tmp_path, monkeypatch
):
    """Crew re-reads it at session start, so no backend's hot reload applies it."""
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    _write_global_mcp(kiro_home, {"kirocrew-core": {"command": "kirocrew-core"}})
    state, slot = _status_slot(tmp_path, monkeypatch, hot_reload=True)
    _write_global_mcp(
        kiro_home, {"kirocrew-core": {"command": "kirocrew-core", "disabledTools": ["cron_add"]}}
    )

    out = asyncio.run(cs.refresh_config_stale(state, slot))

    assert out["stale"] is True
    assert out["changed"] == ["~/.kiro/settings/mcp.json"]


def test_another_server_edit_stays_reconcilable_on_a_hot_reloading_provider(
    kiro_home, tmp_path, monkeypatch
):
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    _write_global_mcp(kiro_home, {"kirocrew-core": {"command": "kirocrew-core"}})
    state, slot = _status_slot(tmp_path, monkeypatch, hot_reload=True)
    _write_global_mcp(
        kiro_home,
        {"kirocrew-core": {"command": "kirocrew-core"}, "gh": {"disabledTools": ["x"]}},
    )

    assert asyncio.run(cs.refresh_config_stale(state, slot))["stale"] is False


def test_creating_an_mcp_json_with_no_kirocrew_server_is_no_declaration_change():
    assert stale_config._identity_part("absent", None) == stale_config._identity_part(
        "ok", {"mcpServers": {"gh": {"command": "gh"}}}
    )


def test_the_sweep_skips_a_chat_mid_turn_and_one_with_no_process(kiro_home, tmp_path, monkeypatch):
    _write_spec(kiro_home, {"prompt": "a"})
    state = _idle_chats(tmp_path, monkeypatch, kiro_home, keys=("busy", "unspawned"))
    busy = MagicMock()
    busy.done.return_value = False
    state._slots["busy"].task = busy  # a turn in flight
    state._slots["unspawned"]._spawn_config = None
    _write_spec(kiro_home, {"prompt": "b"})

    assert asyncio.run(cs.refresh_all_config_stale(state, guarded=True)) == 0
    assert not any(s.config_stale for s in state._slots.values())


def test_the_sweep_loop_ticks_the_guarded_refresh(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    ticks: list[bool] = []

    async def _refresh(_state, *, guarded):
        ticks.append(guarded)
        if len(ticks) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(cs, "refresh_all_config_stale", _refresh)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cs.config_stale_sweep_loop(state, interval=0))
    assert ticks == [True, True]
    assert cs.CONFIG_STALE_SWEEP_SECS == 60.0


def test_a_write_burst_coalesces_into_at_most_two_passes(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    passes: list[int] = []

    async def _refresh(_state, *, guarded):
        passes.append(1)
        if len(passes) == 1:
            cs.schedule_refresh_all(state)  # a write landing while the pass runs
            cs.schedule_refresh_all(state)
        return 0

    monkeypatch.setattr(cs, "refresh_all_config_stale", _refresh)

    async def _run():
        cs.schedule_refresh_all(state)
        cs.schedule_refresh_all(state)
        while state._background_tasks:
            await asyncio.gather(*list(state._background_tasks))

    asyncio.run(_run())
    assert len(passes) == 2


# ── ConfigWatch subscription and the write-route pin ─────────────────────────


def test_a_configwatch_backend_change_refreshes_every_chat(tmp_path, monkeypatch):
    from kiro_crew.config import live

    state = _make_state(tmp_path)
    scheduled = MagicMock()
    monkeypatch.setattr(cs, "schedule_refresh_all", scheduled)
    watch = live.ConfigWatch()
    monkeypatch.setattr(live, "watch", lambda: watch)

    sub = cs.subscribe_backend_changes(state)
    try:
        assert set(sub.prefixes) == {"agent.acp_backend", "agent.member_acp_backend"}
        cb = sub.callback()
        cb(live.ConfigChange(old=None, new=MagicMock(), changed=frozenset({"agent.model"})))
        scheduled.assert_not_called()
        cb(live.ConfigChange(old=None, new=MagicMock(), changed=frozenset({"agent.acp_backend"})))
        scheduled.assert_called_once_with(state)
    finally:
        sub.cancel()


def test_the_gateway_subscribes_the_badge_to_configwatch():
    import inspect

    from kiro_crew.dashboard import server

    assert "subscribe_backend_changes(state)" in inspect.getsource(server._arm_config_stale_sweep)


def test_the_sweep_does_not_stat_config_json(kiro_home, monkeypatch):
    """config.json has one watcher, ConfigWatch; the sweep leaves it alone."""
    from kiro_crew.config.loader import config_path

    statted: list[str] = []
    real = stale_config._stat_in
    monkeypatch.setattr(
        stale_config,
        "_stat_in",
        lambda d, n: statted.append(os.path.join(d.path, n)) or real(d, n),
    )
    stale_config.input_signature(SpawnInputs("kirocrew", ""))
    assert str(config_path()) not in statted
    assert "config" not in dict(
        (k, v) for k, v in stale_config.input_signature(SpawnInputs("kirocrew", ""))
    )


#: Non-GET routes under the agent and MCP paths that write nothing a chat's
#: fingerprint reads, with why. Every other one must match a refresh prefix.
_NOT_CONFIG_WRITES = {
    "/api/mcp-apps/": "an MCP app's tool call or message, not MCP server config",
    "/api/agent-panel/": "replaces a crew's agent-panel webview, not an agent spec",
    "/api/agent-ask/": "opens, waits on or withdraws an agent's question card, not config",
    "/api/mcp-gateway/": (
        "writes mcp_gateway.* in config.json or an in-process resolve cache; the"
        " stub set and its overlay specs are deliberately not fingerprinted"
    ),
}


def test_every_agent_and_mcp_write_route_is_a_refresh_prefix():
    """Pinned to the routes the gateway registers, so a new writer cannot be missed."""
    from aiohttp import web

    from kiro_crew.dashboard.routes import register_all
    from kiro_crew.dashboard.server import _register_mcp_routes

    app = web.Application()
    _register_mcp_routes(app)
    register_all(app)
    writers = {
        (route.method, str(route.resource.canonical))
        for route in app.router.routes()
        if route.method not in {"GET", "HEAD", "OPTIONS"}
        and route.resource is not None
        and str(route.resource.canonical).startswith(("/api/agent", "/api/capability", "/api/mcp"))
    }
    assert writers, "no agent or MCP write routes found"
    for method, path in sorted(writers):
        if path.startswith(tuple(_NOT_CONFIG_WRITES)):
            assert not cs.is_config_write(method, path), path
            continue
        assert cs.is_config_write(method, path), f"{method} {path} refreshes nothing"
