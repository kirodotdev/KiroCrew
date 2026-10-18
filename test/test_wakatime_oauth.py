"""Tests for the WakaTime OAuth module: authorize URL, token parsing, storage.

No network: the token-endpoint POST and the vault are both patched, so the
flow logic (state generation, body validation, expiry derivation, carry-forward
of a missing refresh token) is exercised without reaching WakaTime or disk.
"""

from __future__ import annotations

import time
import urllib.parse

import pytest

from kiro_crew.wakatime import oauth


class _FakeWakaCfg:
    def __init__(self, *, oauth_client_id: str = "", oauth_token_expires_at: int = 0) -> None:
        self.oauth_client_id = oauth_client_id
        self.oauth_token_expires_at = oauth_token_expires_at


class _FakeConfig:
    def __init__(self, *, oauth_client_id: str = "", oauth_token_expires_at: int = 0) -> None:
        self.wakatime = _FakeWakaCfg(
            oauth_client_id=oauth_client_id, oauth_token_expires_at=oauth_token_expires_at
        )

    @classmethod
    def _loader(cls, **kwargs: object):
        return lambda: cls(**kwargs)  # type: ignore[arg-type]


def _patch_config(monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> None:
    monkeypatch.setattr(oauth.KiroCrewConfig, "load", classmethod(lambda _c: _FakeConfig(**kwargs)))


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


def test_tokens_from_body_derives_absolute_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    # Freeze the module's clock so the derived expiry is exact, not a window:
    # _tokens_from_body reads time.time() itself, and asserting against a live
    # second read is racy under a slow worker.
    monkeypatch.setattr(oauth.time, "time", lambda: 1_000_000.0)
    tokens = oauth._tokens_from_body(
        {"access_token": "acc", "refresh_token": "ref", "expires_in": 3600}
    )
    assert tokens.access_token == "acc"
    assert tokens.refresh_token == "ref"
    assert tokens.expires_at == 1_000_000 + 3600


def test_tokens_from_body_zero_expiry_when_missing() -> None:
    tokens = oauth._tokens_from_body({"access_token": "acc", "refresh_token": "ref"})
    assert tokens.expires_at == 0


def test_tokens_from_body_rejects_missing_access_token() -> None:
    with pytest.raises(oauth.WakaTimeOAuthError) as exc:
        oauth._tokens_from_body({"refresh_token": "ref", "expires_in": 60})
    assert exc.value.code == "exchange_failed"


def test_token_is_fresh_false_without_stored_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth, "read_access_token", lambda: "")
    assert (
        oauth.token_is_fresh(_FakeConfig(oauth_token_expires_at=int(time.time()) + 9999)) is False
    )


def test_token_is_fresh_false_when_expiry_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth, "read_access_token", lambda: "acc")
    assert oauth.token_is_fresh(_FakeConfig(oauth_token_expires_at=0)) is False


def test_token_is_fresh_true_before_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth, "read_access_token", lambda: "acc")
    assert oauth.token_is_fresh(_FakeConfig(oauth_token_expires_at=int(time.time()) + 9999)) is True


def test_token_is_fresh_false_within_skew_of_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth, "read_access_token", lambda: "acc")
    # Expires in 10s, inside the 60s skew, so a read should refresh first.
    assert oauth.token_is_fresh(_FakeConfig(oauth_token_expires_at=int(time.time()) + 10)) is False


class _RecordingVault:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.deleted: list[str] = []

    def set_sync(self, name: str, value: str) -> None:
        self.store[name] = value

    def delete_sync(self, name: str) -> None:
        self.deleted.append(name)
        self.store.pop(name, None)


def test_store_tokens_writes_both_secrets_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = _RecordingVault()
    monkeypatch.setattr(oauth, "SecretVault", lambda _root: vault)
    written: dict[str, int] = {}
    monkeypatch.setattr(oauth, "_write_expiry", lambda e: written.update(v=e))
    oauth.store_tokens(oauth.OAuthTokens(access_token="acc", refresh_token="ref", expires_at=123))
    assert vault.store[oauth.CRED_WAKATIME_OAUTH_ACCESS_TOKEN] == "acc"
    assert vault.store[oauth.CRED_WAKATIME_OAUTH_REFRESH_TOKEN] == "ref"
    assert written == {"v": 123}


def test_clear_tokens_deletes_both_and_resets_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = _RecordingVault()
    monkeypatch.setattr(oauth, "SecretVault", lambda _root: vault)
    reset: dict[str, int] = {}
    monkeypatch.setattr(oauth, "_write_expiry", lambda e: reset.update(v=e))
    oauth.clear_tokens()
    assert oauth.CRED_WAKATIME_OAUTH_ACCESS_TOKEN in vault.deleted
    assert oauth.CRED_WAKATIME_OAUTH_REFRESH_TOKEN in vault.deleted
    assert reset == {"v": 0}
