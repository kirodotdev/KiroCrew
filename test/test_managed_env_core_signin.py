"""The lane on Crew's own sign-in (``managed_env.core_signin``).

The lane's bearer is the vault's, only for a sign-in on the lane's start URL, never kiro-cli's.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from managed_env_helpers import (  # noqa: F401
    FULL_SETTINGS,
    fresh_shared_client,
    lane_values,
    no_real_install_ssh,
)

from kiro_crew.auth import bridge
from kiro_crew.auth.login import endpoints
from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.auth.whoami import START_URL_EXTRA_KEY
from kiro_crew.managed_env import core_signin
from kiro_crew.managed_env.engine import SandboxEngineError, default_token_provider

OTHER = "https://other-org.example.test/start"


def _token(identity="identity_center", start_url=FULL_SETTINGS.start_url, access="at-test"):
    return KasToken(
        access_token=access,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        provider="Enterprise" if identity == "identity_center" else "BuilderId",
        identity=identity,
        profile_arn=FULL_SETTINGS.profile_arn if identity == "identity_center" else None,
        extra={START_URL_EXTRA_KEY: start_url} if start_url else {},
    )


def _boom(*_a, **_k):
    raise AssertionError("kiro-cli's sign-in must not be asked")


def _fence_on_the_lane_start_url(monkeypatch):
    """The core's registered-sign-in fence, as it reads with the lane's profile registered and
    the crew_client preview on: on while the stored sign-in is on the lane's start URL."""

    def registered():
        token = bridge.default_token_store().resolve()
        return token is not None and core_signin.token_is_lane_user(token)

    monkeypatch.setattr(endpoints, "stored_sign_in_registered", registered)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    # A new store per read, as a restarted dashboard would open it.
    monkeypatch.setattr(bridge, "default_token_store", lambda: TokenStore(tmp_path))
    monkeypatch.setattr("kiro_crew.lts_sign_in.resolve", _boom)
    return TokenStore(tmp_path)


def test_the_lane_token_is_the_vaults_test_sign_in(vault):
    vault.save(_token())
    assert default_token_provider() == "at-test"
    assert core_signin.vault_token() == "at-test"  # a second, fresh read: the restart case


@pytest.mark.parametrize(
    "stored",
    [
        [],
        [_token(start_url=OTHER, access="at-prod-user")],
        [_token(start_url="", access="at-no-url")],
        [_token(), _token("builder_id", start_url="", access="at-bid")],  # the store prefers it
    ],
    ids=["empty", "another-start-url", "no-start-url", "shadowed"],
)
def test_the_lane_refuses_every_other_sign_in(vault, stored):
    for token in stored:
        vault.save(token)
    with pytest.raises(SandboxEngineError) as ei:
        default_token_provider()
    assert ei.value.code == "not_signed_in"
    assert "Settings → Kiro account" in str(ei.value)


class _Provider:
    def __init__(self, token):
        self._token = token

    async def current(self):
        return self._token


class _NoKiroCli:
    async def credential(self):
        _boom()


@pytest.mark.parametrize(
    "stored, state, local_agent",
    [
        ([], "signin_required", "on"),
        ([_token(start_url=OTHER)], "signin_required", "on"),
        ([_token()], "signed_in", "off"),
        ([_token(), _token("builder_id", start_url="", access="at-bid")], "signin_required", "on"),
    ],
    ids=["empty", "another-start-url", "test-user", "shadowed"],
)
def test_the_lanes_token_free_status(vault, monkeypatch, stored, state, local_agent):
    _fence_on_the_lane_start_url(monkeypatch)
    for token in stored:
        vault.save(token)
    status = core_signin.sign_in_status()
    assert (status["state"], status["local_agent"]) == (state, local_agent)
    assert status["message"] == (
        core_signin.SIGNED_IN if state == "signed_in" else core_signin.NOT_SIGNED_IN
    )
    assert "at-" not in repr(status)  # no token value, ever


def _lapsing(refresh="rt-old"):
    """The test user's sign-in at renewal time: the access token inside the refresh margin and
    a refresh token present, so the core's ``is_usable`` alone reads it as live."""
    token = _token()
    token.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    token.refresh_token = refresh
    token.client_id, token.client_secret = "cid", "csecret"
    return token


