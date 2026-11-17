"""tunnel.relay_lease_credential = kiro-sign-in: a personal Crew leases on its Kiro sign-in.

Every other value sends today's requests and never reads the sign-in.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest
from test_relay_client_lease import CREDENTIAL, FakeLeaseService, answer
from test_relay_client_lease_profile import RawLeaseService, raw, todays_renew

from kiro_crew import __version__
from kiro_crew.auth.provider import KasAuthProvider
from kiro_crew.relay_client import lease as lease_module
from kiro_crew.relay_client import lease_signin
from kiro_crew.relay_client.lease import LeaseCredentialError, LeaseHostTokenSource
from kiro_crew.relay_client.lease_signin import LeaseSignIn
from kiro_crew.relay_client.provider import (
    RelaySettings,
    RelaySettingsError,
    lease_token_source,
    public_tunnel_provider,
)
from kiro_crew.relay_client.token_source import HostTokenRetryLater, HostTokenUnavailable

pytestmark = pytest.mark.asyncio

ARN = "arn:aws:codewhisperer:us-east-1:000000000000:profile/WIRINGPROFILE"
STALE = "signin-stale-bearer-4d1e"
FRESH = "signin-fresh-bearer-8b2c"


def _cfg(url: str, credential: str, token_type: str = "", enabled: bool = True) -> Any:
    tunnel = SimpleNamespace(
        relay_enabled=enabled,
        relay_lease_url=url,
        relay_lease_credential=credential,
        relay_lease_token_type=token_type,
    )
    return SimpleNamespace(tunnel=tunnel)


class _SignIn:
    """Stands in for read_lease_sign_in: STALE until a refresh of STALE is asked for."""

    def __init__(self, token_type: Optional[str] = None, refreshes: bool = True) -> None:
        self.token_type, self.refreshes = token_type, refreshes
        self.asked: List[Optional[str]] = []
        self.fail: Optional[Exception] = None

    async def __call__(self, rejected: Optional[str] = None) -> LeaseSignIn:
        self.asked.append(rejected)
        if self.fail is not None:
            raise self.fail
        bearer = FRESH if self.refreshes and rejected == STALE else STALE
        return LeaseSignIn(bearer=bearer, token_type=self.token_type, profile_arn=ARN)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(lease_module, "_data_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any read of the Crew's sign-in fails the test."""

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the sign-in was read")

    monkeypatch.setattr(lease_signin, "read_lease_sign_in", boom)
    monkeypatch.setattr(KasAuthProvider, "current", boom)


async def _renew_and_release(lease: FakeLeaseService, source: Any) -> tuple:
    lease.queue(200, answer(0))
    try:
        await source.current()
    finally:
        await source.aclose()
    (renew,), (release,) = lease.requests, lease.releases
    return renew, release


@pytest.mark.parametrize("kind", ["secret", "file", "api_key"])
async def test_existing_configs_send_todays_bytes_and_never_read_the_sign_in(
    home: Path, untouched: None, kind: str
) -> None:
    async with RawLeaseService() as lease:
        if kind == "secret":
            from kiro_crew.secrets import SecretVault

            SecretVault(home).set_sync("relay-lease", CREDENTIAL)
            cfg = _cfg(lease.url, "secret://relay-lease")
        else:
            (home / "credential").write_text(CREDENTIAL, encoding="ascii")
            cfg = _cfg(lease.url, str(home / "credential"), "API_KEY" if kind == "api_key" else "")
        source = lease_token_source(RelaySettings.from_config(cfg))
        renew, release = await _renew_and_release(lease, source)
    instance_id = renew["body"]["crewInstanceId"]
    name = lease_module.default_display_name()
    assert renew["raw"] == raw(todays_renew(instance_id, name, __version__))
    assert release["raw"] == raw({"crewInstanceId": instance_id})
    assert renew["auth"] == release["auth"] == f"Bearer {CREDENTIAL}"
    expected_type = "API_KEY" if kind == "api_key" else None
    assert renew["token_type"] == release["token_type"] == expected_type


