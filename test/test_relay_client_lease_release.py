"""The lease client ends its lease when Remote Control stops.

``ReleaseCrewInstanceLease`` requests are checked against
``backend/release-crew-instance-lease.request`` (relay contract section 10.2).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import web
from relay_client_helpers import fixture
from test_relay_client_lease import CREDENTIAL, FakeLeaseService, StepClock, answer, make_source

from kiro_crew.dashboard.server import _TUNNEL_STOP_TIMEOUT_SECS
from kiro_crew.relay_client import lease as lease_module
from kiro_crew.relay_client.client import STOP_BUDGET_SECONDS
from kiro_crew.testing.wait import await_until


@pytest.mark.asyncio
async def test_a_stop_ends_the_lease_with_one_release_call(tmp_path: Path) -> None:
    async with FakeLeaseService() as lease:
        source = make_source(lease, tmp_path, StepClock(), token_type="API_KEY")
        # Never renewed: there is no lease to end.
        await source.aclose()
        assert lease.releases == []
        lease.queue(200, answer(0))
        try:
            await source.current()
        finally:
            await source.aclose()
        # Stopped twice: the second stop has no lease left to end.
        await source.aclose()
    (release,) = lease.releases
    assert release["target"] == "KiroControlPlaneBearerService.ReleaseCrewInstanceLease"
    assert release["content_type"] == "application/x-amz-json-1.0"
    assert release["auth"] == f"Bearer {CREDENTIAL}"
    assert release["token_type"] == "API_KEY"
    expected = fixture("backend/release-crew-instance-lease.request")["examples"][0]["body"]
    assert set(release["body"]) == set(expected)
    assert release["body"]["crewInstanceId"] == lease.requests[0]["body"]["crewInstanceId"]


class UnansweredRelease(FakeLeaseService):
    """A lease service that takes the release call and answers it only when let go."""

    def __init__(self) -> None:
        super().__init__()
        self.arrived = asyncio.Event()
        self.let_go = asyncio.Event()

    async def _release(self, request: web.Request) -> web.Response:
        self.arrived.set()
        await self.let_go.wait()
        return web.json_response({})


def test_the_release_fits_the_gateways_stop_budget() -> None:
    assert STOP_BUDGET_SECONDS + lease_module.RELEASE_TIMEOUT_SECONDS < _TUNNEL_STOP_TIMEOUT_SECS


@pytest.mark.asyncio
async def test_a_release_that_never_answers_does_not_hold_up_the_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the release's own bound can end the call: the per-call timeout is off.
    monkeypatch.setattr(lease_module, "CALL_TIMEOUT_SECONDS", None)
    monkeypatch.setattr(
        lease_module, "RELEASE_TIMEOUT_SECONDS", 0.1  # flake-ok: the bound under test, not a wait
    )
    async with UnansweredRelease() as lease:
        lease.queue(200, answer(0))
        source = make_source(lease, tmp_path, StepClock())
        try:
            await source.current()
            await await_until(asyncio.ensure_future(source.aclose()), what="the stop")
            # The stop returned while the release was still unanswered, and it
            # forgot the lease all the same.
            assert not lease.let_go.is_set()
            assert source._instance_id is None
        finally:
            lease.let_go.set()


@pytest.mark.asyncio
async def test_a_stop_cancelled_during_the_release_still_forgets_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the cancel can end the release, as when the gateway's stop budget runs out.
    monkeypatch.setattr(lease_module, "CALL_TIMEOUT_SECONDS", None)
    monkeypatch.setattr(lease_module, "RELEASE_TIMEOUT_SECONDS", None)
    async with UnansweredRelease() as lease:
        lease.queue(200, answer(0))
        source = make_source(lease, tmp_path, StepClock())
        try:
            await source.current()
            closing = asyncio.ensure_future(source.aclose())
            await await_until(lease.arrived, what="the release")
            closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)
        finally:
            lease.let_go.set()
    assert source._session is None
    assert source._instance_id is None
