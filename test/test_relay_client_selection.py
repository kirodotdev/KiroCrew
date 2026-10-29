"""Which tunnel provider the public composition installs, and the relay client in that slot.

The relay client is off by default, chosen only by ``tunnel.relay_enabled``, never
over an edition's own provider, never under an enterprise build's lock, and never
started past ``tunnel/setup.py``'s ``--no-tunnel`` and token-auth gates.
"""

from __future__ import annotations

import dataclasses
import json
from unittest.mock import MagicMock

import pytest
from relay_client_helpers import OWNER, TOKEN, FakeCrew, FakeRelay, ready_frame

from kiro_crew import edition
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform import (
    PROFILE_ENTERPRISE,
    bootstrap_context,
    build_default_context,
    current_context,
    set_context,
)
from kiro_crew.platform.defaults import DefaultTunnelProvider
from kiro_crew.relay_client.ingress import LoopbackIngress
from kiro_crew.relay_client.protocol import WS_MESSAGE_MAX_BYTES
from kiro_crew.relay_client.provider import (
    RelaySettings,
    RelaySettingsError,
    RelayTunnelProvider,
    gateway_listener,
    public_tunnel_provider,
)
from kiro_crew.relay_client.token_source import HostCredential, StaticHostTokenSource
from kiro_crew.testing.wait import async_wait_until


def _cfg(**tunnel: object) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.tunnel = dataclasses.replace(cfg.tunnel, **tunnel)
    return cfg


class _EditionTunnel(DefaultTunnelProvider):
    """An edition's own tunnel provider."""

    def enabled(self) -> bool:
        return True


class TestSelection:
    def test_off_by_default(self) -> None:
        cfg = KiroCrewConfig()
        assert cfg.tunnel.relay_enabled is False
        assert type(build_default_context(cfg).tunnel) is DefaultTunnelProvider

    def test_relay_enabled_puts_the_relay_client_in_the_slot(self) -> None:
        provider = build_default_context(_cfg(relay_enabled=True)).tunnel
        assert isinstance(provider, RelayTunnelProvider)
        assert provider.enabled() is True
        # Composition starts nothing: no client, no status, no URL.
        assert provider.status_snapshot() is None
        assert provider.public_url() == ""

    def test_a_stand_in_config_never_selects_it(self) -> None:
        assert type(public_tunnel_provider(MagicMock())) is DefaultTunnelProvider

    def test_an_enterprise_build_ignores_the_local_enable(self, monkeypatch) -> None:
        monkeypatch.setattr(edition, "is_enterprise", lambda: True)
        assert type(public_tunnel_provider(_cfg(relay_enabled=True))) is DefaultTunnelProvider

    def test_not_selected_when_an_edition_supplies_its_provider(self, monkeypatch) -> None:
        from kiro_crew.platform import bootstrap as bootstrap_mod

        cfg = _cfg(relay_enabled=True)
        own = _EditionTunnel()
        companion = dataclasses.replace(
            build_default_context(cfg, profile=PROFILE_ENTERPRISE), tunnel=own
        )
        monkeypatch.setenv("KIROCREW_PROFILE", "enterprise")
        monkeypatch.setattr(bootstrap_mod, "plugin_entry_points", lambda: [object()])
        monkeypatch.setattr(
            bootstrap_mod, "discover_companion_context", lambda profile, cfg, **_kw: companion
        )
        ctx = bootstrap_context(cfg)
        assert ctx.tunnel is own
        assert current_context().tunnel is own

    def test_the_relay_enable_is_locked_with_the_tunnel_enable(self) -> None:
        from kiro_crew.edition_locks import CONFIG_PATH_LOCKS

        assert CONFIG_PATH_LOCKS["tunnel.relay_enabled"] == CONFIG_PATH_LOCKS["tunnel.enabled"]


