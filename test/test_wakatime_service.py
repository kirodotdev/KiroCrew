"""Tests for the WakaTime service resolver (config + secret -> client)."""

from __future__ import annotations

import pytest

from kiro_crew.secrets.vault import SecretValue
from kiro_crew.wakatime import service
from kiro_crew.wakatime.client import DEFAULT_API_BASE


class _FakeWakaCfg:
    def __init__(
        self, *, enabled: bool, api_base_url: str = "", allow_self_hosted: bool = False
    ) -> None:
        self.enabled = enabled
        self.api_base_url = api_base_url
        self.allow_self_hosted = allow_self_hosted


class _FakeConfig:
    def __init__(
        self, *, enabled: bool, api_base_url: str = "", allow_self_hosted: bool = False
    ) -> None:
        self.wakatime = _FakeWakaCfg(
            enabled=enabled, api_base_url=api_base_url, allow_self_hosted=allow_self_hosted
        )


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


def test_resolve_base_url_empty_uses_public_default() -> None:
    assert service.resolve_base_url(_FakeConfig(enabled=True)) == DEFAULT_API_BASE


def test_resolve_base_url_honors_https_wakatime_host() -> None:
    url = "https://wakatime.com/api/v1"
    assert service.resolve_base_url(_FakeConfig(enabled=True, api_base_url=url)) == url
    sub = "https://api.wakatime.com/api/v1"
    assert service.resolve_base_url(_FakeConfig(enabled=True, api_base_url=sub)) == sub


def test_resolve_base_url_rejects_unapproved_host_without_opt_in() -> None:
    # A config-set non-wakatime host must NOT receive the Basic-auth API key
    # unless the self-hosted opt-in is deliberately on; fall back to public.
    attacker = "https://evil.example.com/api/v1"
    assert (
        service.resolve_base_url(_FakeConfig(enabled=True, api_base_url=attacker))
        == DEFAULT_API_BASE
    )


def test_resolve_base_url_rejects_non_https_scheme() -> None:
    # Plaintext would leak the Basic-auth header even to wakatime.com.
    plain = "http://wakatime.com/api/v1"
    assert (
        service.resolve_base_url(_FakeConfig(enabled=True, api_base_url=plain)) == DEFAULT_API_BASE
    )


def test_resolve_base_url_self_hosted_honored_only_with_opt_in() -> None:
    url = "https://wakapi.example.com/api/v1"
    # Off (default): unapproved host falls back to public.
    assert service.resolve_base_url(_FakeConfig(enabled=True, api_base_url=url)) == DEFAULT_API_BASE
    # On: the deliberate opt-in honors the https self-hosted host.
    assert (
        service.resolve_base_url(
            _FakeConfig(enabled=True, api_base_url=url, allow_self_hosted=True)
        )
        == url
    )


def test_resolve_base_url_opt_in_still_requires_https() -> None:
    # The opt-in permits a self-hosted HOST, not a plaintext scheme.
    plain = "http://wakapi.example.com/api/v1"
    assert (
        service.resolve_base_url(
            _FakeConfig(enabled=True, api_base_url=plain, allow_self_hosted=True)
        )
        == DEFAULT_API_BASE
    )


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