def test_a_refused_renewal_reads_as_signed_out_until_a_fresh_sign_in(vault, monkeypatch):
    """The issuer refused the stored sign-in's renewal ("has expired and cannot be
    renewed"), and the status must not call it signed in. A fresh sign-in after the refusal
    reads as signed in again: saving it clears the store's marker."""
    _fence_on_the_lane_start_url(monkeypatch)
    vault.save(_lapsing())
    assert vault.resolve().is_usable()  # the core's predicate alone says live
    assert core_signin.sign_in_status()["state"] == "signed_in"  # no refusal yet
    vault.mark_refresh_rejected("identity_center")  # the refresher's record of the refusal
    status = core_signin.sign_in_status()
    # the session limit says so plainly
    assert (status["state"], status["message"]) == ("signin_required", core_signin.SIGN_IN_ENDED)
    assert status["local_agent"] == "off"  # the fence still holds on the stored test sign-in
    vault.save(_token(access="at-fresh"))  # a new sign-in, newer than the refusal
    assert vault.refresh_rejected("identity_center") is None
    assert core_signin.sign_in_status()["state"] == "signed_in"


class _RefusalStore:
    """A vault holding the test user's sign-in, with the refusal reads answering as given."""

    def __init__(self, in_process, strict):
        self.in_process, self.strict, self.asked = in_process, strict, []

    def resolve(self):
        return _lapsing()

    def refused_in_process(self, token):
        self.asked.append("in_process")
        return self.in_process

    def refresh_rejected_strict(self, identity):
        self.asked.append(("strict", identity))
        if isinstance(self.strict, Exception):
            raise self.strict
        return self.strict

    def refresh_rejected(self, identity):
        # The lenient read, as the core's: an unreadable marker is simply absent.
        self.asked.append(("lenient", identity))
        return None if isinstance(self.strict, Exception) else self.strict


@pytest.mark.parametrize(
    "in_process, strict, state",
    [
        (False, None, "signed_in"),
        (True, None, "signin_required"),
        (False, datetime(2026, 11, 18, 17, 6, 20, tzinfo=timezone.utc), "signin_required"),
        (False, PermissionError("marker unreadable"), "signin_required"),
    ],
    ids=["no-refusal", "refused-in-this-process", "marker", "marker-unreadable"],
)
def test_the_status_reads_the_refusal_as_the_core_gate_does(monkeypatch, in_process, strict, state):
    store = _RefusalStore(in_process, strict)
    monkeypatch.setattr(bridge, "default_token_store", lambda: store)
    _fence_on_the_lane_start_url(monkeypatch)
    assert core_signin.sign_in_status()["state"] == state
    # this process's memory first (a refusal whose marker write failed), then the strict marker
    strict_read = [] if in_process else [("strict", "identity_center")]
    assert store.asked == ["in_process", *strict_read]


def _no_spawn(*_a, **_k):
    raise AssertionError("a page load scheduled an eager spawn")


@pytest.mark.parametrize(
    "exc, line",
    [
        ("RefreshRejected", "SIGN_IN_ENDED"),
        ("RefreshBlocked", "SIGN_IN_NOT_RENEWED"),  # a missing local input, not the time limit
        ("RefreshError", "SIGN_IN_NOT_RENEWED"),
        ("OSError", "SIGN_IN_NOT_RENEWED"),
    ],
)
def test_a_renewal_failure_reads_as_one_plain_line(vault, monkeypatch, exc, line):
    from kiro_crew.auth import refresh
    from kiro_crew.auth.provider import KasAuthProvider
    from kiro_crew.managed_env.routes import _short

    cls = getattr(refresh, exc, None) or OSError
    raw = "identity_center refresh failed: HTTP 400 invalid_grant (renewed token inside margin)"

    async def _fails(self):
        raise cls(raw)

    monkeypatch.setattr(KasAuthProvider, "current", _fails)
    with pytest.raises(core_signin.NotLaneSignIn) as got:
        core_signin.vault_token()
    assert str(got.value) == getattr(core_signin, line)
    assert exc not in str(got.value) and "HTTP" not in str(got.value)
    with pytest.raises(SandboxEngineError) as wrapped:
        default_token_provider()
    assert wrapped.value.code == "not_signed_in"
    shown = _short(wrapped.value)  # what a row or the list answer shows
    assert shown == getattr(core_signin, line) and "not_signed_in" not in shown


def test_the_time_limit_line_is_plain():
    assert core_signin.SIGN_IN_ENDED == (
        "Your sign-in reached its time limit. Sign in again under Settings → Kiro account."
    )
