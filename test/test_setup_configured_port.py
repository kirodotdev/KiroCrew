"""Tests for _configured_port and its call sites in cli_setup."""

from unittest.mock import MagicMock, patch

from kiro_crew.cli_setup import (
    _configured_port,
    _maybe_setup_custom_domain,
    _maybe_setup_dashboard_url,
)


class TestConfiguredPort:
    def test_returns_default_without_config(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        with patch("kiro_crew.cli_server.KiroCrewConfig") as mock_cfg:
            mock_cfg.load.return_value.dashboard.url = ""
            assert _configured_port() == 5476

    def test_respects_env_var(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_PORT", "9999")
        assert _configured_port() == 9999

    def test_respects_config_url(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        with patch("kiro_crew.cli_server.KiroCrewConfig") as mock_cfg:
            mock_cfg.load.return_value.dashboard.url = "http://localhost:8888"
            assert _configured_port() == 8888


class TestCustomDomainUsesConfiguredPort:
    def test_prints_configured_port(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("KIROCREW_PORT", "4321")
        monkeypatch.setattr("kiro_crew.cli_setup.Path", lambda *a, **kw: tmp_path / "hosts")
        _maybe_setup_custom_domain()
        out = capsys.readouterr().out
        assert "4321" in out


class TestDashboardUrlUsesConfiguredPort:
    def test_input_prompt_contains_configured_port(self, monkeypatch, capsys):
        monkeypatch.setenv("KIROCREW_PORT", "5678")

        # Mock config to have an existing dashboard.url (bypasses is_remote check)
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = "http://old:1234"
        mock_cfg.load_credentials.return_value = {
            "SLACK_APP_TOKEN": "xapp-fake",
            "SLACK_BOT_TOKEN": "xoxb-fake",
        }
        monkeypatch.setattr("kiro_crew.cli_setup.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr("kiro_crew.cli_setup.config_path", lambda: MagicMock())
        monkeypatch.setattr("kiro_crew.cli_setup.socket.gethostbyname", lambda _: "10.0.0.1")
        monkeypatch.setattr("kiro_crew.cli_setup.socket.gethostname", lambda: "myhost")
        # Capture the prompt string passed to input()
        prompts = []
        monkeypatch.setattr("builtins.input", lambda prompt: (prompts.append(prompt), "")[1])

        _maybe_setup_dashboard_url()
        assert any("5678" in p for p in prompts)