class TestSettings:
    def test_defaults_name_the_provisional_relay(self) -> None:
        settings = RelaySettings.from_config(KiroCrewConfig())
        assert settings.host_url == "wss://kiro-relay.us-east-1.api.aws/v1/host"
        assert settings.crew_domain == "kiro-relay.us-east-1.on.aws"
        assert settings.crew_url_for("k7qm3x2vtzr4hbn6cwapfe5dj3") == (
            "https://k7qm3x2vtzr4hbn6cwapfe5dj3.kiro-relay.us-east-1.on.aws"
        )
        settings.validate()

    def test_the_region_shapes_both_names(self) -> None:
        settings = RelaySettings.from_config(_cfg(relay_region="eu-central-1"))
        assert settings.host_url == "wss://kiro-relay.eu-central-1.api.aws/v1/host"
        assert settings.crew_domain == "kiro-relay.eu-central-1.on.aws"

    def test_explicit_names_win(self) -> None:
        settings = RelaySettings.from_config(
            _cfg(
                relay_host_url="wss://relay.beta.example/v1/host",
                relay_crew_domain="crews.beta.example",
            )
        )
        assert settings.host_url == "wss://relay.beta.example/v1/host"
        assert settings.crew_url_for("e" * 20) == f"https://{'e' * 20}.crews.beta.example"

    @pytest.mark.parametrize(
        "tunnel",
        [
            {"relay_host_url": "ws://relay.example/v1/host"},
            {"relay_host_url": "https://relay.example/v1/host"},
            {"relay_region": "US East"},
            {"relay_crew_domain": "bad domain"},
        ],
        ids=["clear-text-remote", "not-websocket", "bad-region", "bad-domain"],
    )
    def test_unusable_settings_are_refused(self, tunnel: dict) -> None:
        with pytest.raises(RelaySettingsError):
            RelaySettings.from_config(_cfg(**tunnel)).validate()

    def test_a_loopback_test_relay_may_use_ws(self) -> None:
        RelaySettings.from_config(_cfg(relay_host_url="ws://127.0.0.1:9/v1/host")).validate()

    def test_config_json_round_trip(self, tmp_path, monkeypatch) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({"tunnel": {"relay_enabled": True, "relay_region": "eu-central-1"}}),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: config_file)
        cfg = KiroCrewConfig.load()
        assert cfg.tunnel.relay_enabled is True
        assert cfg.tunnel.relay_region == "eu-central-1"
        assert cfg.tunnel.relay_host_url == ""

    def test_a_string_true_does_not_enable_it(self, tmp_path, monkeypatch) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": "yes"}}), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: config_file)
        assert KiroCrewConfig.load().tunnel.relay_enabled is False

    def test_the_move_grace_defaults_to_one_second(self) -> None:
        assert KiroCrewConfig().tunnel.relay_move_grace_seconds == 1.0
        assert RelaySettings.from_config(KiroCrewConfig()).move_grace == 1.0

    @pytest.mark.parametrize(
        ("stored", "grace"),
        [
            (2.5, 2.5),
            (0, 0.0),
            (-1, 0.0),
            (120, 30.0),
            ("2", 2.0),
            ("soon", 1.0),
            (True, 1.0),
            (None, 1.0),
        ],
        ids=[
            "number",
            "zero",
            "negative",
            "past-the-window",
            "numeric-string",
            "string",
            "boolean",
            "null",
        ],
    )
    def test_the_move_grace_reads_only_a_number_up_to_the_move_window(
        self, tmp_path, monkeypatch, stored, grace
    ) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({"tunnel": {"relay_move_grace_seconds": stored}}), encoding="utf-8"
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: config_file)
        cfg = KiroCrewConfig.load()
        assert cfg.tunnel.relay_move_grace_seconds == grace
        assert RelaySettings.from_config(cfg).move_grace == grace

    @pytest.mark.asyncio
    async def test_the_provider_hands_the_move_grace_to_the_client(self) -> None:
        made: list = []

        def factory(**kwargs: object) -> object:
            made.append(kwargs)
            raise RuntimeError("stop here")

        settings = RelaySettings.from_config(_cfg(relay_enabled=True, relay_move_grace_seconds=4.0))
        provider = RelayTunnelProvider(
            settings, ingress_factory=MagicMock, client_factory=factory  # type: ignore[arg-type]
        )
        with pytest.raises(RuntimeError, match="stop here"):
            await provider.start()
        assert made[0]["timings"].move_grace == 4.0

    def test_the_gateway_listener_comes_from_the_bound_port(self, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "6123")
        monkeypatch.delenv("KIROCREW_BIND", raising=False)
        assert gateway_listener() == ("127.0.0.1", 6123)
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        assert gateway_listener() == ("127.0.0.1", 6123)
        monkeypatch.setenv("KIROCREW_BIND", "::")
        assert gateway_listener() == ("::1", 6123)
        # The relay ingress honours a hand-off over loopback only.
        monkeypatch.setenv("KIROCREW_BIND", "10.0.0.5")
        with pytest.raises(RelaySettingsError):
            gateway_listener()
        monkeypatch.delenv("KIROCREW_BIND")
        monkeypatch.delenv("KIROCREW_BOUND_PORT")
        with pytest.raises(RelaySettingsError):
            gateway_listener()

    @pytest.mark.parametrize(
        ("bind", "host"),
        [
            ("127.0.0.2", "127.0.0.2"),
            ("0:0:0:0:0:0:0:1", "::1"),
            ("[::1]", "::1"),
            ("::", "::1"),
            ("0.0.0.0", "127.0.0.1"),
            ("localhost", "127.0.0.1"),
            ("0:0:0:0:0:0:0:0", "::1"),
            ("[::]", "::1"),
            ("", "127.0.0.1"),
            ("*", "127.0.0.1"),
        ],
    )
    def test_the_gateway_listener_uses_address_identity(self, monkeypatch, bind, host) -> None:
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "6123")
        monkeypatch.setenv("KIROCREW_BIND", bind)
        assert gateway_listener() == (host, 6123)

    @pytest.mark.parametrize(
        "bind",
        ["10.0.0.5", "my-host", "::ffff:127.0.0.1", "127.0.0.1.example", "[::1"],
    )
    def test_the_gateway_listener_refuses_non_loopback_binds(self, monkeypatch, bind) -> None:
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "6123")
        monkeypatch.setenv("KIROCREW_BIND", bind)
        with pytest.raises(RelaySettingsError, match="KIROCREW_BIND"):
            gateway_listener()


