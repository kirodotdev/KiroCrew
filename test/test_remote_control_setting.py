"""Personal Remote Control: the setting, the live stop, the CLI and the route.

One-click off stops a running relay without a restart and stays off across one,
including when an overlay names the key; the guard only ever stops the relay and
acts on the setting alone; turning it on is the terminal command and takes a
restart.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from relay_client_helpers import OWNER, TOKEN, FakeCrew, FakeRelay, StaticHostTokenSource

from kiro_crew.config.integration_sections import TunnelConfig
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.remote_control import guard as guard_mod
from kiro_crew.remote_control import setting
from kiro_crew.remote_control.defaults import REMOTE_CONTROL_ON_BY_DEFAULT
from kiro_crew.remote_control.guard import RemoteControlGuard
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until, await_until


@pytest.fixture
def config_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_local_path", lambda: tmp_path / "local.json"
    )
    return path


def _stored(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")).get("tunnel", {}).get("relay_enabled")


# ── the setting ──────────────────────────────────────────────────────────────


class TestTheSetting:
    def test_the_default_is_the_one_named_value(self) -> None:
        assert TunnelConfig().relay_enabled is REMOTE_CONTROL_ON_BY_DEFAULT
        # Pending a product decision: off until it is made.
        assert REMOTE_CONTROL_ON_BY_DEFAULT is False

    def test_the_section_builder_reads_the_field_default_for_an_absent_key(
        self, monkeypatch
    ) -> None:
        # The builder states no default of its own: an omitted key resolves to the
        # field's default, so flipping REMOTE_CONTROL_ON_BY_DEFAULT flips the load.
        # Patched through the sections facade, which mirrors the write to every owner.
        from kiro_crew.config import section_builders, sections

        assert section_builders._build_tunnel_config({}).relay_enabled is False
        real = sections.field_default
        monkeypatch.setattr(
            sections,
            "field_default",
            lambda dto, key: True if key == "relay_enabled" else real(dto, key),
        )
        assert section_builders._build_tunnel_config({}).relay_enabled is True
        assert (
            section_builders._build_tunnel_config({"relay_enabled": False}).relay_enabled is False
        )

    def test_turning_it_on_writes_true(self, config_file) -> None:
        setting.turn_on()
        assert _stored(config_file) is True
        assert setting.requested() is True

    def test_off_is_explicit_and_survives_a_reload(self, config_file) -> None:
        setting.turn_on()
        setting.turn_off()
        # An explicit false, so a later default flip cannot turn it back on.
        assert _stored(config_file) is False
        assert KiroCrewConfig.load().tunnel.relay_enabled is False
        assert setting.requested() is False

    def test_off_also_writes_an_overlay_that_names_the_key(self, config_file, tmp_path) -> None:
        overlay = tmp_path / "local.json"
        overlay.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        setting.turn_off()
        assert _stored(overlay) is False
        assert setting.requested() is False

    def test_locked_follows_the_build_tunnel_lock(self, monkeypatch) -> None:
        from kiro_crew import edition

        assert setting.locked() is False
        monkeypatch.setattr(edition, "is_lts", lambda: True)
        assert setting.locked() is True

    def test_off_leaves_an_overlay_that_does_not_name_it(self, config_file, tmp_path) -> None:
        overlay = tmp_path / "local.json"
        overlay.write_text(json.dumps({"agent": {"bot_name": "x"}}), encoding="utf-8")
        setting.turn_off()
        assert json.loads(overlay.read_text()) == {"agent": {"bot_name": "x"}}


# ── the live stop ────────────────────────────────────────────────────────────


class _FakeProvider:
    def __init__(self, running: bool = True, state: str = "connected") -> None:
        self.is_running = running
        self.state = state

    async def ensure_available(self, *, install: bool = True) -> str:
        return self.state if self.is_running else "disabled"


class _FakeManager:
    def __init__(self, provider: _FakeProvider, *, start_works: bool = True) -> None:
        self.provider = provider
        self.start_works = start_works
        self.starts = 0
        self.stops = 0

    async def start(self) -> None:
        self.starts += 1
        if self.start_works:
            self.provider.is_running = True

    async def stop(self) -> None:
        self.stops += 1
        self.provider.is_running = False


class _Inputs:
    def __init__(self, requested: bool = True) -> None:
        self.guard: RemoteControlGuard | None = None
        self._requested = requested

    @property
    def requested(self) -> bool:
        return self._requested

    @requested.setter
    def requested(self, value: bool) -> None:
        # What the live config watcher dispatches to the guard's `tunnel` applier.
        self._requested = value
        if self.guard is not None:
            self.guard.on_config(_tunnel_change(value))


def _tunnel_change(relay_enabled: bool) -> Any:
    cfg = KiroCrewConfig()
    cfg.tunnel = dataclasses.replace(cfg.tunnel, relay_enabled=relay_enabled)
    return SimpleNamespace(old=None, new=cfg, changed=frozenset({"tunnel.relay_enabled"}))


def _guard(inputs: _Inputs, manager: _FakeManager, **kwargs: Any) -> RemoteControlGuard:
    guard = RemoteControlGuard(manager, manager.provider, requested=inputs.requested, **kwargs)
    inputs.guard = guard
    return guard


@pytest.mark.asyncio
class TestTheGuard:
    async def test_a_running_relay_is_left_alone_while_the_setting_is_on(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        for _ in range(5):
            await guard.tick()
        assert (manager.starts, manager.stops) == (0, 0)

    async def test_turning_it_off_stops_the_relay_at_once(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.requested = False
        await guard.tick()
        assert manager.stops == 1 and not manager.provider.is_running

    async def test_turning_it_off_writes_no_security_event(self, monkeypatch) -> None:
        audit = MagicMock()
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: audit)
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.requested = False
        await guard.tick()
        assert manager.stops == 1
        audit.log_api_access.assert_not_called()

    async def test_turning_it_back_on_does_not_start_it_without_a_restart(self) -> None:
        # `relay_enabled` is restart-only for on: after off then on, the guard
        # leaves the relay down until the gateway restarts.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.requested = False
        await guard.tick()
        inputs.requested = True
        await guard.apply()
        await guard.tick()
        assert (manager.starts, manager.stops) == (0, 1)

    async def test_a_stop_it_did_not_make_is_never_undone(self) -> None:
        # The gateway's shutdown stops the relay; the setting did not change.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        await manager.stop()
        for _ in range(3):
            await guard.tick()
        assert manager.starts == 0

    async def test_a_relay_that_never_started_is_left_alone(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider(running=False))
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.requested = False
        await guard.tick()
        assert (manager.starts, manager.stops) == (0, 0)

    async def test_apply_after_a_watcher_refresh_stops_a_lagging_off(
        self, config_file, monkeypatch
    ) -> None:
        # One-click off wrote false before the watcher's own poll saw it; the
        # handler's refresh_now() makes the watcher adopt it before apply().
        from kiro_crew.config import live

        monkeypatch.setattr(live, "_WATCH", None)
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        manager = _FakeManager(_FakeProvider())
        guard = RemoteControlGuard(manager, manager.provider)
        guard.subscribe()
        try:
            await live.watch().refresh_now()
            await guard.tick()
            setting.turn_off()
            assert manager.stops == 0, "the guard reads no config file of its own"
            await live.watch().refresh_now()
            await guard.apply()
            assert (manager.starts, manager.stops) == (0, 1)
        finally:
            await guard.stop()

    async def test_an_off_the_watcher_adopted_before_install_reaches_the_guard(
        self, config_file, monkeypatch
    ) -> None:
        # `kirocrew remote-control off` landed while the boot start ran: the watcher
        # adopted false before the guard subscribed.
        from kiro_crew.config import live

        monkeypatch.setattr(live, "_WATCH", None)
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": False}}), encoding="utf-8")
        # The first load rewrites the file (schema stamp); the second settles the
        # fingerprint, as the running watcher's next poll would.
        await live.watch().refresh_now()
        await live.watch().refresh_now()
        manager = _FakeManager(_FakeProvider())
        guard = RemoteControlGuard(manager, manager.provider)
        guard.subscribe()
        try:
            await guard.tick()
            assert manager.stops == 1
        finally:
            await guard.stop()

    async def test_racing_writes_end_on_the_one_that_landed_last(self) -> None:
        # An enable and a disable race; the disable is the last one the watcher
        # applied, and the enable's handler is the one that calls apply() last.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.requested = False
        await guard.apply()  # the disable's handler
        await guard.apply()  # the stale enable's handler, after it
        for _ in range(3):
            await guard.tick()
        assert (manager.starts, manager.stops) == (0, 1)

    async def test_a_drained_off_stops_within_the_drain_bound(self) -> None:
        # The route the one-click off takes: stop_after_drain -> apply -> tick.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager, idle=lambda: True)
        await guard.tick()
        inputs.requested = False
        guard.stop_after_drain()
        deferred = guard._deferred
        assert deferred is not None
        await asyncio.wait_for(asyncio.shield(deferred), timeout=guard_mod.DRAIN_SECONDS)
        assert manager.stops == 1 and not manager.provider.is_running
        assert manager.starts == 0

    async def test_off_still_tears_down_a_client_that_stopped_itself(self) -> None:
        # A client that stopped itself is left in its error state and
        # answers "unavailable", not "disabled". Off must still run the tunnel stop,
        # which closes the provider's token source and ingress.
        inputs = _Inputs()
        manager = _FakeManager(_FakeProvider(state="unavailable"))
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.requested = False
        await guard.tick()
        assert manager.stops == 1

    async def test_the_drain_is_bounded(self) -> None:
        # A request that never finishes (a stream left open) cannot keep it on.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager, idle=lambda: False, drain_seconds=0)
        await guard.tick()
        inputs.requested = False
        guard.stop_after_drain()
        guard.stop_after_drain()  # a second click while one is pending is one stop
        await async_wait_until(lambda: manager.stops == 1)
        assert not manager.provider.is_running and manager.stops == 1

    async def test_failed_stop_reservation_preserves_other_writers(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        inputs.requested = False
        with guard.reserve_stop():
            with pytest.raises(OSError), guard.reserve_stop():
                raise OSError("write failed")
            assert guard.stop_pending
            await guard.tick()
            assert manager.stops == 0
        assert not guard.stop_pending
        await guard.tick()
        assert manager.stops == 1

    async def test_shutdown_cancels_a_pending_off(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager, idle=lambda: False, drain_seconds=30.0)
        inputs.requested = False
        guard.stop_after_drain()
        assert guard.stop_pending
        await guard.stop()
        assert not guard.stop_pending and manager.stops == 0

    async def test_relay_idle_reads_every_live_connection(self) -> None:
        busy, done = asyncio.Event(), asyncio.Event()
        done.set()
        provider = MagicMock()
        provider._client._live = {MagicMock(http_streams_idle=done)}
        assert guard_mod._relay_http_idle(provider) is True
        provider._client._live.add(MagicMock(http_streams_idle=busy))
        assert guard_mod._relay_http_idle(provider) is False
        provider._client = None
        assert guard_mod._relay_http_idle(provider) is True

    async def test_install_skips_a_provider_that_is_not_the_relay(self) -> None:
        assert guard_mod.install_for_relay(MagicMock(), MagicMock()) is None
        assert guard_mod.current_guard() is None


@pytest.mark.asyncio
async def test_one_click_off_closes_a_live_relay_connection(monkeypatch) -> None:
    """End to end over a fake relay: connected, then off, and the leg closes."""
    from kiro_crew import tunnel as tunnel_pkg
    from kiro_crew.platform import build_default_context, set_context
    from kiro_crew.relay_client.ingress import LoopbackIngress
    from kiro_crew.relay_client.protocol import WS_MESSAGE_MAX_BYTES
    from kiro_crew.relay_client.provider import RelaySettings, RelayTunnelProvider
    from kiro_crew.relay_client.token_source import HostCredential
    from kiro_crew.tunnel.manager import TunnelState
    from kiro_crew.tunnel.setup import setup_tunnel

    monkeypatch.setattr(tunnel_pkg, "_tunnel_public_url", "")
    async with FakeRelay() as relay, FakeCrew() as crew:
        cfg = KiroCrewConfig()
        cfg.tunnel = dataclasses.replace(cfg.tunnel, relay_enabled=True)
        # config.json names no relay; with no lease service the settings name
        # both the host URL and the crew domain, as an edition builds them.
        provider = RelayTunnelProvider(
            RelaySettings(
                enabled=True,
                host_url=relay.url,
                crew_domain="us-east-1.relay.example",
            ),
            token_source=StaticHostTokenSource(
                HostCredential(TOKEN, crew_kind="personal", owner_user_id=OWNER)
            ),
            ingress_factory=lambda: LoopbackIngress(
                "127.0.0.1",
                crew.port,
                gateway=crew.gateway,
                max_ws_message_bytes=WS_MESSAGE_MAX_BYTES,
            ),
            client_version="0.0.0-test",
        )
        set_context(dataclasses.replace(build_default_context(cfg), tunnel=provider))
        middleware = MagicMock()
        middleware._is_token_auth = True
        manager = await setup_tunnel(
            middlewares=[middleware],
            allowed_origins=set(),
            tunnel_name_mode="username",
            tunnel_name_override="",
            port=crew.port,
            log_api_access=MagicMock(),
        )
        assert manager is not None
        guard = RemoteControlGuard(manager, provider)
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            await async_wait_until(lambda: manager.state is TunnelState.CONNECTED)
            await guard.tick()
            guard.on_config(_tunnel_change(False))
            await guard.apply()
            assert await leg.wait_closed() == 1001
            assert await provider.ensure_available() == "disabled"
            assert manager.state is TunnelState.STOPPED
        finally:
            await manager.stop()


def _cli(*argv: str) -> argparse.Namespace:
    from kiro_crew.remote_control.cli import register_parser

    parser = argparse.ArgumentParser(prog="kirocrew")
    register_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["remote-control", *argv])


class TestTheTerminalCommand:
    def test_on_then_off(self, config_file, capsys) -> None:
        from kiro_crew.remote_control.cli import run

        run(_cli("on"))
        assert "connects after Kiro Crew restarts" in capsys.readouterr().out
        assert _stored(config_file) is True
        run(_cli("off"))
        assert _stored(config_file) is False
        assert "stays off" in capsys.readouterr().out

    @pytest.mark.parametrize("stored, text", [(True, "on"), (False, "off")])
    def test_status_says_what_the_setting_asks_for(self, config_file, capsys, stored, text) -> None:
        from kiro_crew.remote_control.cli import run

        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": stored}}), encoding="utf-8")
        with pytest.raises(SystemExit):
            run(_cli("status"))
        assert capsys.readouterr().out == f"Remote Control: {text}\n"
        with pytest.raises(SystemExit):
            run(_cli("status", "--json"))
        assert json.loads(capsys.readouterr().out) == {"requested": stored, "locked": False}

    def test_a_locked_build_refuses_on_and_status_says_why(
        self, config_file, monkeypatch, capsys
    ) -> None:
        from kiro_crew import edition
        from kiro_crew.remote_control.cli import LOCKED_NOTICE, run

        monkeypatch.setattr(edition, "is_lts", lambda: True)
        with pytest.raises(SystemExit) as exit_:
            run(_cli("on"))
        assert exit_.value.code == 1
        assert LOCKED_NOTICE in capsys.readouterr().err
        assert json.loads(config_file.read_text()) == {}

        # A hand-edited true still reads as off here: a restart never connects.
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        with pytest.raises(SystemExit):
            run(_cli("status"))
        assert capsys.readouterr().out == f"Remote Control: off\n{LOCKED_NOTICE}\n"
        with pytest.raises(SystemExit):
            run(_cli("status", "--json"))
        assert json.loads(capsys.readouterr().out) == {"requested": True, "locked": True}
        # Off still writes: it clears the stored true.
        run(_cli("off"))
        assert _stored(config_file) is False


# ── the dashboard route ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client(monkeypatch, config_file):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import remote_control as handler

    async def _owner(_request, _op):
        return None

    monkeypatch.setattr(handler, "require_owner_dashboard_request", _owner)
    monkeypatch.setattr(handler, "sel", lambda: MagicMock())
    app = web.Application()
    app.router.add_get("/api/remote-control", handler.api_remote_control_get)
    app.router.add_put("/api/remote-control", handler.api_remote_control_put)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


@pytest.mark.asyncio
class TestTheRoute:
    @pytest.fixture(autouse=True)
    def _fresh_config_watcher(self, _floor_monkeypatch) -> None:
        # The off handler reloads through the process watcher; give each test its own.
        from kiro_crew.config import live

        _floor_monkeypatch.setattr(live, "_WATCH", None)

    async def test_get_carries_the_setting_and_the_connection_only(
        self, client, config_file
    ) -> None:
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        body = await (await client.get("/api/remote-control")).json()
        assert body == {"requested": True, "locked": False, "running": False}

    async def test_get_says_a_locked_build_ignores_the_setting(
        self, client, config_file, monkeypatch
    ) -> None:
        # An LTS build never starts the relay, so the card must not ask for a restart.
        from kiro_crew import edition

        monkeypatch.setattr(edition, "is_lts", lambda: True)
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        body = await (await client.get("/api/remote-control")).json()
        assert body == {"requested": True, "locked": True, "running": False}

    async def test_the_dashboard_cannot_turn_it_on(self, client, config_file) -> None:
        # Turning on is the terminal command's job.
        resp = await client.put("/api/remote-control", json={"enabled": True})
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_field_value"
        assert json.loads(config_file.read_text()) == {}

    async def test_a_relay_that_ended_in_error_is_not_reported_connected(
        self, client, config_file, monkeypatch
    ) -> None:
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        provider = _FakeProvider(state="unavailable")
        guard = RemoteControlGuard(_FakeManager(provider), provider)
        monkeypatch.setattr(guard_mod, "_guard", guard)
        guard.subscribe()
        body = await (await client.get("/api/remote-control")).json()
        assert body["running"] is False
        # ...while the stop decision still sees a client to clean up.
        assert await guard.running() is True

    async def test_put_off_writes_false_and_stops_the_relay(
        self, client, config_file, monkeypatch
    ) -> None:
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        manager = _FakeManager(_FakeProvider())
        # The real disk reader, so apply() sees what the PUT stored.
        guard = RemoteControlGuard(manager, manager.provider)
        await guard.tick()
        monkeypatch.setattr(guard_mod, "_guard", guard)
        guard.subscribe()
        resp = await client.put("/api/remote-control", json={"enabled": False})
        body = await resp.json()
        assert resp.status == 200
        # The stop's own config load runs right after the answer. Read the file
        # once it is done: a raw read racing it failed with EACCES on Windows CI.
        await async_wait_until(lambda: manager.stops == 1)
        assert _stored(config_file) is False
        assert body == {"requested": False, "locked": False, "running": False}

    async def test_put_off_answers_before_the_relay_stops(
        self, client, config_file, monkeypatch
    ) -> None:
        # The off request came through the relay: its answer is still open on the
        # relay connection, so stopping the relay first would lose it.
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        manager = _FakeManager(_FakeProvider())
        relay_idle = {"value": False}
        # A drain longer than any runner stall, so only the idle flip below ends it.
        guard = RemoteControlGuard(
            manager,
            manager.provider,
            idle=lambda: relay_idle["value"],
            drain_seconds=FALLBACK_TIMEOUT_SECS,
        )
        await guard.tick()
        monkeypatch.setattr(guard_mod, "_guard", guard)
        guard.subscribe()
        resp = await asyncio.wait_for(
            client.put("/api/remote-control", json={"enabled": False}),
            timeout=FALLBACK_TIMEOUT_SECS,
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"requested": False, "locked": False, "running": False}
        assert manager.stops == 0, "the relay must outlive the off request's answer"
        assert guard.stop_pending
        # A poll tick meanwhile sees the setting off but leaves the stop to the drain.
        await guard.tick()
        assert manager.stops == 0
        relay_idle["value"] = True  # the answer went back over the relay
        await async_wait_until(lambda: manager.stops == 1)
        assert not guard.stop_pending and not manager.provider.is_running

    async def test_poll_during_off_overlay_lock_preserves_the_response(
        self, client, config_file, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.config import loader

        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        overlay = tmp_path / "local.json"
        overlay.write_text(json.dumps({"agent": {"bot_name": "x"}}), encoding="utf-8")
        manager = _FakeManager(_FakeProvider())
        relay_idle = asyncio.Event()
        guard = RemoteControlGuard(
            manager,
            manager.provider,
            idle=relay_idle.is_set,
            drain_seconds=FALLBACK_TIMEOUT_SECS,
        )
        await guard.tick()
        monkeypatch.setattr(guard_mod, "_guard", guard)
        guard.subscribe()
        loop = asyncio.get_running_loop()
        overlay_waiting = asyncio.Event()
        real_lock = loader._config_write_lock

        @contextmanager
        def observed_lock(path, *, wait=True):
            if path == overlay:
                loop.call_soon_threadsafe(overlay_waiting.set)
            with real_lock(path, wait=wait):
                yield

        monkeypatch.setattr(loader, "_config_write_lock", observed_lock)
        request_task = None
        try:
            # The overlay does not override relay_enabled, so the base write is
            # visible to polling while its writer waits for the overlay lock.
            with real_lock(overlay, wait=False):
                request_task = asyncio.create_task(
                    client.put("/api/remote-control", json={"enabled": False})
                )
                await await_until(overlay_waiting)
                assert _stored(config_file) is False
                assert not request_task.done()
                await guard.tick()
                assert manager.stops == 0, "polling must not close the unanswered off request"
                assert guard.stop_pending
                assert guard._deferred is None, "the drain starts only after persistence finishes"
            resp = await await_until(request_task)
            assert resp.status == 200
            assert (await resp.json())["requested"] is False
            assert manager.stops == 0
            assert json.loads(overlay.read_text(encoding="utf-8")) == {"agent": {"bot_name": "x"}}
            relay_idle.set()
            await async_wait_until(lambda: manager.stops == 1)
            assert not guard.stop_pending
        finally:
            if request_task is not None:
                await await_until(request_task)
            await guard.stop()

    @pytest.mark.parametrize("partial_write", [False, True])
    async def test_failed_off_write_releases_poll_suppression(
        self, client, config_file, monkeypatch, partial_write
    ) -> None:
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        manager = _FakeManager(_FakeProvider())
        guard = RemoteControlGuard(manager, manager.provider)
        monkeypatch.setattr(guard_mod, "_guard", guard)
        guard.subscribe()
        real_off = setting.turn_off

        def failed_off():
            assert guard.stop_pending
            if partial_write:
                real_off()
            raise OSError("write failed")

        monkeypatch.setattr(setting, "turn_off", failed_off)
        try:
            resp = await client.put("/api/remote-control", json={"enabled": False})
            assert resp.status == 500
            assert not guard.stop_pending
            assert guard._deferred is None
            # The watcher's own poll (woken by the loader's write) adopts what landed.
            from kiro_crew.config import live

            await live.watch().refresh_now()
            await guard.tick()
            assert manager.stops == int(partial_write)
        finally:
            await guard.stop()

    async def test_put_needs_valid_json(self, client) -> None:
        resp = await client.put("/api/remote-control", data="{")
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_json"

    @pytest.mark.parametrize("body", [{"enabled": "false"}, {"enabled": 0}, {}, []])
    async def test_put_needs_enabled_false(self, client, body) -> None:
        resp = await client.put("/api/remote-control", json=body)
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_field_value"
