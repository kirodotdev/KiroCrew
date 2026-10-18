"""Tests for the WakaTime service resolver (config + secret -> client)."""

from __future__ import annotations

import pytest

from kiro_crew.secrets.vault import SecretValue
from kiro_crew.wakatime import service
from kiro_crew.wakatime.client import DEFAULT_API_BASE


class _FakeWakaCfg:
    def __init__(
        self,
        *,
        enabled: bool,
        api_base_url: str = "",
        oauth_client_id: str = "",
        oauth_token_expires_at: int = 0,
    ) -> None:
        self.enabled = enabled
        self.api_base_url = api_base_url
        self.oauth_client_id = oauth_client_id
        self.oauth_token_expires_at = oauth_token_expires_at


class _FakeConfig:
    def __init__(
        self,
        *,
        enabled: bool,
        api_base_url: str = "",
        oauth_client_id: str = "",
        oauth_token_expires_at: int = 0,
    ) -> None:
        self.wakatime = _FakeWakaCfg(
            enabled=enabled,
            api_base_url=api_base_url,
            oauth_client_id=oauth_client_id,
            oauth_token_expires_at=oauth_token_expires_at,
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


def test_build_client_passes_oauth_token_to_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "resolve_api_key", lambda: "a_key")
    client = service.build_client(_FakeConfig(enabled=True), oauth_access_token="tok_abc")
    assert client is not None
    assert client._oauth_access_token == "tok_abc"


def test_build_client_without_token_stays_on_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "resolve_api_key", lambda: "a_key")
    client = service.build_client(_FakeConfig(enabled=True))
    assert client is not None
    assert client._oauth_access_token is None


class _FakeOAuthModule:
    """Stand-in for kiro_crew.wakatime.oauth with controllable state."""

    class WakaTimeOAuthError(Exception):
        def __init__(self, message: str = "", *, code: str = "err") -> None:
            super().__init__(message)
            self.code = code

    def __init__(
        self,
        *,
        stored: str = "",
        fresh: bool = False,
        refreshed: str | None = None,
        raises: bool = False,
    ) -> None:
        self._stored = stored
        self._fresh = fresh
        self._refreshed = refreshed
        self._raises = raises
        self.refresh_calls = 0
        self.read_calls = 0
        import asyncio as _asyncio

        self._lock = _asyncio.Lock()

    def token_mutation_lock(self) -> object:
        return self._lock

    def read_access_token(self) -> str:
        self.read_calls += 1
        return self._stored

    def token_is_fresh(self, _config: object = None) -> bool:
        return self._fresh

    async def refresh_tokens(self, _redirect_uri: str) -> object:
        self.refresh_calls += 1
        if self._raises:
            raise self.WakaTimeOAuthError(code="unreachable")

        class _T:
            access_token = self._refreshed or ""

        return _T()

    def store_tokens(self, _tokens: object) -> None:
        pass


def _install_fake_oauth(monkeypatch: pytest.MonkeyPatch, fake: _FakeOAuthModule) -> None:
    import kiro_crew.wakatime as wakatime_pkg

    monkeypatch.setattr(wakatime_pkg, "oauth", fake, raising=False)


@pytest.mark.asyncio
async def test_resolve_oauth_token_empty_when_none_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeOAuthModule(stored="")
    _install_fake_oauth(monkeypatch, fake)
    result = await service.resolve_oauth_access_token("https://x/cb", _FakeConfig(enabled=True))
    assert result == ""
    assert fake.refresh_calls == 0


@pytest.mark.asyncio
async def test_resolve_oauth_token_returns_fresh_without_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOAuthModule(stored="live_tok", fresh=True)
    _install_fake_oauth(monkeypatch, fake)
    result = await service.resolve_oauth_access_token("https://x/cb", _FakeConfig(enabled=True))
    assert result == "live_tok"
    assert fake.refresh_calls == 0


@pytest.mark.asyncio
async def test_resolve_oauth_token_refreshes_when_due(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeOAuthModule(stored="stale_tok", fresh=False, refreshed="new_tok")
    _install_fake_oauth(monkeypatch, fake)
    result = await service.resolve_oauth_access_token("https://x/cb", _FakeConfig(enabled=True))
    assert result == "new_tok"
    assert fake.refresh_calls == 1


@pytest.mark.asyncio
async def test_resolve_oauth_token_empty_when_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOAuthModule(stored="stale_tok", fresh=False, raises=True)
    _install_fake_oauth(monkeypatch, fake)
    result = await service.resolve_oauth_access_token("https://x/cb", _FakeConfig(enabled=True))
    assert result == ""
    assert fake.refresh_calls == 1


@pytest.mark.asyncio
async def test_resolve_oauth_token_skips_vault_and_refresh_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOAuthModule(stored="stale_tok", fresh=False, refreshed="new_tok")
    _install_fake_oauth(monkeypatch, fake)
    result = await service.resolve_oauth_access_token("https://x/cb", _FakeConfig(enabled=False))
    assert result == ""
    assert fake.read_calls == 0
    assert fake.refresh_calls == 0
