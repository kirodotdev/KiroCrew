"""Personal Remote Control: the sign-in rule, the setting, the live stop, the CLI and route.

An API-key or external IdP sign-in cannot turn personal Remote Control on from the
dashboard, the terminal command or config; the person is told why; one-click off
stops a running relay without a restart and stays off across one; a personal Kiro
sign-in is unaffected. Nearby inputs that reach the same code are covered too:
sign-out, an account switch, a sign-in kind changing while it is on, an unknown
sign-in kind, an unreadable vault, and an enterprise build's lock.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from relay_client_helpers import OWNER, TOKEN, FakeCrew, FakeRelay

from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.config.integration_sections import TunnelConfig
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.remote_control import guard as guard_mod
from kiro_crew.remote_control import policy, setting
from kiro_crew.remote_control.defaults import REMOTE_CONTROL_ON_BY_DEFAULT
from kiro_crew.remote_control.guard import RemoteControlGuard
from kiro_crew.remote_control.policy import Decision, allowed
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until, await_until

_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)


def _token(provider: str, identity: str = "builder_id", auth_method: str | None = None) -> KasToken:
    return KasToken(
        access_token="atk-secret",
        expires_at=_FUTURE,
        provider=provider,
        identity=identity,
        refresh_token="rtk-secret",
        auth_method=auth_method,
    )


@pytest.fixture
def vault(tmp_path, monkeypatch) -> TokenStore:
    """The gateway's vault, under tmp_path, with no ``KIRO_API_KEY`` in the env."""
    from kiro_crew.auth import bridge

    store = TokenStore(tmp_path / "vault")
    monkeypatch.setattr(bridge, "default_token_store", lambda: store)
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    return store


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


# ── the sign-in rule ─────────────────────────────────────────────────────────


class TestTheSignInRule:
    @pytest.mark.parametrize(
        "provider", ["BuilderId", "Google", "Github", "Enterprise", "Internal"]
    )
    def test_a_personal_kiro_sign_in_is_allowed(self, provider) -> None:
        assert allowed(_token(provider)) == (True, "")

    def test_an_api_key_is_refused(self) -> None:
        assert allowed(_token("ApiKey")) == (False, policy.REASON_API_KEY)

    def test_the_env_api_key_is_refused_when_the_vault_is_empty(self) -> None:
        assert allowed(None, env_api_key=True) == (False, policy.REASON_API_KEY)

    def test_a_stored_personal_sign_in_outranks_the_env_api_key(self) -> None:
        # The same precedence KasAuthProvider applies: the vault wins.
        assert allowed(_token("BuilderId"), env_api_key=True) == (True, "")

    def test_an_external_idp_is_refused_by_provider_or_auth_method(self) -> None:
        assert allowed(_token("ExternalIdp", "external_idp")) == (False, policy.REASON_EXTERNAL_IDP)
        # The auth method decides even under a provider that would otherwise pass.
        assert allowed(_token("Enterprise", auth_method="external_idp")) == (
            False,
            policy.REASON_EXTERNAL_IDP,
        )

    def test_an_unknown_sign_in_kind_is_refused(self) -> None:
        assert allowed(_token("SomethingNew")) == (False, policy.REASON_UNKNOWN_SIGN_IN)

    def test_no_sign_in_is_refused(self) -> None:
        assert allowed(None) == (False, policy.REASON_NOT_SIGNED_IN)

    def test_every_refusal_has_a_notice(self) -> None:
        for reason in (
            policy.REASON_API_KEY,
            policy.REASON_EXTERNAL_IDP,
            policy.REASON_NOT_SIGNED_IN,
            policy.REASON_UNKNOWN_SIGN_IN,
            policy.REASON_LOCKED,
        ):
            assert Decision(False, reason).notice
        assert Decision(True).notice == ""