def _provider(relay: FakeRelay, crew: FakeCrew) -> RelayTunnelProvider:
    settings = RelaySettings.from_config(_cfg(relay_enabled=True, relay_host_url=relay.url))
    credential = HostCredential(TOKEN, crew_kind="personal", owner_user_id=OWNER)
    return RelayTunnelProvider(
        settings,
        token_source=StaticHostTokenSource(credential),
        ingress_factory=lambda: LoopbackIngress(
            "127.0.0.1", crew.port, gateway=crew.gateway, max_ws_message_bytes=WS_MESSAGE_MAX_BYTES
        ),
        client_version="0.0.0-test",
    )


def _token_auth() -> MagicMock:
    middleware = MagicMock()
    middleware._is_token_auth = True
    return middleware


@pytest.mark.asyncio
class TestThroughTheTunnelWrapper:
    async def test_connect_adds_the_crew_url_to_the_allowed_origins(self, monkeypatch) -> None:
        from kiro_crew import tunnel as tunnel_pkg
        from kiro_crew.tunnel.manager import TunnelState
        from kiro_crew.tunnel.setup import setup_tunnel

        monkeypatch.setattr(tunnel_pkg, "_tunnel_public_url", "")
        async with FakeRelay() as relay, FakeCrew() as crew:
            provider = _provider(relay, crew)
            set_context(
                dataclasses.replace(build_default_context(KiroCrewConfig()), tunnel=provider)
            )
            origins: set = set()
            manager = await setup_tunnel(
                middlewares=[_token_auth()],
                allowed_origins=origins,
                tunnel_name_mode="username",
                tunnel_name_override="",
                port=crew.port,
                log_api_access=MagicMock(),
            )
            assert manager is not None
            try:
                leg = await relay.next_leg()
                await leg.handshake()
                url = f"https://{ready_frame()['endpointId']}.kiro-relay.us-east-1.on.aws"
                await async_wait_until(lambda: url in origins)
                assert manager.state is TunnelState.CONNECTED
                assert manager.public_url == url
                assert tunnel_pkg.get_tunnel_url() == url
                assert await provider.ensure_available() == "connected"
            finally:
                await manager.stop()
            assert url not in origins
            assert tunnel_pkg.get_tunnel_url() == ""
            assert await leg.wait_closed() == 1001

    async def test_no_token_auth_never_starts_it(self) -> None:
        from kiro_crew.tunnel.setup import setup_tunnel

        async with FakeRelay() as relay, FakeCrew() as crew:
            provider = _provider(relay, crew)
            set_context(
                dataclasses.replace(build_default_context(KiroCrewConfig()), tunnel=provider)
            )
            manager = await setup_tunnel(
                middlewares=[],
                allowed_origins=set(),
                tunnel_name_mode="username",
                tunnel_name_override="",
                port=crew.port,
                log_api_access=MagicMock(),
            )
            assert manager is None
            assert provider.status_snapshot() is None
            assert relay.dials == 0

    async def test_the_no_tunnel_boot_flag_never_starts_it(self, monkeypatch) -> None:
        from kiro_crew import tunnel as tunnel_pkg
        from kiro_crew.tunnel.setup import setup_tunnel

        monkeypatch.setattr(tunnel_pkg, "_publish_disabled", True)
        async with FakeRelay() as relay, FakeCrew() as crew:
            provider = _provider(relay, crew)
            set_context(
                dataclasses.replace(build_default_context(KiroCrewConfig()), tunnel=provider)
            )
            manager = await setup_tunnel(
                middlewares=[_token_auth()],
                allowed_origins=set(),
                tunnel_name_mode="username",
                tunnel_name_override="",
                port=crew.port,
                log_api_access=MagicMock(),
            )
            assert manager is None
            assert relay.dials == 0

    async def test_the_on_demand_link_path_never_starts_it(self) -> None:
        async with FakeRelay() as relay, FakeCrew() as crew:
            provider = _provider(relay, crew)
            assert await provider.ensure_available() == "disabled"
            assert provider.status_snapshot() is None
            assert relay.dials == 0

    async def test_without_a_token_source_it_reports_why(self, monkeypatch) -> None:
        from kiro_crew.dashboard.kiro_relay.ingress import RelayIngress
        from kiro_crew.relay_client import provider as provider_mod

        monkeypatch.setenv("KIROCREW_BOUND_PORT", "6123")
        monkeypatch.delenv("KIROCREW_BIND", raising=False)
        monkeypatch.setattr(provider_mod, "_gateway_ingress", RelayIngress(enabled=True))
        provider = RelayTunnelProvider(RelaySettings.from_config(_cfg(relay_enabled=True)))
        await provider.start()
        try:
            await async_wait_until(
                lambda: (provider.status_snapshot() or {}).get("state") == "error"
            )
            assert "host token" in provider.status_snapshot()["error"]
            assert await provider.ensure_available() == "unavailable"
        finally:
            await provider.stop()

    async def test_unusable_settings_report_an_error_and_start_nothing(self) -> None:
        provider = RelayTunnelProvider(
            RelaySettings.from_config(
                _cfg(relay_enabled=True, relay_host_url="ws://relay.example/v1")
            )
        )
        await provider.start()
        snapshot = provider.status_snapshot()
        assert snapshot is not None and snapshot["state"] == "error"
        assert "relay_host_url" in snapshot["error"]
        await provider.stop()

    async def test_without_the_dashboards_relay_ingress_it_starts_nothing(
        self, monkeypatch
    ) -> None:
        from kiro_crew.relay_client import provider as provider_mod

        monkeypatch.setenv("KIROCREW_BOUND_PORT", "6123")
        monkeypatch.setattr(provider_mod, "_gateway_ingress", None)
        provider = RelayTunnelProvider(RelaySettings.from_config(_cfg(relay_enabled=True)))
        await provider.start()
        snapshot = provider.status_snapshot()
        assert snapshot is not None and snapshot["state"] == "error"
        assert "relay ingress" in snapshot["error"]
        await provider.stop()

    async def test_the_gateway_publishes_its_relay_ingress_before_the_tunnel(
        self, monkeypatch
    ) -> None:
        from kiro_crew.dashboard import server
        from kiro_crew.dashboard.kiro_relay.ingress import RELAY_INGRESS_APP_KEY, RelayIngress
        from kiro_crew.relay_client import provider as provider_mod

        monkeypatch.setattr(provider_mod, "_gateway_ingress", None)
        gateway = RelayIngress(enabled=True)
        app = {RELAY_INGRESS_APP_KEY: gateway}
        cfg = _cfg(relay_enabled=True)
        set_context(dataclasses.replace(build_default_context(cfg), tunnel=DefaultTunnelProvider()))
        # Relay selected but a no-op provider: the only effect is the publication.
        await server._start_aea_tunnel(app, MagicMock(), cfg, 0)
        assert provider_mod.gateway_ingress() is gateway

    @pytest.mark.parametrize("relay_enabled", [False, None, 1, "true"])
    async def test_gateway_with_relay_off_never_imports_provider(
        self, monkeypatch, relay_enabled
    ) -> None:
        import sys

        from kiro_crew.dashboard import server

        cfg = _cfg(relay_enabled=relay_enabled)
        set_context(build_default_context(cfg))
        monkeypatch.setitem(sys.modules, "kiro_crew.relay_client.provider", None)
        await server._start_aea_tunnel({}, MagicMock(), cfg, 0)


