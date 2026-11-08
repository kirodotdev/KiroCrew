"""Which tunnel provider the public composition installs, and the relay client in that slot.

The relay client is off by default, chosen only by ``tunnel.relay_enabled``, never
over an edition's own provider, never under an LTS build's lock, and never
started past ``tunnel/setup.py``'s ``--no-tunnel`` and token-auth gates.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from relay_client_helpers import (
    OWNER,
    TOKEN,
    FakeCrew,
    FakeRelay,
    StaticHostTokenSource,
    ready_frame,
)

from kiro_crew import edition
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform import (
    PROFILE_ENTERPRISE,
    bootstrap,
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
from kiro_crew.relay_client.token_source import HostCredential
from kiro_crew.testing.wait import async_wait_until, until_parked


def _cfg(**tunnel: object) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.tunnel = dataclasses.replace(cfg.tunnel, **tunnel)
    return cfg


#: Reserved example names for a relay with no lease service, which must name both.
_NAMED = {
    "host_url": "wss://host-us-east-1.relay.example/v1/host",
    "crew_domain": "us-east-1.relay.example",
}


def _named(**settings: Any) -> RelaySettings:
    """Settings as an edition with its own host token source builds them."""
    return RelaySettings(**{"enabled": True, **_NAMED, **settings})


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
        assert type(bootstrap._public_tunnel_provider(MagicMock())) is DefaultTunnelProvider

    def test_an_lts_build_ignores_the_local_enable(self, monkeypatch) -> None:
        monkeypatch.setattr(edition, "is_lts", lambda: True)
        tunnel = bootstrap._public_tunnel_provider(_cfg(relay_enabled=True))
        assert type(tunnel) is DefaultTunnelProvider

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
    def test_no_relay_name_is_built_in(self) -> None:
        # Empty stays empty: the names come from the lease answer or an edition.
        settings = RelaySettings.from_config(_cfg())
        assert settings.host_url == "" and settings.crew_domain == ""
        with pytest.raises(RelaySettingsError, match="relay Crew domain"):
            settings.crew_url_for("k7qm3x2vtzr4hbn6cwapfe5dj3")

    def test_with_a_lease_service_the_names_may_stay_empty(self) -> None:
        RelaySettings.from_config(
            _cfg(relay_lease_url="https://lease.example", relay_lease_credential="/abs/cred")
        ).validate()

    @pytest.mark.parametrize(
        ("names", "missing"),
        [
            ({"host_url": "", "crew_domain": ""}, "relay host URL"),
            ({"crew_domain": ""}, "relay Crew domain"),
            ({"host_url": ""}, "relay host URL"),
        ],
        ids=["neither", "no-domain", "no-host-url"],
    )
    def test_without_a_lease_service_an_empty_name_is_refused_by_name(
        self, names: dict, missing: str
    ) -> None:
        with pytest.raises(RelaySettingsError, match=missing):
            _named(**names).validate()

    def test_explicit_names_win(self) -> None:
        settings = _named(
            host_url="wss://relay.beta.example/v1/host", crew_domain="crews.beta.example"
        )
        settings.validate()
        assert settings.host_url == "wss://relay.beta.example/v1/host"
        assert settings.crew_url_for("e" * 20) == f"https://{'e' * 20}.crews.beta.example"

    @pytest.mark.parametrize(
        "names",
        [
            {"host_url": "ws://relay.example/v1/host"},
            {"host_url": "https://relay.example/v1/host"},
            {"crew_domain": "bad domain"},
        ],
        ids=["clear-text-remote", "not-websocket", "bad-domain"],
    )
    def test_unusable_settings_are_refused(self, names: dict) -> None:
        with pytest.raises(RelaySettingsError):
            _named(**names).validate()

    def test_a_loopback_test_relay_may_use_ws(self) -> None:
        _named(host_url="ws://127.0.0.1:9/v1/host", crew_domain="relay.example").validate()

    @pytest.mark.parametrize(
        "old",
        [
            {"relay_region": "eu-central-1"},
            {"relay_host_url": "wss://host-us-east-1.relay.example/v1/host"},
            {"relay_crew_domain": "us-east-1.relay.example"},
            {"relay_move_grace_seconds": 4.0},
        ],
        ids=["region", "host-url", "crew-domain", "move-grace"],
    )
    def test_an_old_config_naming_a_removed_key_loads_and_keeps_it(
        self, tmp_path, monkeypatch, caplog, old: dict
    ) -> None:
        # None of these keys ever shipped in a release. A file that still names
        # one loads, keeps it on save, logs no warning, and the key has no effect.
        lease = {"relay_lease_url": "https://lease.example", "relay_lease_credential": "/abs/cred"}
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({"tunnel": {"relay_enabled": True, **lease, **old}}), encoding="utf-8"
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: config_file)
        with caplog.at_level("WARNING", logger="kiro_crew"):
            cfg = KiroCrewConfig.load()
        (key,) = old
        assert not hasattr(cfg.tunnel, key)
        settings = RelaySettings.from_config(cfg)
        settings.validate()
        assert (settings.host_url, settings.crew_domain) == ("", "")
        assert [r.getMessage() for r in caplog.records if key in r.getMessage()] == []
        cfg.save()
        assert json.loads(config_file.read_text(encoding="utf-8"))["tunnel"][key] == old[key]

    def test_a_string_true_does_not_enable_it(self, tmp_path, monkeypatch) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"tunnel": {"relay_enabled": "yes"}}), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: config_file)
        assert KiroCrewConfig.load().tunnel.relay_enabled is False

    def test_the_lease_credential_setting_is_masked_as_sensitive(self) -> None:
        # The setting names where the credential is, but a credential pasted in by
        # mistake must not show in the settings API or UI.
        from kiro_crew.config.integration_sections import TunnelConfig

        (spec,) = [
            f for f in dataclasses.fields(TunnelConfig) if f.name == "relay_lease_credential"
        ]
        assert spec.metadata.get("sensitive") is True

    def test_the_lease_token_type_setting_reaches_the_lease_client(
        self, tmp_path, monkeypatch
    ) -> None:
        config_file = tmp_path / "config.json"
        tunnel = {
            "relay_enabled": True,
            "relay_lease_url": "https://lease.example.com",
            "relay_lease_credential": "secret://lease",
            "relay_lease_token_type": "API_KEY",
        }
        config_file.write_text(json.dumps({"tunnel": tunnel}), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: config_file)
        provider = public_tunnel_provider(KiroCrewConfig.load())
        assert provider._tokens._token_type == "API_KEY"

    @pytest.mark.parametrize(
        "token_type, team",
        [("API_KEY", True), ("KIRO_MACHINE_TOKEN", False), ("", False), ("EXTERNAL_IDP", False)],
    )
    def test_only_a_crew_api_key_makes_a_team_crew(self, token_type, team) -> None:
        settings = RelaySettings.from_config(_cfg(relay_lease_token_type=token_type))
        assert settings.is_team_crew is team

    @pytest.mark.asyncio
    async def test_a_cancelled_stop_still_closes_the_token_source_and_the_ingress(self) -> None:
        # The gateway bounds the tunnel stop and cancels it past the bound. The
        # lease renewals and the hand-off sessions must still end.
        released = asyncio.Event()
        closed: list[str] = []

        class _StuckClient:
            running = True

            def register_callbacks(self, **_: object) -> None:
                return None

            async def start(self) -> None:
                return None

            async def stop(self) -> None:
                await released.wait()

        class _Tokens(StaticHostTokenSource):
            async def aclose(self) -> None:
                closed.append("token source")

        class _Ingress:
            async def aclose(self) -> None:
                closed.append("ingress")

        settings = _named(host_url="ws://127.0.0.1:9/v1/host")
        provider = RelayTunnelProvider(
            settings,
            token_source=_Tokens(HostCredential(TOKEN, crew_kind="personal", owner_user_id=OWNER)),
            ingress_factory=_Ingress,  # type: ignore[arg-type]
            client_factory=lambda **_: _StuckClient(),  # type: ignore[arg-type,return-value]
            client_version="0.0.0-test",
        )
        await provider.start()
        stopping = asyncio.ensure_future(provider.stop())
        await until_parked(released)
        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)
        assert closed == ["token source", "ingress"]

    def test_the_gateway_listener_comes_from_the_bound_port(self, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "6123")
        monkeypatch.delenv("KIROCREW_BOUND_HOST", raising=False)
        assert gateway_listener() == ("127.0.0.1", 6123)
        monkeypatch.setenv("KIROCREW_BOUND_HOST", "::1")
        assert gateway_listener() == ("::1", 6123)
        # The relay ingress honours a hand-off over loopback only.
        monkeypatch.setenv("KIROCREW_BOUND_HOST", "10.0.0.5")
        with pytest.raises(RelaySettingsError):
            gateway_listener()
        monkeypatch.delenv("KIROCREW_BOUND_HOST")
        monkeypatch.delenv("KIROCREW_BOUND_PORT")
        with pytest.raises(RelaySettingsError):
            gateway_listener()

    @pytest.mark.parametrize("asked", ["::1", "10.0.0.5", "0.0.0.0", ""])
    def test_the_gateway_listener_is_the_address_actually_bound_not_the_one_asked(
        self, monkeypatch, asked
    ) -> None:
        # The bind policy can refuse KIROCREW_BIND and bind 127.0.0.1 instead. The
        # relay hands requests, and the handover credential, to the socket that
        # was bound, never to the address that was asked for.
        import socket

        from kiro_crew.dashboard.server_runtime.listener import _export_reserved_bind_evidence

        monkeypatch.setenv("KIROCREW_BIND", asked)
        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
        monkeypatch.delenv("KIROCREW_BOUND_HOST", raising=False)
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            _export_reserved_bind_evidence(bound)
            assert gateway_listener() == ("127.0.0.1", bound.getsockname()[1])

    @pytest.mark.parametrize(
        ("exported", "host"),
        [
            ("127.0.0.2", "127.0.0.2"),
            ("0:0:0:0:0:0:0:1", "::1"),
            ("::1", "::1"),
            ("", "127.0.0.1"),
        ],
    )
    def test_the_gateway_listener_uses_address_identity(self, monkeypatch, exported, host) -> None:
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "6123")
        monkeypatch.setenv("KIROCREW_BOUND_HOST", exported)
        assert gateway_listener() == (host, 6123)

    @pytest.mark.parametrize(
        "exported",
        ["10.0.0.5", "my-host", "::ffff:127.0.0.1", "127.0.0.1.example", "[::1"],
    )
    def test_the_gateway_listener_refuses_non_loopback_binds(self, monkeypatch, exported) -> None:
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "6123")
        monkeypatch.setenv("KIROCREW_BOUND_HOST", exported)
        with pytest.raises(RelaySettingsError, match="the gateway is bound to"):
            gateway_listener()


def _provider(relay: FakeRelay, crew: FakeCrew) -> RelayTunnelProvider:
    settings = _named(host_url=relay.url)
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
                url = f"https://{ready_frame()['endpointId']}.us-east-1.relay.example"
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
        monkeypatch.delenv("KIROCREW_BOUND_HOST", raising=False)
        monkeypatch.setattr(provider_mod, "_gateway_ingress", RelayIngress(enabled=True))
        provider = RelayTunnelProvider(_named())
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
        provider = RelayTunnelProvider(_named(host_url="ws://relay.example/v1"))
        await provider.start()
        snapshot = provider.status_snapshot()
        assert snapshot is not None and snapshot["state"] == "error"
        assert "relay host URL" in snapshot["error"]
        await provider.stop()

    @pytest.mark.parametrize(
        ("tunnel", "names"),
        [
            ({}, "relay host URL"),
            ({"relay_lease_url": "https://lease.example"}, "tunnel.relay_lease_credential"),
        ],
        ids=["no-lease", "lease-without-credential"],
    )
    async def test_the_relay_enable_help_matches_a_start_without_a_lease(
        self, tunnel: dict, names: str
    ) -> None:
        """Measured against ``start()``: no client is built, the status names the setting."""
        from kiro_crew.config.schema import SCHEMA_REGISTRY

        built: list[dict] = []
        provider = RelayTunnelProvider(
            RelaySettings.from_config(_cfg(relay_enabled=True, **tunnel)),
            ingress_factory=MagicMock,
            client_factory=lambda **kw: built.append(kw),  # type: ignore[arg-type,return-value]
        )
        await provider.start()
        snapshot = provider.status_snapshot()
        assert built == []
        assert snapshot is not None and snapshot["state"] == "error"
        assert names in snapshot["error"]
        await provider.stop()

        (help_text,) = [e.help for e in SCHEMA_REGISTRY if e.path == "tunnel.relay_enabled"]
        assert "no relay client starts" in help_text
        assert "names the missing setting" in help_text
        assert "starts a relay client that reports it cannot connect" not in help_text
        assert "Relay Host URL" not in help_text

    async def test_without_the_dashboards_relay_ingress_it_starts_nothing(
        self, monkeypatch
    ) -> None:
        from kiro_crew.relay_client import provider as provider_mod

        monkeypatch.setenv("KIROCREW_BOUND_PORT", "6123")
        monkeypatch.setattr(provider_mod, "_gateway_ingress", None)
        provider = RelayTunnelProvider(_named())
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
    """Lock the ``tunnel`` control as an LTS build does, and nothing else.

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

    def test_one_predicate_reads_the_switch(self, monkeypatch) -> None:
        from kiro_crew.edition_locks import relay_selected

        assert relay_selected(_cfg(relay_enabled=True)) is True
        assert relay_selected(_cfg()) is False
        # A stand-in config answers every attribute truthily, and a config with no
        # tunnel section has no switch at all.
        assert relay_selected(MagicMock()) is False
        assert relay_selected(object()) is False
        _lock_tunnel(monkeypatch)
        assert relay_selected(_cfg(relay_enabled=True)) is False

    def test_an_lts_build_locks_the_switch(self, monkeypatch) -> None:
        from kiro_crew import edition_locks

        edition_locks.reset_logged()
        monkeypatch.setattr(edition, "EDITION", "lts")
        try:
            assert edition_locks.config_path_locked("tunnel.relay_enabled")
            assert edition_locks.local_enable_locked("tunnel")
            tunnel = bootstrap._public_tunnel_provider(_cfg(relay_enabled=True))
            assert type(tunnel) is DefaultTunnelProvider
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
