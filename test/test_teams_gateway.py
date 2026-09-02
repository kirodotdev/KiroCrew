"""Tests for Teams gateway boot (maybe_start_teams) and the status endpoint."""

from __future__ import annotations

import json
import tempfile
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.teams.gateway as teams_gateway
from kiro_crew.dashboard.handlers.messaging import api_teams_config_get
from kiro_crew.teams.gateway import maybe_start_teams


class _FakeState:
    def __init__(self) -> None:
        self.teams_on_activity = None
        self.teams_connected = False
        self.teams_connect_error = ""
        self.registered: list = []

    def register_channel_transport(self, transport) -> None:
        self.registered.append(transport)


def _orch(**overrides):
    base = dict(
        _teams_enabled=True,
        _teams_app_id="app-1",
        _teams_app_password="secret",
        _teams_tenant_id="",
        _teams_allowed_emails=["alice@example.com"],
        sessions=object(),
        ctx_builder=object(),
        conv_log=None,
        _approval_mode=None,
        _cfg=SimpleNamespace(agent=SimpleNamespace(approval_mode="interactive")),
        dashboard_state=_FakeState(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    # Never hit the token endpoint during boot tests.
    async def _fake_connect(self):
        self._notify_state(True, "")

    monkeypatch.setattr(teams_gateway.TeamsClient, "connect", _fake_connect)
    monkeypatch.setattr(teams_gateway, "HAS_JWT", True)


class TestMaybeStartTeams:
    @pytest.mark.asyncio
    async def test_noop_when_disabled(self) -> None:
        orch = _orch(_teams_enabled=False)
        client = await maybe_start_teams(orch)
        assert client is None
        assert orch.dashboard_state.teams_on_activity is None

    @pytest.mark.asyncio
    async def test_noop_when_uncredentialed(self) -> None:
        orch = _orch(_teams_app_password="")
        assert await maybe_start_teams(orch) is None

    @pytest.mark.asyncio
    async def test_registers_webhook_when_enabled(self) -> None:
        orch = _orch()
        client = await maybe_start_teams(orch)
        assert client is not None
        # Late-bound webhook handler wired to the client.
        assert orch.dashboard_state.teams_on_activity == client.on_activity
        assert orch.dashboard_state.registered  # transport registered
        assert orch.dashboard_state.teams_connected is True

    @pytest.mark.asyncio
    async def test_exception_contained(self, monkeypatch) -> None:
        # sessions=None -> the assert inside the try raises -> contained.
        orch = _orch(sessions=None)
        client = await maybe_start_teams(orch)
        assert client is None
        assert orch.dashboard_state.teams_connect_error  # reason recorded

    @pytest.mark.asyncio
    async def test_empty_allow_list_warns_but_starts(self, caplog) -> None:
        orch = _orch(_teams_allowed_emails=[])
        with caplog.at_level("WARNING"):
            client = await maybe_start_teams(orch)
        assert client is not None
        assert any("allowed_emails is empty" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_missing_pyjwt_noop(self, monkeypatch) -> None:
        monkeypatch.setattr(teams_gateway, "HAS_JWT", False)
        orch = _orch()
        client = await maybe_start_teams(orch)
        assert client is None
        assert "PyJWT" in orch.dashboard_state.teams_connect_error


class _FakeRequest:
    def __init__(self, state) -> None:
        self.app = {"state": state}
        self.headers: dict = {}
        self.remote = "127.0.0.1"


class TestStatusEndpoint:
    @pytest.mark.asyncio
    async def test_status_reflects_state_and_config(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.messaging.is_direct_local_request",
            lambda req: True,
        )
        # Config with teams enabled + allow-list. The secret is env-only, so
        # it is supplied via credentials (load_credentials), NOT config.json.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(
                {
                    "teams": {
                        "enabled": True,
                        "app_id": "app-1",
                        "tenant_id": "t-1",
                        "allowed_emails": ["alice@example.com"],
                    }
                },
                f,
            )
            tmp = Path(f.name)
        state = _FakeState()
        state.teams_connected = True
        try:
            with unittest.mock.patch(
                "kiro_crew.config.loader.config_path", return_value=tmp
            ), unittest.mock.patch(
                "kiro_crew.config.loader.KiroCrewConfig.load_credentials",
                return_value={"MICROSOFT_APP_PASSWORD": "secret"},
            ):
                resp = await api_teams_config_get(_FakeRequest(state))
        finally:
            tmp.unlink(missing_ok=True)
        body = json.loads(resp.body.decode())
        assert body["enabled"] is True
        assert body["connected"] is True
        assert body["configured"] is True
        assert body["app_id_set"] is True
        assert body["app_password_set"] is True  # sourced from the env credential
        assert body["allowed_emails"] == ["alice@example.com"]