def test_composing_with_the_relay_off_never_imports_the_relay_client(monkeypatch) -> None:
    import sys

    # An entry of None makes any import of the module raise ImportError.
    monkeypatch.setitem(sys.modules, "kiro_crew.relay_client.provider", None)
    assert type(build_default_context(KiroCrewConfig()).tunnel) is DefaultTunnelProvider
    with pytest.raises(ImportError):
        build_default_context(_cfg(relay_enabled=True))


def _lock_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lock the ``tunnel`` control as an enterprise build does, and nothing else.

    Every reader of the relay switch asks ``edition_locks.local_enable_locked`` at
    call time, so this stands in for the edition without its policy or login boot.
    """
    from kiro_crew import edition_locks

    monkeypatch.setattr(edition_locks, "local_enable_locked", lambda control: control == "tunnel")


class TestTheRelaySwitch:
    """``tunnel.relay_enabled``: the one setting that turns the relay path on."""

    def test_only_the_json_boolean_true_turns_it_on(self) -> None:
        from kiro_crew.config.section_builders import _build_tunnel_config

        assert _build_tunnel_config({}).relay_enabled is False
        for hand_edit in ("true", 1, "yes", None):
            assert _build_tunnel_config({"relay_enabled": hand_edit}).relay_enabled is False
        assert _build_tunnel_config({"relay_enabled": True}).relay_enabled is True

    def test_an_enterprise_build_locks_the_switch(self, monkeypatch) -> None:
        from kiro_crew import edition_locks

        edition_locks.reset_logged()
        monkeypatch.setattr(edition, "EDITION", "enterprise")
        try:
            assert edition_locks.config_path_locked("tunnel.relay_enabled")
            assert edition_locks.local_enable_locked("tunnel")
            assert type(public_tunnel_provider(_cfg(relay_enabled=True))) is DefaultTunnelProvider
        finally:
            edition_locks.reset_logged()

    def test_a_locked_build_composes_without_importing_the_relay_client(self, monkeypatch) -> None:
        import sys

        _lock_tunnel(monkeypatch)
        monkeypatch.setitem(sys.modules, "kiro_crew.relay_client.provider", None)
        tunnel = build_default_context(_cfg(relay_enabled=True)).tunnel
        assert type(tunnel) is DefaultTunnelProvider

    @pytest.mark.asyncio
    async def test_a_locked_gateway_never_imports_the_relay_client(self, monkeypatch) -> None:
        import sys

        from kiro_crew.dashboard import server

        cfg = _cfg(relay_enabled=True)
        set_context(build_default_context(KiroCrewConfig()))
        _lock_tunnel(monkeypatch)
        monkeypatch.setitem(sys.modules, "kiro_crew.relay_client.provider", None)
        await server._start_aea_tunnel({}, MagicMock(), cfg, 0)
