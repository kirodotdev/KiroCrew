"""The relay lease of a Crew on its own workload identity (JWT-SVID).

The JWT-SVID file is checked like the engine's sign-in before it is sent, no
``TokenType`` ever goes with it, and a refused sign-in or an invalid request waits 15
minutes instead of stopping.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_relay_client_lease import (
    CREDENTIAL,
    FakeLeaseService,
    StepClock,
    answer,
    calls,
    credential_file,
    make_source,
    refusal,
)
from test_workload_identity import AUDIENCE, LONG_AGO, PROFILE_ARN, make_svid

from kiro_crew import workload_identity as wi
from kiro_crew.relay_client import lease as lease_module
from kiro_crew.relay_client.lease import RETRY_AFTER_MAX_SECONDS, read_lease_credential
from kiro_crew.relay_client.provider import RelaySettings, lease_token_source
from kiro_crew.relay_client.token_source import HostTokenRetryLater

pytestmark = pytest.mark.asyncio


@pytest.fixture
def clock() -> StepClock:
    return StepClock()


@pytest.fixture
def svid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "jwt_svid.token"
    path.write_text(make_svid() + "\n", encoding="ascii")
    monkeypatch.setenv(wi.ENV_MODE, "spiffe")
    monkeypatch.setenv(wi.ENV_SVID_FILE, str(path))
    monkeypatch.setenv(wi.ENV_AUDIENCE, AUDIENCE)
    monkeypatch.setenv(wi.ENV_PROFILE_ARN, PROFILE_ARN)
    return path


def _cfg(url: str, credential: str, token_type: str = "") -> Any:
    tunnel = SimpleNamespace(
        relay_enabled=True,
        relay_lease_url=url,
        relay_lease_credential=credential,
        relay_lease_token_type=token_type,
    )
    return SimpleNamespace(tunnel=tunnel)


def _source(lease: FakeLeaseService, svid: Path, tmp_path: Path, clock: StepClock) -> Any:
    return make_source(lease, tmp_path, clock, credential=svid, workload=True)


async def test_an_expired_svid_is_never_sent(svid: Path, tmp_path: Path, clock: StepClock) -> None:
    svid.write_text(make_svid(exp=LONG_AGO), encoding="ascii")
    async with FakeLeaseService({svid.read_text()}) as lease:
        source = _source(lease, svid, tmp_path, clock)
        try:
            with pytest.raises(HostTokenRetryLater, match="expire"):
                await source.current()
        finally:
            await source.aclose()
    assert lease.requests == [] and lease.releases == []


@pytest.mark.parametrize("aud", ["https://someone-else.example.test", None])
async def test_an_svid_for_another_audience_or_no_subject_is_never_sent(
    svid: Path, tmp_path: Path, clock: StepClock, aud: Any
) -> None:
    content = make_svid(aud=aud) if aud else make_svid(sub="https://not-spiffe.example.test")
    svid.write_text(content, encoding="ascii")
    async with FakeLeaseService({content}) as lease:
        source = _source(lease, svid, tmp_path, clock)
        try:
            with pytest.raises(HostTokenRetryLater, match="audience|subject"):
                await source.current()
        finally:
            await source.aclose()
    assert lease.requests == []


@pytest.mark.parametrize(
    "first",
    [
        (401, {"message": "Unauthorized"}, {}),
        refusal("ValidationException"),
    ],
    ids=["http-401", "validation"],
)
async def test_a_refused_workload_identity_tries_again_after_15_minutes(
    svid: Path, tmp_path: Path, clock: StepClock, first: Any
) -> None:
    token = svid.read_text().strip()
    async with FakeLeaseService({token}) as lease:
        status, body, headers = first
        if status == 401:
            # Refused twice: the first 401 is read again and retried at once.
            lease.credentials = set()
        else:
            lease.queue(status, body, headers)
        lease.queue(200, answer(0))
        source = _source(lease, svid, tmp_path, clock)
        try:
            with pytest.raises(HostTokenRetryLater, match="workload identity"):
                await source.current()
            assert not source.stopped_reason
            assert await clock.next_sleep() == RETRY_AFTER_MAX_SECONDS
            lease.credentials = {token}
            clock.elapse()
            await calls(lease, 3 if status == 401 else 2)
            assert (await source.current()).token == answer(0)["hostToken"]
        finally:
            await source.aclose()
    assert all(r["token_type"] is None for r in lease.requests)


async def test_a_copied_team_token_type_is_never_sent_with_the_svid(
    svid: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lease_module, "_data_home", lambda: tmp_path)
    token = svid.read_text().strip()
    async with FakeLeaseService({token}) as lease:
        settings = RelaySettings.from_config(_cfg(lease.url, str(svid), "API_KEY"))
        settings.validate()
        assert settings.uses_workload_identity
        source = lease_token_source(settings)
        assert source is not None
        lease.queue(200, answer(0))
        try:
            await source.current()
        finally:
            await source.aclose()
    assert [r["token_type"] for r in lease.requests + lease.releases] == [None, None]
    assert lease.requests[0]["auth"] == f"Bearer {token}"


async def test_a_workload_lease_takes_no_sign_in_or_profile(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="workload lease"):
        lease_module.LeaseHostTokenSource(
            lease_url="",
            credential=str(tmp_path / "jwt"),
            crew_version="0",
            workload=True,
            profile_arn=lambda: PROFILE_ARN,
        )


async def test_a_secret_reference_on_a_workload_host_stays_a_crew_key(svid: Path) -> None:
    settings = RelaySettings.from_config(_cfg("", "secret://crew-key", "API_KEY"))
    assert not settings.uses_workload_identity


@pytest.mark.asyncio
async def test_a_key_file_other_than_the_svid_file_stays_a_crew_key(
    svid: Path, tmp_path: Path
) -> None:
    # A team Crew's API key kept in a file on a workload host still leases as the key.
    key_file = tmp_path / "crew-key"
    key_file.write_text("not-an-svid", encoding="ascii")
    settings = RelaySettings.from_config(_cfg("", str(key_file), "API_KEY"))
    assert settings.uses_workload_identity is False
    assert RelaySettings.from_config(_cfg("", str(svid))).uses_workload_identity is True


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are a POSIX file kind")
async def test_a_fifo_named_as_the_lease_credential_is_refused_at_once(tmp_path: Path) -> None:
    fifo = tmp_path / "credential"
    os.mkfifo(fifo)
    outcome: list = []

    def read() -> None:
        try:
            read_lease_credential(str(fifo), home=tmp_path)
        except lease_module.LeaseCredentialError as exc:
            outcome.append(str(exc))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(10)
    if reader.is_alive():
        # Release the blocked open so the thread ends, then fail.
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        reader.join(10)
        pytest.fail("reading the lease credential blocked on a FIFO")
    assert outcome and "NotRegularFileError" in outcome[0]
    # A regular file still reads.
    assert read_lease_credential(str(credential_file(tmp_path)), home=tmp_path) == CREDENTIAL
