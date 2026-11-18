"""The lease client on the Crew's workload identity (``workload-identity:jwt-svid``).

The SVID comes from :mod:`kiro_crew.workload_identity`; here it is read through a
stand-in reader, or through a faked ``fetch_jwt_svid``, so no socket is opened.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable

import pytest
import pytest_asyncio
from relay_client_helpers import TopRng
from test_relay_client_lease import FakeLeaseService, StepClock, answer, calls, make_source, refusal
from test_workload_identity_kas import TOKEN, _env, _set_env, _svid, _use

from kiro_crew import workload_identity as wi
from kiro_crew.relay_client.lease import (
    RENEW_MIN_SECONDS,
    WORKLOAD_IDENTITY_REF,
    LeaseCredentialError,
    LeaseCredentialReferenceError,
    LeaseHostTokenSource,
    credential_reference_problem,
    read_lease_credential,
)
from kiro_crew.relay_client.token_source import HostTokenRetryLater
from kiro_crew.spiffe_workload_api import WorkloadApiError

pytestmark = pytest.mark.asyncio

SVID = "stand-in-svid-9c1e"
STEPS = (60.0, 120.0, 240.0, 480.0, 900.0, 900.0)


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    wi._reset_cache_for_tests()
    yield
    wi._reset_cache_for_tests()


@pytest_asyncio.fixture
async def lease() -> Any:
    async with FakeLeaseService() as service:
        yield service


@pytest.fixture
def clock() -> StepClock:
    return StepClock()


def workload_source(lease: FakeLeaseService, tmp_path: Path, clock: StepClock) -> Any:
    lease.credentials = {SVID}
    return make_source(
        lease, tmp_path, clock, credential=WORKLOAD_IDENTITY_REF, read_credential=lambda ref: SVID
    )


class BottomRng(TopRng):
    """An RNG whose ``uniform`` always answers the bottom of the range."""

    def uniform(self, a: float, b: float) -> float:
        return a


# ── the reference and its reader ──


async def test_the_workload_reference_names_a_credential() -> None:
    assert credential_reference_problem(WORKLOAD_IDENTITY_REF) is None
    assert WORKLOAD_IDENTITY_REF == wi.LEASE_CREDENTIAL_REF
    assert WORKLOAD_IDENTITY_REF in str(credential_reference_problem("relative/path"))


async def test_the_reader_reads_the_svid_and_sorts_its_faults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(wi.ENV_MODE, raising=False)

    # The lease client reads in a thread, which has no event loop of its own.
    def read() -> Awaitable[str]:
        return asyncio.to_thread(read_lease_credential, WORKLOAD_IDENTITY_REF)

    # With the switch off, reading again cannot help, so the source stops.
    with pytest.raises(LeaseCredentialReferenceError, match=wi.ENV_MODE):
        await read()
    _set_env(monkeypatch, _env())
    _use(monkeypatch, _svid())
    assert await read() == TOKEN
    wi._reset_cache_for_tests()
    # An agent with no SVID right now is read again on the reconnect steps.
    _use(monkeypatch, WorkloadApiError("stand-in: no workload identity"))
    with pytest.raises(LeaseCredentialError) as raised:
        await read()
    assert not isinstance(raised.value, LeaseCredentialReferenceError)


# ── refusals back off on capped steps ──


async def test_access_refusals_wait_on_doubling_steps_up_to_15_minutes(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    for _ in STEPS:
        lease.queue(*refusal("AccessDeniedException"))
    lease.queue(200, answer(0))
    source = workload_source(lease, tmp_path, clock)
    try:
        with pytest.raises(HostTokenRetryLater, match="AccessDeniedException"):
            await source.current()
        for count, step in enumerate(STEPS, start=2):
            assert await clock.next_sleep() == step
            clock.elapse()
            await calls(lease, count)
        assert not source.stopped_reason
        assert (await source.current()).token == answer(0)["hostToken"]
    finally:
        await source.aclose()


async def test_an_invalid_refusal_waits_on_the_same_steps_instead_of_stopping(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.queue(*refusal("ValidationException"))
    lease.queue(*refusal("AccessDeniedException"))
    lease.queue(200, answer(0))
    source = workload_source(lease, tmp_path, clock)
    try:
        with pytest.raises(HostTokenRetryLater, match="ValidationException"):
            await source.current()
        assert not source.stopped_reason
        assert await clock.next_sleep() == 60.0
        clock.elapse()
        await calls(lease, 2)
        # One count for both kinds of refusal.
        assert await clock.next_sleep() == 120.0
        clock.elapse()
        await calls(lease, 3)
        assert (await source.current()).token == answer(0)["hostToken"]
    finally:
        await source.aclose()


async def test_a_good_answer_and_a_stop_each_start_the_steps_again(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.queue(*refusal("AccessDeniedException"))
    lease.queue(*refusal("AccessDeniedException"))
    lease.queue(200, answer(0))
    lease.queue(*refusal("AccessDeniedException"))
    source = workload_source(lease, tmp_path, clock)
    try:
        with pytest.raises(HostTokenRetryLater):
            await source.current()
        assert await clock.next_sleep() == 60.0
        clock.elapse()
        await calls(lease, 2)
        assert await clock.next_sleep() == 120.0
        clock.elapse()
        await calls(lease, 3)
        await clock.next_sleep()  # the renewal the good answer scheduled
        clock.elapse()
        await calls(lease, 4)
        assert await clock.next_sleep() == 60.0
        await source.aclose()
        lease.queue(*refusal("AccessDeniedException"))
        with pytest.raises(HostTokenRetryLater):
            await source.current()
        assert await clock.next_sleep() == 60.0
    finally:
        await source.aclose()


async def test_a_wait_drawn_near_zero_is_never_under_the_floor(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.credentials = {SVID}
    lease.queue(*refusal("AccessDeniedException"))
    source = LeaseHostTokenSource(
        lease_url=lease.url,
        credential=WORKLOAD_IDENTITY_REF,
        crew_version="0.9.1.0",
        home=tmp_path,
        read_credential=lambda ref: SVID,
        rng=BottomRng(),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    try:
        with pytest.raises(HostTokenRetryLater):
            await source.current()
        assert await clock.next_sleep() == RENEW_MIN_SECONDS == 5.0
    finally:
        await source.aclose()


# ── the calls ──


async def test_renew_and_release_carry_the_svid_with_no_token_type_or_profile(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.queue(200, answer(0))
    source = workload_source(lease, tmp_path, clock)
    try:
        await source.current()
    finally:
        await source.aclose()
    (renewal,) = lease.requests
    (release,) = lease.releases
    for call in (renewal, release):
        assert call["auth"] == f"Bearer {SVID}"
        assert call["token_type"] is None
        assert "profileArn" not in call["body"]
    assert set(release["body"]) == {"crewInstanceId"}