class TestTheCurrentDecision:
    def test_reads_the_vault(self, vault) -> None:
        assert policy.current_decision() == Decision(False, policy.REASON_NOT_SIGNED_IN)
        vault.save(_token("BuilderId"))
        assert policy.current_decision() == Decision(True)

    def test_an_external_idp_in_the_vault(self, vault) -> None:
        vault.save(_token("ExternalIdp", "external_idp", auth_method="external_idp"))
        assert policy.current_decision().reason == policy.REASON_EXTERNAL_IDP

    def test_the_env_api_key(self, vault, monkeypatch) -> None:
        monkeypatch.setenv("KIRO_API_KEY", "ksk-test")
        assert policy.current_decision().reason == policy.REASON_API_KEY

    def test_an_unreadable_vault_refuses(self, monkeypatch) -> None:
        from kiro_crew.auth import bridge

        def _broken() -> TokenStore:
            raise OSError("vault unreadable")

        monkeypatch.setattr(bridge, "default_token_store", _broken)
        assert policy.current_decision().reason == policy.REASON_UNKNOWN_SIGN_IN

    def test_an_enterprise_lock_refuses_before_the_vault(self, vault, monkeypatch) -> None:
        vault.save(_token("BuilderId"))
        monkeypatch.setattr("kiro_crew.edition_locks.local_enable_locked", lambda _c: True)
        assert policy.current_decision().reason == policy.REASON_LOCKED

    def test_the_snapshot_read_pairs_the_verdict_with_its_markers(self, vault) -> None:
        vault.bump_signin_generation()
        vault.save(_token("BuilderId"))
        decision, gen = policy.current_decision_with_generation()
        assert decision == Decision(True)
        assert gen == policy.sign_in_generation()

    def test_the_markers_move_on_sign_in_and_sign_out(self, vault) -> None:
        before = policy.sign_in_generation()
        vault.bump_signin_generation()
        after_in = policy.sign_in_generation()
        vault.bump_signout_generation()
        assert len({before, after_in, policy.sign_in_generation()}) == 3


# ── the setting ──────────────────────────────────────────────────────────────


class TestTheSetting:
    def test_the_default_is_the_one_named_value(self) -> None:
        assert TunnelConfig().relay_enabled is REMOTE_CONTROL_ON_BY_DEFAULT
        # Pending a product decision: off until it is made.
        assert REMOTE_CONTROL_ON_BY_DEFAULT is False

    def test_turning_on_is_refused_and_writes_nothing(
        self, vault, config_file, monkeypatch
    ) -> None:
        monkeypatch.setenv("KIRO_API_KEY", "ksk-test")
        with pytest.raises(setting.RemoteControlRefused) as refused:
            setting.turn_on()
        assert refused.value.decision.reason == policy.REASON_API_KEY
        assert json.loads(config_file.read_text()) == {}

    def test_a_personal_sign_in_turns_it_on(self, vault, config_file) -> None:
        vault.save(_token("BuilderId"))
        setting.turn_on()
        assert _stored(config_file) is True
        assert setting.read().effective is True

    def test_off_is_explicit_and_survives_a_reload(self, vault, config_file) -> None:
        vault.save(_token("BuilderId"))
        setting.turn_on()
        setting.turn_off()
        # An explicit false, so a later default flip cannot turn it back on.
        assert _stored(config_file) is False
        assert KiroCrewConfig.load().tunnel.relay_enabled is False

    def test_off_also_writes_an_overlay_that_names_the_key(
        self, vault, config_file, tmp_path
    ) -> None:
        overlay = tmp_path / "local.json"
        overlay.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        setting.turn_off()
        assert _stored(overlay) is False

    def test_off_leaves_an_overlay_that_does_not_name_it(
        self, vault, config_file, tmp_path
    ) -> None:
        overlay = tmp_path / "local.json"
        overlay.write_text(json.dumps({"agent": {"bot_name": "x"}}), encoding="utf-8")
        setting.turn_off()
        assert json.loads(overlay.read_text()) == {"agent": {"bot_name": "x"}}

    def test_a_hand_edited_true_with_a_refused_sign_in_is_not_effective(
        self, vault, config_file
    ) -> None:
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        vault.save(_token("ExternalIdp", "external_idp", auth_method="external_idp"))
        view = setting.read()
        assert view.requested is True and view.effective is False
        assert view.decision.reason == policy.REASON_EXTERNAL_IDP


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
    def __init__(self, requested: bool = True, decision: Decision = Decision(True)) -> None:
        self.requested = requested  # the live snapshot
        self.stored: bool | None = None  # what disk holds, when it differs
        self.decision = decision
        self.gen = ("a", "")
        #: The markers the locked vault read reports, when it differs from ``gen``.
        self.locked_gen: tuple[str, str] | None = None
        self.decides = 0

    def decide(self) -> tuple[Decision, tuple[str, str]]:
        self.decides += 1
        return self.decision, self.locked_gen or self.gen


def _guard(inputs: _Inputs, manager: _FakeManager) -> RemoteControlGuard:
    return RemoteControlGuard(
        manager,
        manager.provider,
        requested=lambda: inputs.requested,
        stored=lambda: inputs.requested if inputs.stored is None else inputs.stored,
        decide=inputs.decide,
        generation=lambda: inputs.gen,
    )