async def test_nothing_dials_unless_remote_control_is_on(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in = _SignIn()
    monkeypatch.setattr(lease_signin, "read_lease_sign_in", sign_in)
    async with FakeLeaseService() as lease:
        provider = public_tunnel_provider(_cfg(lease.url, "kiro-sign-in", enabled=False))
        await provider.start()
        await provider.stop()
        # Building the lease client with Remote Control on reads nothing either.
        assert isinstance(
            lease_token_source(RelaySettings.from_config(_cfg(lease.url, "kiro-sign-in"))),
            LeaseHostTokenSource,
        )
    assert lease.requests == [] and lease.releases == [] and sign_in.asked == []


@pytest.mark.parametrize("token_type", ["API_KEY", "EXTERNAL_IDP"])
async def test_kiro_sign_in_with_a_token_type_is_refused(token_type: str) -> None:
    settings = RelaySettings.from_config(
        _cfg("https://lease.example.com", "kiro-sign-in", token_type)
    )
    with pytest.raises(RelaySettingsError, match="must be empty"):
        settings.validate()
    # The composition root builds the client before any start, and never raises.
    assert isinstance(lease_token_source(settings), LeaseHostTokenSource)


async def test_kiro_sign_in_alone_passes_validation() -> None:
    RelaySettings.from_config(_cfg("https://lease.example.com", "kiro-sign-in")).validate()


@pytest.mark.parametrize("token_type", [None, "EXTERNAL_IDP"])
async def test_kiro_sign_in_renews_and_releases_with_the_sign_in_and_its_profile(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    token_type: Optional[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(lease_signin, "read_lease_sign_in", _SignIn(token_type))
    async with RawLeaseService({STALE}) as lease:
        source = lease_token_source(RelaySettings.from_config(_cfg(lease.url, "kiro-sign-in")))
        renew, release = await _renew_and_release(lease, source)
    instance_id = renew["body"]["crewInstanceId"]
    name = lease_module.default_display_name()
    assert renew["raw"] == raw({**todays_renew(instance_id, name, __version__), "profileArn": ARN})
    assert release["raw"] == raw({"crewInstanceId": instance_id, "profileArn": ARN})
    assert renew["auth"] == release["auth"] == f"Bearer {STALE}"
    assert renew["token_type"] == release["token_type"] == token_type
    for secret in (STALE, ARN):
        assert secret not in caplog.text


async def test_a_401_refreshes_the_sign_in_once_then_stops(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    sign_in = _SignIn()
    monkeypatch.setattr(lease_signin, "read_lease_sign_in", sign_in)
    async with FakeLeaseService({FRESH}) as lease:
        lease.queue(200, answer(0))
        source = lease_token_source(RelaySettings.from_config(_cfg(lease.url, "kiro-sign-in")))
        try:
            assert (await source.current()).token == answer(0)["hostToken"]
        finally:
            await source.aclose()
        assert [r["auth"] for r in lease.requests] == [f"Bearer {STALE}", f"Bearer {FRESH}"]
        assert sign_in.asked[:2] == [None, STALE]

        # A refreshed sign-in the service still refuses is not retried again.
        stuck = _SignIn(refreshes=False)
        monkeypatch.setattr(lease_signin, "read_lease_sign_in", stuck)
        lease.requests.clear()
        source = lease_token_source(RelaySettings.from_config(_cfg(lease.url, "kiro-sign-in")))
        try:
            with pytest.raises(HostTokenUnavailable, match="HTTP 401") as caught:
                await source.current()
        finally:
            await source.aclose()
        assert len(lease.requests) == 2 and stuck.asked[:2] == [None, STALE]
    for secret in (STALE, FRESH, ARN):
        assert secret not in caplog.text and secret not in str(caught.value)


async def test_a_sign_in_failure_backs_off_and_a_later_sign_in_resumes(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in = _SignIn()
    sign_in.fail = LeaseCredentialError("the Crew is not signed in to Kiro")
    monkeypatch.setattr(lease_signin, "read_lease_sign_in", sign_in)
    now = [1000.0]
    async with FakeLeaseService({STALE}) as lease:
        settings = RelaySettings.from_config(_cfg(lease.url, "kiro-sign-in"))
        source = lease_token_source(settings)
        assert isinstance(source, LeaseHostTokenSource)
        source._monotonic = lambda: now[0]
        try:
            with pytest.raises(HostTokenRetryLater, match="not signed in"):
                await source.current()
            assert lease.requests == [] and not source.stopped_reason
            # The person signs in again; once the backoff runs out, hosting resumes.
            sign_in.fail = None
            now[0] += 10_000.0
            lease.queue(200, answer(0))
            assert (await source.current()).token == answer(0)["hostToken"]
        finally:
            await source.aclose()
    assert [r["auth"] for r in lease.requests] == [f"Bearer {STALE}"]
