"""Tests for the WakaTime service resolver (config + secret -> client)."""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.secrets.vault import SecretValue
from kiro_crew.wakatime import service
from kiro_crew.wakatime.client import DEFAULT_API_BASE


class _FakeVault:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def get(self, name: str) -> SecretValue | None:
        return SecretValue(self._value) if self._value is not None else None


class _FakeWakaCfg:
    def __init__(self, *, enabled: bool, api_base_url: str = "") -> None:
        self.enabled = enabled
        self.api_base_url = api_base_url


class _FakeConfig:
    def __init__(self, *, enabled: bool, api_base_url: str = "") -> None:
        self.wakatime = _FakeWakaCfg(enabled=enabled, api_base_url=api_base_url)


def test_resolve_api_key_reads_the_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "SecretVault", lambda _dir: _FakeVault("vault_key"))
    monkeypatch.setattr(service, "config_dir", lambda: "/nowhere")
    assert service.resolve_api_key() == "vault_key"


def test_resolve_api_key_empty_when_vault_has_no_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "SecretVault", lambda _dir: _FakeVault(None))
    monkeypatch.setattr(service, "config_dir", lambda: "/nowhere")
    assert service.resolve_api_key() == ""


def test_resolve_api_key_empty_on_vault_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_dir: Any) -> Any:
        raise RuntimeError("vault broken")

    monkeypatch.setattr(service, "SecretVault", _boom)
    monkeypatch.setattr(service, "config_dir", lambda: "/nowhere")
    assert service.resolve_api_key() == ""


def test_resolve_base_url_default_and_override() -> None:
    assert service.resolve_base_url(_FakeConfig(enabled=True)) == DEFAULT_API_BASE
    override = "https://wakapi.example.com/api/v1"
    assert service.resolve_base_url(_FakeConfig(enabled=True, api_base_url=override)) == override


def test_build_client_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "resolve_api_key", lambda: "a_key")
    assert service.build_client(_FakeConfig(enabled=False)) is None


def test_build_client_none_when_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "resolve_api_key", lambda: "")
    assert service.build_client(_FakeConfig(enabled=True)) is None


def test_build_client_ready_when_enabled_and_keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "resolve_api_key", lambda: "a_key")
    client = service.build_client(_FakeConfig(enabled=True))
    assert client is not None
    assert client._api_base == DEFAULT_API_BASE