@pytest.mark.asyncio
class TestTheGuard:
    async def test_a_running_relay_is_left_alone_while_on_and_allowed(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        await guard.tick()
        assert (manager.starts, manager.stops) == (0, 0)

    async def test_turning_it_off_stops_the_relay_at_once(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.requested = False
        await guard.tick()
        assert manager.stops == 1 and not manager.provider.is_running

    async def test_a_sign_in_that_stops_being_allowed_stops_the_relay(self) -> None:
        # The account switched to an API key while Remote Control was on.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.decision = Decision(False, policy.REASON_API_KEY)
        await guard.tick()
        assert manager.stops == 0, "a cached verdict is used until the markers move"
        inputs.gen = ("b", "")
        await guard.tick()
        assert manager.stops == 1

    async def test_sign_out_stops_the_relay(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.decision, inputs.gen = Decision(False, policy.REASON_NOT_SIGNED_IN), ("a", "out")
        await guard.tick()
        assert manager.stops == 1

    async def test_the_vault_is_read_only_when_the_markers_move(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        for _ in range(5):
            await guard.tick()
        assert inputs.decides == 1

    async def test_turning_it_back_on_starts_it_once(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.requested = False
        await guard.tick()
        inputs.requested = True
        await guard.tick()
        await guard.tick()
        assert (manager.starts, manager.stops) == (1, 1)

    async def test_a_relay_that_cannot_start_is_not_retried_every_tick(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider(), start_works=False)
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.requested = False
        await guard.tick()
        inputs.requested = True
        for _ in range(4):
            await guard.tick()
        assert manager.starts == 1

    async def test_a_stop_it_did_not_make_is_never_undone(self) -> None:
        # The gateway's shutdown stops the relay; the inputs did not change.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        await manager.stop()
        for _ in range(3):
            await guard.tick()
        assert manager.starts == 0

    async def test_a_boot_refusal_does_not_start_it_until_the_sign_in_changes(self) -> None:
        # Booted with an external IdP: the provider refused, nothing runs.
        inputs = _Inputs(decision=Decision(False, policy.REASON_EXTERNAL_IDP))
        manager = _FakeManager(_FakeProvider(running=False))
        guard = _guard(inputs, manager)
        await guard.tick()
        await guard.tick()
        assert (manager.starts, manager.stops) == (0, 0)
        # The person signs in with their own Kiro account.
        inputs.decision, inputs.gen = Decision(True), ("b", "")
        await guard.tick()
        assert manager.starts == 1

    async def test_apply_reads_disk_while_the_live_config_lags(self) -> None:
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        # One-click off wrote false; the live snapshot still says true.
        inputs.stored = False
        await guard.apply()
        await guard.tick()
        assert (manager.starts, manager.stops) == (0, 1)
        inputs.requested, inputs.stored = False, None  # the snapshot catches up
        await guard.tick()
        assert (manager.starts, manager.stops) == (0, 1)

    async def test_racing_writes_end_on_the_one_that_landed_last(self) -> None:
        # An enable and a disable race; the disable lands on disk last, and the
        # enable's handler is the one that calls apply() last.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.stored = False
        await guard.apply()  # the disable's handler
        await guard.apply()  # the stale enable's handler, after it
        for _ in range(3):
            await guard.tick()
        assert (manager.starts, manager.stops) == (0, 1)

    async def test_a_sign_in_landing_between_the_two_reads_is_seen_next_tick(self) -> None:
        # The cheap marker read sees the new sign-in's marker, but the locked
        # vault read still holds the old account: its verdict is cached under the
        # OLD markers, so the next tick re-reads.
        inputs, manager = _Inputs(), _FakeManager(_FakeProvider())
        guard = _guard(inputs, manager)
        await guard.tick()
        inputs.gen, inputs.locked_gen = ("b", ""), ("a", "")
        await guard.tick()
        assert manager.stops == 0
        inputs.decision, inputs.locked_gen = Decision(False, policy.REASON_EXTERNAL_IDP), None
        await guard.tick()
        assert manager.stops == 1

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
        guard = RemoteControlGuard(
            manager,
            manager.provider,
            requested=lambda: inputs.requested,
            stored=lambda: False,
            decide=inputs.decide,
            generation=lambda: inputs.gen,
            idle=lambda: False,
            drain_seconds=0,
        )
        await guard.tick()
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
        guard = RemoteControlGuard(
            manager,
            manager.provider,
            requested=lambda: inputs.requested,
            stored=lambda: False,
            decide=inputs.decide,
            generation=lambda: inputs.gen,
            idle=lambda: False,
            drain_seconds=30.0,
        )
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
    from kiro_crew.relay_client.token_source import HostCredential, StaticHostTokenSource
    from kiro_crew.tunnel.manager import TunnelState
    from kiro_crew.tunnel.setup import setup_tunnel

    monkeypatch.setattr(policy, "current_decision", lambda: Decision(True))
    monkeypatch.setattr(tunnel_pkg, "_tunnel_public_url", "")
    async with FakeRelay() as relay, FakeCrew() as crew:
        cfg = KiroCrewConfig()
        # With no lease service the relay's crew domain must be named too.
        cfg.tunnel = dataclasses.replace(
            cfg.tunnel,
            relay_enabled=True,
            relay_host_url=relay.url,
            relay_crew_domain="us-east-1.relay.example",
        )
        provider = RelayTunnelProvider(
            RelaySettings.from_config(cfg),
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
        requested = {"value": True}
        guard = RemoteControlGuard(
            manager,
            provider,
            requested=lambda: True,
            stored=lambda: requested["value"],
            decide=lambda: (Decision(True), ("a", "")),
            generation=lambda: ("a", ""),
        )
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            await async_wait_until(lambda: manager.state is TunnelState.CONNECTED)
            await guard.tick()
            requested["value"] = False
            await guard.apply()
            assert await leg.wait_closed() == 1001
            assert await provider.ensure_available() == "disabled"
            assert manager.state is TunnelState.STOPPED
        finally:
            await manager.stop()


@pytest.mark.asyncio
async def test_the_provider_refuses_to_start_for_a_refused_sign_in(monkeypatch) -> None:
    from kiro_crew.relay_client.provider import RelaySettings, RelayTunnelProvider

    monkeypatch.setattr(
        policy, "current_decision", lambda: Decision(False, policy.REASON_EXTERNAL_IDP)
    )
    cfg = KiroCrewConfig()
    cfg.tunnel = dataclasses.replace(cfg.tunnel, relay_enabled=True)
    factory = MagicMock()
    provider = RelayTunnelProvider(
        RelaySettings.from_config(cfg), ingress_factory=MagicMock, client_factory=factory
    )
    await provider.start()
    factory.assert_not_called()
    snapshot = provider.status_snapshot()
    assert snapshot is not None and snapshot["state"] == "error"
    assert "external identity provider" in snapshot["error"]
    assert await provider.ensure_available() == "disabled"


# ── the terminal command ─────────────────────────────────────────────────────


def _cli(*argv: str) -> argparse.Namespace:
    from kiro_crew.remote_control.cli import register_parser

    parser = argparse.ArgumentParser(prog="kirocrew")
    register_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["remote-control", *argv])


class TestTheTerminalCommand:
    def test_on_is_refused_for_an_api_key(self, vault, config_file, monkeypatch, capsys) -> None:
        from kiro_crew.remote_control.cli import run

        monkeypatch.setenv("KIRO_API_KEY", "ksk-test")
        with pytest.raises(SystemExit) as exit_:
            run(_cli("on"))
        assert exit_.value.code == 1
        assert "API key" in capsys.readouterr().err
        assert json.loads(config_file.read_text()) == {}

    def test_on_then_off_for_a_personal_sign_in(self, vault, config_file, capsys) -> None:
        from kiro_crew.remote_control.cli import run

        vault.save(_token("BuilderId"))
        run(_cli("on"))
        assert _stored(config_file) is True
        run(_cli("off"))
        assert _stored(config_file) is False
        assert "stays off" in capsys.readouterr().out

    def test_status_names_the_refusal(self, vault, config_file, capsys) -> None:
        from kiro_crew.remote_control.cli import run

        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        vault.save(_token("ExternalIdp", "external_idp", auth_method="external_idp"))
        with pytest.raises(SystemExit):
            run(_cli("status", "--json"))
        out = json.loads(capsys.readouterr().out)
        assert out == {
            "requested": True,
            "allowed": False,
            "reason": "external_idp",
            "notice": policy.NOTICES["external_idp"],
            "effective": False,
        }

    def test_config_set_refuses_turning_it_on(
        self, vault, config_file, monkeypatch, capsys
    ) -> None:
        from kiro_crew import cli_config

        audit = MagicMock()
        monkeypatch.setattr(cli_config, "sel", lambda: audit)
        monkeypatch.setenv("KIRO_API_KEY", "ksk-test")
        args = argparse.Namespace(
            config_action="set",
            key="tunnel.relay_enabled",
            value="true",
            file=None,
            value_base64=None,
            local=False,
        )
        with pytest.raises(SystemExit) as exit_:
            cli_config._run_config_cmd(args)
        assert exit_.value.code == 1
        assert "API key" in capsys.readouterr().err
        assert json.loads(config_file.read_text()) == {}
        audit.log_api_access.assert_called_once()
        assert audit.log_api_access.call_args.kwargs["outcome"] == "denied"


# ── the dashboard route ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client(monkeypatch, vault, config_file):
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
    async def test_get_carries_the_reason_for_the_notice(
        self, client, config_file, monkeypatch
    ) -> None:
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        monkeypatch.setenv("KIRO_API_KEY", "ksk-test")
        body = await (await client.get("/api/remote-control")).json()
        assert body["requested"] is True and body["allowed"] is False
        assert body["reason"] == "api_key" and body["notice"]
        assert body["live"] is False and body["default_on"] is REMOTE_CONTROL_ON_BY_DEFAULT

    async def test_put_on_is_refused_with_409(self, client, vault, config_file) -> None:
        vault.save(_token("ExternalIdp", "external_idp", auth_method="external_idp"))
        resp = await client.put("/api/remote-control", json={"enabled": True})
        assert resp.status == 409
        assert (await resp.json())["reason"] == "external_idp"
        assert json.loads(config_file.read_text()) == {}

    async def test_put_off_writes_false_and_stops_the_relay(
        self, client, vault, config_file, monkeypatch
    ) -> None:
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        manager = _FakeManager(_FakeProvider())
        # The real disk reader, so apply() sees what the PUT stored.
        guard = RemoteControlGuard(
            manager,
            manager.provider,
            requested=lambda: True,
            decide=lambda: (Decision(True), ("a", "")),
            generation=lambda: ("a", ""),
        )
        await guard.tick()
        monkeypatch.setattr(guard_mod, "_guard", guard)
        resp = await client.put("/api/remote-control", json={"enabled": False})
        body = await resp.json()
        assert resp.status == 200
        assert _stored(config_file) is False
        await async_wait_until(lambda: manager.stops == 1)
        assert body["requested"] is False and body["running"] is False
        assert body["restart_required"] is False

    async def test_put_off_answers_before_the_relay_stops(
        self, client, vault, config_file, monkeypatch
    ) -> None:
        # The off request came through the relay: its answer is still open on the
        # relay connection, so stopping the relay first would lose it.
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
        manager = _FakeManager(_FakeProvider())
        relay_idle = {"value": False}
        guard = RemoteControlGuard(
            manager,
            manager.provider,
            requested=lambda: True,
            decide=lambda: (Decision(True), ("a", "")),
            generation=lambda: ("a", ""),
            idle=lambda: relay_idle["value"],
        )
        await guard.tick()
        monkeypatch.setattr(guard_mod, "_guard", guard)
        resp = await client.put("/api/remote-control", json={"enabled": False})
        assert resp.status == 200
        body = await resp.json()
        assert body["requested"] is False and body["running"] is False
        assert manager.stops == 0, "the relay must outlive the off request's answer"
        assert guard.stop_pending
        # A poll tick meanwhile sees the setting off but leaves the stop to the drain.
        await guard.tick()
        assert manager.stops == 0
        relay_idle["value"] = True  # the answer went back over the relay
        await async_wait_until(lambda: manager.stops == 1)
        assert not guard.stop_pending and not manager.provider.is_running

    async def test_put_on_without_a_relay_at_boot_says_a_restart_is_needed(
        self, client, vault, config_file
    ) -> None:
        vault.save(_token("BuilderId"))
        body = await (await client.put("/api/remote-control", json={"enabled": True})).json()
        assert body["requested"] is True and body["restart_required"] is True

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
            requested=setting.requested,
            decide=lambda: (Decision(True), ("a", "")),
            generation=lambda: ("a", ""),
            idle=relay_idle.is_set,
            drain_seconds=FALLBACK_TIMEOUT_SECS,
        )
        await guard.tick()
        monkeypatch.setattr(guard_mod, "_guard", guard)
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
        guard = RemoteControlGuard(
            manager,
            manager.provider,
            requested=setting.requested,
            decide=lambda: (Decision(True), ("a", "")),
            generation=lambda: ("a", ""),
        )
        monkeypatch.setattr(guard_mod, "_guard", guard)
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
            await guard.tick()
            assert manager.stops == int(partial_write)
        finally:
            await guard.stop()

    async def test_put_needs_valid_json(self, client) -> None:
        resp = await client.put("/api/remote-control", data="{")
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_json"

    @pytest.mark.parametrize("body", [{"enabled": "false"}, {"enabled": 0}, {}, []])
    async def test_put_needs_a_boolean(self, client, body) -> None:
        resp = await client.put("/api/remote-control", json=body)
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_field_type"
