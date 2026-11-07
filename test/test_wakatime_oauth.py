"""Tests for the WakaTime OAuth module: authorize URL, token parsing, storage.

No network: the token-endpoint POST and the vault are both patched, so the
flow logic (state generation, body validation, expiry derivation, carry-forward
of a missing refresh token) is exercised without reaching WakaTime or disk.
"""

from __future__ import annotations

import json
import types
import urllib.parse
from typing import Any

import pytest

from kiro_crew.wakatime import oauth

_NOW = 1_000_000


class _FakeWakaCfg:
    def __init__(self, *, oauth_client_id: str = "") -> None:
        self.oauth_client_id = oauth_client_id


class _FakeConfig:
    def __init__(self, *, oauth_client_id: str = "") -> None:
        self.wakatime = _FakeWakaCfg(oauth_client_id=oauth_client_id)


def _patch_config(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    monkeypatch.setattr(oauth.KiroCrewConfig, "load", classmethod(lambda _c: _FakeConfig(**kwargs)))


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # Replace the module's own ``time`` binding with a stand-in, never the
    # stdlib module's attribute: patching ``oauth.time.time`` would freeze the
    # clock for every thread and loop in the worker.
    monkeypatch.setattr(oauth, "time", types.SimpleNamespace(time=lambda: float(_NOW)))


def _tokens(expires_at: int, *, refresh: str = "ref", uri: str = "https://a/cb") -> Any:
    return oauth.OAuthTokens(
        access_token="acc", refresh_token=refresh, expires_at=expires_at, redirect_uri=uri
    )


def test_build_authorize_url_sets_required_args(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, oauth_client_id="cid123")
    url, state = oauth.build_authorize_url("https://host/api/wakatime/oauth/callback")
    parsed = urllib.parse.urlparse(url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    assert parsed.scheme == "https" and parsed.netloc == "wakatime.com"
    assert query["client_id"] == "cid123"
    assert query["response_type"] == "code"
    assert query["redirect_uri"] == "https://host/api/wakatime/oauth/callback"
    assert query["scope"] == "read_stats read_summaries"
    assert query["state"] == state and len(state) >= 32


def test_build_authorize_url_errors_without_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, oauth_client_id="")
    with pytest.raises(oauth.WakaTimeOAuthError) as exc:
        oauth.build_authorize_url("https://host/cb")
    assert exc.value.code == "not_configured"


def test_build_authorize_url_state_differs_each_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, oauth_client_id="cid")
    _, state_a = oauth.build_authorize_url("https://host/cb")
    _, state_b = oauth.build_authorize_url("https://host/cb")
    assert state_a != state_b


@pytest.mark.usefixtures("frozen_clock")
def test_tokens_from_body_derives_absolute_expiry() -> None:
    tokens = oauth._tokens_from_body(
        {"access_token": "acc", "refresh_token": "ref", "expires_in": 3600}, "https://a/cb"
    )
    assert tokens.access_token == "acc"
    assert tokens.refresh_token == "ref"
    assert tokens.expires_at == _NOW + 3600
    assert tokens.redirect_uri == "https://a/cb"


def test_tokens_from_body_zero_expiry_when_missing() -> None:
    tokens = oauth._tokens_from_body({"access_token": "acc", "refresh_token": "ref"}, "u")
    assert tokens.expires_at == 0


def test_tokens_from_body_rejects_missing_access_token() -> None:
    with pytest.raises(oauth.WakaTimeOAuthError) as exc:
        oauth._tokens_from_body({"refresh_token": "ref", "expires_in": 60}, "u")
    assert exc.value.code == "exchange_failed"


@pytest.mark.usefixtures("frozen_clock")
def test_token_is_fresh_false_when_expiry_unknown() -> None:
    assert oauth.token_is_fresh(_tokens(0)) is False


@pytest.mark.usefixtures("frozen_clock")
def test_token_is_fresh_true_before_expiry() -> None:
    assert oauth.token_is_fresh(_tokens(_NOW + 9999)) is True


@pytest.mark.usefixtures("frozen_clock")
def test_token_is_fresh_false_within_skew_of_expiry() -> None:
    # Expires in 10s, inside the 60s skew, so a read should refresh first.
    assert oauth.token_is_fresh(_tokens(_NOW + 10)) is False


class _Secret:
    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value


class _RecordingVault:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.deleted: list[str] = []

    def get(self, name: str) -> _Secret | None:
        return _Secret(self.store[name]) if name in self.store else None

    def set_sync(self, name: str, value: str) -> None:
        self.store[name] = value

    def delete_sync(self, name: str) -> None:
        self.deleted.append(name)
        self.store.pop(name, None)


def test_store_tokens_writes_one_entry_that_reads_back(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = _RecordingVault()
    monkeypatch.setattr(oauth, "SecretVault", lambda _root: vault)
    oauth.store_tokens(_tokens(123))
    assert list(vault.store) == [oauth.CRED_WAKATIME_OAUTH_TOKENS]
    assert oauth.read_tokens() == _tokens(123)
    assert oauth.read_access_token() == "acc"


@pytest.mark.parametrize("raw", ["not json", "[]", json.dumps({"refresh_token": "r"})])
def test_read_tokens_treats_malformed_entry_as_absent(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    vault = _RecordingVault()
    vault.store[oauth.CRED_WAKATIME_OAUTH_TOKENS] = raw
    monkeypatch.setattr(oauth, "SecretVault", lambda _root: vault)
    assert oauth.read_tokens() is None
    assert oauth.read_access_token() == ""


def test_clear_tokens_deletes_the_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = _RecordingVault()
    monkeypatch.setattr(oauth, "SecretVault", lambda _root: vault)
    oauth.store_tokens(_tokens(123))
    oauth.clear_tokens()
    assert vault.deleted == [oauth.CRED_WAKATIME_OAUTH_TOKENS]
    assert oauth.read_tokens() is None


@pytest.mark.asyncio
async def test_refresh_sends_the_stored_redirect_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    # The refresh grant must reuse the redirect URI saved at connect time, not
    # one rebuilt from whichever host served the current read.
    sent: dict[str, str] = {}

    async def _fake_post(data: dict[str, str]) -> dict[str, Any]:
        sent.update(data)
        return {"access_token": "new", "expires_in": 3600}

    monkeypatch.setattr(oauth, "_client_credentials", lambda: ("cid", "sec"))
    monkeypatch.setattr(oauth, "_post_token", _fake_post)
    result = await oauth.refresh_tokens(_tokens(1, uri="https://localhost:1/cb"))
    assert sent["redirect_uri"] == "https://localhost:1/cb"
    assert sent["refresh_token"] == "ref"
    # The response omitted a refresh token, so the stored one carries forward.
    assert result.refresh_token == "ref"
    assert result.redirect_uri == "https://localhost:1/cb"
    assert result.access_token == "new"


@pytest.mark.asyncio
async def test_refresh_without_refresh_token_is_not_configured() -> None:
    with pytest.raises(oauth.WakaTimeOAuthError) as exc:
        await oauth.refresh_tokens(_tokens(1, refresh=""))
    assert exc.value.code == "not_configured"
