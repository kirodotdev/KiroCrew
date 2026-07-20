"""Tests for the AEA Tunnel manager."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.tunnel.manager import TunnelManager, TunnelState, TunnelStatus


@pytest.fixture
def manager():
    """Create a TunnelManager with test defaults."""
    return TunnelManager(port=5476, name_mode="username", name_override=None)


class TestTunnelName:
    def test_username_mode(self, manager: TunnelManager):
        assert manager._tunnel_name() == "kirocrew"

    def test_hash_mode(self):
        mgr = TunnelManager(port=5476, name_mode="hash")
        name = mgr._tunnel_name()
        assert name.startswith("kirocrew-")
        assert len(name) == len("kirocrew-") + 8  # 8-char hash

    def test_override(self):
        mgr = TunnelManager(port=5476, name_override="my-custom-tunnel")
        assert mgr._tunnel_name() == "my-custom-tunnel"


class TestStateTransitions:
    @pytest.mark.asyncio
    async def test_start_is_noop_disabled_in_oss(self, manager: TunnelManager):
        """Stub: the tunnel feature is not available in OSS, so start() leaves
        the tunnel disabled rather than spawning a managed tunnel."""
        await manager.start()
        assert manager.state == TunnelState.DISABLED
        assert manager.status.error == "not available in OSS"

    @pytest.mark.asyncio
    async def test_stop_sets_stopped(self, manager: TunnelManager):
        manager._status.state = TunnelState.CONNECTED
        manager._status.url = "https://test.tunnels.corp.amazon.com"
        await manager.stop()
        assert manager.state == TunnelState.STOPPED
        assert manager.public_url == ""

    @pytest.mark.asyncio
    async def test_stop_does_not_call_disconnect(self, manager: TunnelManager):
        """Stub: stop() is a no-op teardown and never invokes _on_disconnect."""
        disconnect_cb = AsyncMock()
        manager._on_disconnect = disconnect_cb
        manager._status.state = TunnelState.CONNECTED
        await manager.stop()
        disconnect_cb.assert_not_called()


class TestTunnelStatusEndpoint:
    @pytest.mark.asyncio
    async def test_disabled_when_no_manager(self):
        from kiro_crew.dashboard.handlers.tunnel import api_tunnel_status

        state = MagicMock()
        state.tunnel_manager = None
        request = MagicMock()
        request.app = {"state": state}
        resp = await api_tunnel_status(request)
        import json

        data = json.loads(resp.body)
        assert data["state"] == "disabled"

    @pytest.mark.asyncio
    async def test_returns_connected_state(self):
        from kiro_crew.dashboard.handlers.tunnel import api_tunnel_status

        status = TunnelStatus(
            state=TunnelState.CONNECTED,
            url="https://test.tunnels.dev",
            connected_at=1000.0,
        )
        mgr = MagicMock()
        mgr.status = status
        state = MagicMock()
        state.tunnel_manager = mgr
        request = MagicMock()
        request.app = {"state": state}
        with patch("time.time", return_value=1060.0):
            resp = await api_tunnel_status(request)
        import json

        data = json.loads(resp.body)
        assert data["state"] == "connected"
        assert data["url"] == "https://test.tunnels.dev"
        assert data["uptime"] == 60


class TestPresignedLinkIntegration:
    def test_set_tunnel_url(self):
        from kiro_crew.tunnel import get_tunnel_url, set_tunnel_url

        set_tunnel_url("https://kirocrew-gsanc.tunnels.corp.amazon.com")
        assert get_tunnel_url() == "https://kirocrew-gsanc.tunnels.corp.amazon.com"

        set_tunnel_url("")
        assert get_tunnel_url() == ""


class TestConfigIntegration:
    def test_tunnel_config_defaults(self):
        from kiro_crew.config.loader import TunnelConfig

        cfg = TunnelConfig()
        assert cfg.enabled is False
        assert cfg.name_mode == "username"
        assert cfg.name_override == ""

    def test_tunnel_config_loads_from_json(self, tmp_path):
        """TunnelConfig is properly deserialized from config JSON."""
        import json

        from kiro_crew.config.loader import KiroCrewConfig

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"tunnel": {"enabled": True, "name_mode": "hash"}}))
        with patch("kiro_crew.config.loader.config_path", return_value=config_file):
            cfg = KiroCrewConfig.load()
        assert cfg.tunnel.enabled is True
        assert cfg.tunnel.name_mode == "hash"
        assert cfg.tunnel.name_override == ""

    def test_tunnel_config_missing_section_uses_defaults(self, tmp_path):
        """Missing tunnel section uses defaults."""
        import json

        from kiro_crew.config.loader import KiroCrewConfig

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"agent": {"model": "auto"}}))
        with patch("kiro_crew.config.loader.config_path", return_value=config_file):
            cfg = KiroCrewConfig.load()
        assert cfg.tunnel.enabled is False


class TestSetupTunnel:
    """Tests for tunnel.setup.setup_tunnel — no dashboard imports needed."""

    @pytest.mark.asyncio
    async def test_denied_without_token_auth(self):
        """Refuses to start tunnel when token auth middleware is missing."""
        from kiro_crew.tunnel.setup import setup_tunnel

        mock_log = MagicMock()
        result = await setup_tunnel(
            middlewares=[],  # No token auth
            allowed_origins=set(),
            tunnel_name_mode="username",
            tunnel_name_override="",
            port=5476,
            log_api_access=mock_log,
        )
        assert result is None
        mock_log.assert_called_once()

    @pytest.mark.asyncio
    async def test_starts_when_token_auth_present(self):
        """Starts tunnel when token auth middleware is active."""
        from kiro_crew.tunnel.setup import setup_tunnel

        mw = MagicMock()
        mw._is_token_auth = True

        with patch("kiro_crew.tunnel.setup.TunnelManager") as mock_tm:
            mock_mgr = AsyncMock()
            mock_tm.return_value = mock_mgr
            result = await setup_tunnel(
                middlewares=[mw],
                allowed_origins=set(),
                tunnel_name_mode="username",
                tunnel_name_override="",
                port=5476,
                log_api_access=MagicMock(),
            )

        assert result is mock_mgr
        mock_mgr.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_callback_adds_origin(self):
        """Connect callback adds URL to CORS origins and sets tunnel URL."""
        from kiro_crew.tunnel import get_tunnel_url, set_tunnel_url
        from kiro_crew.tunnel.setup import setup_tunnel

        set_tunnel_url("")
        allowed_origins: set = set()
        captured_on_connect = None

        def capture_tm(*args, **kwargs):
            nonlocal captured_on_connect
            captured_on_connect = kwargs.get("on_connect")
            mgr = AsyncMock()
            return mgr

        mw = MagicMock()
        mw._is_token_auth = True

        with patch("kiro_crew.tunnel.setup.TunnelManager", side_effect=capture_tm):
            await setup_tunnel(
                middlewares=[mw],
                allowed_origins=allowed_origins,
                tunnel_name_mode="username",
                tunnel_name_override="",
                port=5476,
                log_api_access=MagicMock(),
            )

        await captured_on_connect("https://gsanc-kirocrew.tunnels.dev")
        assert "https://gsanc-kirocrew.tunnels.dev" in allowed_origins
        assert get_tunnel_url() == "https://gsanc-kirocrew.tunnels.dev"
        set_tunnel_url("")

    @pytest.mark.asyncio
    async def test_disconnect_callback_removes_origin(self):
        """Disconnect callback removes URL from CORS origins."""
        from kiro_crew.tunnel import get_tunnel_url, set_tunnel_url
        from kiro_crew.tunnel.setup import setup_tunnel

        set_tunnel_url("")
        allowed_origins: set = set()
        captured_connect = None
        captured_disconnect = None

        def capture_tm(*args, **kwargs):
            nonlocal captured_connect, captured_disconnect
            captured_connect = kwargs.get("on_connect")
            captured_disconnect = kwargs.get("on_disconnect")
            mgr = AsyncMock()
            return mgr

        mw = MagicMock()
        mw._is_token_auth = True

        with patch("kiro_crew.tunnel.setup.TunnelManager", side_effect=capture_tm):
            await setup_tunnel(
                middlewares=[mw],
                allowed_origins=allowed_origins,
                tunnel_name_mode="username",
                tunnel_name_override="",
                port=5476,
                log_api_access=MagicMock(),
            )

        await captured_connect("https://test.tunnels.dev")
        assert "https://test.tunnels.dev" in allowed_origins

        await captured_disconnect()
        assert "https://test.tunnels.dev" not in allowed_origins
        assert get_tunnel_url() == ""


class TestStartLogsDisabledNotice:
    @pytest.mark.asyncio
    async def test_start_logs_oss_disabled_notice(self, manager: TunnelManager):
        """Stub: start() logs that the tunnel feature is unavailable in OSS."""
        with patch("kiro_crew.tunnel.manager.logger") as mock_log:
            await manager.start()
        mock_log.info.assert_called()
        assert manager._status.started_at > 0


class TestTunnelStatusEndpointDisabledField:
    @pytest.mark.asyncio
    async def test_disabled_response_has_reconnect_attempt(self):
        """Disabled response includes reconnect_attempt field."""
        from kiro_crew.dashboard.handlers.tunnel import api_tunnel_status

        state = MagicMock()
        state.tunnel_manager = None
        request = MagicMock()
        request.app = {"state": state}
        resp = await api_tunnel_status(request)
        import json

        data = json.loads(resp.body)
        assert data["reconnect_attempt"] == 0


class TestAllowlistTunnelBranch:
    def test_send_dashboard_link_uses_tunnel_url(self):
        """When tunnel URL is set, presigned link uses it."""
        from kiro_crew.tunnel import get_tunnel_url, set_tunnel_url

        set_tunnel_url("https://gsanc-kirocrew.tunnels.dev")
        try:
            url = get_tunnel_url()
            assert url == "https://gsanc-kirocrew.tunnels.dev"
            # The actual send_dashboard_link requires too many deps to mock,
            # but we verify the get_tunnel_url path works
        finally:
            set_tunnel_url("")


class TestLoaderEdgeCases:
    def test_tunnel_data_non_dict_uses_defaults(self, tmp_path):
        """When tunnel value is not a dict, defaults are used."""
        import json

        from kiro_crew.config.loader import KiroCrewConfig

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"tunnel": "invalid_string"}))
        with patch("kiro_crew.config.loader.config_path", return_value=config_file):
            cfg = KiroCrewConfig.load()
        assert cfg.tunnel.enabled is False
        assert cfg.tunnel.name_mode == "username"


# ── Seam delegation: a companion-style TunnelProvider drives the manager ──


class _FakeTunnelProvider:
    """A stand-in companion provider that records the seam calls.

    ``enabled()`` returns True so the manager treats it as an active tunnel
    (skipping the OSS-disabled notice); ``start`` fires the registered
    ``on_connect`` callback with its public URL to mimic a real connect.
    """

    def __init__(self, url: str = "https://companion.tunnels.example") -> None:
        self._url = url
        self.started = 0
        self.stopped = 0
        self._on_connect = None
        self._on_disconnect = None

    def register_callbacks(self, *, on_connect=None, on_disconnect=None) -> None:
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

    async def start(self) -> None:
        self.started += 1
        if self._on_connect is not None:
            await self._on_connect(self._url)

    async def stop(self) -> None:
        self.stopped += 1
        if self._on_disconnect is not None:
            await self._on_disconnect()

    def public_url(self) -> str:
        return self._url

    def enabled(self) -> bool:
        return True

    def status_snapshot(self):
        return {
            "state": "connected",
            "url": self._url,
            "connected_at": 1000.0,
            "reconnect_attempt": 2,
        }


def _install_tunnel_provider(provider):
    """Install a PlatformContext whose ``tunnel`` field is *provider*.

    The autouse ``_reset_platform_context`` fixture resets the context after the
    test, so this leaks nothing.
    """
    import dataclasses

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import build_default_context, set_context

    ctx = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(ctx, tunnel=provider))
    return provider


class TestTunnelProviderDelegation:
    @pytest.mark.asyncio
    async def test_start_stop_delegate_to_provider(self):
        """start()/stop() call through to the active provider unconditionally."""
        provider = _install_tunnel_provider(_FakeTunnelProvider())
        mgr = TunnelManager(port=5476)

        await mgr.start()
        assert provider.started == 1

        await mgr.stop()
        assert provider.stopped == 1

    @pytest.mark.asyncio
    async def test_public_url_flows_from_provider(self):
        """The manager's public_url reflects the provider's live URL."""
        _install_tunnel_provider(_FakeTunnelProvider("https://gsanc.tunnels.example"))
        mgr = TunnelManager(port=5476)
        await mgr.start()
        assert mgr.public_url == "https://gsanc.tunnels.example"

    @pytest.mark.asyncio
    async def test_status_reflects_provider_snapshot(self):
        """status_snapshot() from the provider projects onto TunnelStatus."""
        _install_tunnel_provider(_FakeTunnelProvider("https://snap.tunnels.example"))
        mgr = TunnelManager(port=5476)
        status = mgr.status
        assert status.state == TunnelState.CONNECTED
        assert status.url == "https://snap.tunnels.example"
        assert status.reconnect_attempt == 2

    @pytest.mark.asyncio
    async def test_default_provider_status_is_local_and_disabled(self):
        """With the Default provider, status stays local + disabled (unchanged)."""
        # No provider installed → the standalone Default composes lazily.
        mgr = TunnelManager(port=5476)
        await mgr.start()
        assert mgr.state == TunnelState.DISABLED
        assert mgr.status.error == "not available in OSS"
        assert mgr.public_url == ""

    @pytest.mark.asyncio
    async def test_stop_wins_over_stale_snapshot(self):
        """An explicit stop() pins STOPPED even if the provider snapshot lags.

        The fake provider keeps returning a "connected" snapshot after stop();
        the local STOPPED write must win so /api/tunnel/status does not report a
        live tunnel with a URL after teardown.
        """
        provider = _install_tunnel_provider(_FakeTunnelProvider())
        mgr = TunnelManager(port=5476)
        await mgr.start()
        assert mgr.status.state == TunnelState.CONNECTED  # snapshot flows pre-stop

        await mgr.stop()
        assert provider.stopped == 1
        # Provider snapshot still says "connected", but STOPPED must win.
        assert mgr.status.state == TunnelState.STOPPED
        assert mgr.status.url == ""
        assert mgr.public_url == ""

    @pytest.mark.asyncio
    async def test_stop_failure_does_not_pin_stopped(self):
        """If the provider's stop() RAISES, the tunnel may still be reachable —
        the manager must NOT pin STOPPED (which would hide a live URL); it
        surfaces an error and lets the live snapshot flow instead."""

        class _FailStopProvider(_FakeTunnelProvider):
            async def stop(self) -> None:
                raise RuntimeError("provider stop boom")

        _install_tunnel_provider(_FailStopProvider())
        mgr = TunnelManager(port=5476)
        await mgr.start()
        assert mgr.status.state == TunnelState.CONNECTED

        await mgr.stop()
        # Not pinned STOPPED — the provider's still-connected snapshot keeps
        # flowing (the tunnel may still be up), so the dashboard doesn't wrongly
        # hide a live tunnel + URL after a failed teardown.
        assert mgr.status.state == TunnelState.CONNECTED
        assert mgr.public_url == "https://companion.tunnels.example"

    @pytest.mark.asyncio
    async def test_start_after_stop_unpins_and_snapshot_flows_again(self):
        """A fresh start() clears the STOPPED pin so the snapshot flows again."""
        _install_tunnel_provider(_FakeTunnelProvider("https://again.tunnels.example"))
        mgr = TunnelManager(port=5476)
        await mgr.start()
        await mgr.stop()
        assert mgr.status.state == TunnelState.STOPPED

        await mgr.start()
        assert mgr.status.state == TunnelState.CONNECTED
        assert mgr.status.url == "https://again.tunnels.example"

    @pytest.mark.asyncio
    async def test_snapshot_failure_does_not_leave_stale_connected(self):
        """When status_snapshot() later raises, status must not report a stale
        connected tunnel from a prior snapshot."""
        provider = _install_tunnel_provider(_FakeTunnelProvider("https://old.example"))
        mgr = TunnelManager(port=5476)
        assert mgr.status.state == TunnelState.CONNECTED  # first snapshot flows

        def _boom():
            raise RuntimeError("provider unreachable")

        provider.status_snapshot = _boom  # type: ignore[assignment]
        # safe_context_call degrades to None → fall back to local status, which
        # was never contaminated by the earlier snapshot (fresh projection).
        assert mgr.status.state == TunnelState.DISABLED
        assert mgr.status.url == ""

    @pytest.mark.asyncio
    async def test_snapshot_omitting_key_resets_stale_field(self):
        """A later snapshot that omits a previously-set key (error/url) resets it
        to default rather than retaining the stale value."""
        provider = _install_tunnel_provider(_FakeTunnelProvider())
        mgr = TunnelManager(port=5476)

        provider.status_snapshot = lambda: {  # type: ignore[assignment]
            "state": "error",
            "error": "boom",
        }
        assert mgr.status.state == TunnelState.ERROR
        assert mgr.status.error == "boom"

        provider.status_snapshot = lambda: {  # type: ignore[assignment]
            "state": "connected",
            "url": "https://new.example",
        }
        status = mgr.status
        assert status.state == TunnelState.CONNECTED
        assert status.url == "https://new.example"
        assert status.error == ""  # stale "boom" must not persist

    @pytest.mark.asyncio
    async def test_public_url_empty_while_reconnecting(self):
        """public_url returns "" when the provider is not CONNECTED even if it
        still exposes a last-known URL (guard mirrors the pre-seam stub)."""
        provider = _install_tunnel_provider(_FakeTunnelProvider("https://last.example"))
        mgr = TunnelManager(port=5476)
        provider.status_snapshot = lambda: {  # type: ignore[assignment]
            "state": "reconnecting",
            "url": "https://last.example",
        }
        assert mgr.status.state == TunnelState.RECONNECTING
        assert mgr.public_url == ""


class TestSetupTunnelThroughProvider:
    @pytest.mark.asyncio
    async def test_provider_start_called_when_token_auth_present(self):
        """setup_tunnel drives the installed provider's start via TunnelManager."""
        from kiro_crew.tunnel import get_tunnel_url, set_tunnel_url
        from kiro_crew.tunnel.setup import setup_tunnel

        set_tunnel_url("")
        provider = _install_tunnel_provider(_FakeTunnelProvider("https://flow.tunnels.example"))
        allowed_origins: set = set()
        mw = MagicMock()
        mw._is_token_auth = True

        result = await setup_tunnel(
            middlewares=[mw],
            allowed_origins=allowed_origins,
            tunnel_name_mode="username",
            tunnel_name_override="",
            port=5476,
            log_api_access=MagicMock(),
        )

        assert result is not None
        assert provider.started == 1
        # public_url flowed into set_tunnel_url + the CORS allow-list. Compare by
        # exact set membership (not a substring/`in` scan) so the origin is
        # matched whole.
        assert allowed_origins == {"https://flow.tunnels.example"}
        assert get_tunnel_url() == "https://flow.tunnels.example"
        set_tunnel_url("")

    @pytest.mark.asyncio
    async def test_provider_start_not_called_without_token_auth(self):
        """The token-auth deny gate is evaluated BEFORE provider.start()."""
        from kiro_crew.tunnel.setup import setup_tunnel

        provider = _install_tunnel_provider(_FakeTunnelProvider())

        result = await setup_tunnel(
            middlewares=[],  # no token auth
            allowed_origins=set(),
            tunnel_name_mode="username",
            tunnel_name_override="",
            port=5476,
            log_api_access=MagicMock(),
        )

        assert result is None
        assert provider.started == 0
