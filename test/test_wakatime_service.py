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


def test_build_client_passes_oauth_token_to_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "resolve_api_key", lambda: "a_key")
    client = service.build_client(_FakeConfig(enabled=True), oauth_access_token="tok_abc")
    assert client is not None
    assert client._oauth_access_token == "tok_abc"


def test_build_client_drops_oauth_token_for_a_self_hosted_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "resolve_api_key", lambda: "a_key")
    cfg = _FakeConfig(enabled=True, api_base_url="https://wakapi.example.com/api")
    client = service.build_client(cfg, oauth_access_token="tok_abc")
    assert client is not None
    assert client._oauth_access_token is None


def test_build_client_without_token_stays_on_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "resolve_api_key", lambda: "a_key")
    client = service.build_client(_FakeConfig(enabled=True))
    assert client is not None
    assert client._oauth_access_token is None


class _Tok:
    def __init__(self, access_token: str, *, fresh: bool) -> None:
        self.access_token = access_token
        self.fresh = fresh


class _FakeOAuthModule:
    """Stand-in for kiro_crew.wakatime.oauth with controllable state.

    ``reads`` is the sequence of values successive ``read_tokens`` calls
    return (the last one repeats), so a test can model a sibling refresh that
    lands while this read waits for the lock.
    """

    class WakaTimeOAuthError(Exception):
        def __init__(self, message: str = "", *, code: str = "err") -> None:
            super().__init__(message)
            self.code = code

    def __init__(
        self,
        *,
        reads: list[_Tok | None],
        refreshed: str | None = None,
        raises: bool = False,
    ) -> None:
        self._reads = reads
        self._refreshed = refreshed
        self._raises = raises
        self.refresh_calls = 0
        self.read_calls = 0
        self.stored: list[object] = []
        import asyncio as _asyncio

        self._lock = _asyncio.Lock()

    def token_mutation_lock(self) -> object:
        return self._lock

    def read_tokens(self) -> _Tok | None:
        value = self._reads[min(self.read_calls, len(self._reads) - 1)]
        self.read_calls += 1
        return value

    def token_is_fresh(self, tokens: _Tok) -> bool:
        return tokens.fresh

    async def refresh_tokens(self, _stored: object) -> object:
        self.refresh_calls += 1
        if self._raises:
            raise self.WakaTimeOAuthError(code="unreachable")
        return _Tok(self._refreshed or "", fresh=True)

    def store_tokens(self, tokens: object) -> None:
        self.stored.append(tokens)


def _install_fake_oauth(monkeypatch: pytest.MonkeyPatch, fake: _FakeOAuthModule) -> None:
    import kiro_crew.wakatime as wakatime_pkg

    monkeypatch.setattr(wakatime_pkg, "oauth", fake, raising=False)


@pytest.mark.asyncio
async def test_resolve_oauth_token_empty_when_none_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeOAuthModule(reads=[None])
    _install_fake_oauth(monkeypatch, fake)
    result = await service.resolve_oauth_access_token(_FakeConfig(enabled=True))
    assert result == ""
    assert fake.refresh_calls == 0


@pytest.mark.asyncio
async def test_resolve_oauth_token_returns_fresh_without_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOAuthModule(reads=[_Tok("live_tok", fresh=True)])
    _install_fake_oauth(monkeypatch, fake)
    result = await service.resolve_oauth_access_token(_FakeConfig(enabled=True))
    assert result == "live_tok"
    assert fake.refresh_calls == 0


@pytest.mark.asyncio
async def test_resolve_oauth_token_refreshes_when_due(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeOAuthModule(reads=[_Tok("stale_tok", fresh=False)], refreshed="new_tok")
    _install_fake_oauth(monkeypatch, fake)
    result = await service.resolve_oauth_access_token(_FakeConfig(enabled=True))
    assert result == "new_tok"
    assert fake.refresh_calls == 1
    assert len(fake.stored) == 1


@pytest.mark.asyncio
async def test_resolve_oauth_token_skips_refresh_a_sibling_already_did(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The first read sees a due token; by the time the lock is held, a sibling
    # read has stored a fresh one. The re-read inside the lock must see it and
    # not send a second refresh.
    fake = _FakeOAuthModule(
        reads=[_Tok("stale_tok", fresh=False), _Tok("sibling_tok", fresh=True)],
        refreshed="unused",
    )
    _install_fake_oauth(monkeypatch, fake)
    result = await service.resolve_oauth_access_token(_FakeConfig(enabled=True))
    assert result == "sibling_tok"
    assert fake.refresh_calls == 0


@pytest.mark.asyncio
async def test_resolve_oauth_token_empty_when_disconnected_while_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOAuthModule(reads=[_Tok("stale_tok", fresh=False), None], refreshed="new")
    _install_fake_oauth(monkeypatch, fake)
    result = await service.resolve_oauth_access_token(_FakeConfig(enabled=True))
    assert result == ""
    assert fake.refresh_calls == 0
    assert fake.stored == []


@pytest.mark.asyncio
async def test_resolve_oauth_token_empty_when_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOAuthModule(reads=[_Tok("stale_tok", fresh=False)], raises=True)
    _install_fake_oauth(monkeypatch, fake)
    result = await service.resolve_oauth_access_token(_FakeConfig(enabled=True))
    assert result == ""
    assert fake.refresh_calls == 1
    assert fake.stored == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base", ["https://wakapi.example.com/api", "https://hackatime.example.org/api/hackatime/v1"]
)
async def test_resolve_oauth_token_never_sent_to_a_self_hosted_backend(
    monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    # The stored grant is a wakatime.com credential; a self-hosted base keeps
    # the API key and the vault is not even read.
    fake = _FakeOAuthModule(reads=[_Tok("live_tok", fresh=True)])
    _install_fake_oauth(monkeypatch, fake)
    result = await service.resolve_oauth_access_token(_FakeConfig(enabled=True, api_base_url=base))
    assert result == ""
    assert fake.read_calls == 0


@pytest.mark.asyncio
async def test_resolve_oauth_token_used_for_the_explicit_public_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOAuthModule(reads=[_Tok("live_tok", fresh=True)])
    _install_fake_oauth(monkeypatch, fake)
    result = await service.resolve_oauth_access_token(
        _FakeConfig(enabled=True, api_base_url=DEFAULT_API_BASE + "/")
    )
    assert result == "live_tok"


@pytest.mark.asyncio
async def test_resolve_oauth_token_skips_vault_and_refresh_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOAuthModule(reads=[_Tok("stale_tok", fresh=False)], refreshed="new_tok")
    _install_fake_oauth(monkeypatch, fake)
    result = await service.resolve_oauth_access_token(_FakeConfig(enabled=False))
    assert result == ""
    assert fake.read_calls == 0
    assert fake.refresh_calls == 0
