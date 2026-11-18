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


@pytest.fixture(autouse=True)
def _no_agent_identity(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the agent identity seam off: the host's edition must not decide these tests."""
    from kiro_crew.platform.context import current_context

    _floor_monkeypatch.setattr(current_context().agent_identity, "enabled", lambda: False)


def _pin_seam(monkeypatch: pytest.MonkeyPatch, current_context: Any) -> None:
    """Answer the seam from a ``current_context`` this test controls."""
    from kiro_crew.platform import context as context_module

    monkeypatch.setattr(context_module, "current_context", current_context)


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


@pytest.mark.parametrize("credential", ["kiro-sign-in", ""])
async def test_nothing_dials_unless_remote_control_is_on(
    home: Path, monkeypatch: pytest.MonkeyPatch, credential: str
) -> None:
    sign_in = _SignIn()
    monkeypatch.setattr(lease_signin, "read_lease_sign_in", sign_in)
    async with FakeLeaseService() as lease:
        provider = public_tunnel_provider(_cfg(lease.url, credential, enabled=False))
        await provider.start()
        await provider.stop()
        # Building the lease client with Remote Control on reads nothing either.
        assert isinstance(
            lease_token_source(RelaySettings.from_config(_cfg(lease.url, credential))),
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


@pytest.mark.parametrize("url", ["https://lease.example.com", ""])
async def test_an_empty_credential_with_no_token_type_is_the_sign_in(url: str) -> None:
    settings = RelaySettings.from_config(_cfg(url, ""))
    assert settings.uses_sign_in
    settings.validate()


@pytest.mark.parametrize("token_type", ["API_KEY", "EXTERNAL_IDP"])
async def test_an_empty_credential_with_a_token_type_is_still_refused_by_name(
    home: Path, monkeypatch: pytest.MonkeyPatch, token_type: str
) -> None:
    # A team Crew (API_KEY) must name its crew API key: it never falls to the sign-in.
    sign_in = _SignIn()
    monkeypatch.setattr(lease_signin, "read_lease_sign_in", sign_in)
    settings = RelaySettings.from_config(_cfg("", "", token_type))
    assert not settings.uses_sign_in
    with pytest.raises(RelaySettingsError, match="tunnel.relay_lease_credential is not set"):
        settings.validate()
    source = lease_token_source(settings)
    try:
        with pytest.raises(HostTokenUnavailable, match="tunnel.relay_lease_credential"):
            await source.current()
    finally:
        await source.aclose()
    assert sign_in.asked == []


async def test_an_empty_credential_on_a_workload_identity_host_is_still_refused_by_name(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A host whose agent identity seam reports a workload identity keeps its own
    # rule: an empty credential never falls to the owner's sign-in.
    seam = SimpleNamespace(agent_identity=SimpleNamespace(enabled=lambda: True))
    _pin_seam(monkeypatch, lambda: seam)
    sign_in = _SignIn()
    monkeypatch.setattr(lease_signin, "read_lease_sign_in", sign_in)
    settings = RelaySettings.from_config(_cfg("", ""))
    with pytest.raises(RelaySettingsError, match="tunnel.relay_lease_credential is not set"):
        settings.validate()
    provider = public_tunnel_provider(_cfg("", ""))
    await provider.start()
    try:
        snapshot = provider.status_snapshot()
        assert snapshot is not None and snapshot["state"] == "error"
        assert "tunnel.relay_lease_credential is not set" in snapshot["error"]
    finally:
        await provider.stop()
    assert sign_in.asked == []


async def test_a_failed_agent_identity_lookup_refuses_the_sign_in_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fail closed: a seam that cannot be read counts as a workload identity.
    def broken() -> Any:
        raise RuntimeError("adapter down")

    _pin_seam(monkeypatch, broken)
    with pytest.raises(RelaySettingsError, match="tunnel.relay_lease_credential is not set"):
        RelaySettings.from_config(_cfg("", "")).validate()
    # A named sign-in never asks the seam.
    RelaySettings.from_config(_cfg("", "kiro-sign-in")).validate()


async def test_a_seam_reporting_no_workload_identity_keeps_the_sign_in_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seam = SimpleNamespace(agent_identity=SimpleNamespace(enabled=lambda: False))
    _pin_seam(monkeypatch, lambda: seam)
    RelaySettings.from_config(_cfg("", "")).validate()


async def test_the_workload_switch_refuses_an_empty_credential_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The switch composes the SPIFFE identity into the seam, so an empty credential is
    # not taken as the owner's sign-in: the Crew must name workload-identity:jwt-svid.
    from kiro_crew.platform.context import reset_context

    monkeypatch.setenv("KIROCREW_WORKLOAD_IDENTITY", "spiffe")
    reset_context()
    with pytest.raises(RelaySettingsError, match="tunnel.relay_lease_credential is not set"):
        RelaySettings.from_config(_cfg("", "")).validate()


@pytest.mark.parametrize("url", ["https://lease.example.com", ""])
async def test_the_workload_reference_passes_on_a_workload_identity_host(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    # The seam refuses only the empty default; a named workload reference still passes.
    seam = SimpleNamespace(agent_identity=SimpleNamespace(enabled=lambda: True))
    _pin_seam(monkeypatch, lambda: seam)
    settings = RelaySettings.from_config(_cfg(url, "workload-identity:jwt-svid"))
    assert not settings.uses_sign_in
    settings.validate()


async def test_an_empty_credential_leases_from_the_built_in_url_on_the_sign_in(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lease_signin, "read_lease_sign_in", _SignIn())
    async with FakeLeaseService({STALE}) as lease:
        asked: List[Optional[str]] = []

        def built_in(profile_arn: Optional[str]) -> str:
            asked.append(profile_arn)
            return lease.url

        monkeypatch.setattr(lease_module, "built_in_lease_url", built_in)
        lease.queue(200, answer(0))
        source = lease_token_source(RelaySettings.from_config(_cfg("", "")))
        try:
            assert (await source.current()).token == answer(0)["hostToken"]
        finally:
            await source.aclose()
    assert set(asked) == {ARN}
    (renew,) = lease.requests
    assert renew["auth"] == f"Bearer {STALE}" and renew["body"]["profileArn"] == ARN


@pytest.mark.parametrize("credential", ["kiro-sign-in", ""])
@pytest.mark.parametrize("token_type", [None, "EXTERNAL_IDP"])
async def test_kiro_sign_in_renews_and_releases_with_the_sign_in_and_its_profile(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    token_type: Optional[str],
    credential: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(lease_signin, "read_lease_sign_in", _SignIn(token_type))
    async with RawLeaseService({STALE}) as lease:
        source = lease_token_source(RelaySettings.from_config(_cfg(lease.url, credential)))
        renew, release = await _renew_and_release(lease, source)
    instance_id = renew["body"]["crewInstanceId"]
    name = lease_module.default_display_name()
    assert renew["raw"] == raw({**todays_renew(instance_id, name, __version__), "profileArn": ARN})
    assert release["raw"] == raw({"crewInstanceId": instance_id, "profileArn": ARN})
    assert renew["auth"] == release["auth"] == f"Bearer {STALE}"
    assert renew["token_type"] == release["token_type"] == token_type
    for secret in (STALE, ARN):
        assert secret not in caplog.text


@pytest.mark.parametrize("credential", ["kiro-sign-in", ""])
async def test_a_401_refreshes_the_sign_in_once_then_stops(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    credential: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    sign_in = _SignIn()
    monkeypatch.setattr(lease_signin, "read_lease_sign_in", sign_in)
    async with FakeLeaseService({FRESH}) as lease:
        lease.queue(200, answer(0))
        source = lease_token_source(RelaySettings.from_config(_cfg(lease.url, credential)))
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
        source = lease_token_source(RelaySettings.from_config(_cfg(lease.url, credential)))
        try:
            with pytest.raises(HostTokenUnavailable, match="HTTP 401") as caught:
                await source.current()
        finally:
            await source.aclose()
        assert len(lease.requests) == 2 and stuck.asked[:2] == [None, STALE]
    for secret in (STALE, FRESH, ARN):
        assert secret not in caplog.text and secret not in str(caught.value)


@pytest.mark.parametrize("credential", ["kiro-sign-in", ""])
async def test_a_sign_in_failure_backs_off_and_a_later_sign_in_resumes(
    home: Path, monkeypatch: pytest.MonkeyPatch, credential: str
) -> None:
    sign_in = _SignIn()
    sign_in.fail = LeaseCredentialError("the Crew is not signed in to Kiro")
    monkeypatch.setattr(lease_signin, "read_lease_sign_in", sign_in)
    now = [1000.0]
    async with FakeLeaseService({STALE}) as lease:
        settings = RelaySettings.from_config(_cfg(lease.url, credential))
        source = lease_token_source(settings)
        assert isinstance(source, LeaseHostTokenSource)
        source._monotonic = lambda: now[0]
        try:
            with pytest.raises(
                HostTokenRetryLater,
                match="the Crew's Kiro sign-in could not be used: the Crew is not signed in",
            ):
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
