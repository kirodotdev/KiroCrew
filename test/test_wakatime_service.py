"""Tests for the WakaTime service resolver (config + secret -> client)."""

from __future__ import annotations

import pytest

from kiro_crew.secrets.vault import SecretValue
from kiro_crew.wakatime import service
from kiro_crew.wakatime.client import DEFAULT_API_BASE


class _FakeWakaCfg:
    def __init__(self, *, enabled: bool, api_base_url: str = "") -> None:
        self.enabled = enabled
        self.api_base_url = api_base_url


class _FakeConfig:
    def __init__(self, *, enabled: bool, api_base_url: str = "") -> None:
        self.wakatime = _FakeWakaCfg(enabled=enabled, api_base_url=api_base_url)


class _FakeVault:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def get(self, _name: str) -> SecretValue | None:
        return SecretValue(self.value) if self.value is not None else None


def test_resolve_api_key_reads_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "SecretVault", lambda _root: _FakeVault("vault_key"))
    assert service.resolve_api_key() == "vault_key"


def test_resolve_api_key_empty_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "SecretVault", lambda _root: _FakeVault(None))
    assert service.resolve_api_key() == ""


def test_resolve_api_key_empty_on_vault_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service, "SecretVault", lambda _root: (_ for _ in ()).throw(RuntimeError("broken"))
    )
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
